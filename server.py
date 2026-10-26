"""Online version: one web server that runs BOTH
  1. the Telegram bot (Telegram pushes your messages to /telegram), and
  2. the MCP server, so Claude / ChatGPT / any AI app can search your memory.

The MCP address has a secret in it: https://<your-app>.onrender.com/mcp/<MCP_SECRET>
Anyone without the secret gets "not found".
"""
import asyncio
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


def _fmt(row, excerpt=1500):
    """One saved note as readable text for the AI."""
    i, created, stype, url, title, topic, summary, points, content = row[:9]
    out = [f"#{i} {title or '(untitled)'}", f"Saved: {created:%d %b %Y} | type: {stype} | topic: {topic or '-'}"]
    if len(row) > 9 and isinstance(row[9], float):
        out[1] += f" | relevance: {row[9]:.2f}"
    if url:
        out.append(f"Link: {url}")
    if summary:
        out.append(f"Summary: {summary}")
    if points:
        out.append("Key points:\n" + "\n".join(f"- {p}" for p in points))
    if content and excerpt:
        label = "Most relevant passages" if excerpt < 60_000 else "Full original"
        out.append(f"{label}: {content[:excerpt]}" + ("…" if len(content) > excerpt else ""))
    return "\n".join(out)


@mcp.tool()
async def search_my_brain(query: str, limit: int = 6) -> str:
    """Search everything Arun saved, by meaning AND exact words, including details deep inside long videos.
    Call this first for any advice / how-to / what-to-say / work / business / content question. Returns the best
    saves with summary, key points and the most relevant passages, plus who Arun is. Use get_save for full text."""
    limit = max(1, min(limit, 15))
    rows = await asyncio.to_thread(memory.search, query, limit)
    profile = await asyncio.to_thread(db.get_setting, "profile", "")
    waiting = await asyncio.to_thread(db.queue_size)
    note = (f"\n\n(Note: {waiting} newly shared link(s) are still being processed and aren't searchable yet. "
            f"They usually appear within a few minutes.)") if waiting else ""
    if not rows:
        return "The second brain is empty: nothing saved yet." + note
    about = f"About Arun (tailor the advice to this): {profile}\n\n" if profile else ""
    return about + "\n\n---\n\n".join(_fmt(r) for r in rows) + note


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
