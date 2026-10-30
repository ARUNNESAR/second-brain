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

HELP = """🧠 What I can do (just talk to me, no commands)

📥 Remember things
Share a reel, video or post, or send a screenshot, voice note or PDF. 👀 = reading, 👌 = saved.

💬 Help you in the moment
Tell me your situation, e.g. "a client says we're too expensive". I pick the best moves from what you saved
and tell you what to do. The next evening I'll ask how it went, so I learn what works for you.
Not helpful? Tap 👎 and I'll try again.
"what would Hormozi say about…": advice from one mentor. "who are my mentors": everyone you learn from.

🧠 Daily review
Every morning: 3 moves to remember, 2 minutes. On any save: ⋯ More → 🧠 Quiz me.

🎭 Practice
"practice asking for a raise": I play the other person and coach you, then score you. Answer by voice and I'll
also tell you your pace and filler words.

📘 Playbooks
"make my sales playbook": everything you saved on a topic, turned into one clear guide.

🔎 Find & tidy up
"what did I save about pricing", "show my latest saves", "delete the last one".
Reply to anything you shared to ask about it, or to add more to it.

🎙 Voice
Send a voice note to ask. I'll answer by voice too.

⚙️ Settings, in plain words
"pause reminders", "voice replies off", "remember that I'm a startup founder"."""


def _short(text, limit=MAX_MSG):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _friendly_error(e):
    s = str(e)
    if "429" in s or "RESOURCE_EXHAUSTED" in s:
        return ("I've used up my free thinking for today. Try again tomorrow, or ask in Claude/ChatGPT with "
                "Second Brain switched on.")
    if "503" in s or "UNAVAILABLE" in s or "overloaded" in s.lower():
        return "Gemini is busy right now. Try again in a minute."
    if isinstance(e, social.Blocked):
        return f"⚠️ {s}"
    return "Something went wrong on my side. Please try again in a minute."


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


def _details_button(save_id, more=False, kind=None):
    """The one-tap buttons under every save: 📖 Read · 🗑 Delete · ⋯ More (✅ It worked, ❓ Ask about it, and for
    videos/posts 📝 Screen text, 🎁 Get resource)."""
    rows = [[InlineKeyboardButton("📖 Read", callback_data=f"d:{save_id}"),
             InlineKeyboardButton("🗑 Delete", callback_data=f"x:{save_id}")]]
    if not more:
        rows[0].append(InlineKeyboardButton("⋯ More", callback_data=f"m:{save_id}"))
        return InlineKeyboardMarkup(rows)
    rows.append([InlineKeyboardButton("✅ It worked for me", callback_data=f"wk:{save_id}"),
                 InlineKeyboardButton("❓ Ask about it", callback_data=f"aq:{save_id}")])
    rows.append([InlineKeyboardButton("🧠 Quiz me on it", callback_data=f"rv:go:0:{save_id}")])
    if kind in VIDEO_KINDS:
        rows.append([InlineKeyboardButton("📝 Screen text", callback_data=f"st:{save_id}"),
                     InlineKeyboardButton("🎁 Get resource", callback_data=f"rs:{save_id}")])
    return InlineKeyboardMarkup(rows)


def _details_text(row):
    i, created, stype, url, title, topic, summary, points, _ = row
    where = {"youtube": "YouTube", "instagram": "Instagram", "x": "X", "linkedin": "LinkedIn", "reddit": "Reddit",
             "image": "Screenshot", "note": "Your note", "voice": "Your voice note", "pdf": "PDF", "video": "Video"}.get(stype, "Saved")
    lines = [f"📖 {title}", f"{where} · {topic or 'general'} · saved {created:%d %b}", "", summary or ""]
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


# ---------- silent capture: emoji reactions on Arun's own message instead of chatty replies ----------
# 👀 reading it · 👌 saved · 🤔 blocked for now, will retry by itself · 😢 couldn't save (a message explains)
_bot = None  # set when the app starts (post_init), so background jobs can react too


async def _react(msg_id, emoji):
    if not (_bot and msg_id):
        return
    try:
        await _bot.set_message_reaction(chat_id=OWNER, message_id=msg_id, reaction=emoji)
    except Exception:
        log.debug("couldn't set reaction %s on %s", emoji, msg_id)


async def _saved_quietly(save_id, note, msg_id, reply_to=None, warning=""):
    """Saved: 👌 on his message, and only speak if something needs his attention (a likely duplicate)."""
    await asyncio.to_thread(db.set_tg_msg, save_id, msg_id)
    await _react(msg_id, "👌")
    dup = _similar.pop(save_id, None)
    if _bot:  # a small card under his message: the title + one-tap buttons
        text = f"👌 {note.title}" + (f"\n⚠️ Looks like a repeat of “{dup[1]}”. Tap 🗑 if you don't need both." if dup else "")
        row = await asyncio.to_thread(db.get, save_id)
        await _bot.send_message(OWNER, text, reply_to_message_id=msg_id, allow_sending_without_reply=True,
                                reply_markup=_details_button(save_id, kind=row[2] if row else None),
                                disable_notification=True)
    await _tip_once("saved", "Tip: 👀 = I'm reading it, 👌 = saved. Under each save you get buttons to read it, delete "
                             "it, or more (ask about it, mark it as worked…).")


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

WELCOME = """🧠 Hi! I'm your Second Brain.

1️⃣ Share anything useful with me: a reel, video, post, screenshot or voice note. I'll remember it.
2️⃣ When you need help, just tell me your situation, by text or voice. I'll answer using everything you saved.

That's it. Try one of these:"""


def _welcome_buttons():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 What have I saved?", callback_data="ex:recent"),
         InlineKeyboardButton("📚 My topics", callback_data="ex:topics")],
        [InlineKeyboardButton("🎭 Practice a conversation", callback_data="ex:practice"),
         InlineKeyboardButton("📘 Make a playbook", callback_data="ex:playbook")],
        [InlineKeyboardButton("🙋 Tell me about you", callback_data="ex:me"),
         InlineKeyboardButton("❓ Everything I can do", callback_data="ex:help")]])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, reply_markup=_welcome_buttons())


_awaiting = {}  # what the next message means, e.g. {"practice": True} after tapping "Practice a conversation"


async def on_example(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """The welcome buttons: show instead of tell."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    what = q.data.split(":")[1]
    u = _Upd(q.message)
    if what == "recent":
        await cmd_recent(u, _Ctx(context))
    elif what == "topics":
        await cmd_topics(u, _Ctx(context))
    elif what == "practice":
        _awaiting["practice"] = True
        await q.message.reply_text("🎭 What do you want to practice? Just describe it, e.g.\n"
                                   "• asking my boss for a raise\n• telling a client our price is going up\n"
                                   "• a job interview for a product role")
    elif what == "playbook":
        await make_playbook(q.message, "")
    elif what == "me":
        _awaiting["profile"] = True
        now = await asyncio.to_thread(db.get_setting, "profile", "")
        await q.message.reply_text((f"Here's what I know now:\n{now}\n\n" if now else "") +
                                   "🙋 Tell me a bit about yourself in a few lines: what you do, what you're working on, "
                                   "your goals. I'll use it to make every answer fit you.")
    else:
        await q.message.reply_text(HELP)


async def _tip_once(key, text, reply_to_id=None, markup=None):
    """A short tip the FIRST time he meets a feature, never again. Teaches by doing, no guide needed."""
    if not _bot or await asyncio.to_thread(db.get_setting, f"tip_{key}", ""):
        return
    await asyncio.to_thread(db.set_setting, f"tip_{key}", "shown")
    try:
        await _bot.send_message(OWNER, text, reply_to_message_id=reply_to_id, allow_sending_without_reply=True,
                                reply_markup=markup)
    except Exception:
        log.debug("tip %s not sent", key)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = msg.text.strip()
    urls = list(dict.fromkeys(URL_RE.findall(text)))
    replied = msg.reply_to_message
    if replied and replied.from_user and replied.from_user.is_bot:  # telling how a past situation went
        jid = await asyncio.to_thread(db.journal_by_msg, replied.message_id)
        if jid:
            await asyncio.to_thread(db.add_outcome, jid, text)
            await msg.reply_text("📝 Thanks, noted. I'll remember this next time something similar comes up.")
            return
    if replied and replied.message_id in _clarify:  # answering the bot's quick question in his own words
        pending = _clarify.pop(replied.message_id)
        await respond(msg, pending["q"] + f"\n(More detail from Arun: {text})", ask_first=False)
        return
    # a reply to a message that is about ONE save: one of the bot's save messages, or his own shared link/screenshot
    reply_save = None
    if replied and replied.from_user and replied.from_user.is_bot:
        reply_save = _scope_of(replied)
    elif replied:
        reply_save = await asyncio.to_thread(db.by_tg_msg, replied.message_id)
    if reply_save:
        if urls or msg.forward_origin or text.lower().startswith("add"):
            await attach_to(msg, reply_save, text=re.sub(r"^add:?\s*", "", text, flags=re.I))  # DM link / text
        else:
            await handle_words(msg, context, text, reply_save=reply_save)  # "screen text", "worked", a question…
        return
    if _awaiting.pop("profile", None):  # he tapped "🙋 Tell me about you": this is the description
        await asyncio.to_thread(db.set_setting, "profile", text[:2000])
        await msg.reply_text("✅ Thanks! Every answer will now fit you.")
        return
    if "attach" in _awaiting:  # he tapped "📎 I got it": this is the resource (link or text)
        await attach_to(msg, _awaiting.pop("attach"), text=text)
        return
    if "ask" in _awaiting and not urls:  # he tapped "❓ Ask about it": this is the question
        await respond(msg, text, only_id=_awaiting.pop("ask"))
        return
    if "mentor" in _awaiting and not urls:  # he tapped "💬 <mentor>": this is what he wants to ask them
        await respond(msg, f"What would {_awaiting.pop('mentor')} tell me? {text}")
        return
    if _awaiting.pop("practice", None) and not urls:  # he tapped "Practice a conversation": this is the situation
        await start_practice(msg, text)
        return
    if _practice.get("active") and not urls:  # in a practice session, every message is his next line
        if text.lower().strip(" .!") in ("stop", "end", "done", "finish", "end practice", "stop practice"):
            await end_practice(msg)
        else:
            await practice_reply(msg, text)
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
        await handle_words(msg, context, text)  # plain words: a question, or a request like "delete the last one"


def _scope_of(m):
    """The one save a bot message is about: from "#12" in its text, or from its buttons (📖/🗑 carry the save id)."""
    hit = SCOPED_RE.match(m.text or "")
    if hit:
        return int(hit.group(1))
    ids = {int(x.group(2)) for row in (m.reply_markup.inline_keyboard if m.reply_markup else []) for b in row
           if (x := re.match(r"^(d|x|st|rs):(\d+)$", b.callback_data or ""))}
    return ids.pop() if len(ids) == 1 else None


# bot messages that are about ONE save: replying to them asks about that save
SCOPED_RE = re.compile(r"^(?:✅ Saved|🔁 Saved later|📥 Got it|💡 Remember this\?|🎁|📝 On-screen text from|📎 Added to)? ?#(\d+)\b")

_texts = {}  # message_id -> long text waiting for the Save/Ask choice


async def save_text(reply_to, text, react_id=None):
    """Save a typed/forwarded note quietly: 👀 then 👌 on his message."""
    react_id = react_id or reply_to.message_id
    await _react(react_id, "👀")
    try:
        topics = await asyncio.to_thread(db.topics)
        note = await asyncio.to_thread(brain.note_from_text, text, topics)
        save_id = await _store(note, "note", None)
        await _saved_quietly(save_id, note, react_id)
    except Exception as e:
        log.exception("text save failed")
        await _react(react_id, "😢")
        await reply_to.reply_text(_friendly_error(e))


async def on_save_or_ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    action, mid = q.data.split(":")
    text = _texts.pop(int(mid), None)
    if not text:
        await q.edit_message_text("Sorry, I lost track of that one. Please send it again.")
        return
    if action == "sv":
        await q.edit_message_text("💾 Saving it. You'll see 👌 on your message when it's done.")
        await save_text(q.message, text, react_id=int(mid))
    else:
        await q.edit_message_reply_markup(None)
        await respond(q.message, text)


# ---------- files: voice notes, videos, PDFs, screenshot groups ----------

MAX_TG_FILE = 20 * 1024 * 1024  # Telegram lets bots download files up to 20 MB


async def _save_files(reply_to, tg_files, kind, caption, label, react_id=None):
    """Download Telegram files, let Gemini read them together, save one note."""
    if any((f.file_size or 0) > MAX_TG_FILE for f, _ in tg_files):
        await reply_to.reply_text("That file is too big for me to open. "
                                  "Share the link instead (YouTube/Instagram/X…).")
        return
    react_id = react_id or reply_to.message_id
    await _react(react_id, "👀")
    post = social.Post(url=None, kind=kind, text=caption, workdir=social._workdir())
    try:
        for n, (f, mime) in enumerate(tg_files):
            path = os.path.join(post.workdir, f"f{n}")
            await (await f.get_file()).download_to_drive(path)
            post.media.append((path, mime))
        topics = await asyncio.to_thread(db.topics)
        note = await asyncio.to_thread(brain.note_from_post, post, topics, "")
        save_id = await _store(note, kind, None)
        await _saved_quietly(save_id, note, react_id)
    except Exception as e:
        log.exception("%s save failed", kind)
        await _react(react_id, "😢")
        await reply_to.reply_text(_friendly_error(e) + f"\nPlease send the {label} again.")
    finally:
        post.cleanup()


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    f = msg.voice or msg.audio
    if msg.voice and _practice.get("active") and (f.file_size or 0) <= MAX_TG_FILE:  # practice: his spoken line
        data = bytes(await (await f.get_file()).download_as_bytearray())
        said = await asyncio.to_thread(brain.transcribe, data, f.mime_type or "audio/ogg", True)
        _practice.setdefault("voice", []).append((said, msg.voice.duration or 0))  # for feedback on HOW he speaks
        await msg.reply_text(f"🎙 {said}")
        await practice_reply(msg, said, by_voice=True)
        return
    replied = msg.reply_to_message
    if msg.voice and replied and replied.from_user and replied.from_user.is_bot:  # telling how a situation went
        jid = await asyncio.to_thread(db.journal_by_msg, replied.message_id)
        if jid:
            data = bytes(await (await f.get_file()).download_as_bytearray())
            said = await asyncio.to_thread(brain.transcribe, data, f.mime_type or "audio/ogg")
            await asyncio.to_thread(db.add_outcome, jid, said)
            await msg.reply_text(f"📝 Noted: “{_short(said, 300)}”\nI'll remember this next time.")
            return
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
        await q.edit_message_text("Sorry, I lost track of that voice note. Please send it again.")
        return
    f, mime = pending
    if action == "vs":
        await q.edit_message_text("💾 Saving your voice note…")
        await _save_files(q.message, [(f, mime)], "voice", "", "voice note", react_id=int(mid))
        return
    if (f.file_size or 0) > MAX_TG_FILE:
        await q.edit_message_text("That voice note is too long for me. Try a shorter one (under a few minutes).")
        return
    await q.edit_message_text("🎙 Listening…")
    try:
        data = bytes(await (await f.get_file()).download_as_bytearray())
        question = await asyncio.to_thread(brain.transcribe, data, mime)
    except Exception as e:
        log.exception("voice question failed")
        await q.edit_message_text(_friendly_error(e))
        return
    await q.edit_message_text(f"🎙 You said: {question}")
    await handle_words(q.message, context, question, by_voice=True)  # a question, or a request like "delete…"


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
    Silent: 👀 on his message now, 👌 when it's saved. No chat messages unless something needs him."""
    msg_id = update.message.message_id
    found = await asyncio.to_thread(db.find_url, clean)
    if found and found[2] == "done":
        await _react(msg_id, "👌")  # already saved earlier: nothing to do
        return
    if found:  # it failed or is still waiting: move it to the front
        save_id = found[0]
        await asyncio.to_thread(db.retry_one, save_id)
    else:
        save_id = await asyncio.to_thread(db.add, kind, clean, clean, None, None, None, caption, None, "pending")
    await asyncio.to_thread(db.set_tg_msg, save_id, msg_id)
    await _react(msg_id, "👀")
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
                # Saved: 👌 on his message. A problem signalling it must not undo the save.
                try:
                    await _saved_quietly(save_id, note, await asyncio.to_thread(db.tg_msg_of, save_id))
                except Exception:
                    log.exception("queue: saved #%s but couldn't signal it", save_id)
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
    """🤔 on his message while it waits for a retry; 😢 + a short reply only when it finally can't be saved."""
    s = str(e)
    msg_id = await asyncio.to_thread(db.tg_msg_of, save_id)

    async def explain(text):
        await _react(msg_id, "😢")
        await bot.send_message(OWNER, text, reply_to_message_id=msg_id, allow_sending_without_reply=True,
                               disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup([[
                                   InlineKeyboardButton("🔁 Try again", callback_data=f"rt:{save_id}"),
                                   InlineKeyboardButton("🗑 Remove", callback_data=f"x:{save_id}")]]))

    if isinstance(e, social.Gone) or "Video unavailable" in s or "Private video" in s:
        # deleted / private / empty: retrying won't help, say so now
        await asyncio.to_thread(db.set_status, save_id, "failed")
        await explain("😢 I can't read this one: it looks deleted or private. Send a screenshot of it instead.")
        return
    wait = 120 if isinstance(e, social.Blocked) else 60 if ("429" in s or "RESOURCE_EXHAUSTED" in s) else 30
    status = await asyncio.to_thread(db.postpone, save_id, wait)
    if status == "failed":
        await explain("😢 I couldn't save this after several tries. Send a screenshot of it instead.")
    elif status:
        await _react(msg_id, "🤔")  # blocked for now; it retries by itself and turns 👌 when saved
        await _tip_once("blocked", "🤔 means that site is blocking me for a while. I'll keep trying by myself and it "
                                     "turns 👌 when it's saved. Nothing for you to do.", msg_id)


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
    if replied and not m and not msg.media_group_id:  # a reply to his own shared link/screenshot
        sid = await asyncio.to_thread(db.by_tg_msg, replied.message_id)
        m = re.match(r"(\d+)", str(sid)) if sid else None
    if m and not msg.media_group_id:  # a screenshot (e.g. of the DM) sent as a reply to a save: add it there
        src = msg.photo[-1] if msg.photo else msg.document
        if (src.file_size or 0) <= MAX_TG_FILE:
            data = bytes(await (await src.get_file()).download_as_bytearray())
            await attach_to(msg, int(m.group(1)), image=(data, "image/jpeg" if msg.photo else msg.document.mime_type))
            return
    if "attach" in _awaiting and not msg.media_group_id:  # he tapped "📎 I got it" and sent a screenshot
        src = msg.photo[-1] if msg.photo else msg.document
        data = bytes(await (await src.get_file()).download_as_bytearray())
        await attach_to(msg, _awaiting.pop("attach"), image=(data, "image/jpeg" if msg.photo else msg.document.mime_type))
        return
    if msg.media_group_id:  # several screenshots sent together: wait a moment, then save them as ONE note
        group = _albums.setdefault(msg.media_group_id, [])
        group.append(msg)
        if len(group) == 1:
            context.job_queue.run_once(flush_album, when=3, data=msg.media_group_id)
        return
    src = msg.photo[-1] if msg.photo else msg.document
    if (src.file_size or 0) > MAX_TG_FILE:
        await msg.reply_text("That image is too big for me to open. Send a normal screenshot instead.")
        return
    await _react(msg.message_id, "👀")
    try:
        mime = "image/jpeg" if msg.photo else msg.document.mime_type
        data = bytes(await (await src.get_file()).download_as_bytearray())
        topics = await asyncio.to_thread(db.topics)
        note = await asyncio.to_thread(brain.note_from_image, data, mime, topics, (msg.caption or "").strip())
        save_id = await _store(note, "image", None)
        await _saved_quietly(save_id, note, msg.message_id)
    except Exception as e:
        log.exception("image save failed")
        await _react(msg.message_id, "😢")
        await msg.reply_text(_friendly_error(e) + "\nPlease send the screenshot again.")


async def cmd_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Just type it, like: remember: always ask the client's budget first")
        return
    await save_text(update.message, text)


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
        profile = await asyncio.to_thread(db.get_setting, "profile", "")
        moves, ctx = None, {}  # mentor moves: found with the saves below, or all of one save's
        if only_id:
            row = await asyncio.to_thread(db.get, only_id)
            rows = [tuple(row) + (1.0,)] if row else []
            if not rows:
                _last_qa["id"] = None
                return "That save was deleted, so I can't answer from it.", None, []
            moves = await asyncio.to_thread(db.moves_of, only_id)
        else:
            # a short follow-up ("why?", "tell me more") is searched together with the previous question
            query = f"{_last_qa['q']} {question}" if history and len(question) < 60 else question
            rows, moves, ctx = await asyncio.to_thread(memory.mentor_context, query, 10 if previous else 6,
                                                       20 if previous else 15, profile)
        if not rows:
            return "Nothing saved yet. Share a YouTube video or a screenshot first, then ask me.", None, []
        covered = ctx.get("direct", 1) >= memory.MOVE_FLOOR
        excerpt = 30000 if only_id else (3000 if moves else (12000 if previous else 6000))
        text = await asyncio.to_thread(brain.answer, question, rows, history, excerpt, profile, previous,
                                       moves if covered else None, ctx.get("past"), ctx.get("mentors"))
        _last_qa.update(at=time.time(), q=question, a=text, id=only_id)
        plan = ctx.get("plan")
        if plan and plan.real_situation and covered and not previous and not history:  # a real moment: ask later how it went
            cited_ids = [rows[int(n) - 1][0] for n in set(re.findall(r"\[(\d+)\]", text)) if 0 < int(n) <= len(rows)]
            await asyncio.to_thread(db.add_journal, plan.summary or question[:200], text[:600], cited_ids,
                                    ctx["vector"], _followup_time())
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= len(rows)})
        sources = []
        for n in cited:
            r = rows[n - 1]
            line = f"[{n}] {r[4]} ({r[1]:%d %b})"
            if r[3]:
                line += f"\n     {r[3]}"
            sources.append(line)
        return _short(text + ("\n\n📚 From your saves:\n" + "\n".join(sources) if sources else "")), None, rows
    except Exception as e:
        log.exception("ask failed")
        s = str(e)
        if rows and ("429" in s or "RESOURCE_EXHAUSTED" in s):
            # Gemini's free limit is used up: still show the saves that match, so the answer isn't lost
            return ("I've used up my free thinking for today, so I can't write an answer.\n"
                    "These saves match your question. Tap to read:"), _list_markup([(r[0], r[4]) for r in rows[:5]]), rows
        return _friendly_error(e), None, rows or []


# answers waiting for 👍/👎 and questions waiting for Arun's quick reply (kept in memory; fine for one person)
_answers = {}    # answer message_id -> {"q", "only_id", "text", "ids"}
_clarify = {}    # clarifying-question message_id -> original question


def _rate_buttons(mid, listen=True):
    """Under every answer: was it helpful, and the two natural next steps (hear it, rehearse it)."""
    second = [InlineKeyboardButton("🎭 Practice this", callback_data=f"pr:{mid}")]
    if listen:
        second.insert(0, InlineKeyboardButton("🔊 Listen", callback_data=f"ls:{mid}"))
    return InlineKeyboardMarkup([[InlineKeyboardButton("👍 Helpful", callback_data=f"up:{mid}"),
                                  InlineKeyboardButton("👎 Not helpful", callback_data=f"dn:{mid}")], second])


def _retry_button(mid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Try again", callback_data=f"ag:{mid}")]])


async def respond(reply_to, question, only_id=None, ask_first=True, by_voice=False):
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
                _clarify[status.message_id] = {"q": question, "options": c.options, "voice": by_voice}
                await status.edit_text(f"🤔 Quick question so the advice fits: {c.question}\n\n"
                                       f"Tap one, or reply to this message in your own words.",
                                       reply_markup=InlineKeyboardMarkup(buttons))
                return
        except Exception:
            log.exception("clarify check failed; answering directly")
    await _send_answer(status, question, only_id, rows=rows, by_voice=by_voice)


async def _send_answer(status, question, only_id=None, previous=None, rows=None, by_voice=False):
    text, markup, used = await _answer(question, only_id, previous, rows)
    voice = await _want_voice(by_voice)
    if markup is None and used:  # a real answer: 👍 / 👎 plus one-tap next steps
        markup = _rate_buttons(status.message_id, listen=not voice)
        _answers[status.message_id] = {"q": question, "only_id": only_id, "text": text, "ids": [r[0] for r in used]}
    elif markup is None:  # it failed (e.g. out of free thinking): one tap to try again later
        markup = _retry_button(status.message_id)
        _answers[status.message_id] = {"q": question, "only_id": only_id, "text": "", "ids": []}
    await status.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    if used and _answers.get(status.message_id, {}).get("text") and voice:
        asyncio.create_task(_voice_answer(question, text, status.message_id))
        await _tip_once("voice", "🎙 You asked by voice, so I'll answer by voice too. Say \"voice replies off\" if you "
                                   "prefer text, or \"always reply by voice\".",
                        markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔇 Text only", callback_data="vo:off")]]))
    elif used and markup is not None:
        await _tip_once("answer", "Tip: if an answer misses the mark, tap 👎 and I'll try again. To ask about one "
                                    "thing you saved, reply to the link you shared and ask.")


# ---------- voice replies: asked by voice → answered by voice (Puck); "always by voice" / "voice off" ----------

async def _want_voice(by_voice):
    mode = await asyncio.to_thread(db.get_setting, "voice", "auto")
    return mode == "on" or (mode == "auto" and by_voice)


async def _voice_answer(question, text, reply_to_id):
    """Voice reply = a script written for listening (not the written answer read out), in Puck."""
    script = await asyncio.to_thread(brain.voice_script, question, text)
    await _send_voice(script, reply_to_id)


async def _send_voice(text, reply_to_id, voice=None, style=None):
    """Send text as a voice note. Never breaks the chat: if speech fails, the text answer is already there."""
    if not (_bot and text.strip()):
        return
    try:
        voice = voice or await asyncio.to_thread(db.get_setting, "voice_name", "") or brain.MENTOR_VOICE
        kw = {"style": style} if style else {}
        audio, fmt = await asyncio.to_thread(brain.speak, text, voice, **kw)
        buf = io.BytesIO(audio)
        if fmt == "ogg":  # Telegram's own voice-note format: plays inline like any voice message
            buf.name = "reply.ogg"
            await _bot.send_voice(OWNER, voice=buf, reply_to_message_id=reply_to_id, allow_sending_without_reply=True)
        else:  # fallback: a normal audio file with a player (always downloadable)
            buf.name = "reply.mp3"
            await _bot.send_audio(OWNER, audio=buf, title="Second Brain", reply_to_message_id=reply_to_id,
                                  allow_sending_without_reply=True)
    except Exception:
        log.exception("voice reply failed (text answer was sent)")


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
        await q.edit_message_text("Sorry, I lost track of that one. Please ask again.")
        return
    question = pending["q"]
    if choice != "x":
        question += f"\n(More detail from Arun: {pending['options'][int(choice)]})"
    await q.edit_message_text("🔎 Got it, thinking…")
    await _send_answer(q.message, question, by_voice=pending.get("voice", False))


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
    status = await q.message.reply_text("🔎 Sorry about that. Looking again, more carefully…")
    await _send_answer(status, info["q"], info["only_id"], previous=info["text"])

def _list_markup(rows):
    """One row of buttons per save: 📖 #id title  |  🗑"""
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"📖 {(title or 'Untitled')[:40]}", callback_data=f"d:{i}"),
                                  InlineKeyboardButton("🗑", callback_data=f"x:{i}")]
                                 for i, title, *_ in rows])


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    words = " ".join(context.args).strip()
    if not words:
        await update.message.reply_text("What should I look for? Just say e.g. \"what did I save about cold calling\".")
        return
    vec = await asyncio.to_thread(brain.embed, words, True)
    rows = await asyncio.to_thread(db.find, words, vec, 8)
    if not rows:
        await update.message.reply_text(f"I didn't find anything about \"{words}\". Try other words, or say \"show my topics\".")
        return
    await update.message.reply_text(f"🔎 Here's what you saved about \"{words}\". Tap one to read it:", reply_markup=_list_markup(rows))


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
    await q.message.reply_text(f"📚 Your {topic} saves. Tap one to read it:", reply_markup=_list_markup(rows))


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(db.recent, 10)
    if not rows:
        await update.message.reply_text("Nothing saved yet.")
        return
    mark = {"pending": "⏳ ", "failed": "😢 ", "kept": "🔗 "}
    items = [(i, mark.get(st, "") + (title if st == "done" else "still saving…" if st == "pending" else title or "a link"))
             for i, created, t, title, topic, st in rows]
    await update.message.reply_text("🕘 Your latest saves. Tap one to read it:", reply_markup=_list_markup(items))


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
        await update.message.reply_text("Which one? Reply to that save and say \"delete\", or say \"delete the last one\".")
        return
    title = await asyncio.to_thread(db.soft_delete, int(arg))
    await update.message.reply_text(f"🗑 Deleted “{title}”" if title else "I couldn't find that save.",
                                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                                        "↩ Undo", callback_data=f"u:{arg}")]]) if title else None)


async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if len(args) < 2 or not args[0].lstrip("#").isdigit():
        await update.message.reply_text(
            "Write the number and the correction, like:\n/fix 12 He said 50 conversations, not 100")
        return
    save_id, text = int(args[0].lstrip("#")), " ".join(args[1:])
    row = await asyncio.to_thread(db.add_correction, save_id, text)
    if not row:
        await update.message.reply_text("I couldn't find that save.")
        return
    title, topic, summary, points = row
    vec = await asyncio.to_thread(brain.embed, f"{title}\nTopic: {topic}\n{summary}\n" + "\n".join(points))
    await asyncio.to_thread(db.set_embedding, save_id, vec)
    await _index(save_id)
    await update.message.reply_text(f"✏️ Fixed “{title}”. Your correction is now part of the note.")


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    n = await asyncio.to_thread(db.retry_all)
    if not n:
        await update.message.reply_text("Nothing to retry. 👍")
        return
    await update.message.reply_text(f"🔁 Trying {n} link(s) again. You'll see 👌 on each one when it's saved.")
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
        await q.message.reply_text(_details_text(row), disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗑 Delete", callback_data=f"x:{row[0]}")]]))  # replying to it = about this save
    else:
        await q.message.reply_text("That save was deleted.")


async def on_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🗑 = deleted in ONE tap, with ↩ Undo (kept hidden for a day, then removed for good). Old confirm buttons
    (xx/k/kk) still work."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    action, sid = q.data.split(":")
    save_id = int(sid)
    kb = q.message.reply_markup.inline_keyboard if q.message.reply_markup else []
    if action in ("k", "kk"):
        await q.answer("Kept 👍")
        await q.edit_message_reply_markup(_details_button(save_id))
        return
    title = await asyncio.to_thread(db.soft_delete, save_id)
    await q.answer("🗑 Deleted" if title else "Already deleted")
    undo = InlineKeyboardMarkup([[InlineKeyboardButton("↩ Undo", callback_data=f"u:{save_id}")]])
    in_list = len(kb) > 1 and all(len(r) == 2 and r[1].text == "🗑" for r in kb)
    if in_list:  # a list of saves: take that row out, and confirm with Undo underneath
        rest = [r for r in kb if r[1].callback_data != f"x:{save_id}"]
        await q.edit_message_reply_markup(InlineKeyboardMarkup(rest) if rest else None)
        await q.message.reply_text(f"🗑 Deleted “{title or 'that save'}”", reply_markup=undo, disable_notification=True)
    else:
        await q.edit_message_text(f"🗑 Deleted “{title or 'that save'}”", reply_markup=undo)


async def on_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    save_id = int(q.data.split(":")[1])
    title = await asyncio.to_thread(db.restore, save_id)
    await q.answer("↩ Restored" if title else "Too late to undo, sorry")
    if title:
        await q.edit_message_text(f"↩ Back: “{title}”", reply_markup=_details_button(save_id))
    else:
        await q.edit_message_reply_markup(None)


async def on_save_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """⋯ More (m:), ✅ It worked (wk:), ❓ Ask about it (aq:)."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    action, sid = q.data.split(":")
    save_id = int(sid)
    row = await asyncio.to_thread(db.get, save_id)
    if not row:
        await q.answer("That save was deleted.")
        return
    if action == "m":
        await q.answer()
        await q.edit_message_reply_markup(_details_button(save_id, more=True, kind=row[2]))
    elif action == "wk":
        await asyncio.to_thread(db.mark_proven, save_id)
        await q.answer("✅ Marked as proven. It'll come first in answers.")
        await q.edit_message_reply_markup(_details_button(save_id))
    elif action == "aq":
        await q.answer()
        _awaiting["ask"] = save_id
        await q.message.reply_text(f"❓ What do you want to know about “{row[4]}”? Type it or send a voice note.")


async def on_next_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-tap next steps: 🔊 Listen (ls:<answer>), 🎭 Practice this (pr:<answer>), 🔁 Try again (ag:<answer>),
    🔁 Practice again (pa:0), 🔊 recap (lr:0), 🔁 retry a failed save (rt:<save>), 📎 I got it (ga:<save>)."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    action, arg = q.data.split(":")
    info = _answers.get(int(arg)) if action in ("ls", "pr", "ag") else None
    if action in ("ls", "pr", "ag") and not info:
        await q.answer("That's an old message. Please ask again.")
        return
    if action == "ls":
        await q.answer("🔊 Recording it…")
        await _voice_answer(info["q"], info["text"], q.message.message_id)
    elif action == "pr":
        await q.answer()
        await start_practice(q.message, info["q"])
    elif action == "ag":
        await q.answer()
        await q.edit_message_text("🔎 Trying again…")
        await _send_answer(q.message, info["q"], info["only_id"])
    elif action == "pa":
        await q.answer()
        if _last_practice.get("situation"):
            await start_practice(q.message, _last_practice["situation"])
        else:
            await q.message.reply_text("What do you want to practice? Just describe it.")
            _awaiting["practice"] = True
    elif action == "lr":
        recap = _last_recap.get("text") or (q.message.text or "")
        if not recap.strip():
            await q.answer("Please ask for a new recap first.")
            return
        await q.answer("🔊 Recording it…")
        await _send_voice(await asyncio.to_thread(brain.voice_script, "my weekly recap", recap), q.message.message_id)
    elif action == "rt":
        await asyncio.to_thread(db.retry_one, int(arg))
        await q.answer("🔁 Trying again. You'll see 👌 when it's saved.")
        await q.edit_message_reply_markup(None)
        _kick_queue(context)
    elif action == "ga":
        await q.answer()
        _awaiting["attach"] = int(arg)
        await q.message.reply_text("📎 Send it here: the link, the text, or a screenshot of the DM.")


async def on_setting_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔕 Pause reminders / 🔔 back on (rem:off|on), 🔇 Text only / 🎙 Voice again (vo:off|auto)."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    kind, value = q.data.split(":")
    key = "reminders" if kind == "rem" else "voice"
    await asyncio.to_thread(db.set_setting, key, value)
    if kind == "rem":
        flip = ("🔔 Turn reminders back on", "rem:on") if value == "off" else ("🔕 Pause reminders", "rem:off")
        await q.answer("🔕 Daily reminders paused" if value == "off" else "🔔 Daily reminders are back on")
    else:
        flip = ("🎙 Voice replies again", "vo:auto") if value == "off" else ("🔇 Text only", "vo:off")
        await q.answer("🔇 Text only from now on" if value == "off" else "🎙 Voice question = voice reply")
    kb = [r for r in (q.message.reply_markup.inline_keyboard if q.message.reply_markup else [])
          if not any((b.callback_data or "").startswith(f"{kind}:") for b in r)]
    await q.edit_message_reply_markup(InlineKeyboardMarkup(kb + [[InlineKeyboardButton(flip[0], callback_data=flip[1])]]))


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
        return "That save was deleted."
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
        return "That save was deleted."
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
        lines.append("\nCouldn't find it online, so I've put it on your to-get list.\n"
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
    status = await q.message.reply_text(f"⏳ Looking for the {label}… (up to a minute)")
    try:
        text = await (screen_text_for(save_id) if action == "st" else resource_for(save_id))
        got = InlineKeyboardMarkup([[InlineKeyboardButton("📎 I got it, add it", callback_data=f"ga:{save_id}")]]) \
            if "to-get list" in text else None
        await status.edit_text(_short(text), disable_web_page_preview=True, reply_markup=got)
    except Exception as e:
        log.exception("%s failed for #%s", label, save_id)
        await status.edit_text(_friendly_error(e))


async def attach_to(reply_to, save_id, text=None, image=None):
    """Arun replied to a save with a link / forwarded DM / screenshot: add it to that save."""
    status = await reply_to.reply_text("⏳ Adding it to that save…")
    try:
        if image:
            data, mime = image
            text = await asyncio.to_thread(brain.image_text, data, mime)
        else:
            text = await asyncio.to_thread(brain.read_attachment, text)
        updated = await asyncio.to_thread(db.append_extra, save_id, "Added by Arun", text,
                                          "📎 Added: " + " ".join(text.split())[:120])
        if not updated:
            await status.edit_text("That save was deleted.")
            return
        await _reembed(updated)
        done = await asyncio.to_thread(db.finish_toget, save_id)
        await status.edit_text(f"📎 Added to “{updated[1]}”" + ("\n✅ Ticked off your /toget list." if done else ""))
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


# ---------- "Just talk": plain words instead of commands ----------

class _Upd:  # lets the old /command handlers run from plain words
    def __init__(self, msg):
        self.message = msg


class _Ctx:
    def __init__(self, context, args=()):
        self.args = list(args)
        self.job_queue = getattr(context, "job_queue", None)
        self.bot = getattr(context, "bot", None)


async def _resolve(target, reply_save=None):
    """Which save does he mean? A reply, "#12", "the last one", or words like "the one about cold calling"."""
    if reply_save:
        return reply_save
    t = (target or "").strip().lower()
    m = re.search(r"#?(\d+)", t)
    if m and (t.startswith("#") or t.isdigit()):
        return int(m.group(1))
    if not t or t in ("last", "the last one", "latest", "last one", "that", "this", "it"):
        rows = await asyncio.to_thread(db.recent, 1)
        return rows[0][0] if rows else None
    rows = await asyncio.to_thread(memory.search, t, 1)
    return rows[0][0] if rows else None


async def handle_words(msg, context, text, reply_save=None, by_voice=False):
    """Understand what he wants from plain words (or a voice note), then do it."""
    try:
        intent = await asyncio.to_thread(brain.classify, text)
    except Exception:
        log.exception("couldn't classify; treating as a question")
        intent = brain.Intent(action="ask", target="", text=text)
    a, payload = intent.action, (intent.text or "").strip()
    u, c = _Upd(msg), _Ctx(context)
    log.info("words -> %s", a)

    if a == "ask":
        await respond(msg, text, only_id=reply_save, by_voice=by_voice)
    elif a == "save_note":
        await save_text(msg, payload or text)
    elif a in ("delete", "worked", "screen_text", "resource"):
        save_id = await _resolve(intent.target, reply_save)
        row = save_id and await asyncio.to_thread(db.get, save_id)
        if not row:
            await msg.reply_text("I couldn't tell which save you mean. Reply to that save (or the link you shared) and say it again.")
        elif a == "delete":  # one step, with Undo (no "are you sure?")
            title = await asyncio.to_thread(db.soft_delete, save_id)
            await msg.reply_text(f"🗑 Deleted “{title or row[4]}”", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Undo", callback_data=f"u:{save_id}")]]))
        elif a == "worked":
            await asyncio.to_thread(db.mark_proven, save_id)
            await msg.reply_text(f"✅ Nice! Marked “{row[4]}” as proven by you. "
                                 f"It now comes first in answers and playbooks.")
        else:
            status = await msg.reply_text("⏳ On it… (up to a minute)")
            try:
                out = await (screen_text_for(save_id) if a == "screen_text" else resource_for(save_id))
                got = InlineKeyboardMarkup([[InlineKeyboardButton("📎 I got it, add it", callback_data=f"ga:{save_id}")]]) \
                    if "to-get list" in out else None
                await status.edit_text(_short(out), disable_web_page_preview=True, reply_markup=got)
            except Exception as e:
                log.exception("%s failed", a)
                await status.edit_text(_friendly_error(e))
    elif a == "find":
        await cmd_find(u, _Ctx(context, (payload or text).split()))
    elif a == "recent":
        await cmd_recent(u, c)
    elif a == "topics":
        await cmd_topics(u, c)
    elif a == "reminders":
        await cmd_reminders(u, _Ctx(context, ["off" if "off" in (payload + text).lower() else "on"]))
    elif a == "profile":
        await asyncio.to_thread(db.set_setting, "profile", (payload or text)[:2000])
        await msg.reply_text("✅ Got it. Every answer will now take this into account.")
    elif a == "recap":
        await cmd_recap(u, c)
    elif a == "practice":
        await start_practice(msg, payload or text)
    elif a == "voice_replies":
        p = (payload + " " + text).lower()
        mode = "off" if ("off" in p or "stop" in p or "don't" in p) else "on" if ("always" in p or " on" in p) else "auto"
        await asyncio.to_thread(db.set_setting, "voice", mode)
        await msg.reply_text({"on": "🔊 I'll answer every question by voice too.", "off": "🔇 Voice replies off. Text only.",
                              "auto": "🎙 I'll reply by voice when you ask by voice."}[mode])
    elif a == "playbook":
        await make_playbook(msg, payload)
    elif a == "review":
        await send_review_card(msg.get_bot(), "d")
    elif a == "mentors":
        await show_mentors(msg)
    elif a == "backup":
        await cmd_backup(u, c)
    elif a == "export":
        await cmd_export(u, c)
    else:
        await cmd_start(u, c)


# ---------- Practice mode: rehearse a real conversation; the mentors coach between lines ----------

_practice = {}  # {"active", "situation", "notes", "history", "at"}
_last_practice = {}  # the situation of the last finished practice, for 🔁 Practice again
PRACTICE_IDLE = 30 * 60  # a session left alone for 30 minutes ends by itself


def _end_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏁 Finish & score me", callback_data="pe:0")]])


async def start_practice(msg, situation):
    if not situation or len(situation) < 8:
        await msg.reply_text("What do you want to practice? e.g. \"practice asking my boss for a raise\" or "
                             "\"rehearse a price negotiation with a client\".")
        return
    status = await msg.reply_text("🎭 Setting up your practice…")
    try:
        profile = await asyncio.to_thread(db.get_setting, "profile", "")
        rows, moves, _ = await asyncio.to_thread(memory.mentor_context, situation, 6, 10, profile)
        notes = brain.moves_block(moves) if moves else brain.notes_for(rows)  # the coach judges by these moves
        _practice.clear()
        _practice.update(active=True, situation=situation, notes=notes, history=[], at=time.time(), profile=profile)
        first = await asyncio.to_thread(brain.practice_turn, situation, notes, [], profile)
        _practice["history"].append(("bot", first))
        await status.edit_text(_short(f"🎭 Practice: {situation}\nReply as yourself (text or voice). "
                                      f"Say \"stop\" or tap 🏁 when done.\n\n{first}"), reply_markup=_end_button())
    except Exception as e:
        log.exception("practice start failed")
        _practice.clear()
        await status.edit_text(_friendly_error(e))


async def practice_reply(msg, text, by_voice=False):
    if time.time() - _practice.get("at", 0) > PRACTICE_IDLE:
        _practice.clear()
        await handle_words(msg, None, text)
        return
    _practice["history"].append(("arun", text))
    _practice["at"] = time.time()
    status = await msg.reply_text("🎭 …")
    try:
        turn = await asyncio.to_thread(brain.practice_turn, _practice["situation"], _practice["notes"],
                                       _practice["history"], _practice.get("profile", ""))
        _practice["history"].append(("bot", turn))
        await status.edit_text(_short(turn), reply_markup=_end_button())
        if await _want_voice(by_voice):  # the other person SPEAKS their line, in a different voice; coach stays text
            line = turn.split("💡")[0].strip().strip('"')
            asyncio.create_task(_send_voice(line, status.message_id, voice=brain.OTHER_VOICE,
                                            style="Say this naturally, like a real person in a live conversation"))
    except Exception as e:
        log.exception("practice turn failed")
        await status.edit_text(_friendly_error(e))


async def end_practice(msg):
    if not _practice.get("active"):
        await msg.reply_text("There's no practice going on. Say e.g. \"practice asking my boss for a raise\" to start one.")
        return
    status = await msg.reply_text("🏁 Scoring your practice…")
    try:
        out = await asyncio.to_thread(brain.practice_debrief, _practice["situation"], _practice["notes"],
                                      _practice["history"])
        out += _voice_feedback(_practice.get("voice", []))
        _last_practice["situation"] = _practice["situation"]
        await status.edit_text(_short(f"🏁 Practice debrief\n\n{out}"), reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔁 Practice again", callback_data="pa:0")]]))
    except Exception as e:
        log.exception("debrief failed")
        await status.edit_text(_friendly_error(e))
    finally:
        _practice.clear()


FILLERS = ("um", "uh", "umm", "like", "basically", "actually", "you know", "i mean", "literally")


def _voice_feedback(lines):
    """How he SPOKE in a voice practice (like Yoodli): pace and filler words, counted from his voice notes."""
    words = sum(len(t.split()) for t, _ in lines)
    secs = sum(d for _, d in lines)
    if not lines or words < 15 or secs < 5:
        return ""
    counts = {f: len(re.findall(rf"\b{f}\b", " ".join(t.lower() for t, _ in lines))) for f in FILLERS}
    top = sorted(((n, f) for f, n in counts.items() if n), reverse=True)[:3]
    fillers = sum(counts.values())
    wpm = round(words / secs * 60)
    pace = ("a bit fast: slow down, pauses sound confident" if wpm > 170 else
            "a bit slow: keep it moving" if wpm < 110 else "a good, calm pace")
    out = f"\n\n🎙 Your voice: {wpm} words a minute ({pace})."
    if top:
        out += (f"\nFiller words: {fillers} (" + ", ".join(f'"{f}" ×{n}' for n, f in top) + "). "
                + ("Replace them with a short pause." if fillers * 100 / words > 3 else "Very few, nice."))
    return out


async def on_practice_end(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    await q.edit_message_reply_markup(None)
    await end_practice(q.message)


async def cmd_practice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_practice(update.message, " ".join(context.args))


# ---------- Living playbooks ----------

async def make_playbook(msg, topic):
    topic = (topic or "").strip().strip(".").lower()
    if not topic:
        have = await asyncio.to_thread(db.list_playbooks)
        topics = await asyncio.to_thread(db.topic_counts)
        names = list(dict.fromkeys([t for t, *_ in have] + [t for t, n in topics if n >= 3]))[:10]
        if not names:
            await msg.reply_text("📘 Save a few more things on one topic first (3 or more), then I can turn them into a playbook.")
            return
        buttons = [InlineKeyboardButton(f"📘 {n.title()}", callback_data="pb:" + _fit_bytes(n, 60)) for n in names]
        await msg.reply_text("📘 Which topic should I turn into one clear guide?",
                             reply_markup=InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)]))
        return
    status = await msg.reply_text(f"📘 Writing your {topic} playbook from everything you saved… (about a minute)")
    try:
        vec = await asyncio.to_thread(brain.embed, topic, True)
        rows = await asyncio.to_thread(db.playbook_rows, topic, vec)
        if len(rows) < 3:
            await status.edit_text(f"You have only {len(rows)} save(s) about {topic}. Save a few more first.")
            return
        profile = await asyncio.to_thread(db.get_setting, "profile", "")
        text = await asyncio.to_thread(brain.build_playbook, topic, rows, profile)
        await asyncio.to_thread(db.save_playbook, topic, text, len(rows))
        top = re.search(r"top 5 rules.*", text, re.I | re.S)
        await status.edit_text(_short(f"📘 Your {topic.title()} Playbook is ready, built from {len(rows)} saves.\n\n" +
                                      (re.sub(r"[#*]", "", top.group(0)).strip() if top else "") +
                                      "\n\nThe full playbook is in the file below. Say \"update my "
                                      f"{topic} playbook\" any time you've saved more."))
        buf = io.BytesIO(text.encode("utf-8"))
        buf.name = f"{topic.title()} Playbook.md"
        await msg.reply_document(buf, caption=f"📘 {topic.title()} Playbook ({len(rows)} saves)",
                                 reply_markup=InlineKeyboardMarkup([[
                                     InlineKeyboardButton("🔄 Update it", callback_data="pb:" + _fit_bytes(topic, 60)),
                                     InlineKeyboardButton("📘 Another topic", callback_data="ex:playbook")]]))
    except Exception as e:
        log.exception("playbook failed")
        await status.edit_text(_friendly_error(e))


async def on_playbook_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    await q.edit_message_reply_markup(None)
    await make_playbook(q.message, q.data[3:])


async def cmd_playbook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await make_playbook(update.message, " ".join(context.args))


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
        await status.edit_text(f"✅ Backed up. All {n[2]} saves are safe." if n is not None else
                               "✅ Backup file below. (GitHub backup isn't set up yet.)")
    except Exception as e:
        log.exception("manual backup failed")
        await status.edit_text("❌ The backup didn't work this time. I'll try again tonight automatically.")


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
    """The 2-minute daily review (like Readwise's daily review): 3 moves come back as quick cards, picked by
    spaced repetition (due ones first, then new ones closest to his goal)."""
    try:
        if await asyncio.to_thread(db.get_setting, "reminders", "on") == "off":
            return
        streak = await _streak()
        await context.bot.send_message(OWNER, f"🧠 Your 2-minute review: {REVIEW_CARDS} moves for your goal."
                                       + (f"\n🔥 Streak: {streak} days" if streak > 1 else ""),
                                       reply_markup=InlineKeyboardMarkup([
                                           [InlineKeyboardButton("▶️ Start", callback_data="rv:go:0:d")],
                                           [InlineKeyboardButton("🔕 Pause reminders", callback_data="rem:off")]]))
    except Exception:
        log.exception("daily review failed")


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


# ---------- Daily review + quiz: a move comes back as a question, he thinks, then taps to see it ----------

REVIEW_CARDS = 3
_goal_vec = {}  # date -> embedding of his profile (the goal new cards are picked for)


def _today():
    return dt.datetime.now(IST).date()


async def _streak():
    last = await asyncio.to_thread(db.get_setting, "review_last", "")
    n = int(await asyncio.to_thread(db.get_setting, "review_streak", "0") or 0)
    alive = last in (str(_today()), str(_today() - dt.timedelta(days=1)))
    return n if alive else 0


async def _bump_streak():
    today = str(_today())
    last = await asyncio.to_thread(db.get_setting, "review_last", "")
    if last == today:
        return await _streak()
    n = (await _streak()) + 1
    await asyncio.to_thread(db.set_setting, "review_streak", str(n))
    await asyncio.to_thread(db.set_setting, "review_last", today)
    return n


async def send_review_card(bot, where, after_move=None):
    """where: "d" = daily review, or a save id = quiz on that one save."""
    if where == "d":
        done = await asyncio.to_thread(db.reviewed_today)
        if done >= REVIEW_CARDS:
            streak = await _bump_streak()
            await bot.send_message(OWNER, f"✅ Done for today!" + (f" 🔥 {streak}-day streak." if streak > 1 else "")
                                   + "\nSmall reps every day is how it sticks.")
            return
        day = str(_today())
        if day not in _goal_vec:
            profile = await asyncio.to_thread(db.get_setting, "profile", "")
            _goal_vec.clear()
            _goal_vec[day] = await asyncio.to_thread(brain.embed, profile, True) if profile else None
        m = await asyncio.to_thread(db.next_review_move, _goal_vec[day])
        label = f"Card {done + 1}/{REVIEW_CARDS}"
    else:
        m = await asyncio.to_thread(db.next_review_move, None, int(where), after_move)
        label = "Quiz"
    if not m:
        await bot.send_message(OWNER, "✅ That's all of them!" if where != "d" else "Nothing to review yet. Save a few "
                               "videos first.")
        return
    await bot.send_message(OWNER, f"🧠 {label}\n\n{m[1]}\n\nWhat would you do or say? Think first, then tap.",
                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                               "👀 Show the move", callback_data=f"rv:show:{m[11]}:{where}")]]))


async def on_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """rv:go:0:<where> start · rv:show:<move>:<where> reveal · rv:ok|no:<move>:<where> knew it / not yet."""
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    _, act, move_id, where = q.data.split(":")
    move_id = int(move_id)
    if act == "go":
        await q.edit_message_reply_markup(None)
        await send_review_card(context.bot, where)
    elif act == "show":
        m = await asyncio.to_thread(db.move, move_id)
        if not m:
            await q.edit_message_text("That move was removed. Tap ▶️ again for the next one.")
            return
        text = q.message.text.replace("\n\nWhat would you do or say? Think first, then tap.", "")
        text += f"\n\n✅ {m[2]}"
        if m[3]:
            text += f'\n💬 "{m[3]}"'
        if m[4]:
            text += f"\n💡 {m[4]}"
        text += f"\n— {m[12] + ', ' if m[12] and m[12] not in m[7] else ''}{m[7]}"
        await q.edit_message_text(_short(text), reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Knew it", callback_data=f"rv:ok:{move_id}:{where}"),
            InlineKeyboardButton("🔁 Not yet", callback_data=f"rv:no:{move_id}:{where}")]]))
    else:
        await asyncio.to_thread(db.review_mark, move_id, act == "ok")
        await q.edit_message_reply_markup(None)
        await send_review_card(context.bot, where, after_move=move_id)


# ---------- Journey: the next evening, "how did it go?" ----------

FOLLOWUP_HOURS = (10, 21)  # India time: no "how did it go?" at night


def _followup_time():
    """Tomorrow 7 PM India time: after he's had the day to try it."""
    now = dt.datetime.now(IST)
    return (now + dt.timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)


async def followups(context: ContextTypes.DEFAULT_TYPE):
    """Every 30 minutes, 10 AM to 9 PM India time: ask about one past situation whose time has come."""
    try:
        if not FOLLOWUP_HOURS[0] <= dt.datetime.now(IST).hour < FOLLOWUP_HOURS[1]:
            return
        for jid, at, situation in await asyncio.to_thread(db.due_followups, 1):
            sent = await context.bot.send_message(
                OWNER, f"🧭 On {at.astimezone(IST):%a} you asked me about:\n“{situation}”\n\nHow did it go?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Worked", callback_data=f"fu:{jid}:w"),
                     InlineKeyboardButton("😐 So-so", callback_data=f"fu:{jid}:m"),
                     InlineKeyboardButton("❌ Didn't work", callback_data=f"fu:{jid}:n")],
                    [InlineKeyboardButton("⏭ Not yet", callback_data=f"fu:{jid}:s"),
                     InlineKeyboardButton("🔕 Don't ask", callback_data=f"fu:{jid}:x")]]))
            # if he doesn't answer, ask once more two days later; after that, let it go
            again = None if at < dt.datetime.now(IST) - dt.timedelta(days=2) else _followup_time() + dt.timedelta(days=1)
            await asyncio.to_thread(db.set_followup, jid, sent.message_id, again)
    except Exception:
        log.exception("follow-up failed")


async def on_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    _, jid, choice = q.data.split(":")
    jid = int(jid)
    if choice == "s":
        await asyncio.to_thread(db.set_followup, jid, q.message.message_id, _followup_time() + dt.timedelta(days=1))
        await q.edit_message_text(q.message.text + "\n\n⏭ OK, I'll ask again in a couple of days.")
        return
    if choice == "x":
        await asyncio.to_thread(db.set_followup, jid, q.message.message_id, None)
        await q.edit_message_text(q.message.text + "\n\n🔕 OK, I won't ask about this one again.")
        return
    result = {"w": 1, "m": 0, "n": -1}[choice]
    save_ids = await asyncio.to_thread(db.set_result, jid, result)
    if result == 1:
        for sid in save_ids:
            await asyncio.to_thread(db.mark_proven, sid)  # moves that worked come first next time
    reply = {1: "✅ Nice! I'll suggest what worked first next time.",
             0: "😐 Noted. Next time I'll build on what half-worked.",
             -1: "❌ Thanks for being honest. Next time I'll try a different move."}[result]
    await q.edit_message_text(q.message.text + f"\n\n{reply}\nWant to add what happened? Reply to this message "
                                               f"(optional, text or voice).")


# ---------- Named mentors ----------

async def show_mentors(msg):
    names = await asyncio.to_thread(db.creators, 12)
    if not names:
        await msg.reply_text("I don't know the names of your mentors yet. Save a few more videos.")
        return
    lines = "\n".join(f"• {n} ({c} save{'s' if c > 1 else ''})" for n, c in names)
    _awaiting.pop("mentor", None)
    await msg.reply_text(f"👥 The mentors you learn from:\n{lines}\n\nTap one to ask what they'd tell you:",
                         reply_markup=InlineKeyboardMarkup(
                             [[InlineKeyboardButton(f"💬 {n}"[:40], callback_data=_fit_bytes(f"mn:{n}", 64))]
                              for n, _ in names[:6]]))


async def on_mentor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER:
        return
    await q.answer()
    name = q.data.split(":", 1)[1]
    _awaiting["mentor"] = name
    await q.message.reply_text(f"💬 What do you want to ask {name}? Type or say your situation.")


_last_recap = {}


def _listen_recap():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔊 Listen", callback_data="lr:0")]])


async def build_recap():
    rows = await asyncio.to_thread(db.since, 7)
    throwback = await asyncio.to_thread(db.random_older, 7)
    journal = await asyncio.to_thread(db.journal_since, 7)
    if not rows and not journal:
        msg = "🧠 Sunday recap: you didn't save anything this week. Share one useful video today 🙂"
        if throwback:
            msg += f"\n\n🔁 From your past: #{throwback[0]} {throwback[4]}\n{throwback[6]}"
        return msg
    return await asyncio.to_thread(brain.weekly_recap, rows, throwback, journal)


async def weekly_recap(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = await build_recap()
        _last_recap["text"] = text
        await context.bot.send_message(OWNER, _short(text), disable_web_page_preview=True, reply_markup=_listen_recap())
        log.info("weekly recap sent")
    except Exception:
        log.exception("weekly recap failed")


async def cmd_recap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.message.reply_text("⏳ Making your recap of the last 7 days…")
    try:
        _last_recap["text"] = await build_recap()
        await status.edit_text(_short(_last_recap["text"]), disable_web_page_preview=True, reply_markup=_listen_recap())
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
        await asyncio.to_thread(db.purge_deleted)
        n = await asyncio.to_thread(memory.index_missing)
        if n:
            log.info("indexed %s saves that had no search chunks", n)
        n = await asyncio.to_thread(memory.moves_missing, 20)
        if n:
            log.info("made mentor moves for %s saves that had none", n)
    except Exception:
        log.exception("daily database ping failed")


async def set_menu(app):
    # Steve Jobs rule: no commands to learn. The menu shows only 4; everything else is plain words (old commands
    # still work if typed). Also keep the bot handle so background jobs can react with emoji.
    global _bot
    _bot = app.bot
    await app.bot.set_my_commands([
        ("practice", "Rehearse a conversation, e.g. a negotiation"),
        ("playbook", "Turn your saves on a topic into one guide"),
        ("me", "Tell me about yourself"),
        ("help", "What I can do"),
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
    app.add_handler(CommandHandler("practice", cmd_practice, filters=me))
    app.add_handler(CommandHandler("playbook", cmd_playbook, filters=me))
    app.add_handler(CallbackQueryHandler(on_practice_end, pattern=r"^pe:0$"))
    app.add_handler(CallbackQueryHandler(on_example, pattern=r"^ex:\w+$"))
    app.add_handler(CallbackQueryHandler(on_playbook_pick, pattern=r"^pb:"))
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
    app.add_handler(CallbackQueryHandler(on_undo, pattern=r"^u:\d+$"))
    app.add_handler(CallbackQueryHandler(on_save_button, pattern=r"^(m|wk|aq):\d+$"))
    app.add_handler(CallbackQueryHandler(on_setting_button, pattern=r"^(rem|vo):\w+$"))
    app.add_handler(CallbackQueryHandler(on_next_step, pattern=r"^(ls|pr|ag|pa|lr|rt|ga):\d+$"))
    app.add_handler(CallbackQueryHandler(on_save_or_ask, pattern=r"^(sv|ak):\d+$"))
    app.add_handler(CallbackQueryHandler(on_topic, pattern=r"^t:"))
    app.add_handler(CallbackQueryHandler(on_review, pattern=r"^rv:(go|show|ok|no):\d+:(d|\d+)$"))
    app.add_handler(CallbackQueryHandler(on_followup, pattern=r"^fu:\d+:[wmnsx]$"))
    app.add_handler(CallbackQueryHandler(on_mentor, pattern=r"^mn:"))
    app.add_handler(MessageHandler(~me, ignore_strangers))

    app.job_queue.run_daily(daily_ping, time=dt.time(hour=3, minute=0))
    app.job_queue.run_once(daily_ping, when=5)
    app.job_queue.run_repeating(queue_tick, interval=10 * 60, first=30)
    app.job_queue.run_repeating(followups, interval=30 * 60, first=120)  # "how did it go?" (10 AM - 9 PM only)
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
