# Second Brain: morning guide

**DONE: Step 1 is built, tested and running.** All 8 steps finished at 12:40 AM.

| # | Step | Status |
|---|---|---|
| 1 | Check all 4 keys work | DONE |
| 2 | Create the memory table in Supabase | DONE |
| 3 | Write the bot code | DONE |
| 4 | Test: TED talk, Short, screenshot, questions | DONE, passed |
| 5 | Test: 1-hour video, phone share links, videos without captions | DONE, passed (1-hour video in 41s) |
| 6 | Test every bot command with fake messages | DONE, passed |
| 7 | Clean test data, start the bot | DONE, memory is empty and ready for you |
| 8 | This guide | DONE |

---

## NOW: your 2-minute morning test

1. **If the PC was off:** double-click `D:\SecondBrain\start-bot.bat`. A black window opens. **Keep it open** (minimise it), because closing it stops the bot.
2. On your phone, open YouTube, pick any useful video, tap **Share**, then **Telegram**, then **@arun_brain_bot**.
3. Wait for `✅ Saved #1 ...` (10–60 seconds).
4. Type a question about that video, like *"what was the main advice?"*
5. Tell me in chat if it worked.

---

## How to use it (phone or laptop, same thing)

| You want to… | Do this |
|---|---|
| Save a YouTube video or Short | Share it to @arun_brain_bot. You can type a comment with it too. |
| Save a screenshot | Send the image to the bot. Add a caption if you like. |
| Save your own thought | `/note always ask the budget first` |
| Ask something | Type a normal question. The answer comes from your saves, with sources. |
| See your last saves | `/recent` |
| Download everything | `/export` sends a file with all your notes, which is your own backup |
| Remove a save | `/delete 12` (the number comes from /recent) |
| A video failed to save | `/retry` |

Tap the **/** button in the bot chat to see all the commands.

**Instagram, X and Reddit links:** the bot keeps the link but can't read it yet (that's step 2). For now, **send a screenshot** of the post and it saves what it says.

---

## What's inside (so you know your own system)

```
Phone/laptop → Telegram → bot.py (runs on your PC) → Gemini (understands) → Supabase (memory)
```

| File | What it is |
|---|---|
| `bot.py` | The bot: receives your messages and replies |
| `brain.py` | Gemini part: watches videos, reads screenshots, writes notes, answers |
| `db.py` + `schema.sql` | Memory part: saves notes to Supabase and searches them by meaning |
| `.env` | Your 4 secret keys. **Never share this file.** |
| `start-bot.bat` | Double-click to start the bot |
| `logs\bot.log` | What the bot did. Useful if something breaks. |

**How a YouTube video gets saved:** it first tries the video's captions (fast and cheap). If there are none, or YouTube blocks the request, Gemini watches the video itself. Both routes work.

**How an answer is found:** your question is turned into numbers (an "embedding"), Supabase finds the 6 closest notes by meaning, and Gemini answers using only those notes, citing them as [1], [2].

---

## Good to know

- **The bot only works while the PC is on and that black window is open.** Tomorrow we put it online (Render, free) so it runs 24/7. That needs your GitHub login, about 10 minutes together.
- **Only you can use it.** It ignores messages from anyone else.
- **Free limits:** Gemini has a daily free limit (plenty for you). If it's ever hit, the bot says so and it resets the next day. Supabase holds about 20,000+ saves. The bot pings the database daily so it never pauses.
- **If Gemini is busy,** the bot automatically tries backup models.
- **YouTube caption blocking:** during testing, YouTube temporarily blocked caption downloads from your home internet. That's normal after many requests, and it clears by itself. It doesn't affect your normal YouTube use, and the bot just uses Gemini to watch instead.

## NEXT (after you've used it for a few days)

1. Put it online (Render), so it works when the PC is off.
2. Instagram reels, X and Reddit links.
3. Sunday weekly recap.
4. Connect it to Claude and ChatGPT.
