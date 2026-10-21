# Second Brain: status

**DONE: online 24/7 and connected to ChatGPT** (Oct 21, 2026)

```
Phone/laptop → Telegram bot ┐
ChatGPT / Claude (MCP)      ┼→ Render server (second-brain-xbgr.onrender.com) → Gemini + Supabase memory
```

## How to use it

| You want to… | Do this |
|---|---|
| Save a YouTube video or Short | Share it to **@arun_brain_bot** in Telegram |
| Save a screenshot | Send the image to the bot |
| Save your own thought | `/note …` in the bot |
| Ask in Telegram | Type your question to the bot (Gemini answers) |
| Ask in ChatGPT (PC or phone) | New chat → **+** → turn on **Second Brain** → ask (ChatGPT answers from your notes) |
| See or back up your saves | `/recent`, `/export` in the bot |
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
1. Instagram reels, X and Reddit links
3. Personal Claude account → add the same connector
