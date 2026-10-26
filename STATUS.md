# Second Brain: status

**DONE: online 24/7 and connected to ChatGPT** (Oct 21, 2026)

```
Phone/laptop → Telegram bot ┐
ChatGPT / Claude (MCP)      ┼→ Render server (second-brain-xbgr.onrender.com) → Gemini + Supabase memory
```

## How to use it

| You want to… | Do this |
|---|---|
| Save a YouTube video, Instagram reel, X post or LinkedIn post | Share it to **@arun_brain_bot** in Telegram |
| Copy prompts/text shown in a video | Tap 📝 Screen text on the save (or type `full` with the link) |
| Get a "comment X and I'll DM you" resource | Tap 🎁 Get resource. When you get the DM, reply to the save with the link/screenshot. `/toget` = still to collect |
| A note got something wrong | `/fix 12 the correct thing` |
| Save screenshots / voice note / video file / PDF | Send it to the bot (several screenshots together = one note) |
| Forwarded message | Forward it to the bot and it gets saved |
| Find or delete an old save | `/find words` or `/topics`, then tap 📖 or 🗑 |
| Save your own thought | `/note …` in the bot |
| Ask in Telegram | Type your question to the bot (Gemini answers). Follow-ups like `why?` work. |
| Ask about ONE save | Reply to its `✅ Saved #12` message with your question |
| Make answers fit you | `/me` + a few lines about yourself |
| Check it remembers | `/memtest` (also runs by itself every Sunday) |
| Pause daily reminders | `/reminders off` (`/reminders on` to restart) |
| Ask in ChatGPT (PC or phone) | New chat → **+** → turn on **Second Brain** → ask (ChatGPT answers from your notes) |
| See or back up your saves | `/recent`, `/export` in the bot |
| Daily reminder | One old save every morning 9 AM (Mon-Sat) |
| Weekly recap | Comes by itself **every Sunday 9 AM** in Telegram. `/recap` gets it any time. |

The PC does **not** need to be on anymore. **Don't run `start-bot.bat`.** It's only for offline testing, and two copies of the bot fight over your messages.

## Where things live

| Thing | Where |
|---|---|
| Code | `D:\SecondBrain` + private GitHub `ARUNNESAR/second-brain` |
| Running server | Render (free) → service `second-brain`, Singapore |
| Memory | Supabase project `second-brain` (Seoul) |
| Secret keys | `D:\SecondBrain\.env` (and Render → Environment). Never share them. |
| Connector address | `D:\SecondBrain\MY-CONNECTOR-ADDRESS.txt`. Works like a password. |
| Keep-awake | Supabase pings the server every 10 min (`keepalive.sql`) |

## Add it to another AI later (personal Claude, etc.)
Use Settings → Connectors (or Plugins) → add a custom MCP server → paste the address from `MY-CONNECTOR-ADDRESS.txt` → Authentication: none. Nothing else needs rebuilding.

## NEXT (ideas)
1. Reddit links
2. Personal Claude account → add the same connector
