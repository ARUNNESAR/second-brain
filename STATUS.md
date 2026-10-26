# Second Brain: status

**DONE: online 24/7 and connected to ChatGPT** (Oct 21, 2026)

```
Phone/laptop → Telegram bot ┐
ChatGPT / Claude (MCP)      ┼→ Render server (second-brain-xbgr.onrender.com) → Gemini + Supabase memory
```

## How to use it (no commands needed: just talk, by text or voice)

| You want to… | Do this |
|---|---|
| Save anything | Share it to **@arun_brain_bot**. It reacts 👀 while reading, 👌 when saved (🤔 = blocked, retries by itself; 😢 = couldn't, it tells you why) |
| Get help in a situation | Describe it ("client says we're too expensive"). Your saved mentors answer. 👎 = try harder |
| Rehearse a conversation | "practice asking my boss for a raise" → it plays the other person and coaches you; "stop" for a score |
| One guide per topic | "make my sales playbook" → one merged guide (also readable by Claude/ChatGPT) |
| Find / delete / list | "what did I save about pricing", "delete the last one", "show my latest saves" |
| A tip worked | "that twisted-words tip worked for me" → it's marked ✅ proven and comes first |
| Ask about one save | Reply to your shared link (or the bot's message) with your question |
| Add the DM resource | Reply to the save with the link / screenshot you received |
| About you | /me + a few lines (tailors every answer) |
| Ask in Claude / ChatGPT | Turn on **Second Brain**; it searches first and can read your playbooks |
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
