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

SAVE: share a YouTube video, Instagram reel, or an X, LinkedIn or Reddit post. Or send screenshots, a voice note, a video file or a PDF. Forwarded messages get saved too.
NOTE: /note <text> saves your own thought.
ASK: just type a question, e.g. "how do I handle price objections?"

/find <words>  find a save (to read or delete it)
/topics  browse saves by topic
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
    return InlineKeyboardMarkup([[InlineKeyboardButton("📖 Details", callback_data=f"d:{save_id}"),
                                  InlineKeyboardButton("🗑 Delete", callback_data=f"x:{save_id}")]])


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
    msg = update.message
    text = msg.text.strip()
    urls = list(dict.fromkeys(URL_RE.findall(text)))
    if urls:  # every link in the message gets saved, not just the first
        caption = URL_RE.sub("", text).strip()
        for u in urls:
            if brain.youtube_id(u):
                await save_youtube(update, context, u, caption)
            else:
                await save_link(update, context, u, caption)
        return
    if msg.forward_origin:  # forwarded from another chat: that's something to remember, not a question
        await save_text(msg, text)
    elif len(text) > 280 or text.count("\n") >= 3:  # long text: could be a tip to save or a long question
        _texts[msg.message_id] = text
        await msg.reply_text("Save this, or answer it as a question?", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("💾 Save it", callback_data=f"sv:{msg.message_id}"),
            InlineKeyboardButton("❓ Ask it", callback_data=f"ak:{msg.message_id}")]]))
    else:
        await ask(update, text)


_texts = {}  # message_id -> long text waiting for the Save/Ask choice


async def save_text(reply_to, text):
    status = await reply_to.reply_text("⏳ Saving…")
    try:
        topics = await asyncio.to_thread(db.topics)
        note = await asyncio.to_thread(brain.note_from_text, text, topics)
        save_id = await _store(note, "note", None)
        t, markup = _saved_reply(save_id, note)
        await status.edit_text(t, reply_markup=markup)
    except Exception as e:
        log.exception("text save failed")
        await status.edit_text(_friendly_error(e))


async def on_save_or_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    action, mid = q.data.split(":")
    text = _texts.pop(int(mid), None)
    if not text:
        await q.edit_message_text("I lost that text (the bot restarted). Please send it again.")
        return
    await q.edit_message_reply_markup(None)
    if action == "sv":
        await save_text(q.message, text)
    else:
        status = await q.message.reply_text("🔎 Looking through your saves…")
        await status.edit_text(await _answer(text), disable_web_page_preview=True)


# ---------- files: voice notes, videos, PDFs, screenshot groups ----------

MAX_TG_FILE = 20 * 1024 * 1024  # Telegram lets bots download files up to 20 MB


async def _save_files(reply_to, tg_files, kind, caption, label):
    """Download Telegram files, let Gemini read them together, save one note."""
    if any((f.file_size or 0) > MAX_TG_FILE for f, _ in tg_files):
        await reply_to.reply_text("That file is over 20 MB, which Telegram doesn't let bots download. "
                                  "Share the link instead (YouTube/Instagram/X…).")
        return
    status = await reply_to.reply_text(f"⏳ Reading the {label}…")
    post = social.Post(url=None, kind=kind, text=caption, workdir=social._workdir())
    try:
        for n, (f, mime) in enumerate(tg_files):
            path = os.path.join(post.workdir, f"f{n}")
            await (await f.get_file()).download_to_drive(path)
            post.media.append((path, mime))
        topics = await asyncio.to_thread(db.topics)
        note = await asyncio.to_thread(brain.note_from_post, post, topics, "")
        save_id = await _store(note, kind, None)
        t, markup = _saved_reply(save_id, note)
        await status.edit_text(t, reply_markup=markup)
    except Exception as e:
        log.exception("%s save failed", kind)
        await status.edit_text(_friendly_error(e) + f"\nPlease send the {label} again.")
    finally:
        post.cleanup()


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    f = msg.voice or msg.audio
    await _save_files(msg, [(f, f.mime_type or "audio/ogg")], "voice", (msg.caption or "").strip(), "voice note")


async def on_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    f = msg.video or msg.video_note or msg.document
    await _save_files(msg, [(f, getattr(f, "mime_type", None) or "video/mp4")], "video",
                      (msg.caption or "").strip(), "video")


async def on_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    await _save_files(msg, [(msg.document, "application/pdf")], "pdf", (msg.caption or "").strip(), "PDF")


_albums = {}  # media_group_id -> list of messages (screenshots sent together)


async def flush_album(context: ContextTypes.DEFAULT_TYPE):
    msgs = _albums.pop(context.job.data, [])
    if not msgs:
        return
    files = [(m.photo[-1], "image/jpeg") if m.photo else (m.document, m.document.mime_type) for m in msgs]
    caption = " ".join((m.caption or "").strip() for m in msgs).strip()
    await _save_files(msgs[0], files, "image", caption, f"{len(files)} screenshots")


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
    if msg.media_group_id:  # several screenshots sent together: wait a moment, then save them as ONE note
        group = _albums.setdefault(msg.media_group_id, [])
        group.append(msg)
        if len(group) == 1:
            context.job_queue.run_once(flush_album, when=3, data=msg.media_group_id)
        return
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


async def _answer(question):
    """Search the saves and write an answer with sources (plain text, ready to send)."""
    try:
        vec = await asyncio.to_thread(brain.embed, question, True)
        rows = await asyncio.to_thread(db.search, vec, 6)
        if not rows:
            return "Nothing saved yet. Share a YouTube video or a screenshot first, then ask me."
        text = await asyncio.to_thread(brain.answer, question, rows)
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= len(rows)})
        sources = []
        for n in cited:
            r = rows[n - 1]
            line = f"[{n}] #{r[0]} {r[4]} · {r[1]:%d %b}"
            if r[3]:
                line += f"\n{r[3]}"
            sources.append(line)
        return _short(text + ("\n\nSources:\n" + "\n".join(sources) if sources else ""))
    except Exception as e:
        log.exception("ask failed")
        return _friendly_error(e)


async def ask(update, question):
    status = await update.message.reply_text("🔎 Looking through your saves…")
    await status.edit_text(await _answer(question), disable_web_page_preview=True)


def _list_markup(rows):
    """One row of buttons per save: 📖 #id title  |  🗑"""
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"📖 #{i} {(title or '(untitled)')[:35]}", callback_data=f"d:{i}"),
                                  InlineKeyboardButton("🗑", callback_data=f"x:{i}")]
                                 for i, title, *_ in rows])


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    words = " ".join(context.args).strip()
    if not words:
        await update.message.reply_text("Type a few words you remember, like:\n/find cold calling")
        return
    vec = await asyncio.to_thread(brain.embed, words, True)
    rows = await asyncio.to_thread(db.find, words, vec, 8)
    if not rows:
        await update.message.reply_text(f"Nothing found for \"{words}\". Try other words, or /topics.")
        return
    await update.message.reply_text(f"🔎 Saves matching \"{words}\":", reply_markup=_list_markup(rows))


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.topic_counts)
    if not rows:
        await update.message.reply_text("Nothing saved yet.")
        return
    buttons = [InlineKeyboardButton(f"{t} ({n})", callback_data=f"t:{t[:50]}") for t, n in rows]
    await update.message.reply_text("📚 Your topics. Tap one to see its saves:",
                                    reply_markup=InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)]))


async def on_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    topic = q.data[2:]
    rows = await asyncio.to_thread(db.by_topic, topic, 15)
    await q.message.reply_text(f"📚 {topic}: latest {len(rows)}", reply_markup=_list_markup(rows))


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
    if q.from_user.id != OWNER:
        return
    await q.answer()
    row = await asyncio.to_thread(db.get, int(q.data.split(":")[1]))
    if row:
        await q.message.reply_text(_details_text(row), disable_web_page_preview=True)
    else:
        await q.message.reply_text("That save was deleted.")


async def on_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🗑 Delete button: x:<id> asks to confirm, xx:<id> deletes, k:<id> cancels."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    action, sid = q.data.split(":")
    save_id = int(sid)
    await q.answer()
    in_list = len(q.message.reply_markup.inline_keyboard) > 1 if q.message.reply_markup else False
    if action == "x" and in_list:  # from a /find or /topics list: confirm in a separate message, keep the list
        row = await asyncio.to_thread(db.get, save_id)
        await q.message.reply_text(f"Delete #{save_id}: {row[4] if row else '?'}?", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"xx:{save_id}"),
            InlineKeyboardButton("✖ Cancel", callback_data=f"kk:{save_id}")]]))
    elif action == "x":
        await q.edit_message_reply_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"xx:{save_id}"),
            InlineKeyboardButton("✖ Cancel", callback_data=f"k:{save_id}")]]))
    elif action == "k":
        await q.edit_message_reply_markup(_details_button(save_id))
    elif action == "kk":
        await q.edit_message_text(f"Kept #{save_id} 👍")
    else:
        gone = await asyncio.to_thread(db.delete, save_id)
        await q.edit_message_text(f"🗑 Deleted #{save_id}" + (f": {gone[0]}" if gone else " (it was already gone)"))


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
        ("find", "Find a save, e.g. /find cold calling"),
        ("topics", "Browse saves by topic"),
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
    app.add_handler(CommandHandler("find", cmd_find, filters=me))
    app.add_handler(CommandHandler("topics", cmd_topics, filters=me))
    app.add_handler(MessageHandler(me & (filters.VOICE | filters.AUDIO), on_voice))
    app.add_handler(MessageHandler(me & (filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO), on_video))
    app.add_handler(MessageHandler(me & filters.Document.PDF, on_pdf))
    app.add_handler(MessageHandler(me & (filters.PHOTO | filters.Document.IMAGE), on_image))
    app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_details, pattern=r"^d:\d+$"))
    app.add_handler(CallbackQueryHandler(on_delete, pattern=r"^(x|xx|k|kk):\d+$"))
    app.add_handler(CallbackQueryHandler(on_save_or_ask, pattern=r"^(sv|ak):\d+$"))
    app.add_handler(CallbackQueryHandler(on_topic, pattern=r"^t:"))
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
