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
        "and his own thoughts. When he says 'second brain', 'my saves', 'my notes', or asks something he may have "
        "saved advice about, call search_my_brain first and base your answer on what it returns. Mention which "
        "save each point came from (title + date). If nothing relevant is found, say so before giving general advice."
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
        out.append(f"Original (excerpt): {content[:excerpt]}" + ("…" if len(content) > excerpt else ""))
    return "\n".join(out)


@mcp.tool()
async def search_my_brain(query: str, limit: int = 6) -> str:
    """Search Arun's saved notes by meaning (not just keywords).
    Use for any question he might have saved advice about. Returns the closest saves with summary,
    key points and an excerpt of the original. Use get_save for the full text of one save."""
    limit = max(1, min(limit, 15))
    vec = await asyncio.to_thread(brain.embed, query, True)
    rows = await asyncio.to_thread(db.search, vec, limit)
    if not rows:
        return "The second brain is empty: nothing saved yet."
    return "\n\n---\n\n".join(_fmt(r) for r in rows)


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
                     + (" | not processed yet" if st == "pending" else "")
                     for i, created, t, title, topic, st in rows)


@mcp.tool()
async def list_topics() -> str:
    """The topics Arun has saved things about, most-used first."""
    topics = await asyncio.to_thread(db.topics)
    return ", ".join(topics) if topics else "No topics yet."


@mcp.tool()
async def save_to_brain(text_or_link: str) -> str:
    """Save something new into Arun's second brain. Only use when he asks you to save/remember something.
    Give a YouTube, Instagram (reel/post), X (Twitter) or LinkedIn link (it will be watched/read and summarised),
    or plain text (saved as his note)."""
    topics = await asyncio.to_thread(db.topics)
    words = text_or_link.split()
    yt = next((w for w in words if brain.youtube_id(w)), None)
    post = next((w for w in words if social.kind_of(w)), None)
    if yt:
        if await asyncio.to_thread(db.find_url, f"https://www.youtube.com/watch?v={brain.youtube_id(yt)}"):
            return "That video is already saved."
        note, link = await asyncio.to_thread(brain.note_from_youtube, yt, topics)
        stype = "youtube"
    elif post:
        if await asyncio.to_thread(db.find_url, social.clean_url(post)):
            return "That post is already saved."
        note, link, stype, _ = await asyncio.to_thread(social.make_note, post, topics)
    else:
        note = await asyncio.to_thread(brain.note_from_text, text_or_link, topics)
        stype, link = "note", None
    vec = await asyncio.to_thread(brain.embed, brain.note_text_for_search(note))
    i = await asyncio.to_thread(db.add, stype, link, note.title, note.topic, note.summary, note.key_points,
                                note.content, vec)
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
