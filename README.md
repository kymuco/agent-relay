# AgentRelay

Minimal, self-hosted Telegram-to-[CWA](https://github.com/kymuco/chatgpt-web-adapter) bridge. Each owner runs one local host and connects it to **their own existing ChatGPT conversation**. No central server, separate memory database, Telegram webhook, or browser extension.

> **Experimental:** CWA is an unofficial integration using a private, changing consumer ChatGPT protocol. It is not an OpenAI-supported API and may be incompatible with provider terms; review the applicable conditions before use.

## Quick start — Windows: one file

Prerequisites: **Python 3.10–3.14** on PATH and internet access for the first installation. On Windows 10/11, `curl.exe` is normally preinstalled. You do **not** need a CWA browser extension, automatic auth wizard, or a system-wide Node.js installation.

```powershell
git clone -b feat/telegram-cwa-mvp https://github.com/kymuco/agent-relay.git
cd agent-relay
.\start.cmd
```

Double-click **`start.cmd`** instead if you prefer. It calls `bootstrap.py`, which:

1. Creates an isolated `.venv` and installs Python packages from `requirements.txt` (reuses them on later launches).
2. Uses an existing Node.js 24+ if available; otherwise downloads the **official portable Node.js 24** to `.runtime/node.exe` for Windows x64/ARM64, verifying the archive against the official SHA-256 manifest. No admin privileges or machine-wide installation; PATH changes apply only to the running process.
3. Copies `.env.example` to `.env` only if missing, then stops to let you fill in the settings; it never overwrites them.
4. Requires your manually saved local `auth_data.json` and launches `agent_relay.py` only after the configuration files are present. It never extracts browser cookies or prints session credentials.

On later launches, double-click `start.cmd` again. **`start.cmd --setup-only`** installs dependencies without starting the bot or requiring any credentials. Node.js is still used *internally by CWA*; it just does not require a separate installation.

Manual setup remains an option:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Fill .env and put auth_data.json in this folder; Node.js 24+ must be on PATH
.\.venv\Scripts\python.exe agent_relay.py
```

Create your own Telegram bot with [@BotFather](https://t.me/BotFather), add it to **one private group**, then fill `.env`:

- `TELEGRAM_BOT_TOKEN` — your bot token, stored locally.
- `TELEGRAM_CHAT_ID` — negative numeric group ID.
- `ALLOWED_USER_IDS` — *numeric* Telegram user IDs allowed to invoke the bot (typically you and your friend).
- `CWA_CONVERSATION_ID` — ID from your own existing `https://chatgpt.com/c/<id>` conversation. For Project chats, extract the last `/c/<id>` component.
- Optionally set both `PEER_BOT_USERNAMES` and `PEER_BOT_IDS` for your friend's bot. Otherwise **no other bots** can invoke it.

**Authorization:** visit `https://chatgpt.com/api/auth/session` **in your already signed-in browser**, save the complete returned JSON locally as `auth_data.json` next to `agent_relay.py`. This contains credentials. Do not send it to friends, Telegram, GitHub, or issues; do not paste it into this chat. CWA's auth parser supports this raw session JSON (`accessToken` and `sessionToken`), so there is no conversion step. A standalone GET outside your authenticated browser may return an unauthenticated response. You can also choose a previously created CWA `auth_data.json` and point `CWA_AUTH_FILE` at it.

Then rerun **`start.cmd`**. If you deliberately use manual setup and already have Node.js 24+ on PATH, run:

```powershell
.\.venv\Scripts\python.exe agent_relay.py
```

The bot now responds only in the allowlisted Telegram group, to `@YourBot` or human Reply from allowlisted users. It ignores **all private messages**, anonymous senders, other groups, and strangers in your group. Each person runs the same program with their own `.env` and `auth_data.json`. The bootstrap installs dependencies and launches the bot, but never authenticates automatically.

### Bot-to-bot

Telegram's bot-to-bot group delivery depends on BotFather configuration (Bot-to-Bot Mode). For text `@mentions`, privacy mode may also need adjustment; check Telegram's current bot settings. On **both PCs**, configure the other bot's numeric ID **and** username as an allowlisted peer. Never exchange tokens or session files. Peer bots activate only via explicit `@mention` and chains stop after one bot-to-bot hop.

### Security & limits

- **Default deny:** one `TELEGRAM_CHAT_ID`, explicit human IDs, explicit peer bot IDs/usernames, plus `@mention`/Reply. This is enforced **before** a request goes to CWA. Public Telegram bot profiles and private `/start` messages do not grant model access.
- Both `.env` and `auth_data.json` are ignored by Git. Verify `git status` before pushing. The existing ChatGPT conversation may contain **private history** that could leak into group replies: choose the conversation consciously. Keep the group private.
- First-run Telegram backlog is skipped. Each processed update is recorded before attempting CWA to prevent automatic replay of ambiguous writes; a crash at that moment can lose an update. No automatic CWA retries, file uploads, auto-responses to ambient chat, or autonomous conversations.
- Unaddressed group messages are **not** imported into ChatGPT. Model context-window limits still apply even though ChatGPT retains the chat history.
- Telegram has a 4096 UTF-16-unit text limit; replies are split into safe plain-text chunks. The program requires connectivity and a running PC. Full live E2E testing still requires each owner's credentials and bot token.

### Why Node.js is still used

CWA `BrowserlessHTTPClient` evaluates a JavaScript protocol module with **Node.js 24+**. The bootstrap downloads a portable Node executable from the official distribution host with SHA-256 verification when needed. It is **not** a genuinely Node-free transport, but it removes the requirement to install Node globally. A genuinely Node-free version would require changes to CWA.


Offline tests (no credentials used):

```powershell
python -m unittest discover -s tests -v
```

MIT licensed. CWA, Telegram, and ChatGPT are separate products.
