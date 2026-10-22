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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,  # noqa: E402
                          MessageHandler, filters)

import brain  # noqa: E402
import db  # noqa: E402
import social  # noqa: E402

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

SAVE: share a YouTube video, Instagram reel, or an X, LinkedIn or Reddit post, or send a screenshot. Add a comment with it if you like.
NOTE: /note <text> saves your own thought.
ASK: just type a question, e.g. "how do I handle price objections?"

/recent  last 10 saves
/export  download everything as a file
/fix <number> <correction>  correct a note that got something wrong
/delete <number>  remove a save
/retry  try again on links that failed
/recap  recap of the last 7 days (it also comes by itself every Sunday 9 AM)

Every other morning at 9 AM you get one old save to remember."""


def _short(text, limit=MAX_MSG):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _friendly_error(e):
    s = str(e)
    if "429" in s or "RESOURCE_EXHAUSTED" in s:
        return "Gemini's free limit is used up for now. Try again in a minute (or tomorrow if it keeps happening)."
    if "503" in s or "UNAVAILABLE" in s or "overloaded" in s.lower():
        return "Gemini is busy right now. Try again in a minute."
    if isinstance(e, social.Blocked):
        return f"⚠️ {s}"
    return f"Something went wrong: {s[:200]}"


def _saved_reply(save_id, note, prefix="✅ Saved"):
    """Short confirmation + a Details button (the full note only when Arun taps it)."""
    text = f"{prefix} #{save_id} · {note.topic}\n{note.title}"
    return text, _details_button(save_id)


def _details_button(save_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("📖 Details", callback_data=f"d:{save_id}")]])


def _details_text(row):
    i, created, stype, url, title, topic, summary, points, _ = row
    lines = [f"#{i} {title} · {topic or '-'} · {created:%d %b}", "", summary or ""]
    lines += [f"• {p}" for p in points or []]
    if url:
        lines += ["", url]
    return _short("\n".join(lines))


def _make_note(url, kind, caption):
    """Any supported link -> (note, clean_url, kind, warning). Runs in a worker thread."""
    topics = db.topics()
    if kind == "youtube":
        note, clean = brain.note_from_youtube(url, topics, caption)
        return note, clean, "youtube", ""
    return social.make_note(url, topics, caption)


async def _store(note, source_type, url, pending_id=None):
    vec = await asyncio.to_thread(brain.embed, brain.note_text_for_search(note))
    if pending_id:  # this link failed before: fill in the same save number
        await asyncio.to_thread(db.update_note, pending_id, note.title, note.topic, note.summary,
                                note.key_points, note.content, vec)
        return pending_id
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
        await save_youtube(update, context, yt[0], URL_RE.sub("", text).strip())
    elif urls:
        await save_link(update, context, urls[0], URL_RE.sub("", text).strip())
    else:
        await ask(update, text)


async def _save_url(update, context, url, clean, kind, caption):
    """Links go into a queue and are saved in the background, one at a time (so Instagram doesn't block us).
    The '📥 In queue' message is later edited into '✅ Saved'."""
    found = await asyncio.to_thread(db.find_url, clean)
    if found and found[2] == "done":
        await update.message.reply_text(f"Already saved as #{found[0]}: {found[1]}")
        return
    if found:  # it failed or is still waiting: move it to the front
        save_id = found[0]
        await asyncio.to_thread(db.retry_one, save_id)
    else:
        save_id = await asyncio.to_thread(db.add, kind, clean, clean, None, None, None, caption, None, "pending")
    waiting = await asyncio.to_thread(db.queue_size)
    msg = await update.message.reply_text(
        f"📥 Got it #{save_id}" + (f" ({waiting} in queue)" if waiting > 1 else "") + ". Saving in the background…")
    _queue_msgs[save_id] = (msg.chat_id, msg.message_id)
    _kick_queue(context)


# save_id -> (chat_id, message_id) of its "📥 Got it" message, so it can be edited when saved
_queue_msgs = {}
_queue_lock = asyncio.Lock()


def _kick_queue(context):
    context.job_queue.run_once(process_queue, when=1)


async def _tell(bot, save_id, text, markup=None):
    """Edit the save's 'Got it' message if we still know it, otherwise send a new message."""
    where = _queue_msgs.pop(save_id, None)
    if where:
        try:
            await bot.edit_message_text(text, chat_id=where[0], message_id=where[1], reply_markup=markup,
                                        disable_web_page_preview=True)
            return
        except Exception:
            pass
    await bot.send_message(OWNER, text, reply_markup=markup, disable_web_page_preview=True)


async def process_queue(context: ContextTypes.DEFAULT_TYPE):
    """Save queued links one by one. Instagram gets a pause between reels; blocked links wait 2 hours."""
    if _queue_lock.locked():
        return
    async with _queue_lock:
        while row := await asyncio.to_thread(db.next_queued):
            save_id, stype, url, caption = row
            kind = "youtube" if stype == "youtube" else social.kind_of(url or "")
            if not kind:  # a site we can't read: keep the link, stop trying
                await asyncio.to_thread(db.set_status, save_id, "kept")
                continue
            try:
                note, clean, kind, warning = await asyncio.to_thread(_make_note, url, kind, caption or "")
                await _store(note, kind, clean, save_id)
                text, markup = _saved_reply(save_id, note)
                if warning:
                    text += "\n(only the caption/text could be read)"
                await _tell(context.bot, save_id, text, markup)
            except Exception as e:
                log.exception("queue: #%s failed", save_id)
                s = str(e)
                wait = 120 if isinstance(e, social.Blocked) else 60 if ("429" in s or "RESOURCE_EXHAUSTED" in s) else 30
                status = await asyncio.to_thread(db.postpone, save_id, wait)
                if status == "failed":
                    await _tell(context.bot, save_id, f"❌ Couldn't save #{save_id} after several tries.\n{url}\n"
                                                      f"Send a screenshot of it instead.")
                elif save_id in _queue_msgs:  # first failure: tell once, quietly
                    await _tell(context.bot, save_id, f"⏳ #{save_id} is blocked for now. I'll retry by myself and "
                                                      f"message you when it's saved.")
            if kind == "instagram":
                await asyncio.sleep(40)  # space out reels so Instagram doesn't block the server


async def save_youtube(update, context, url, caption):
    clean = f"https://www.youtube.com/watch?v={brain.youtube_id(url)}"
    await _save_url(update, context, url, clean, "youtube", caption)


async def save_link(update, context, url, caption):
    kind = social.kind_of(url)
    if not kind:
        # Other sites (articles, etc.) can't be read yet. Keep the link anyway.
        save_id = await asyncio.to_thread(db.add, "link", url, url, None, None, None, caption, None, "kept")
        await update.message.reply_text(
            f"📌 Kept the link as #{save_id}, but I can't read this site yet.\n"
            f"Send a screenshot and I'll save what it says.")
        return
    await _save_url(update, context, url, social.clean_url(url), kind, caption)


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
        text, markup = _saved_reply(save_id, note)
        await status.edit_text(text, reply_markup=markup)
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
        text, markup = _saved_reply(save_id, note)
        await status.edit_text(text, reply_markup=markup)
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
    icons = {"youtube": "▶️", "image": "🖼", "note": "📝", "link": "🔗", "instagram": "📸", "x": "𝕏", "linkedin": "💼", "reddit": "👽"}
    lines = [f"#{i} {icons.get(t, '•')} {title or '(untitled)'}"
             + (f" · {topic}" if topic else "") + {"pending": " (in queue)", "failed": " (failed)", "kept": " (link only)"}.get(st, "")
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


async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        await update.message.reply_text(
            "Write the number and the correction, like:\n/fix 12 He said 50 conversations, not 100")
        return
    save_id, text = int(args[0].lstrip("#")), " ".join(args[1:])
    row = await asyncio.to_thread(db.add_correction, save_id, text)
    if not row:
        await update.message.reply_text(f"No save #{save_id} found.")
        return
    title, topic, summary, points = row
    vec = await asyncio.to_thread(brain.embed, f"{title}\nTopic: {topic}\n{summary}\n" + "\n".join(points))
    await asyncio.to_thread(db.set_embedding, save_id, vec)
    await update.message.reply_text(f"✏️ Fixed #{save_id}: {title}\nYour correction is now part of the note.")


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = await asyncio.to_thread(db.retry_all)
    if not n:
        await update.message.reply_text("Nothing to retry. 👍")
        return
    await update.message.reply_text(f"🔁 Retrying {n} link(s) in the background. I'll message you as each one saves.")
    _kick_queue(context)


async def queue_tick(context: ContextTypes.DEFAULT_TYPE):
    # Safety net every 10 min: picks up links whose retry time has come (e.g. 2 h after an Instagram block).
    await process_queue(context)


async def on_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The '📖 Details' button: show the full note under the short message."""
    q = update.callback_query
    await q.answer()
    row = await asyncio.to_thread(db.get, int(q.data.split(":")[1]))
    if row:
        await q.message.reply_text(_details_text(row), disable_web_page_preview=True)
    else:
        await q.message.reply_text("That save was deleted.")


async def daily_tip(context: ContextTypes.DEFAULT_TYPE):
    """One old save a day, so it comes back before it's forgotten (each save rotates; least recent first)."""
    try:
        row = await asyncio.to_thread(db.pick_tip)
        if not row:
            return
        i, created, stype, url, title, topic, summary, points = row
        best = "\n".join(f"• {p}" for p in (points or [])[:3])
        text = f"💡 Remember this? (saved {created:%d %b})\n\n{title}\n\n{best}"
        await context.bot.send_message(OWNER, _short(text), reply_markup=_details_button(i),
                                       disable_web_page_preview=True)
    except Exception:
        log.exception("daily tip failed")


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
        ("fix", "Correct a note, e.g. /fix 12 …"),
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
    app.add_handler(CommandHandler("fix", cmd_fix, filters=me))
    app.add_handler(MessageHandler(me & (filters.PHOTO | filters.Document.IMAGE), on_image))
    app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_details, pattern=r"^d:\d+$"))
    app.add_handler(MessageHandler(~me, ignore_strangers))

    app.job_queue.run_daily(daily_ping, time=dt.time(hour=3, minute=0))
    app.job_queue.run_once(daily_ping, when=5)
    app.job_queue.run_repeating(queue_tick, interval=10 * 60, first=30)
    # Daily reminder Mon-Sat 9:00 AM India time; Sunday 9:00 AM is the weekly recap instead (0 = Sunday)
    app.job_queue.run_daily(daily_tip, time=dt.time(hour=9, minute=0, tzinfo=IST), days=(1, 2, 3, 4, 5, 6))
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
