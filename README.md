# AgentRelay

Minimal, self-hosted Telegram-to-[CWA](https://github.com/kymuco/chatgpt-web-adapter) bridge. Each owner runs one local host and connects it to **their own existing ChatGPT conversation**. No central server, separate memory database, Telegram webhook, or browser extension.

> **Experimental:** CWA is an unofficial integration using a private, changing consumer ChatGPT protocol. It is not an OpenAI-supported API and may be incompatible with provider terms; review the applicable conditions before use.

## Quick start — manual authorization, no launcher

Prerequisites: **Python 3.10–3.14**, **Node.js 24+**, and `curl` on PATH (the latter two are required by **CWA Direct HTTP**, not by Telegram). No Chromium or browser extension needed when `auth_data.json` already exists.

```powershell
git clone -b feat/telegram-cwa-mvp https://github.com/kymuco/agent-relay.git
cd agent-relay
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Create your own Telegram bot with [@BotFather](https://t.me/BotFather), add it to **one private group**, then fill `.env`:

- `TELEGRAM_BOT_TOKEN` — your bot token, stored locally.
- `TELEGRAM_CHAT_ID` — negative numeric group ID.
- `ALLOWED_USER_IDS` — *numeric* Telegram user IDs allowed to invoke the bot (typically you and your friend).
- `CWA_CONVERSATION_ID` — ID from your own existing `https://chatgpt.com/c/<id>` conversation. For Project chats, extract the last `/c/<id>` component.
- Optionally set both `PEER_BOT_USERNAMES` and `PEER_BOT_IDS` for your friend's bot. Otherwise **no other bots** can invoke it.

**Authorization:** visit `https://chatgpt.com/api/auth/session` **in your already signed-in browser**, save the complete returned JSON locally as `auth_data.json` next to `agent_relay.py`. This contains credentials. Do not send it to friends, Telegram, GitHub, or issues; do not paste it into this chat. CWA's auth parser supports this raw session JSON (`accessToken` and `sessionToken`), so there is no conversion step. A standalone GET outside your authenticated browser may return an unauthenticated response. You can also choose a previously created CWA `auth_data.json` and point `CWA_AUTH_FILE` at it.

Then run directly:

```powershell
python agent_relay.py
```

The bot now responds only in the allowlisted Telegram group, to `@YourBot` or human Reply from allowlisted users. It ignores **all private messages**, anonymous senders, other groups, and strangers in your group. Each person runs the same program with their own `.env` and `auth_data.json`. No launcher or auto-authentication step runs.

### Bot-to-bot

Telegram's bot-to-bot group delivery depends on BotFather configuration (Bot-to-Bot Mode). For text `@mentions`, privacy mode may also need adjustment; check Telegram's current bot settings. On **both PCs**, configure the other bot's numeric ID **and** username as an allowlisted peer. Never exchange tokens or session files. Peer bots activate only via explicit `@mention` and chains stop after one bot-to-bot hop.

### Security & limits

- **Default deny:** one `TELEGRAM_CHAT_ID`, explicit human IDs, explicit peer bot IDs/usernames, plus `@mention`/Reply. This is enforced **before** a request goes to CWA. Public Telegram bot profiles and private `/start` messages do not grant model access.
- Both `.env` and `auth_data.json` are ignored by Git. Verify `git status` before pushing. The existing ChatGPT conversation may contain **private history** that could leak into group replies: choose the conversation consciously. Keep the group private.
- First-run Telegram backlog is skipped. Each processed update is recorded before attempting CWA to prevent automatic replay of ambiguous writes; a crash at that moment can lose an update. No automatic CWA retries, file uploads, auto-responses to ambient chat, or autonomous conversations.
- Unaddressed group messages are **not** imported into ChatGPT. Model context-window limits still apply even though ChatGPT retains the chat history.
- Telegram has a 4096 UTF-16-unit text limit; replies are split into safe plain-text chunks. The program requires connectivity and a running PC. Full live E2E testing still requires each owner's credentials and bot token.

### Why Node.js is still required

Current CWA `BrowserlessHTTPClient` directly calls a **Node.js 24+** runtime to evaluate its reviewed JavaScript protocol module. Removing the Node check does not remove that runtime requirement. A genuinely Node-free version would require a new CWA transport, which is **not implemented** in this release. This minimal/manual setup removes **Chromium, browser extension, launcher, and separate memory infrastructure**, not Node itself.

Offline tests (no credentials used):

```powershell
python -m unittest discover -s tests -v
```

MIT licensed. CWA, Telegram, and ChatGPT are separate products.
