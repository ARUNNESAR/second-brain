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
import time
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,  # noqa: E402
                          MessageHandler, filters)

import backup  # noqa: E402
import brain  # noqa: E402
import db  # noqa: E402
import memory  # noqa: E402
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
ASK: type your situation or question (or send a voice note and tap Ask), e.g. "client says we're too expensive". I may ask one quick question first. Tap 👎 on an answer to make me try harder.

/find <words>  find a save (to read or delete it)
/topics  browse saves by topic
/recent  last 10 saves
/export  download everything as a file
/fix <number> <correction>  correct a note that got something wrong
/delete <number>  remove a save
/retry  try again on links that failed
/me  tell me about yourself so answers fit you
/memtest  check how well I remember your saves
/recap  recap of the last 7 days (it also comes by itself every Sunday 9 AM)

Every other morning at 9 AM you get one old save to remember (/reminders off to pause).
Reply to any "✅ Saved #12" message with a question to ask about that save only, or with a link / forwarded DM / screenshot to ADD it to that save.
On videos: tap 📝 Screen text (copies prompts shown on screen) or 🎁 Get resource (finds "comment X and I'll DM you" links). Or type full with the link to do both. /toget lists what you still need to collect."""


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


VIDEO_KINDS = {"youtube", "instagram", "x", "linkedin", "reddit"}  # links we can open again for extras


def _saved_reply(save_id, note, prefix="✅ Saved", kind=None):
    """Short confirmation + buttons. Video/post links also get the optional 📝 Screen text and 🎁 Get resource."""
    text = f"{prefix} #{save_id} · {note.topic}\n{note.title}"
    dup = _similar.pop(save_id, None)
    if dup:
        text += f"\n\n⚠️ Looks like you already saved this: #{dup[0]} {dup[1]}\nTap 🗑 to remove this new copy."
    markup = _details_button(save_id)
    if kind in VIDEO_KINDS:
        markup = InlineKeyboardMarkup(list(markup.inline_keyboard) + [[
            InlineKeyboardButton("📝 Screen text", callback_data=f"st:{save_id}"),
            InlineKeyboardButton("🎁 Get resource", callback_data=f"rs:{save_id}")]])
    return text, markup


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


_similar = {}  # new save id -> (id, title) of an existing save that says almost the same thing


async def _store(note, source_type, url, pending_id=None):
    vec = await asyncio.to_thread(brain.embed, brain.note_text_for_search(note))
    if pending_id:  # this link failed before: fill in the same save number
        await asyncio.to_thread(db.update_note, pending_id, note.title, note.topic, note.summary,
                                note.key_points, note.content, vec)
        save_id = pending_id
    else:
        save_id = await asyncio.to_thread(db.add, source_type, url, note.title, note.topic, note.summary,
                                          note.key_points, note.content, vec)
    dup = await asyncio.to_thread(db.similar, vec, save_id)
    if dup:
        _similar[save_id] = dup
    await _index(save_id)
    return save_id


async def _index(save_id):
    """Split the save into search chunks. A failure here must never lose the save: the daily job retries it."""
    try:
        await asyncio.to_thread(memory.index_save, save_id)
    except Exception:
        log.exception("indexing #%s failed (will retry tonight)", save_id)


# ---------- handlers ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text.strip()
    urls = list(dict.fromkeys(URL_RE.findall(text)))
    replied = msg.reply_to_message
    if replied and replied.message_id in _clarify:  # answering the bot's quick question in his own words
        pending = _clarify.pop(replied.message_id)
        await respond(msg, pending["q"] + f"\n(More detail from Arun: {text})", ask_first=False)
        return
    if replied and replied.from_user and replied.from_user.is_bot:
        m = SCOPED_RE.match(replied.text or "")
        if m:  # a reply to one of the bot's messages about ONE save
            save_id = int(m.group(1))
            if urls or msg.forward_origin or text.lower().startswith("add"):
                await attach_to(msg, save_id, text=re.sub(r"^add:?\s*", "", text, flags=re.I))  # DM link / text
            else:
                await ask(update, text, only_id=save_id)  # a question about that save
            return
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


# bot messages that are about ONE save: replying to them asks about that save
SCOPED_RE = re.compile(r"^(?:✅ Saved|🔁 Saved later|📥 Got it|💡 Remember this\?|🎁|📝 On-screen text from|📎 Added to)? ?#(\d+)\b")

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
        await respond(q.message, text)


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
    if msg.voice and not msg.forward_origin:  # his own voice note: could be an idea to save OR a question
        _voices[msg.message_id] = (f, f.mime_type or "audio/ogg")
        await msg.reply_text("🎙 Ask this as a question, or save it?", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❓ Ask it", callback_data=f"va:{msg.message_id}"),
            InlineKeyboardButton("💾 Save it", callback_data=f"vs:{msg.message_id}")]]))
        return
    await _save_files(msg, [(f, f.mime_type or "audio/ogg")], "voice", (msg.caption or "").strip(), "voice note")


_voices = {}  # message_id -> (telegram file, mime) waiting for Ask / Save


async def on_voice_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    action, mid = q.data.split(":")
    pending = _voices.pop(int(mid), None)
    if not pending:
        await q.edit_message_text("I lost that voice note (the bot restarted). Please send it again.")
        return
    f, mime = pending
    if action == "vs":
        await q.edit_message_text("💾 Saving your voice note…")
        await _save_files(q.message, [(f, mime)], "voice", "", "voice note")
        return
    if (f.file_size or 0) > MAX_TG_FILE:
        await q.edit_message_text("That voice note is too long for me to download. Try a shorter one.")
        return
    await q.edit_message_text("🎙 Listening…")
    try:
        data = bytes(await (await f.get_file()).download_as_bytearray())
        question = await asyncio.to_thread(brain.transcribe, data, mime)
    except Exception as e:
        log.exception("voice question failed")
        await q.edit_message_text(_friendly_error(e))
        return
    await q.edit_message_text(f"🎙 You asked: {question}")
    await respond(q.message, question)


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
            except Exception as e:
                log.exception("queue: #%s failed", save_id)
                try:
                    await _queue_failed(context.bot, save_id, url, e)
                except Exception:
                    log.exception("queue: couldn't record failure of #%s", save_id)
            else:
                # Saved. A problem telling Arun must not undo the save.
                try:
                    text, markup = _saved_reply(save_id, note, kind=kind)
                    if warning:
                        text += "\n(only the caption/text could be read)"
                    await _tell(context.bot, save_id, text, markup)
                except Exception:
                    log.exception("queue: saved #%s but couldn't send the message", save_id)
                if FULL_RE.search(caption or ""):  # Arun typed "full" with the link: do the extras right away
                    for extra in (screen_text_for, resource_for):
                        try:
                            await context.bot.send_message(OWNER, _short(await extra(save_id)),
                                                           disable_web_page_preview=True)
                        except Exception:
                            log.exception("full extras failed for #%s", save_id)
            if kind == "instagram":
                await asyncio.sleep(40)  # space out reels so Instagram doesn't block the server


async def _queue_failed(bot, save_id, url, e):
    s = str(e)
    if isinstance(e, social.Gone) or "Video unavailable" in s or "Private video" in s:
        # deleted / private / empty: retrying won't help, say so now
        await asyncio.to_thread(db.set_status, save_id, "failed")
        await _tell(bot, save_id, f"❌ #{save_id} can't be read: it looks deleted or private.\n"
                                  f"{url}\nSend a screenshot of it if you still have it.")
        return
    wait = 120 if isinstance(e, social.Blocked) else 60 if ("429" in s or "RESOURCE_EXHAUSTED" in s) else 30
    status = await asyncio.to_thread(db.postpone, save_id, wait)
    if status is None:  # deleted while it was being processed
        _queue_msgs.pop(save_id, None)
    elif status == "failed":
        await _tell(bot, save_id, f"❌ Couldn't save #{save_id} after several tries.\n{url}\n"
                                  f"Send a screenshot of it instead.")
    elif save_id in _queue_msgs:  # first failure: tell once, quietly
        await _tell(bot, save_id, f"⏳ #{save_id} is blocked for now. I'll retry by myself and "
                                  f"message you when it's saved.")


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
    replied = msg.reply_to_message
    m = SCOPED_RE.match(replied.text or "") if replied and replied.from_user and replied.from_user.is_bot else None
    if m and not msg.media_group_id:  # a screenshot (e.g. of the DM) sent as a reply to a save: add it there
        src = msg.photo[-1] if msg.photo else msg.document
        if (src.file_size or 0) <= MAX_TG_FILE:
            data = bytes(await (await src.get_file()).download_as_bytearray())
            await attach_to(msg, int(m.group(1)), image=(data, "image/jpeg" if msg.photo else msg.document.mime_type))
            return
    if msg.media_group_id:  # several screenshots sent together: wait a moment, then save them as ONE note
        group = _albums.setdefault(msg.media_group_id, [])
        group.append(msg)
        if len(group) == 1:
            context.job_queue.run_once(flush_album, when=3, data=msg.media_group_id)
        return
    src = msg.photo[-1] if msg.photo else msg.document
    if (src.file_size or 0) > MAX_TG_FILE:
        await msg.reply_text("That image is over 20 MB, which Telegram doesn't let bots download. Send a smaller screenshot.")
        return
    status = await msg.reply_text("⏳ Reading the screenshot…")
    try:
        mime = "image/jpeg" if msg.photo else msg.document.mime_type
        data = bytes(await (await src.get_file()).download_as_bytearray())
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


_last_qa = {"at": 0.0, "q": "", "a": "", "id": None}  # previous question + answer (+ save it was about)
FOLLOW_UP_SECONDS = 10 * 60
# words that mean "the thing we were just talking about" (only then does a short question stay on that save)
FOLLOW_UP_RE = re.compile(r"\b(it|that|this|these|those|them|they|why|more|again|explain|example|step|steps)\b", re.I)


async def _answer(question, only_id=None, previous=None, rows=None):
    """Search the saves and write an answer with sources. Returns (text, buttons or None, rows used).
    only_id: answer from that one save (when Arun replies to a save's message).
    previous: an answer he rated 👎 — search wider and think harder this time."""
    try:
        recent = time.time() - _last_qa["at"] < FOLLOW_UP_SECONDS
        if recent and not only_id and not previous and _last_qa["id"] and len(question) <= 40 and FOLLOW_UP_RE.search(question):
            only_id = _last_qa["id"]  # short follow-up after asking about one save: stay on that save
        history = f"Q: {_last_qa['q']}\nA: {_last_qa['a'][:1500]}" if recent and not previous else ""
        if only_id:
            row = await asyncio.to_thread(db.get, only_id)
            rows = [tuple(row) + (1.0,)] if row else []
            if not rows:
                _last_qa["id"] = None
                return f"Save #{only_id} doesn't exist anymore.", None, []
        elif rows is None:
            # a short follow-up ("why?", "tell me more") is searched together with the previous question
            query = f"{_last_qa['q']} {question}" if history and len(question) < 60 else question
            rows = await asyncio.to_thread(memory.search, query, 10 if previous else 6)
        if not rows:
            return "Nothing saved yet. Share a YouTube video or a screenshot first, then ask me.", None, []
        profile = await asyncio.to_thread(db.get_setting, "profile", "")
        excerpt = 30000 if only_id else (12000 if previous else 6000)
        text = await asyncio.to_thread(brain.answer, question, rows, history, excerpt, profile, previous)
        _last_qa.update(at=time.time(), q=question, a=text, id=only_id)
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= len(rows)})
        sources = []
        for n in cited:
            r = rows[n - 1]
            line = f"[{n}] #{r[0]} {r[4]} · {r[1]:%d %b}"
            if r[3]:
                line += f"\n{r[3]}"
            sources.append(line)
        return _short(text + ("\n\nSources:\n" + "\n".join(sources) if sources else "")), None, rows
    except Exception as e:
        log.exception("ask failed")
        s = str(e)
        if rows and ("429" in s or "RESOURCE_EXHAUSTED" in s):
            # Gemini's free limit is used up: still show the saves that match, so the answer isn't lost
            return ("Gemini's free limit is used up for today, so I can't write an answer.\n"
                    "These saves match your question. Tap to read:"), _list_markup([(r[0], r[4]) for r in rows[:5]]), rows
        return _friendly_error(e), None, rows or []


# answers waiting for 👍/👎 and questions waiting for Arun's quick reply (kept in memory; fine for one person)
_answers = {}    # answer message_id -> {"q", "only_id", "text", "ids"}
_clarify = {}    # clarifying-question message_id -> original question


def _rate_buttons(mid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("👍", callback_data=f"up:{mid}"),
                                  InlineKeyboardButton("👎 Try harder", callback_data=f"dn:{mid}")]])


async def respond(reply_to, question, only_id=None, ask_first=True):
    """Answer a question in the chat. Like a good coach, it may first ask ONE quick question when the right advice
    depends on a missing detail; then the answer comes with 👍 / 👎 (👎 = try again, harder)."""
    status = await reply_to.reply_text("🔎 Looking through your saves…")
    rows = None
    recent = time.time() - _last_qa["at"] < FOLLOW_UP_SECONDS
    if ask_first and not only_id and not (recent and len(question) < 60):  # never for follow-ups
        try:
            rows = await asyncio.to_thread(memory.search, question, 6)
            profile = await asyncio.to_thread(db.get_setting, "profile", "")
            c = await asyncio.to_thread(brain.clarify_check, question, profile, [r[4] for r in rows])
            if c.ask and c.question:
                buttons = [[InlineKeyboardButton(o, callback_data=f"cl:{status.message_id}:{n}")]
                           for n, o in enumerate(c.options)]
                buttons.append([InlineKeyboardButton("⏩ Just answer", callback_data=f"cl:{status.message_id}:x")])
                _clarify[status.message_id] = {"q": question, "options": c.options}
                await status.edit_text(f"🤔 Quick question so the advice fits: {c.question}\n\n"
                                       f"Tap one, or reply to this message in your own words.",
                                       reply_markup=InlineKeyboardMarkup(buttons))
                return
        except Exception:
            log.exception("clarify check failed; answering directly")
    await _send_answer(status, question, only_id, rows=rows)


async def _send_answer(status, question, only_id=None, previous=None, rows=None):
    text, markup, used = await _answer(question, only_id, previous, rows)
    if markup is None and used:  # a real answer: add 👍 / 👎
        markup = _rate_buttons(status.message_id)
        _answers[status.message_id] = {"q": question, "only_id": only_id, "text": text, "ids": [r[0] for r in used]}
    await status.edit_text(text, reply_markup=markup, disable_web_page_preview=True)


async def ask(update, question, only_id=None):
    await respond(update.message, question, only_id)


async def on_clarify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A tap on one of the quick-question options (cl:<msg>:<n>) or ⏩ Just answer (cl:<msg>:x)."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    _, mid, choice = q.data.split(":")
    pending = _clarify.pop(int(mid), None)
    if not pending:
        await q.edit_message_text("I lost that question (the bot restarted). Please ask again.")
        return
    question = pending["q"]
    if choice != "x":
        question += f"\n(More detail from Arun: {pending['options'][int(choice)]})"
    await q.edit_message_text("🔎 Got it, thinking…")
    await _send_answer(q.message, question)


async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """👍 = remember it helped. 👎 = try again: wider search, more of each save, longer thinking."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    action, mid = q.data.split(":")
    info = _answers.get(int(mid))
    if not info:
        await q.answer("That answer is too old to rate.")
        return
    await asyncio.to_thread(db.add_feedback, info["q"], info["text"], action == "up", info["ids"])
    if action == "up":
        await q.answer("👍 Noted, thanks!")
        await q.edit_message_reply_markup(None)
        return
    await q.answer("Trying harder…")
    await q.edit_message_reply_markup(None)
    status = await q.message.reply_text("🔎 Searching wider and thinking harder…")
    await _send_answer(status, info["q"], info["only_id"], previous=info["text"])

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


def _fit_bytes(text, limit):
    """Cut text to fit Telegram's 64-byte button data without breaking a letter (Tamil letters are 3 bytes)."""
    return text.encode()[:limit].decode("utf-8", "ignore")


async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.topic_counts)
    if not rows:
        await update.message.reply_text("Nothing saved yet.")
        return
    buttons = [InlineKeyboardButton(f"{t} ({n})", callback_data="t:" + _fit_bytes(t, 60)) for t, n in rows]
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
    today = dt.datetime.now(IST).date()
    out = [f"# My Second Brain: export {today:%d %b %Y}\n\n{len(rows)} saves\n"]
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
    buf.name = f"second-brain-{today:%Y-%m-%d}.md"
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
    await _index(save_id)
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
    kb = q.message.reply_markup.inline_keyboard if q.message.reply_markup else []
    in_list = bool(kb) and kb[0][0].text.startswith("📖 #")  # rows from /find or /topics
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


# ---------- on-request extras: on-screen text, hidden resources, attachments ----------

FULL_RE = re.compile(r"\bfull\b", re.I)  # "full" typed with a link = do the extras automatically


async def _reembed(row):
    """After adding text to a save, refresh its search vector so the new text is findable."""
    save_id, title, topic, summary, points, content = row[:6]
    extra = (content or "").split("\n\n--- ", 1)[1][:3000] if "\n\n--- " in (content or "") else ""
    vec = await asyncio.to_thread(brain.embed, f"{title}\nTopic: {topic}\n{summary}\n" + "\n".join(points or []) + "\n" + extra)
    await asyncio.to_thread(db.set_embedding, save_id, vec)
    await _index(save_id)


async def screen_text_for(save_id):
    """Re-open a saved video/post and keep every on-screen text block word for word. Returns a message."""
    row = await asyncio.to_thread(db.get, save_id)
    if not row:
        return f"#{save_id} doesn't exist anymore."
    stype, url = row[2], row[3]
    if stype == "youtube":
        text = await asyncio.to_thread(brain.screen_text_youtube, url)
    elif stype in VIDEO_KINDS and url:
        post = await asyncio.to_thread(social.fetch, url)
        try:
            text = await asyncio.to_thread(brain.screen_text_post, post)
        finally:
            post.cleanup()
    else:
        return "I can only do this for YouTube, Instagram, X, LinkedIn and Reddit links."
    if not text or "NO_SCREEN_TEXT" in text:
        return f"📝 #{save_id}: no prompts or text blocks on screen in this one."
    updated = await asyncio.to_thread(db.append_extra, save_id, "On-screen text", text,
                                      "📝 On-screen text saved: " + " ".join(text.split())[:120])
    await _reembed(updated)
    return f"📝 On-screen text from #{save_id} (saved into the note):\n\n{text}"


async def resource_for(save_id):
    """Find the 'comment X and I'll DM you…' resource: links in the post, a web search, or the to-get list."""
    row = await asyncio.to_thread(db.get, save_id)
    if not row:
        return f"#{save_id} doesn't exist anymore."
    _, _, stype, url, title, _, summary, points, content = row
    offer = await asyncio.to_thread(brain.find_offer, title, "", f"{summary}\n" + "\n".join(points or []) + f"\n{content or ''}")
    if not offer.has_offer and not offer.urls:
        return f"🎁 #{save_id}: this one doesn't offer an extra resource."
    lines = [f"🎁 #{save_id} offers: {offer.what or 'a resource'}" + (f"\nHow to get it: {offer.how_to_get}" if offer.how_to_get else "")]
    found = "\n".join(offer.urls)
    if not found:
        found = await asyncio.to_thread(brain.search_resource, offer.what, "")
    if found:
        updated = await asyncio.to_thread(db.append_extra, save_id, "Resource (found online, please check)", found,
                                          "🎁 Resource link saved (see Details)")
        await _reembed(updated)
        lines.append(f"\nFound this (check it's the right one):\n{found}")
    if offer.how_to_get and not found:
        await asyncio.to_thread(db.add_toget, save_id, offer.what, offer.how_to_get)
        lines.append("\nCouldn't find it online, so it's on your /toget list.\n"
                     f"When you get it, reply to this message with the link, text or screenshot and I'll add it to #{save_id}.")
    return "\n".join(lines)


async def on_extra_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📝 Screen text (st:<id>) and 🎁 Get resource (rs:<id>) buttons."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    action, sid = q.data.split(":")
    save_id = int(sid)
    label = "on-screen text" if action == "st" else "resource"
    status = await q.message.reply_text(f"⏳ Looking for the {label} in #{save_id}… (up to a minute)")
    try:
        text = await (screen_text_for(save_id) if action == "st" else resource_for(save_id))
        await status.edit_text(_short(text), disable_web_page_preview=True)
    except Exception as e:
        log.exception("%s failed for #%s", label, save_id)
        await status.edit_text(_friendly_error(e))


async def attach_to(reply_to, save_id, text=None, image=None):
    """Arun replied to a save with a link / forwarded DM / screenshot: add it to that save."""
    status = await reply_to.reply_text(f"⏳ Adding it to #{save_id}…")
    try:
        if image:
            data, mime = image
            text = await asyncio.to_thread(brain.image_text, data, mime)
        else:
            text = await asyncio.to_thread(brain.read_attachment, text)
        updated = await asyncio.to_thread(db.append_extra, save_id, "Added by Arun", text,
                                          "📎 Added: " + " ".join(text.split())[:120])
        if not updated:
            await status.edit_text(f"#{save_id} doesn't exist anymore.")
            return
        await _reembed(updated)
        done = await asyncio.to_thread(db.finish_toget, save_id)
        await status.edit_text(f"📎 Added to #{save_id}: {updated[1]}" + ("\n✅ Ticked off your /toget list." if done else ""))
    except Exception as e:
        log.exception("attach failed for #%s", save_id)
        await status.edit_text(_friendly_error(e))


async def cmd_toget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.open_toget)
    if not rows:
        await update.message.reply_text("🎁 Nothing on your to-get list. 👍")
        return
    lines = [f"#{sid} {what or title}\n   ↳ {how}" for _, sid, what, how, title in rows]
    await update.message.reply_text(
        _short("🎁 Still to get (reply to a save with what you receive):\n\n" + "\n".join(lines)),
        reply_markup=_list_markup([(sid, title) for _, sid, _, _, title in rows[:10]]))


# ---------- About Arun + memory test ----------

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/me <a few lines about yourself> — used to tailor every answer (here and in Claude/ChatGPT)."""
    text = update.message.text.partition(" ")[2].strip()
    if text:
        await asyncio.to_thread(db.set_setting, "profile", text[:2000])
        await update.message.reply_text("✅ Saved. Every answer will now take this into account.")
        return
    now = await asyncio.to_thread(db.get_setting, "profile", "")
    await update.message.reply_text(
        (f"About you (used in every answer):\n\n{now}\n\n" if now else "I don't know anything about you yet.\n\n") +
        "To set or change it, send /me followed by a few lines, e.g.\n"
        "/me I'm an AR/VR founder in Chennai. I make tech reels. Learning sales and communication. Goal: grow my startup.")


def _memtest_text(found, total, misses):
    pct = round(100 * found / total) if total else 0
    lines = [f"🧪 Memory test: found {found}/{total} saves ({pct}%) from a real-life question about each."]
    if misses:
        lines.append("\nMissed (I'll keep an eye on these):")
        lines += [f"• #{sid} {title[:50]}\n   asked: {q}" for sid, title, q in misses[:5]]
    return _short("\n".join(lines))


async def cmd_memtest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("🧪 Testing my memory on 15 random saves… (about 1 minute)")
    try:
        await status.edit_text(_memtest_text(*await asyncio.to_thread(memory.memory_test, 15)))
    except Exception as e:
        log.exception("memory test failed")
        await status.edit_text(_friendly_error(e))


async def weekly_memtest(context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(OWNER, _memtest_text(*await asyncio.to_thread(memory.memory_test, 20)),
                                       disable_notification=True)
    except Exception:
        log.exception("weekly memory test failed")


async def daily_backup(context: ContextTypes.DEFAULT_TYPE):
    """2:30 AM: copy every save to the private GitHub backup repo. Tells Arun only if it fails."""
    try:
        n = await asyncio.to_thread(backup.to_github)
        log.info("daily backup: %s", "not set up" if n is None else f"{n[0]} new/changed, {n[1]} removed, {n[2]} total")
    except Exception as e:
        log.exception("daily backup failed")
        await context.bot.send_message(OWNER, f"⚠️ Last night's backup to GitHub failed: {str(e)[:150]}\n"
                                              f"Send /backup to try again. (If it says 401, the GitHub token expired.)")


async def weekly_backup_file(context: ContextTypes.DEFAULT_TYPE):
    """Sunday: send the backup file in this chat too, silently, as a second copy in a different place."""
    try:
        content, n = await asyncio.to_thread(backup.make_file)
        buf = io.BytesIO(content)
        buf.name = f"second-brain-backup-{dt.datetime.now(IST):%Y-%m-%d}.json.gz"
        await context.bot.send_document(OWNER, buf, caption=f"🗄 Weekly backup: {n} saves. Keep this file.",
                                        disable_notification=True)
    except Exception:
        log.exception("weekly backup file failed")


async def cmd_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("⏳ Backing up…")
    try:
        n = await asyncio.to_thread(backup.to_github)
        content, total = await asyncio.to_thread(backup.make_file)
        buf = io.BytesIO(content)
        buf.name = f"second-brain-backup-{dt.datetime.now(IST):%Y-%m-%d}.json.gz"
        await update.message.reply_document(buf, caption=f"🗄 Backup file: {total} saves.")
        await status.edit_text(f"✅ GitHub backup done: {n[0]} new/changed, {n[1]} removed, {n[2]} saves in total." if n is not None else
                               "✅ Backup file below. (GitHub backup isn't set up yet.)")
    except Exception as e:
        log.exception("manual backup failed")
        await status.edit_text(f"❌ Backup failed: {str(e)[:200]}")


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "off"):
        await asyncio.to_thread(db.set_setting, "reminders", arg)
        await update.message.reply_text("🔔 Daily reminders are ON (9 AM, Mon-Sat)." if arg == "on" else
                                        "🔕 Daily reminders are OFF. The Sunday recap still comes. /reminders on to restart.")
        return
    now = await asyncio.to_thread(db.get_setting, "reminders", "on")
    await update.message.reply_text(f"Daily reminders are {now.upper()}.\n/reminders off to pause, /reminders on to restart.")


async def daily_tip(context: ContextTypes.DEFAULT_TYPE):
    """One old save a day, so it comes back before it's forgotten (each save rotates; least recent first)."""
    try:
        if await asyncio.to_thread(db.get_setting, "reminders", "on") == "off":
            return
        row = await asyncio.to_thread(db.pick_tip)
        if not row:
            return
        i, created, stype, url, title, topic, summary, points = row
        best = "\n".join(f"• {p}" for p in (points or [])[:3])
        text = f"💡 Remember this? #{i} (saved {created:%d %b})\n\n{title}\n\n{best}"
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
        n = await asyncio.to_thread(memory.index_missing)
        if n:
            log.info("indexed %s saves that had no search chunks", n)
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
        ("reminders", "Turn daily reminders on/off"),
        ("backup", "Back up all saves now"),
        ("toget", "Resources still to get (comment-for-link)"),
        ("me", "Tell me about yourself (tailors answers)"),
        ("memtest", "Test how well I remember your saves"),
        ("help", "How to use me"),
    ])


def build_app():
    """The bot with all its handlers. Used by main() on the PC and by server.py online."""
    app = (Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"].strip())
           .concurrent_updates(True).post_init(set_menu).build())

    me = filters.User(user_id=OWNER) & filters.UpdateType.MESSAGE  # edits of old messages are ignored
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
    app.add_handler(CommandHandler("reminders", cmd_reminders, filters=me))
    app.add_handler(CommandHandler("backup", cmd_backup, filters=me))
    app.add_handler(CommandHandler("toget", cmd_toget, filters=me))
    app.add_handler(CommandHandler("me", cmd_me, filters=me))
    app.add_handler(CommandHandler("memtest", cmd_memtest, filters=me))
    app.add_handler(CallbackQueryHandler(on_extra_button, pattern=r"^(st|rs):\d+$"))
    app.add_handler(CallbackQueryHandler(on_clarify, pattern=r"^cl:\d+:(\d+|x)$"))
    app.add_handler(CallbackQueryHandler(on_rate, pattern=r"^(up|dn):\d+$"))
    app.add_handler(CallbackQueryHandler(on_voice_choice, pattern=r"^(va|vs):\d+$"))
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
    app.job_queue.run_daily(daily_backup, time=dt.time(hour=2, minute=30, tzinfo=IST))
    app.job_queue.run_daily(weekly_backup_file, time=dt.time(hour=9, minute=5, tzinfo=IST), days=(0,))
    app.job_queue.run_daily(weekly_memtest, time=dt.time(hour=9, minute=10, tzinfo=IST), days=(0,))
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
