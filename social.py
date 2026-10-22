"""Fetches Instagram reels/posts and X (Twitter) posts so Gemini can read them.

Each fetch returns a Post: the text (caption / tweet), plus media files to show Gemini.
Media is downloaded to a temp folder and deleted right after the note is made.
"""
import html
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
TMP_ROOT = os.environ.get("TMP_DIR") or os.path.join(HERE, "tmp")
MAX_BYTES = 60 * 1024 * 1024  # skip media bigger than 60 MB
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

INSTAGRAM_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(reels?|p|tv)/([\w-]+)", re.I)
LINKEDIN_RE = re.compile(r"(?:linkedin\.com/(?:posts|pulse|feed/update|embed/feed/update)/|lnkd\.in/)[^\s?#]+", re.I)
REDDIT_RE = re.compile(r"(?:reddit\.com/r/\w+/(?:comments|s)/\w+|reddit\.com/comments/\w+|redd\.it/\w+)", re.I)
X_RE = re.compile(r"(?:twitter|x|fxtwitter|vxtwitter|fixupx)\.com/(\w+)/status(?:es)?/(\d+)", re.I)


class Blocked(RuntimeError):
    """The site refused us (rate limit / login wall). Usually temporary, so the link is retried later."""


class Gone(RuntimeError):
    """The post is deleted, private or empty. Retrying won't help."""


@dataclass
class Post:
    url: str                       # clean link we store
    kind: str                      # instagram | x | linkedin | reddit
    author: str = ""
    text: str = ""
    media: list = field(default_factory=list)   # [(path, mime)]
    workdir: str = ""
    note: str = ""                 # what we could / couldn't get, for the reply

    def cleanup(self):
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)


def kind_of(url):
    if INSTAGRAM_RE.search(url):
        return "instagram"
    if X_RE.search(url):
        return "x"
    if LINKEDIN_RE.search(url):
        return "linkedin"
    if REDDIT_RE.search(url):
        return "reddit"
    return None


def clean_url(url):
    m = INSTAGRAM_RE.search(url)
    if m:
        kind = "reel" if m.group(1).lower().startswith("reel") else m.group(1).lower()
        return f"https://www.instagram.com/{kind}/{m.group(2)}/"
    m = X_RE.search(url)
    if m:
        return f"https://x.com/{m.group(1)}/status/{m.group(2)}"
    m = LINKEDIN_RE.search(url)
    if m:
        return "https://www." + m.group(0) if m.group(0).lower().startswith("linkedin") else "https://" + m.group(0)
    m = REDDIT_RE.search(url)
    if m:
        return ("https://" if m.group(0).startswith("redd.it") else "https://www.") + m.group(0) + ("/" if "/comments/" in m.group(0) else "")
    return url


def _workdir():
    os.makedirs(TMP_ROOT, exist_ok=True)
    return tempfile.mkdtemp(dir=TMP_ROOT)


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    return urllib.request.urlopen(req, timeout=timeout)


def _download(url, path):
    with _get(url, timeout=60) as r, open(path, "wb") as f:
        size = 0
        while chunk := r.read(1 << 16):
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("media too big")
            f.write(chunk)
    return path


# ---------- Instagram ----------

def fetch_instagram(url):
    import yt_dlp

    post = Post(url=clean_url(url), kind="instagram", workdir=_workdir())
    opts = {
        "format": "b/ba/bv",  # one file with sound if possible (no ffmpeg on the server)
        "outtmpl": os.path.join(post.workdir, "%(id)s.%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": False,
        "max_filesize": MAX_BYTES, "socket_timeout": 30, "retries": 2,
    }
    cookies = os.environ.get("INSTAGRAM_COOKIES_FILE")
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(post.url, download=True)
        entries = info.get("entries") or [info]
        post.author = info.get("uploader") or info.get("channel") or ""
        post.text = (info.get("description") or info.get("title") or "").strip()
        for e in entries:
            for d in e.get("requested_downloads") or []:
                p = d.get("filepath")
                if p and os.path.exists(p):
                    ext = os.path.splitext(p)[1].lower()
                    mime = {".mp4": "video/mp4", ".m4a": "audio/mp4", ".webm": "video/webm",
                            ".jpg": "image/jpeg", ".png": "image/png"}.get(ext, "video/mp4")
                    post.media.append((p, mime))
    except Exception as e:  # blocked / login wall / photo post: fall back to the page's caption
        post.note = f"couldn't download the video ({str(e)[:80]})"
    if not post.text:
        post.text = _instagram_caption(post.url)
    if not post.media and not post.text:
        post.cleanup()
        raise Blocked("Instagram is blocking my server right now (it does this after several reels in a row).")
    return post


def _instagram_caption(url):
    """Caption from the page's preview tags (what WhatsApp/Telegram show as the link preview)."""
    try:
        page = _get(url).read(400_000).decode("utf-8", "ignore")
        m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', page) or \
            re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', page)
        return html.unescape(m.group(1)).strip() if m else ""
    except Exception:
        return ""


# ---------- X / Twitter ----------

def fetch_x(url):
    user, sid = X_RE.search(url).groups()
    post = Post(url=f"https://x.com/{user}/status/{sid}", kind="x", workdir=_workdir())
    try:
        data = json.load(_get(f"https://api.fxtwitter.com/{user}/status/{sid}"))
        t = data["tweet"]
        post.author = f'{t["author"]["name"]} (@{t["author"]["screen_name"]})'
        parts = [t.get("text") or ""]
        if t.get("quote"):
            q = t["quote"]
            parts.append(f'Quoted post by @{q["author"]["screen_name"]}: {q.get("text", "")}')
        art = t.get("article")
        if art:
            parts.append(f'Article: {art.get("title", "")}\n{art.get("preview_text", "")}')
        post.text = "\n\n".join(p for p in parts if p).strip()
        media = (t.get("media") or {})
        for i, v in enumerate(media.get("videos") or []):
            post.media.append((_download(v["url"], os.path.join(post.workdir, f"v{i}.mp4")), "video/mp4"))
        for i, p in enumerate((media.get("photos") or [])[:4]):
            post.media.append((_download(p["url"], os.path.join(post.workdir, f"p{i}.jpg")), "image/jpeg"))
    except Exception as e:
        post.note = f"used the basic version ({str(e)[:80]})"
        post.text = _x_oembed(post.url)
    if not post.text and not post.media:
        post.cleanup()
        raise Gone("Couldn't read that X post (deleted or private?). Send a screenshot instead.")
    return post


def _x_oembed(url):
    """X's official embed: gives the post text without login."""
    try:
        q = "https://publish.twitter.com/oembed?omit_script=1&url=" + urllib.parse.quote(url, safe="")
        h = json.load(_get(q)).get("html", "")
        h = re.sub(r"<br\s*/?>", "\n", h)
        return html.unescape(re.sub(r"<[^>]+>", " ", h)).strip()
    except Exception:
        return ""


# ---------- LinkedIn ----------

def fetch_linkedin(url):
    """Public LinkedIn posts/articles carry their full text in the page's structured data (no login needed)."""
    post = Post(url=clean_url(url), kind="linkedin", workdir=_workdir())
    try:
        resp = _get(post.url)
        final = resp.geturl()  # lnkd.in short links redirect to the real post
        if "authwall" in final or "/login" in final:
            raise RuntimeError("LinkedIn asked for a login")
        if "linkedin.com" in final:
            post.url = final.split("?")[0]
        page = resp.read(2_000_000).decode("utf-8", "ignore")
    except Exception as e:
        post.cleanup()
        raise Blocked(f"LinkedIn is blocking my server right now ({str(e)[:60]}).")

    images, videos = [], []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if d.get("articleBody") or d.get("text"):
            post.text = (d.get("headline", "") + "\n\n" if d.get("@type") == "Article" else "") + \
                (d.get("articleBody") or d.get("text"))
            a = d.get("author") or {}
            post.author = (a[0] if isinstance(a, list) and a else a).get("name", "") if a else ""
            img = d.get("image")
            for i in (img if isinstance(img, list) else [img] if img else []):
                images.append(i.get("url") if isinstance(i, dict) else i)
            vid = d.get("video")
            for v in (vid if isinstance(vid, list) else [vid] if vid else []):
                if isinstance(v, dict) and v.get("contentUrl"):
                    videos.append(v["contentUrl"])
    if not post.text:  # fall back to the link-preview text
        m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', page)
        post.text = html.unescape(m.group(1)).strip() if m else ""
    if not videos:  # LinkedIn's own video player lists its files in data-sources
        m = re.search(r'data-sources="([^"]+)"', page)
        if m:
            try:
                srcs = json.loads(html.unescape(m.group(1)))
                videos.append(sorted(srcs, key=lambda s: s.get("data-bitrate", 0))[-1]["src"])
            except Exception:
                pass
    try:
        if videos:
            post.media.append((_download(videos[0], os.path.join(post.workdir, "v0.mp4")), "video/mp4"))
        for i, u in enumerate([u for u in images if u][:4]):
            post.media.append((_download(u, os.path.join(post.workdir, f"p{i}.jpg")), "image/jpeg"))
    except Exception as e:
        post.note = f"couldn't download the media ({str(e)[:60]})"
    if not post.text and not post.media:
        post.cleanup()
        raise Gone("Couldn't read that LinkedIn post (private or login-only?). Send a screenshot instead.")
    return post


# ---------- Reddit ----------

REDDIT_UA = "SecondBrainBot/1.0 (personal notes app)"  # Reddit asks for a descriptive user agent


def _reddit_json(path):
    last = None
    for host in ("www.reddit.com", "old.reddit.com"):
        try:
            req = urllib.request.Request(f"https://{host}{path}", headers={"User-Agent": REDDIT_UA})
            return json.load(urllib.request.urlopen(req, timeout=30))
        except Exception as e:
            last = e
    raise last


def _reddit_rss(url):
    """Post + top comments from Reddit's RSS feed (often still open when the JSON API is blocked)."""
    try:
        req = urllib.request.Request(url.rstrip("/") + "/.rss?sort=top&limit=12", headers={"User-Agent": REDDIT_UA})
        feed = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    except Exception:
        return ""
    out = []
    for i, e in enumerate(re.findall(r"<entry>(.*?)</entry>", feed, re.S)):
        author = re.search(r"<name>(.*?)</name>", e)
        title = re.search(r"<title>(.*?)</title>", e)
        body = re.search(r'<content type="html">(.*?)</content>', e, re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(body.group(1)))) if body else ""
        text = " ".join(text.replace("[link] [comments]", "").split())
        if i == 0:
            out.append(f"{html.unescape(title.group(1)) if title else ''}\n\n{text}")
        elif text:
            out.append(f"- Comment by {author.group(1) if author else '?'}: {text[:1500]}")
    if len(out) > 1:
        out.insert(1, "Top comments:")
    return "\n".join(out).strip()


def _reddit_via_gemini(url):
    """Last resort: Gemini opens the page itself (Google's fetcher is rarely blocked by Reddit)."""
    import brain
    try:
        text = brain.read_url(url, "the post title, the post body, and the top 8 comments with their upvote counts")
        return "" if "CANNOT_OPEN" in text else text
    except Exception:
        return ""


def fetch_reddit(url):
    post = Post(url=clean_url(url), kind="reddit", workdir=_workdir())
    try:
        if "/s/" in url or "redd.it/" in url:  # share links redirect to the real post
            req = urllib.request.Request(post.url, headers={"User-Agent": REDDIT_UA})
            post.url = urllib.request.urlopen(req, timeout=30).geturl().split("?")[0]
        pid = re.search(r"/comments/(\w+)", post.url).group(1)
        data = _reddit_json(f"/comments/{pid}.json?limit=30&depth=1&sort=top&raw_json=1")
    except Exception as e:
        # Reddit often blocks its free API now. Backup routes: its RSS feed, then Gemini's own web reader.
        post.text = _reddit_rss(post.url) or _reddit_via_gemini(post.url)
        if post.text:
            post.note = "read through a backup route, so no images/video"
            return post
        post.cleanup()
        raise Blocked(f"Reddit is blocking my server right now ({str(e)[:60]}).")

    p = data[0]["data"]["children"][0]["data"]
    post.url = "https://www.reddit.com" + p["permalink"]
    post.author = f'u/{p.get("author")} in r/{p.get("subreddit")}'
    parts = [p.get("title", ""), p.get("selftext", "")]
    if p.get("url") and "reddit.com" not in p["url"] and "redd.it" not in p["url"]:
        parts.append(f"Linked page: {p['url']}")
    comments = [c["data"] for c in data[1]["data"]["children"] if c["kind"] == "t1" and c["data"].get("body")]
    top = sorted(comments, key=lambda c: c.get("score", 0), reverse=True)[:8]
    if top:
        parts.append("Top comments:\n" + "\n".join(f'- ({c.get("score", 0)} upvotes) {c["body"][:1500]}' for c in top))
    post.text = "\n\n".join(x for x in parts if x).strip()

    try:
        video = ((p.get("secure_media") or {}).get("reddit_video") or {})
        if video.get("fallback_url"):
            v = _download(video["fallback_url"], os.path.join(post.workdir, "v.mp4"))
            post.media.append((v, "video/mp4"))
            base = video["fallback_url"].split("/DASH_")[0]
            for name in ("DASH_AUDIO_128.mp4", "DASH_audio.mp4", "DASH_AUDIO_64.mp4"):  # Reddit keeps sound separate
                try:
                    post.media.append((_download(f"{base}/{name}", os.path.join(post.workdir, "a.mp4")), "audio/mp4"))
                    break
                except Exception:
                    continue
        images = []
        if p.get("is_gallery"):
            for m in (p.get("media_metadata") or {}).values():
                if m.get("s", {}).get("u"):
                    images.append(m["s"]["u"])
        elif re.search(r"\.(jpe?g|png|webp)$", p.get("url", ""), re.I):
            images.append(p["url"])
        for i, u in enumerate(images[:4]):
            post.media.append((_download(u, os.path.join(post.workdir, f"p{i}.jpg")), "image/jpeg"))
    except Exception as e:
        post.note = f"couldn't download the media ({str(e)[:60]})"
    if not post.text and not post.media:
        post.cleanup()
        raise Gone("That Reddit post looks empty or deleted. Send a screenshot instead.")
    return post


def fetch(url):
    return {"instagram": fetch_instagram, "x": fetch_x, "linkedin": fetch_linkedin,
            "reddit": fetch_reddit}[kind_of(url)](url)


def make_note(url, topics, caption=""):
    """Fetch the post and turn it into a note. Returns (note, clean_url, kind, warning)."""
    import brain
    post = fetch(url)
    try:
        note = brain.note_from_post(post, topics, caption)
        if not post.media:
            note.content = post.text  # nothing watched: the caption/post text is the stored copy
        return note, post.url, post.kind, post.note
    finally:
        post.cleanup()
