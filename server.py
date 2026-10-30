"""Online version: one web server that runs BOTH
  1. the Telegram bot (Telegram pushes your messages to /telegram), and
  2. the MCP server, so Claude / ChatGPT / any AI app can search your memory.

The MCP address has a secret in it: https://<your-app>.onrender.com/mcp/<MCP_SECRET>
Anyone without the secret gets "not found".
"""
import asyncio
import re
import contextlib
import logging
import os

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

import uvicorn  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse  # noqa: E402
from telegram import Update  # noqa: E402

import brain  # noqa: E402
import db  # noqa: E402
import memory  # noqa: E402
import social  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs that contain the bot token
log = logging.getLogger("brain-server")

MCP_SECRET = os.environ["MCP_SECRET"].strip()
WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).strip().rstrip("/")  # Render fills RENDER_EXTERNAL_URL itself
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

mcp = MCPServer(
    name="Arun's Second Brain",
    instructions=(
        "This is Arun's personal knowledge base: notes he saved from YouTube videos, reels, posts, screenshots "
        "and his own thoughts on sales, communication, career, content creation, productivity, AI tools and more. "
        "ALWAYS call search_my_brain FIRST for any question about advice, how to do something, what to say, work, "
        "people, business, content or learning, even if he doesn't mention his saves. Search again with different "
        "words if the first results don't fit. Then APPLY what he saved to his situation (don't just list it). Mention which "
        "save each point came from (title + date). If saves disagree with each other, show both sides with their dates "
        "(say which is newer) instead of blending them or picking one. If nothing relevant is found, say so before "
        "giving general advice."
    ),
)


MATCH_FLOOR = 0.58  # tested: unrelated questions score <= 0.55, real matches >= 0.63
MOVE_FLOOR = 0.63   # tested: off-topic questions' best move <= 0.62, real situations >= 0.67


def _cut(text, limit):
    """Shorten at a sentence end, never mid-word."""
    if len(text) <= limit:
        return text
    cut = max(text.rfind(". ", 0, limit), text.rfind("\n", 0, limit))
    return text[:cut + 1 if cut > limit // 2 else limit].rstrip() + " …"


STAMP = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?\s+")


def _clean(passage):
    """A passage as readable text: no video timestamps, and no half sentence at the start (chunks overlap)."""
    passage = STAMP.sub("", passage).strip()
    if passage[:1].islower():
        start = min((i for i in (passage.find(". "), passage.find("? "), passage.find("! ")) if 0 <= i < 200),
                    default=-1)
        passage = passage[start + 2:] if start >= 0 else "…" + passage
    return passage


def _fmt(row, excerpt=1200, brief=False):
    """One saved note, clean for the AI: what it is, the key advice, and (from search) the passages that matched."""
    i, created, stype, url, title, topic, summary, points, content = row[:9]
    where = {"youtube": "YouTube video", "instagram": "Instagram reel", "x": "X post", "linkedin": "LinkedIn post",
             "reddit": "Reddit post", "image": "screenshot", "note": "Arun's own note", "voice": "Arun's voice note",
             "pdf": "PDF"}.get(stype, stype)
    out = [f"[save #{i}] {title or '(untitled)'} ({where}, saved {created:%d %b %Y}, topic: {topic or '-'})"]
    if url:
        out.append(f"Link: {url}")
    if summary:
        out.append(f"Summary: {summary}")
    if brief:
        return "\n".join(out)
    if points:
        out.append("Key advice:\n" + "\n".join(f"- {p}" for p in points))
    if content and excerpt:
        if excerpt >= 60_000:
            out.append(f"Full original:\n{content[:excerpt]}")
        else:  # search passages: drop the one that only repeats the summary/points above
            passages = [p.split(": ", 1)[1] if p.startswith(f"{title}: ") else p
                        for p in content.split("\n…\n") if not p.startswith(f"{title}\nTopic:")]
            if passages:
                out.append("What was actually said (the parts that match the question):\n" +
                           "\n…\n".join(_cut(_clean(p), excerpt) for p in passages[:2]))
    return "\n".join(out)


@mcp.tool()
async def search_my_brain(query: str, limit: int = 6) -> str:
    """ALWAYS call this FIRST, before answering, whenever Arun asks for advice, how to do something, what to say,
    or about work, sales, negotiation, communication, career, content, coding, learning or any situation he faces.
    It searches everything he saved from experts (videos, reels, posts, notes), including details deep inside long
    videos, and returns the best matches with their key advice and the exact parts that were said. Then answer FROM
    these saves. Write the query as his situation in plain words. Use get_save for one save's full text."""
    limit = max(1, min(limit, 15))
    rows, moves = await asyncio.to_thread(memory.mentor_context, query, limit)
    profile = await asyncio.to_thread(db.get_setting, "profile", "")
    waiting = await asyncio.to_thread(db.queue_size)
    note = (f"\n\n(Note: {waiting} newly shared link(s) are still being processed and aren't searchable yet. "
            f"They usually appear within a few minutes.)") if waiting else ""
    if not rows:
        return "The second brain is empty: nothing saved yet." + note
    rows = [r for r in rows if r[9] >= MATCH_FLOOR]
    moves = [m for m in moves if m[6] >= MOVE_FLOOR]
    if not moves:  # every save is broken into moves, so no fitting move = nothing he saved covers this
        return (f"Nothing in Arun's second brain matches \"{query}\". If the words may just be different, search once "
                "more with other words. Otherwise tell him \"Your saves don't cover this.\", give your best advice "
                "marked as yours, and name 1-2 specific, well-known people, books or podcasts known for exactly this, "
                "for him to learn from and save next." + note)
    about = f"About Arun: {profile}\n\n" if profile else ""
    return (f"Arun's second brain: the mentor moves that best fit \"{query}\", out of everything he saved.\n\n"
            f"HOW TO ANSWER, as his mentor:\n{brain.MENTOR_RULES}\n"
            "If his question is too broad to pick a move (like \"help me with sales\"), first ask him one short "
            "question about his situation, with 2-3 options based on these moves.\n\n"
            f"{about}CANDIDATE MOVES (best fit first; most won't fit, choose):\n{brain.moves_block(moves)}\n\n"
            "THE SAVES THEY COME FROM (get_save gives one save's full text):\n\n" +
            "\n\n".join(_fmt(r, brief=True) for r in rows) + note)


@mcp.prompt(name="ask_my_second_brain", title="Ask my Second Brain",
            description="Get advice on a situation from everything you saved")
def ask_my_second_brain(situation: str) -> str:
    return (f"My situation: {situation}\n\n"
            "First call search_my_brain with my situation (and once more with different words if the results don't "
            "fit). Then be my mentor: pick the 1-3 moves that fit my exact case and tell me what to do (Do this / "
            "Backup / Avoid / Your next step), naming where each move came from. Your own view only after, marked "
            "as yours.")


@mcp.tool()
async def get_playbook(topic: str = "") -> str:
    """Arun's playbook for a topic (e.g. "sales"): one guide merged from all his saves on it, with his proven tips
    marked. Leave topic empty to list the playbooks he has."""
    if not topic.strip():
        rows = await asyncio.to_thread(db.list_playbooks)
        return ("Playbooks: " + ", ".join(f"{t} ({n} saves, updated {u:%d %b})" for t, n, u in rows)) if rows else \
            "No playbooks yet. In Telegram he can say \"make my <topic> playbook\"."
    row = await asyncio.to_thread(db.get_playbook, topic.strip().lower())
    return f"{row[1]}\n\n(Built from {row[2]} saves, updated {row[3]:%d %b %Y})" if row else \
        f"No {topic} playbook yet. Use search_my_brain instead."


@mcp.tool()
async def get_save(save_id: int) -> str:
    """Full details of one save, including the whole transcript / original text. Use the #number from search results."""
    row = await asyncio.to_thread(db.get, save_id)
    return _fmt(row, excerpt=60_000) if row else f"No save #{save_id}."


@mcp.tool()
async def recent_saves(limit: int = 10) -> str:
    """List what Arun saved most recently (newest first)."""
    rows = await asyncio.to_thread(db.recent, max(1, min(limit, 50)))
    if not rows:
        return "Nothing saved yet."
    return "\n".join(f"#{i} {title or '(untitled)'} | {topic or '-'} | {t} | {created:%d %b %Y}"
                     + ("" if st == "done" else f" | {st}: not processed")
                     for i, created, t, title, topic, st in rows)


@mcp.tool()
async def list_topics() -> str:
    """The topics Arun has saved things about, most-used first."""
    topics = await asyncio.to_thread(db.topics)
    return ", ".join(topics) if topics else "No topics yet."


def _already(found):
    """Message if this link is already saved or waiting in the Telegram bot's queue, else None."""
    if not found:
        return None
    if found[2] == "done":
        return f"Already saved as #{found[0]}: {found[1]}"
    if found[2] == "pending":
        return f"Already in the queue as #{found[0]}; it will be saved automatically."
    return None


@mcp.tool()
async def save_to_brain(text_or_link: str) -> str:
    """Save something new into Arun's second brain. Only use when he asks you to save/remember something.
    Give a YouTube, Instagram (reel/post), X (Twitter), LinkedIn or Reddit link (it will be watched/read and summarised),
    or plain text (saved as his note)."""
    topics = await asyncio.to_thread(db.topics)
    words = text_or_link.split()
    yt = next((w for w in words if brain.youtube_id(w)), None)
    post = next((w for w in words if social.kind_of(w)), None)
    if yt:
        msg = _already(await asyncio.to_thread(db.find_url, f"https://www.youtube.com/watch?v={brain.youtube_id(yt)}"))
        if msg:
            return msg
        note, link = await asyncio.to_thread(brain.note_from_youtube, yt, topics)
        stype = "youtube"
    elif post:
        msg = _already(await asyncio.to_thread(db.find_url, social.clean_url(post)))
        if msg:
            return msg
        note, link, stype, _ = await asyncio.to_thread(social.make_note, post, topics)
    else:
        note = await asyncio.to_thread(brain.note_from_text, text_or_link, topics)
        stype, link = "note", None
    vec = await asyncio.to_thread(brain.embed, brain.note_text_for_search(note))
    i = await asyncio.to_thread(db.add, stype, link, note.title, note.topic, note.summary, note.key_points,
                                note.content, vec)
    try:
        await asyncio.to_thread(memory.index_save, i)  # make every part of it searchable right away
    except Exception:
        log.exception("indexing #%s failed (the nightly job retries)", i)
    return f"Saved #{i}: {note.title} (topic: {note.topic})"


# ---------- Telegram (webhook) + health check ----------

tg_app = None


@mcp.custom_route("/telegram", methods=["POST"])
async def telegram_webhook(request: Request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET or tg_app is None:
        return PlainTextResponse("forbidden", status_code=403)
    await tg_app.update_queue.put(Update.de_json(await request.json(), tg_app.bot))
    return PlainTextResponse("ok")


@mcp.custom_route("/selftest/{key}", methods=["GET"])
async def selftest(request: Request):
    """Checks the parts that can only be tested on the server itself (e.g. the MP3 encoder for voice replies).
    Protected by the same secret as the connector address."""
    if request.path_params.get("key") != MCP_SECRET:
        return PlainTextResponse("not found", status_code=404)
    out = {}
    try:
        audio, fmt = await asyncio.to_thread(brain.speak, "Self test. Voice replies are working.")
        out["voice_format"], out["voice_bytes"] = fmt, len(audio)
        out["voice_ok"] = fmt == "ogg" and audio[:4] == b"OggS"
    except Exception as e:
        out["voice_ok"], out["voice_error"] = False, str(e)[:300]
    return JSONResponse(out)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request):
    return JSONResponse({"ok": True})


def create_app():
    app = mcp.streamable_http_app(
        streamable_http_path=f"/mcp/{MCP_SECRET}",
        stateless_http=True,
        json_response=True,
        # Render puts its own address in front of us; the secret path is what protects the server.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        global tg_app
        async with inner(a):
            await asyncio.to_thread(db.setup)
            # index any save that has no search chunks yet (e.g. right after this update), in the background
            asyncio.get_running_loop().run_in_executor(None, memory.index_missing)
            if WEBHOOK_URL:  # online: start the Telegram bot too
                import bot
                tg_app = bot.build_app()
                await tg_app.initialize()
                await tg_app.post_init(tg_app)
                await tg_app.start()
                await tg_app.bot.set_webhook(f"{WEBHOOK_URL}/telegram", secret_token=WEBHOOK_SECRET,
                                             allowed_updates=Update.ALL_TYPES)
                log.info("Telegram bot running (webhook).")
            try:
                yield
            finally:
                if tg_app:
                    await tg_app.stop()
                    await tg_app.shutdown()

    app.router.lifespan_context = lifespan
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
