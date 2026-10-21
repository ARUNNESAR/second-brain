"""Arun's Second Brain: a private Telegram bot.

Share a YouTube link or screenshot -> it makes a note and saves it.
Type a question -> it answers from your saved notes, with sources.
"""
import asyncio
import datetime as dt
import io
import logging
import os
import re
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from telegram import Update  # noqa: E402
from telegram.ext import (Application, CommandHandler, ContextTypes, MessageHandler,  # noqa: E402
                          filters)

import brain  # noqa: E402
import db  # noqa: E402

os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[logging.StreamHandler(),
              RotatingFileHandler(os.path.join(HERE, "logs", "bot.log"), maxBytes=2_000_000, backupCount=3,
                                  encoding="utf-8")],
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs URLs that contain the bot token
log = logging.getLogger("brain-bot")

OWNER = int(os.environ["TELEGRAM_USER_ID"].strip())
URL_RE = re.compile(r"https?://\S+")
MAX_MSG = 4000

HELP = """Your Second Brain 🧠

SAVE: share a YouTube video (Shorts or long) or send a screenshot. Add a comment with it if you like.
NOTE: /note <text> saves your own thought.
ASK: just type a question, e.g. "how do I handle price objections?"

/recent  last 10 saves
/export  download everything as a file
/delete <number>  remove a save
/retry  try again on saves that failed to read
/recap  recap of the last 7 days (it also comes by itself every Sunday 9 AM)"""


def _short(text, limit=MAX_MSG):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _friendly_error(e):
    s = str(e)
    if "429" in s or "RESOURCE_EXHAUSTED" in s:
        return "Gemini's free limit is used up for now. Try again in a minute (or tomorrow if it keeps happening)."
    if "503" in s or "UNAVAILABLE" in s or "overloaded" in s.lower():
        return "Gemini is busy right now. Try again in a minute."
    return f"Something went wrong: {s[:200]}"


def _saved_reply(save_id, note):
    points = "\n".join(f"• {p}" for p in note.key_points[:6])
    more = f"\n…and {len(note.key_points) - 6} more" if len(note.key_points) > 6 else ""
    return _short(f"✅ Saved #{save_id} · {note.topic}\n{note.title}\n\n{note.summary}\n\n{points}{more}")


async def _store(note, source_type, url):
    vec = await asyncio.to_thread(brain.embed, brain.note_text_for_search(note))
    return await asyncio.to_thread(db.add, source_type, url, note.title, note.topic, note.summary,
                                   note.key_points, note.content, vec)


# ---------- handlers ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    urls = URL_RE.findall(text)
    yt = [u for u in urls if brain.youtube_id(u)]
    if yt:
        await save_youtube(update, yt[0], URL_RE.sub("", text).strip())
    elif urls:
        await save_link(update, urls[0], URL_RE.sub("", text).strip())
    else:
        await ask(update, text)


async def save_youtube(update, url, caption):
    vid = brain.youtube_id(url)
    clean = f"https://www.youtube.com/watch?v={vid}"
    dup = await asyncio.to_thread(db.find_url, clean)
    if dup:
        await update.message.reply_text(f"Already saved as #{dup[0]}: {dup[1]}")
        return
    status = await update.message.reply_text("⏳ Watching the video… (long videos take up to a minute)")
    try:
        topics = await asyncio.to_thread(db.topics)
        note, clean = await asyncio.to_thread(brain.note_from_youtube, url, topics, caption)
        save_id = await _store(note, "youtube", clean)
        await status.edit_text(_saved_reply(save_id, note))
    except Exception as e:
        log.exception("youtube save failed")
        # Keep the link so nothing is lost; /retry can process it later.
        save_id = await asyncio.to_thread(db.add, "youtube", clean, brain.youtube_title(clean) or clean, None,
                                          None, None, caption, None, "pending")
        await status.edit_text(f"{_friendly_error(e)}\n\nI kept the link as #{save_id}. Send /retry later.")


async def save_link(update, url, caption):
    # Step 1 can't read Instagram / X / Reddit yet. Keep the link so it can be processed later.
    save_id = await asyncio.to_thread(db.add, "link", url, url, None, None, None, caption, None, "pending")
    await update.message.reply_text(
        f"📌 Kept the link as #{save_id}.\nRight now I can only read YouTube videos and screenshots. "
        f"Tip: send a screenshot of the post and I'll save what it says.")


async def on_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo:
        file, mime = await msg.photo[-1].get_file(), "image/jpeg"
    else:
        file, mime = await msg.document.get_file(), msg.document.mime_type
    data = bytes(await file.download_as_bytearray())
    status = await msg.reply_text("⏳ Reading the screenshot…")
    try:
        topics = await asyncio.to_thread(db.topics)
        note = await asyncio.to_thread(brain.note_from_image, data, mime, topics, (msg.caption or "").strip())
        save_id = await _store(note, "image", None)
        await status.edit_text(_saved_reply(save_id, note))
    except Exception as e:
        log.exception("image save failed")
        await status.edit_text(_friendly_error(e) + "\nPlease send the screenshot again.")


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Write it after the command, like:\n/note Always ask the client's budget first")
        return
    status = await update.message.reply_text("⏳ Saving your note…")
    try:
        topics = await asyncio.to_thread(db.topics)
        note = await asyncio.to_thread(brain.note_from_text, text, topics)
        save_id = await _store(note, "note", None)
        await status.edit_text(f"✅ Saved #{save_id} · {note.topic}\n{note.title}")
    except Exception as e:
        log.exception("note save failed")
        await status.edit_text(_friendly_error(e))


async def ask(update, question):
    status = await update.message.reply_text("🔎 Looking through your saves…")
    try:
        vec = await asyncio.to_thread(brain.embed, question, True)
        rows = await asyncio.to_thread(db.search, vec, 6)
        if not rows:
            await status.edit_text("Nothing saved yet. Share a YouTube video or a screenshot first, then ask me.")
            return
        text = await asyncio.to_thread(brain.answer, question, rows)
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= len(rows)})
        sources = []
        for n in cited:
            r = rows[n - 1]
            line = f"[{n}] #{r[0]} {r[4]} · {r[1]:%d %b}"
            if r[3]:
                line += f"\n{r[3]}"
            sources.append(line)
        reply = text + ("\n\nSources:\n" + "\n".join(sources) if sources else "")
        await status.edit_text(_short(reply), disable_web_page_preview=True)
    except Exception as e:
        log.exception("ask failed")
        await status.edit_text(_friendly_error(e))


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.recent, 10)
    if not rows:
        await update.message.reply_text("Nothing saved yet.")
        return
    icons = {"youtube": "▶️", "image": "🖼", "note": "📝", "link": "🔗"}
    lines = [f"#{i} {icons.get(t, '•')} {title or '(untitled)'}"
             + (f" · {topic}" if topic else "") + (" (not read yet)" if st == "pending" else "")
             + f" · {created:%d %b}"
             for i, created, t, title, topic, st in rows]
    await update.message.reply_text(_short("Your last saves:\n\n" + "\n".join(lines)), disable_web_page_preview=True)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.all_saves)
    out = [f"# My Second Brain: export {dt.date.today():%d %b %Y}\n\n{len(rows)} saves\n"]
    for i, created, stype, url, title, topic, summary, points, content, st in rows:
        out.append(f"\n---\n\n## #{i} {title or '(untitled)'}\n")
        out.append(f"- Saved: {created:%d %b %Y} · Type: {stype} · Topic: {topic or '-'} · Status: {st}")
        if url:
            out.append(f"- Link: {url}")
        if summary:
            out.append(f"\n{summary}\n")
        for p in points or []:
            out.append(f"- {p}")
        if content:
            out.append(f"\n<details><summary>Original</summary>\n\n{content}\n\n</details>")
    buf = io.BytesIO("\n".join(out).encode("utf-8"))
    buf.name = f"second-brain-{dt.date.today():%Y-%m-%d}.md"
    await update.message.reply_document(buf, caption=f"All {len(rows)} saves.")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0] if context.args else "").lstrip("#")
    if not arg.isdigit():
        await update.message.reply_text("Tell me which one, like: /delete 12  (numbers are in /recent)")
        return
    gone = await asyncio.to_thread(db.delete, int(arg))
    await update.message.reply_text(f"🗑 Deleted #{arg}: {gone[0]}" if gone else f"No save #{arg} found.")


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = [r for r in await asyncio.to_thread(db.pending) if r[1] == "youtube"]
    if not rows:
        await update.message.reply_text("Nothing to retry. 👍")
        return
    status = await update.message.reply_text(f"⏳ Retrying {len(rows)} video(s)…")
    done, failed = 0, 0
    topics = await asyncio.to_thread(db.topics)
    for save_id, _, url, caption in rows:
        try:
            note, _ = await asyncio.to_thread(brain.note_from_youtube, url, topics, caption or "")
            vec = await asyncio.to_thread(brain.embed, brain.note_text_for_search(note))
            await asyncio.to_thread(db.update_note, save_id, note.title, note.topic, note.summary,
                                    note.key_points, note.content, vec)
            done += 1
        except Exception:
            log.exception("retry failed for #%s", save_id)
            failed += 1
    await status.edit_text(f"Retry finished: {done} saved" + (f", {failed} still failing" if failed else "") + ".")


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


async def build_recap():
    rows = await asyncio.to_thread(db.since, 7)
    throwback = await asyncio.to_thread(db.random_older, 7)
    if not rows:
        msg = "🧠 Sunday recap: you didn't save anything this week. Share one useful video today 🙂"
        if throwback:
            msg += f"\n\n🔁 From your past: #{throwback[0]} {throwback[4]}\n{throwback[6]}"
        return msg
    return await asyncio.to_thread(brain.weekly_recap, rows, throwback)


async def weekly_recap(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await build_recap()
        await context.bot.send_message(OWNER, _short(text), disable_web_page_preview=True)
        log.info("weekly recap sent")
    except Exception:
        log.exception("weekly recap failed")


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("⏳ Making your recap of the last 7 days…")
    try:
        await status.edit_text(_short(await build_recap()), disable_web_page_preview=True)
    except Exception as e:
        log.exception("recap failed")
        await status.edit_text(_friendly_error(e))


async def ignore_strangers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        log.warning("Ignored message from non-owner %s", update.effective_user.id)


async def daily_ping(context: ContextTypes.DEFAULT_TYPE):
    # Supabase pauses free projects after 7 quiet days; one tiny query a day keeps it awake.
    try:
        await asyncio.to_thread(db.ping)
        log.info("daily database ping ok")
    except Exception:
        log.exception("daily database ping failed")


async def set_menu(app):
    # Fills the "/" menu in Telegram so commands don't have to be remembered.
    await app.bot.set_my_commands([
        ("recent", "Last 10 saves"),
        ("note", "Save your own note"),
        ("export", "Download everything"),
        ("delete", "Remove a save, e.g. /delete 12"),
        ("recap", "Recap of the last 7 days"),
        ("retry", "Retry saves that failed"),
        ("help", "How to use me"),
    ])


def build_app():
    """The bot with all its handlers. Used by main() on the PC and by server.py online."""
    app = (Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"].strip())
           .concurrent_updates(True).post_init(set_menu).build())

    me = filters.User(user_id=OWNER)
    app.add_handler(CommandHandler(["start", "help"], cmd_start, filters=me))
    app.add_handler(CommandHandler("note", cmd_note, filters=me))
    app.add_handler(CommandHandler("recent", cmd_recent, filters=me))
    app.add_handler(CommandHandler("export", cmd_export, filters=me))
    app.add_handler(CommandHandler("delete", cmd_delete, filters=me))
    app.add_handler(CommandHandler("retry", cmd_retry, filters=me))
    app.add_handler(CommandHandler("recap", cmd_recap, filters=me))
    app.add_handler(MessageHandler(me & (filters.PHOTO | filters.Document.IMAGE), on_image))
    app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~me, ignore_strangers))

    app.job_queue.run_daily(daily_ping, time=dt.time(hour=3, minute=0))
    app.job_queue.run_once(daily_ping, when=5)
    # Every Sunday 9:00 AM India time (0 = Sunday)
    app.job_queue.run_daily(weekly_recap, time=dt.time(hour=9, minute=0, tzinfo=IST), days=(0,))
    return app


def main():
    # On the PC: we keep asking Telegram for new messages ("polling").
    # Online, server.py runs the same bot but Telegram pushes messages to it instead.
    db.setup()
    app = build_app()
    log.info("Bot started on this PC (polling).")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
