"""The thinking part: Gemini reads videos/images, writes notes, makes embeddings, answers questions."""
import json
import os
import re
import time
import urllib.parse
import urllib.request

from google import genai
from google.genai import types
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi

MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768

# The free plan allows ~20 requests a day PER MODEL, so we walk down this list when one is used up or busy.
# Best models first, lighter ones last. Together: roughly 140 requests a day.
FALLBACK_MODELS = [MODEL, "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                   "gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

_client = None
_used_up = {}  # model -> time until which we skip it (its daily free quota ran out)


def client():
    global _client
    if _client is None:
        # Without a timeout a stuck request can hang forever; 4 min is enough for a 1-hour video.
        # Only retry "busy" errors here; a used-up quota (429) should move to the next model at once.
        _client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"].strip(),
            http_options=types.HttpOptions(timeout=240_000, retry_options=types.HttpRetryOptions(
                attempts=2, http_status_codes=[500, 502, 503, 504])),
        )
    return _client


def _generate(contents, config):
    last = None
    for model in dict.fromkeys(FALLBACK_MODELS):
        if _used_up.get(model, 0) > time.time():
            continue
        try:
            return client().models.generate_content(model=model, contents=contents, config=config)
        except Exception as e:  # busy (503), quota (429) or model gone (404): move on
            s = str(e)
            if not any(code in s for code in ("503", "504", "429", "404", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
                                              "DEADLINE", "imeout")):
                raise
            last = e
            if "429" in s or "404" in s:
                _used_up[model] = time.time() + 60 * 60  # check it again in an hour
            else:
                time.sleep(2)
    raise last or RuntimeError("RESOURCE_EXHAUSTED: all free Gemini models are used up for today")


class Note(BaseModel):
    title: str
    topic: str
    summary: str
    key_points: list[str]
    content: str


NOTE_RULES = """You are filling Arun's personal knowledge base. He saves things to learn from them later.
Write everything in clear, simple English (translate any Tamil/Hindi; keep a Tamil phrase only if it matters).
- title: short and specific (max 10 words).
- topic: 1-3 lowercase words, e.g. "sales", "coding", "fitness", "ai tools". Reuse one of these existing topics if it fits: {topics}
- summary: 2-3 sentences: what this is and the main lesson.
- key_points: the actual useful advice, 3-10 points. Keep the specifics (numbers, names, exact steps, example lines to say). No vague filler.
- content: {content_rule}
Accuracy rules (very important):
- Only use what is actually said, written or shown in this item. Add nothing from your own knowledge.
- Keep the creator's framing. When something is their opinion or claim, say so (e.g. "The creator claims…").
- If a word, name or number is unclear, write [unclear] instead of guessing.
- Never present a guess as a fact."""


def _note_config(topics, content_rule):
    return types.GenerateContentConfig(
        system_instruction=NOTE_RULES.format(topics=", ".join(topics) or "none yet", content_rule=content_rule),
        response_mime_type="application/json",
        response_schema=Note,
    )


def _parse(resp):
    return resp.parsed if resp.parsed else Note(**json.loads(resp.text))


# ---------- YouTube ----------

def youtube_id(url):
    m = re.search(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/))([\w-]{11})", url)
    return m.group(1) if m else None


def youtube_title(url):
    try:
        q = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(url, safe="")
        return json.load(urllib.request.urlopen(q, timeout=10)).get("title")
    except Exception:
        return None


def youtube_transcript(video_id):
    """Spoken words of the video. Free and cheap; None if the video has no captions."""
    try:
        api = YouTubeTranscriptApi()
        tlist = api.list(video_id)
        try:
            t = tlist.find_transcript(["en", "en-IN", "en-US", "en-GB", "ta", "hi"])
        except Exception:
            t = next(iter(tlist))
        return " ".join(s.text for s in t.fetch()).replace("\n", " ")
    except Exception:
        return None


def note_from_youtube(url, topics, caption=""):
    vid = youtube_id(url)
    clean_url = f"https://www.youtube.com/watch?v={vid}"
    yt_title = youtube_title(clean_url)
    transcript = youtube_transcript(vid)
    extra = f"\nArun's own comment when saving: {caption}" if caption else ""

    if transcript and len(transcript) > 200:
        # Cheap route: read the transcript as text.
        resp = _generate(contents=f"YouTube video: {yt_title or clean_url}{extra}\n\nTranscript:\n{transcript[:400_000]}",
            config=_note_config(topics, "write an empty string (the transcript is stored separately)."),
        )
        note = _parse(resp)
        note.content = transcript
    else:
        # No captions: let Gemini watch the video itself (low resolution keeps long videos within free limits).
        cfg = _note_config(topics, "a word-for-word transcript of everything spoken (translated to English if not English; mark unclear words as [unclear]), then a short list of any important text shown on screen. This is the stored original, so do not summarise it.")
        cfg.media_resolution = types.MediaResolution.MEDIA_RESOLUTION_LOW
        resp = _generate(contents=types.Content(parts=[
                types.Part(file_data=types.FileData(file_uri=clean_url)),
                types.Part(text=f"Make a note of this video.{extra}"),
            ]),
            config=cfg,
        )
        note = _parse(resp)
    if yt_title and not note.title:
        note.title = yt_title
    return note, clean_url


# ---------- Images ----------

def note_from_image(image_bytes, mime, topics, caption=""):
    extra = f"\nArun's own comment when saving: {caption}" if caption else ""
    resp = _generate(contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime),
                  f"Make a note of this screenshot.{extra}"],
        config=_note_config(topics, "all the readable text in the image, word for word."),
    )
    return _parse(resp)


# ---------- Instagram / X posts ----------

def _upload(path, mime):
    """Videos go through Gemini's file upload; wait until Gemini has processed them."""
    f = client().files.upload(file=path, config=types.UploadFileConfig(mime_type=mime))
    for _ in range(60):
        if f.state and f.state.name == "ACTIVE":
            return f
        if f.state and f.state.name == "FAILED":
            raise RuntimeError("Gemini couldn't process the video")
        time.sleep(2)
        f = client().files.get(name=f.name)
    raise RuntimeError("Gemini took too long to process the video")


def note_from_post(post, topics, caption=""):
    """post is a social.Post (Instagram reel/post or X post) with text and downloaded media."""
    where = {"instagram": "Instagram reel/post", "x": "X (Twitter) post", "linkedin": "LinkedIn post", "reddit": "Reddit post (with its top comments)",
             "voice": "Voice note Arun recorded himself", "video": "Video file Arun sent", "pdf": "PDF document",
             "images": "Set of screenshots Arun sent together", "text": "Text Arun forwarded"}.get(post.kind, "Item")
    intro = f"{where}" + (f" by {post.author}" if post.author else "") + f".\nText / caption:\n{post.text or '(none)'}"
    if caption:
        intro += f"\n\nArun's own comment when saving: {caption}"
    parts, uploaded = [], []
    try:
        for path, mime in post.media:
            if mime.startswith("image/"):
                with open(path, "rb") as fh:
                    parts.append(types.Part.from_bytes(data=fh.read(), mime_type=mime))
            else:
                f = _upload(path, mime)
                uploaded.append(f)
                parts.append(types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=mime)))
        spoken = ("a word-for-word transcript of everything spoken (translated to English if not English; mark unclear "
                  "words as [unclear]), then a short list of any important text shown on screen. This is the stored "
                  "original, so do not summarise it.")
        if post.kind == "pdf":
            rule = "the full text of the document, word for word (this is the stored original, do not summarise it)."
        elif any(m.startswith(("video/", "audio/")) for _, m in post.media):
            rule = "the post text (if any), then " + spoken
        else:
            rule = "the full text plus any text in the images, word for word."
        resp = _generate(contents=parts + [intro + "\n\nMake a note of this post."],
                         config=_note_config(topics, rule))
        return _parse(resp)
    finally:
        for f in uploaded:  # don't leave copies on Google's side
            try:
                client().files.delete(name=f.name)
            except Exception:
                pass


def read_url(url, what):
    """Gemini opens a web page from Google's side and copies `what` word for word."""
    resp = _generate(
        contents=f"Open this page: {url}\nCopy, word for word: {what}. Do not summarise. "
                 f"If you cannot open the page or it doesn't exist, reply exactly CANNOT_OPEN.",
        config=types.GenerateContentConfig(tools=[types.Tool(url_context=types.UrlContext())]))
    return (resp.text or "").strip()


# ---------- Plain notes ----------

def note_from_text(text, topics):
    resp = _generate(contents=f"Arun typed this note to remember:\n{text}",
        config=_note_config(topics, "Arun's note exactly as he typed it."),
    )
    note = _parse(resp)
    note.content = text
    return note


# ---------- Embeddings + answers ----------

def embed(text, for_query=False):
    r = client().models.embed_content(
        model=EMBED_MODEL,
        contents=text[:8000],
        config=types.EmbedContentConfig(
            output_dimensionality=EMBED_DIM,
            task_type="RETRIEVAL_QUERY" if for_query else "RETRIEVAL_DOCUMENT",
        ),
    )
    return r.embeddings[0].values


def note_text_for_search(note):
    return f"{note.title}\nTopic: {note.topic}\n{note.summary}\n" + "\n".join(note.key_points)


RECAP_RULES = """You write Arun's Sunday recap of what he saved in his second brain this week.
Goal: help him actually remember and use it. Plain, friendly English, like a friend texting. Plain text only (no markdown, no ** bold **, no tables); use emoji section markers and • bullets.
Format:
🧠 Your week: <N> saves
📚 By topic: one line per topic: the topic, then the 1-2 most useful lessons in a few words each
💡 Top 3 lessons: the three most useful, specific points from the whole week (keep numbers / exact steps)
✅ Try this week: 3 small, concrete actions he can do, based on the saves
{throwback_rule}
Keep it under 250 words. No intro or sign-off."""


def weekly_recap(rows, throwback=None):
    def fmt(r):
        _, created, stype, _, title, topic, summary, points = r
        return f"- {title} ({topic}, {stype}, {created:%a}): {summary} Points: " + "; ".join(points or [])
    rule = ("🔁 From your past: one line reminding him of this older save and why it still matters."
            if throwback else "")
    text = "This week's saves:\n" + "\n".join(fmt(r) for r in rows)
    if throwback:
        text += f"\n\nOlder save to bring back (saved {throwback[1]:%d %b}):\n" + fmt(throwback)
    resp = _generate(contents=text, config=types.GenerateContentConfig(
        system_instruction=RECAP_RULES.format(throwback_rule=rule)))
    return resp.text.strip()


ANSWER_RULES = """You are Arun's second brain. Answer his question using ONLY the saved notes below.
- Plain, friendly English. Short: get to the useful advice fast. Use bullet points for steps.
- After each fact, cite the note number like [1] or [2].
- If the notes don't answer the question, say so honestly in one line, then mention the closest thing he did save (if any).
- Never invent advice that isn't in the notes.
- If the notes DISAGREE with each other on his question, never blend them and never silently pick one. Start with
  "Your saves disagree:" then give each side in one line with its citation and saved date, say which is newer,
  and add the reason each side gives (if the notes give one). Let Arun decide.
- Plain text only: no markdown headings, no ** bold **, no tables."""


def answer(question, rows):
    blocks = []
    for i, r in enumerate(rows, 1):
        _, created, stype, url, title, topic, summary, points, content, _ = r
        body = f"[{i}] {title} ({stype}, saved {created:%d %b %Y %H:%M}, topic: {topic})\nSummary: {summary}\nKey points:\n- " + "\n- ".join(points or [])
        if content:
            body += f"\nOriginal (excerpt): {content[:6000]}"
        blocks.append(body)
    resp = _generate(contents=f"Saved notes:\n\n" + "\n\n".join(blocks) + f"\n\nArun's question: {question}",
        config=types.GenerateContentConfig(system_instruction=ANSWER_RULES),
    )
    return resp.text.strip()
