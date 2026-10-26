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

# Free plan: the Flash models allow only ~20 requests a day each, the Flash-Lite models ~500.
# So the jobs are split: SAVING (lots of requests, mostly reading/extracting) uses Lite first, and keeps the
# scarce Flash quota for ANSWERING (where reasoning quality matters). Each list falls back to the other models.
FLASH_MODELS = [MODEL, "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]
LITE_MODELS = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.1-flash-lite"]
ANSWER_MODELS = FLASH_MODELS + LITE_MODELS
SAVE_MODELS = LITE_MODELS + FLASH_MODELS
FALLBACK_MODELS = ANSWER_MODELS  # (kept for older callers)

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


def _generate(contents, config, models=None):
    last = None
    for model in dict.fromkeys(models or ANSWER_MODELS):
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
        max_output_tokens=60000,  # room for a full transcript of a long video inside the note
    )


def _parse(resp):
    if resp.parsed:
        return resp.parsed
    if not resp.text:  # blocked by a safety filter, or the answer came back empty
        raise RuntimeError("Gemini returned an empty note for this item")
    return Note(**json.loads(resp.text))


# Gemini refuses to copy long stretches of published videos word for word ("RECITATION" = copyright block),
# which used to make whole saves fail. This rule is always allowed:
DETAILED_NOTES = ("detailed notes of everything useful that is said and shown, in order, in your own words. Quote short "
                  "key lines (under 15 words) exactly, in quotes. List any important text shown on screen. "
                  "Do not copy long passages word for word.")


def _note_call(contents, topics, content_rule, resolution=None):
    """Make a note with the cheap, high-quota Lite models first. If Gemini blocks or empties the answer
    (often a copyright 'recitation' block on a full transcript), retry with detailed notes, then a plain note."""
    last = None
    for rule in dict.fromkeys([content_rule, DETAILED_NOTES, "write an empty string."]):
        cfg = _note_config(topics, rule)
        if resolution:
            cfg.media_resolution = resolution
        try:
            return _parse(_generate(contents=contents, config=cfg, models=SAVE_MODELS))
        except (RuntimeError, ValueError, json.JSONDecodeError) as e:
            if "RESOURCE_EXHAUSTED" in str(e):
                raise
            last = e  # empty / blocked / cut-off JSON: try the safer rule
    raise last


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
        # Cheap route: read the transcript as text (works from home internet; YouTube blocks it from servers).
        note = _note_call(f"YouTube video: {yt_title or clean_url}{extra}\n\nTranscript:\n{transcript[:400_000]}",
                          topics, "write an empty string (the transcript is stored separately).")
        note.content = transcript
    else:
        # No captions (always the case on the server): Gemini watches the video itself, at low resolution so long
        # videos fit the free limits, and writes detailed notes (a full transcript would be copyright-blocked).
        note = _note_call(types.Content(parts=[types.Part(file_data=types.FileData(file_uri=clean_url)),
                                               types.Part(text=f"Make a note of this video.{extra}")]),
                          topics, DETAILED_NOTES, resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW)
    if yt_title and not note.title:
        note.title = yt_title
    return note, clean_url


# ---------- Images ----------

def note_from_image(image_bytes, mime, topics, caption=""):
    extra = f"\nArun's own comment when saving: {caption}" if caption else ""
    return _note_call([types.Part.from_bytes(data=image_bytes, mime_type=mime), f"Make a note of this screenshot.{extra}"],
                      topics, "all the readable text in the image, word for word.")


# ---------- Instagram / X posts ----------

def _upload(path, mime):
    """Videos go through Gemini's file upload; wait until Gemini has processed them."""
    f = client().files.upload(file=path, config=types.UploadFileConfig(mime_type=mime))
    for _ in range(60):
        if f.state and f.state.name == "ACTIVE":
            return f
        if f.state and f.state.name == "FAILED":
            _delete_upload(f)
            raise RuntimeError("Gemini couldn't process the video")
        time.sleep(2)
        f = client().files.get(name=f.name)
    _delete_upload(f)
    raise RuntimeError("Gemini took too long to process the video")


def _delete_upload(f):
    try:
        client().files.delete(name=f.name)
    except Exception:
        pass


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
        return _note_call(parts + [intro + "\n\nMake a note of this post."], topics, rule)
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
        config=types.GenerateContentConfig(tools=[types.Tool(url_context=types.UrlContext())]), models=SAVE_MODELS)
    return (resp.text or "").strip()


# ---------- On request: on-screen text, hidden resources, things Arun attaches ----------

SCREEN_RULES = """Copy, WORD FOR WORD, every block of text shown on screen in this video that someone might want to
reuse: prompts, code, commands, links/URLs, tool names, lists, settings, templates, slide text.
- Keep the original line breaks. Put each block under a line like "[0:42] Prompt shown on screen:".
- Skip subtitles that only repeat what is being spoken, logos, and watermarks.
- If a word is unreadable write [unclear]. Never guess or invent text.
- If there is no such text at all, reply exactly: NO_SCREEN_TEXT"""


def screen_text_youtube(url):
    """Read on-screen text of a YouTube video (Gemini watches it at normal, not low, resolution)."""
    resp = _generate(contents=types.Content(parts=[types.Part(file_data=types.FileData(file_uri=url)),
                                                   types.Part(text="Copy the on-screen text.")]),
                     config=types.GenerateContentConfig(system_instruction=SCREEN_RULES,
                                                        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM),
                     models=SAVE_MODELS)
    return (resp.text or "").strip()


def screen_text_post(post):
    """Read on-screen text from a downloaded post (social.Post): its video and images."""
    parts, uploaded = [], []
    try:
        for path, mime in post.media:
            if mime.startswith("image/"):
                with open(path, "rb") as fh:
                    parts.append(types.Part.from_bytes(data=fh.read(), mime_type=mime))
            elif mime.startswith("video/"):
                f = _upload(path, mime)
                uploaded.append(f)
                parts.append(types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=mime)))
        if not parts:
            return "NO_SCREEN_TEXT"
        resp = _generate(contents=parts + ["Copy the on-screen text."],
                         config=types.GenerateContentConfig(system_instruction=SCREEN_RULES), models=SAVE_MODELS)
        return (resp.text or "").strip()
    finally:
        for f in uploaded:
            _delete_upload(f)


class Offer(BaseModel):
    has_offer: bool          # does the creator offer something extra (prompt, template, link, guide…)?
    how_to_get: str          # e.g. "Comment PROMPT and they DM it", "Link in bio", "" if none
    what: str                # what the resource is, e.g. "the full ChatGPT prompt for cold emails"
    urls: list[str]          # any URLs already written in the post text / transcript


def find_offer(title, author, text):
    """Spot 'comment X and I'll DM you…' style offers in a save's text."""
    resp = _generate(
        contents=f"Title: {title}\nCreator: {author or 'unknown'}\n\nText of the post / video:\n{text[:20000]}",
        config=types.GenerateContentConfig(
            system_instruction="Does the creator offer an extra resource (a prompt, template, guide, website, link, "
                               "file) that viewers get by commenting a word, DMing, following a link in bio, or a link "
                               "in the text? Only report what is actually stated. Use empty strings/lists when none.",
            response_mime_type="application/json", response_schema=Offer), models=SAVE_MODELS)
    return resp.parsed if resp.parsed else Offer(**json.loads(resp.text or "{}"))


def search_resource(what, author):
    """Try to find the offered resource publicly on the web (Google Search through Gemini). '' if not found."""
    try:
        resp = _generate(
            contents=f"Find where this is publicly available online: {what}" + (f" (by {author})" if author else "") +
                     ". Reply with the best 1-3 URLs and one line each on what they are. "
                     "If you can't find it with confidence, reply exactly NOT_FOUND.",
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]))
        text = (resp.text or "").strip()
        return "" if not text or "NOT_FOUND" in text else text
    except Exception:
        return ""  # search not available on the free plan / quota: fall back to the to-get list


def read_attachment(text_or_url):
    """What Arun attached to a save (DM text or a link). For a plain web link, read the page itself."""
    url = re.search(r"https?://\S+", text_or_url or "")
    if url and len(text_or_url.strip()) < len(url.group(0)) + 40:  # basically just a link
        try:
            page = read_url(url.group(0), "the main content of the page (the prompt / template / guide / text)")
            if page and "CANNOT_OPEN" not in page:
                return f"{text_or_url.strip()}\n\n{page}"
        except Exception:
            pass  # Gemini busy / out of free quota: keep the link itself, never lose what Arun sent
    return text_or_url.strip()


def image_text(image_bytes, mime):
    """All text in a screenshot, word for word (for screenshots attached to an existing save)."""
    resp = _generate(contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime),
                               "Copy all the text in this image, word for word. Keep line breaks."],
                     config=types.GenerateContentConfig(), models=SAVE_MODELS)
    return (resp.text or "").strip()


# ---------- Plain notes ----------

def note_from_text(text, topics):
    note = _note_call(f"Arun typed this note to remember:\n{text}", topics, "Arun's note exactly as he typed it.")
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


_embed_log = []  # (time, how many texts) of recent embedding calls
EMBED_PER_MINUTE = 90  # free plan: 100 texts per minute; stay a little under


def _pace(n):
    """Wait if embedding n more texts now would go over the free per-minute limit."""
    while True:
        now = time.time()
        _embed_log[:] = [(t, k) for t, k in _embed_log if now - t < 60]
        used = sum(k for _, k in _embed_log)
        if used + n <= EMBED_PER_MINUTE or not _embed_log:
            _embed_log.append((now, n))
            return
        time.sleep(61 - (now - _embed_log[0][0]))


def embed_many(texts, batch=40):
    """Embeddings for many texts in few requests, paced to the free per-minute limit (retries if it's hit anyway)."""
    out = []
    for i in range(0, len(texts), batch):
        part = [t[:8000] for t in texts[i:i + batch]]
        for attempt in range(4):
            _pace(len(part))
            try:
                r = client().models.embed_content(
                    model=EMBED_MODEL, contents=part,
                    config=types.EmbedContentConfig(output_dimensionality=EMBED_DIM, task_type="RETRIEVAL_DOCUMENT"))
                break
            except Exception as e:
                if "429" not in str(e) or attempt == 3:
                    raise
                time.sleep(35)
        out += [e.values for e in r.embeddings]
    return out


def test_question(title, summary, points):
    """A realistic question Arun might ask later that THIS save answers (for the weekly memory test).
    Written like a real-life problem, without copying the title, so the test is honest."""
    resp = _generate(
        contents=f"Saved note:\nTitle: {title}\nSummary: {summary}\nPoints: " + "; ".join(points or []),
        config=types.GenerateContentConfig(system_instruction=(
            "Write ONE short question (under 20 words) that someone would ask in real life when they need the advice "
            "in this note. Describe their situation; do not copy the title's words. Reply with the question only.")),
        models=SAVE_MODELS)
    return (resp.text or "").strip().strip('"')


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
    return (resp.text or "").strip()


ANSWER_RULES = """Arun saves videos and posts from people who are great at things (sales, negotiation, coding,
content, communication, career, anything). You are his panel of those mentors, standing next to him in a real moment
of need. Use what THEY taught (the saved notes) to tell him exactly what to do now.

How to answer:
1. First line: the direct answer for his situation. No greeting, no restating the question.
2. "What your mentors would tell you": the 2-4 most useful moves from the notes, each APPLIED to his situation, with the
   specific technique, steps, numbers or exact words to say. Name the source naturally (the creator if the note names
   them, otherwise the video's title) and cite it like [1]. Combine several mentors into one plan when they fit together.
3. "Your move now": 1-3 concrete next actions or the exact lines to say, in order.
4. Only if the notes don't fully cover it: a short "My take:" with your own reasoning, never presented as something
   he saved. If nothing fits at all, say so in one line, then "My take:".
5. If the notes DISAGREE: start with "Your saves disagree:", give each side in one line with citation, saved date and
   reason, say which is newer, and let him decide.
Style: plain, friendly English like a smart friend who has been there. Short (under 220 words). Bullet points for steps.
Plain text only: no markdown headings, no ** bold **, no tables."""

DEEPER = """
His previous answer to this question was NOT helpful enough:
---
{previous}
---
Do clearly better: be more specific to his situation, use more of the notes' concrete details (exact steps, numbers,
words to say), fix anything vague or generic, and don't repeat the same advice in the same way."""


def answer(question, rows, history="", excerpt=6000, profile="", previous=None):
    """history: the previous question + answer, so follow-ups like 'tell me more' make sense.
    previous: an answer Arun rated 👎; the retry then thinks longer and must do better."""
    blocks = []
    for i, r in enumerate(rows, 1):
        _, created, stype, url, title, topic, summary, points, content = r[:9]
        body = f"[{i}] {title} ({stype}, saved {created:%d %b %Y %H:%M}, topic: {topic})\nSummary: {summary}\nKey points:\n- " + "\n- ".join(points or [])
        if content:
            body += f"\nMost relevant part of the original: {content[:excerpt]}"
        blocks.append(body)
    prev = f"\n\nEarlier in this chat (for context only):\n{history}" if history else ""
    if profile:  # who Arun is, so advice fits his real situation
        prev = f"\n\nAbout Arun (use this to tailor the advice):\n{profile}" + prev
    rules = ANSWER_RULES + (DEEPER.format(previous=previous[:3000]) if previous else "")
    resp = _generate(contents=f"Saved notes:\n\n" + "\n\n".join(blocks) + prev + f"\n\nArun's question: {question}",
        config=types.GenerateContentConfig(system_instruction=rules, thinking_config=types.ThinkingConfig(
            thinking_budget=8192 if previous else 2048)),
        models=ANSWER_MODELS,  # the best models first: reasoning quality matters most here
    )
    return (resp.text or "").strip()


class Clarify(BaseModel):
    ask: bool            # true only if the best advice really depends on something we don't know
    question: str        # one short question to ask Arun
    options: list[str]   # 2-3 likely answers as short tap buttons (under 25 characters each)


def clarify_check(question, profile, titles):
    """Like a good coach: before advising on a vague situation, ask ONE quick question (only when it truly matters)."""
    resp = _generate(
        contents=f"About Arun: {profile or 'unknown'}\nRelevant saves he has: " + "; ".join(titles[:6]) +
                 f"\n\nArun's message: {question}",
        config=types.GenerateContentConfig(
            system_instruction=(
                "Decide if ONE short clarifying question is needed before giving him advice. Ask only when the right "
                "advice would be clearly different depending on a missing detail (who the other person is, the goal, "
                "the stage, the constraint). Do NOT ask if the message is already specific, is a simple factual or "
                "how-to question, or asks for a list/summary. Most messages need no question."),
            response_mime_type="application/json", response_schema=Clarify),
        models=SAVE_MODELS)
    c = resp.parsed if resp.parsed else Clarify(**json.loads(resp.text or '{"ask": false, "question": "", "options": []}'))
    c.options = [o[:30] for o in c.options[:3]]
    return c


# ---------- "Just talk": understand plain-language requests ----------

class Intent(BaseModel):
    action: str    # one of ACTIONS
    target: str    # which save: "last", "#12", or words describing it ("the one about cold calling"); else ""
    text: str      # the payload: the question, the note to save, search words, profile text, topic, on/off…


ACTIONS = {
    "ask": "a question or a situation he needs advice/help with (THE DEFAULT when unsure)",
    "save_note": "he wants to remember/save a thought or tip he typed (\"remember that…\", \"note: …\", \"save this…\")",
    "delete": "delete/remove a save",
    "find": "find/show/list saves about something (\"what did I save about pricing\", \"show my negotiation saves\")",
    "recent": "show the latest saves",
    "topics": "show topics / what kinds of things he saved",
    "reminders": "turn daily reminders on or off (text = on/off)",
    "profile": "tell the bot who he is / update his background (text = the description)",
    "recap": "a recap/summary of the last week",
    "practice": "rehearse / role-play / practice a conversation or situation (text = the situation)",
    "playbook": "make/show/update a playbook or guide for a topic (text = the topic)",
    "worked": "a tip/save WORKED for him in real life (target = which one)",
    "screen_text": "get the text/prompt shown on screen in a saved video (target = which one)",
    "resource": "get the resource/link a creator offers (comment-for-link) (target = which one)",
    "voice_replies": "turn voice replies on / off / back to automatic (text = always / off / auto)",
    "backup": "make a backup now",
    "export": "download/export everything",
    "help": "how to use the bot / what can you do",
}


def classify(message):
    """What does Arun want? Plain words instead of commands."""
    resp = _generate(
        contents=f"Arun's message to his second-brain bot:\n{message}",
        config=types.GenerateContentConfig(
            system_instruction="Pick the action he wants. Actions:\n" +
                               "\n".join(f"- {k}: {v}" for k, v in ACTIONS.items()) +
                               "\nIf it's a question, a problem, or a situation, it is 'ask' (keep his full words in "
                               "text). Use 'ask' whenever unsure.",
            response_mime_type="application/json", response_schema=Intent),
        models=SAVE_MODELS)
    i = resp.parsed if resp.parsed else Intent(**json.loads(resp.text or '{"action":"ask","target":"","text":""}'))
    if i.action not in ACTIONS:
        i.action = "ask"
    return i


# ---------- Practice mode: rehearse a real conversation with the mentors coaching ----------

PRACTICE_RULES = """You are running a practice session for Arun. Situation he wants to rehearse: {situation}
About Arun: {profile}

You play TWO roles in every reply:
1. THE OTHER PERSON in the situation (client, boss, interviewer, colleague…). Be realistic: push back, raise real
   objections, don't make it easy, but stay fair. 1-3 sentences, in character, in quotes.
2. THE COACH, after a line break, starting with "💡 Coach:" — talking TO Arun as "you": one or two short sentences on how
   his last line landed and his best next move, using what his saved mentors teach (notes below; cite like [2]).
   Praise what was good. Only comment on what he actually said.
On the very first turn (no messages from Arun yet), open the scene in character, then a one-line coach tip TO Arun for
how to open (e.g. "💡 Coach: Lead with the news, don't apologise [3].").
Plain text, no markdown.

His saved mentors' advice for this situation:
{notes}"""

DEBRIEF_RULES = """The practice session below is over. Give Arun a short debrief, talking to him as "you". Judge ONLY the lines
marked ARUN; never claim he did something he didn't say. If he only said a line or two, say the session was short and
focus on how to continue:
- Score: X/10 and one line why.
- What you did well (1-2 bullets).
- Fix next time (2-3 bullets), each with the better line to say, based on his mentors' advice [cite].
- Your best lines to use for real (2-3 exact sentences).
Plain text, no markdown, under 180 words.

Situation: {situation}
His mentors' advice:
{notes}"""


def notes_for(rows, limit=6, chars=1500):
    return "\n\n".join(f"[{i}] {r[4]}: {r[6]}\n- " + "\n- ".join(r[7] or []) + (f"\n{r[8][:chars]}" if r[8] else "")
                       for i, r in enumerate(rows[:limit], 1))


def practice_turn(situation, notes, history, profile=""):
    """history: list of ("arun"|"bot", text). Returns the bot's next turn (other person + coach)."""
    convo = "\n".join(f"{'ARUN' if who == 'arun' else 'YOU'}: {t}" for who, t in history) or "(not started yet)"
    resp = _generate(contents=f"Conversation so far:\n{convo}\n\nYour next turn:",
                     config=types.GenerateContentConfig(system_instruction=PRACTICE_RULES.format(
                         situation=situation, profile=profile or "unknown", notes=notes)))
    return (resp.text or "").strip()


def practice_debrief(situation, notes, history):
    convo = "\n".join(f"{'ARUN' if who == 'arun' else 'OTHER/COACH'}: {t}" for who, t in history)
    resp = _generate(contents=convo, config=types.GenerateContentConfig(
        system_instruction=DEBRIEF_RULES.format(situation=situation, notes=notes)))
    return (resp.text or "").strip()


# ---------- Living playbooks ----------

PLAYBOOK_RULES = """Write "Arun's {topic} Playbook": ONE clean, practical guide built from the saved notes below
(advice from creators he trusts). It replaces re-watching 20 videos.

Structure:
- Title line, then a 2-line "The core idea" summary.
- Sections by real situation (e.g. "When a client says it's too expensive", "Before the call"), 4-8 sections.
  In each: the moves, steps, numbers and exact lines to say, merged from all notes that help, each with its source like
  (#21). No repetition across sections.
- When creators DISAGREE, never blend or pick: write "⚖️ Two schools of thought:" with each side, its source, and WHEN
  each one fits (type of client, stage, goal). End with "👉 Your situation decides."
- {proven_rule}
- Finish with "Top 5 rules" (one line each).
Use markdown headings (##) and bullets. Only use what the notes say; mark anything else as "My take:"."""


def build_playbook(topic, rows, profile=""):
    blocks = []
    for r in rows:
        sid, created, stype, url, title, t, summary, points, content, proven = r
        blocks.append(f"(#{sid}) {title}{' — PROVEN by Arun' if proven else ''} [{t}]\n{summary}\n- " +
                      "\n- ".join(points or []) + (f"\n{content[:2500]}" if content else ""))
    resp = _generate(contents=(f"About Arun: {profile}\n\n" if profile else "") + "Saved notes:\n\n" + "\n\n".join(blocks),
                     config=types.GenerateContentConfig(system_instruction=PLAYBOOK_RULES.format(
                         topic=topic.title(), proven_rule=(
                             "Notes whose header ends with '— PROVEN by Arun' are tips he tried and that worked: mark ONLY "
                             "those with '✅ proven by you' and put them first in their section." if any(r[9] for r in rows) else
                             "None of these tips is proven by Arun yet: never write 'proven by you' or a ✅ mark.")),
                         max_output_tokens=12000))
    return (resp.text or "").strip()


# ---------- Voice replies (Gemini text-to-speech → MP3 voice note) ----------

# Free plan: about 10 voice replies a day per model. His REELS use gemini-3.8-flash-lite-tts, so that one goes last
# (Google can share free quotas within one account), keeping his reel voice quota for reels.
TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts", "gemini-3.8-flash-tts",
              "gemini-3.8-flash-lite-tts"]
MENTOR_VOICE = "Achird"    # natural, friendly everyday voice (Puck is his REELS voice; too performed for this)
OTHER_VOICE = "Charon"     # the other person in practice mode: a different, clearly separate voice


def spoken_version(text, limit=900):
    """Turn a chat answer into something nice to listen to: no sources, links, [1] marks, emoji or bullets."""
    text = re.split(r"\n\s*Sources:", text)[0]
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[\d+\]|#\d+", "", text)
    text = re.sub(r"[*_`#>•]|[\U0001F300-\U0001FAFF☀-➿]", "", text)
    text = re.sub(r"\n\s*-\s*", "\n", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) > limit:  # about a minute of speech; the full answer is in the text above it
        text = text[:limit].rsplit(". ", 1)[0] + "."
    return text


VOICE_SCRIPT_RULES = """Rewrite this written answer as a short VOICE NOTE that Arun will listen to, like a smart friend
who has been there talking to him. It is spoken, not read:
- 80-120 words (about 45 seconds). Short, simple sentences. Contractions (you're, don't, here's).
- Start straight with the main point for his situation. No greeting, no "great question".
- Mention at most two mentors naturally by name or video ("Like Simridhi says in her speaking video…"). Never say
  numbers in brackets, "source", links, or "number one / bullet".
- If there's an exact line for him to say, say it slowly and clearly: "Try saying: …" then pause.
- No headings, no lists, no symbols. Use "..." for a natural pause between ideas.
- End with ONE next step: "So, your move: …"
Reply with the script only."""


def voice_script(question, answer_text):
    """A listen-friendly script for a voice reply (the written answer has headings and lists that sound robotic)."""
    try:
        resp = _generate(contents=f"His question: {question}\n\nWritten answer:\n{answer_text}",
                         config=types.GenerateContentConfig(system_instruction=VOICE_SCRIPT_RULES), models=SAVE_MODELS)
        script = (resp.text or "").strip()
        if script:
            return script
    except Exception:
        pass
    return spoken_version(answer_text)  # fallback: the written answer, cleaned up


# Plain and natural, NOT the reels delivery (he found that exaggerated for everyday answers).
VOICE_STYLE = ("Say this plainly and naturally, like an ordinary voice message to a friend: normal everyday tone, "
               "steady relaxed pace, no dramatic emphasis, no acting, no announcer voice")
NATURAL_VOICES = {"Achird": "friendly", "Umbriel": "easy-going", "Zubenelgenubi": "casual"}


def _tts_pcm(text, voice, style):
    """Gemini text-to-speech → (raw 16-bit mono PCM, sample rate)."""
    import io as _io
    import wave
    resp = _generate(contents=f"{style}:\n\n{text}", models=TTS_MODELS, config=types.GenerateContentConfig(
        response_modalities=["AUDIO"], speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)))))
    blob = resp.candidates[0].content.parts[0].inline_data
    if "wav" in (blob.mime_type or ""):
        with wave.open(_io.BytesIO(blob.data)) as w:
            return w.readframes(w.getnframes()), w.getframerate()
    m = re.search(r"rate=(\d+)", blob.mime_type or "")
    return blob.data, int(m.group(1)) if m else 24000


def speak(text, voice=None, style=VOICE_STYLE):
    """Text → (audio bytes, format). 'ogg' = Telegram's own voice-note format (OGG/Opus, plays inline like a normal
    voice message); 'mp3' only as a fallback if the converter isn't available."""
    import subprocess
    pcm, rate = _tts_pcm(text, voice or os.environ.get("VOICE_NAME") or MENTOR_VOICE, style)
    try:
        import imageio_ffmpeg  # a bundled ffmpeg, so the server needs nothing extra installed
        out = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "s16le", "-ar", str(rate),
                              "-ac", "1", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1"],
                             input=pcm, capture_output=True, timeout=60, check=True).stdout
        if out[:4] == b"OggS":
            return out, "ogg"
    except Exception:
        pass
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(48)
    enc.set_in_sample_rate(rate)
    enc.set_channels(1)
    enc.set_quality(2)
    return bytes(enc.encode(pcm) + enc.flush()), "mp3"

def transcribe(audio_bytes, mime):
    """What Arun said in a voice note (to answer it as a question)."""
    resp = _generate(contents=[types.Part.from_bytes(data=audio_bytes, mime_type=mime),
                               "Write down exactly what is said in this voice note, in English (translate Tamil/Hindi). "
                               "Reply with the words only."],
                     config=types.GenerateContentConfig(), models=SAVE_MODELS)
    return (resp.text or "").strip()
