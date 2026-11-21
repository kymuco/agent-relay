# AgentRelay

Minimal, self-hosted Telegram-to-[CWA](https://github.com/kymuco/chatgpt-web-adapter) bridge. Each owner runs one local host and connects it to **their own existing ChatGPT conversation**. No central server, separate memory database, Telegram webhook, or browser extension.

> **Experimental:** CWA is an unofficial integration using a private, changing consumer ChatGPT protocol. It is not an OpenAI-supported API and may be incompatible with provider terms; review the applicable conditions before use.

## Quick start — Windows, one file

Prerequisites: **Python 3.10–3.14** on PATH and internet access for the first installation. On Windows 10/11, `curl.exe` is normally preinstalled. You do **not** need to install the CWA browser extension, run an auth wizard, or install Node.js system-wide.

```powershell
git clone -b feat/telegram-cwa-mvp https://github.com/kymuco/agent-relay.git
cd agent-relay
.\start.cmd
```

Or simply double-click **`start.cmd`** in the project folder. It calls `bootstrap.py`, which:

1. Creates `.venv` and installs packages from `requirements.txt` (only re-installs when requirements change or the package is missing).
2. Uses an existing Node.js 24+ if available. Otherwise downloads **portable official Node.js 24** to `.runtime/node.exe` (Windows x64/ARM64), checks the archive against the official SHA-256 manifest, and adds it to `PATH` **only for this process**. No administrator rights or machine-wide Node installation.
3. Copies `.env.example` to `.env` **only if missing**, then stops so you can fill it in. It never overwrites your bot settings.
4. Requires your existing local `auth_data.json` (manual authorization) and runs `agent_relay.py`. Nothing reads Chrome cookies or logs your session data.

On subsequent launches, double-click `start.cmd` again: it reuses `.venv`, `.runtime/node.exe`, `.env`, and `auth_data.json`. Setup-only mode: `start.cmd --setup-only` (does not launch the bot or require credentials). The Node runtime is still used internally by CWA, but installation is automated and private to AgentRelay.

If you prefer **manual** setup, this also remains supported:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Fill .env and place auth_data.json; requires Node.js 24+ on PATH
.\.venv\Scripts\python.exe agent_relay.py
```

Create your own Telegram bot with [@BotFather](https://t.me/BotFather), add it to **one private group**, then fill `.env`:

- `TELEGRAM_BOT_TOKEN` — your bot token, stored locally.
- `TELEGRAM_CHAT_ID` — negative numeric group ID.
- `ALLOWED_USER_IDS` — *numeric* Telegram user IDs allowed to invoke the bot (typically you and your friend).
- `CWA_CONVERSATION_ID` — ID from your own existing `https://chatgpt.com/c/<id>` conversation. For Project chats, extract the last `/c/<id>` component.
- Optionally set both `PEER_BOT_USERNAMES` and `PEER_BOT_IDS` for your friend's bot. Otherwise **no other bots** can invoke it.

**Authorization:** visit `https://chatgpt.com/api/auth/session` **in your already signed-in browser**, save the complete returned JSON locally as `auth_data.json` next to `agent_relay.py`. This contains credentials. Do not send it to friends, Telegram, GitHub, or issues; do not paste it into this chat. CWA's auth parser supports this raw session JSON (`accessToken` and `sessionToken`), so there is no conversion step. A standalone GET outside your authenticated browser may return an unauthenticated response. You can also choose a previously created CWA `auth_data.json` and point `CWA_AUTH_FILE` at it.

Then rerun **`start.cmd`** to launch the bot. If you deliberately chose manual setup and already have Node.js 24+ on PATH, you can instead run:

```powershell
.\.venv\Scripts\python.exe agent_relay.py
```

The bot now responds only in the allowlisted Telegram group, to `@YourBot`, human Reply, or a configured `BOT_ALIASES` name from allowlisted users. It ignores **all private messages**, anonymous senders, other groups, and strangers in your group. Each person runs the same program with their own `.env` and `auth_data.json`. The bootstrap script never authenticates automatically; it only installs dependencies and starts the bot.

### Bot-to-bot

Telegram's bot-to-bot group delivery depends on BotFather configuration (Bot-to-Bot Mode). For text `@mentions`, privacy mode may also need adjustment; check Telegram's current bot settings. On **both PCs**, configure the other bot's numeric ID **and** username as an allowlisted peer. Never exchange tokens or session files. Peer bots activate via explicit `@mention` or `/relay@Peer` command, and chains stop after one bot-to-bot hop.

### Group awareness and controlled bot-to-bot handoff (v0.2)

- Ordinary text from configured `ALLOWED_USER_IDS` and verified peer bots is retained in an **in-memory buffer** (default 30 messages) when Telegram delivers it, even without an @mention. No ChatGPT request is made for an ordinary unaddressed message. The buffer is sent to your **own selected ChatGPT conversation** as untrusted quoted context only on the next authorized invocation; both users should consent to sharing group context with both ChatGPT accounts.
- Human messages still require `@BotUsername`, a Reply, or optional `BOT_ALIASES`. Automatic spontaneous responses to casual chat are intentionally **off**.
- The AI can deliberately hand the floor to a configured peer by including `[[handoff:PeerBotUsername]]` anywhere in its answer. AgentRelay validates that the username is allowlisted, removes the marker, and adds a Telegram `/relay@PeerBotUsername` command mention, which Telegram delivers under bot-to-bot command rules. A leading `@PeerBotUsername` in a human-triggered model reply is also normalized to that command. **A response to another bot cannot hand off again**, so there is no automatic ping-pong.
- Configure `PEER_BOT_USERNAMES` **and** `PEER_BOT_IDS` on both PCs; in BotFather enable Bot-to-Bot Communication Mode and disable Group Privacy for group-context delivery. Telegram delivery is still subject to server-side rules.
- Example: human says `Рагна, обсуди с Mirri эту идею`; Ragna may reply `Мой аргумент... [[handoff:MirriMmannnBot]]`, and the relay emits a message starting with `/relay@MirriMmannnBot`. Mirri can reply once, then stops.

### Security & limits

- **Default deny:** one `TELEGRAM_CHAT_ID`, explicit human IDs, explicit peer bot IDs/usernames, plus `@mention`/Reply. This is enforced **before** a request goes to CWA. Public Telegram bot profiles and private `/start` messages do not grant model access.
- Both `.env` and `auth_data.json` are ignored by Git. Verify `git status` before pushing. The existing ChatGPT conversation may contain **private history** that could leak into group replies: choose the conversation consciously. Keep the group private.
- First-run Telegram backlog is skipped. Each processed update is recorded before attempting CWA to prevent automatic replay of ambiguous writes; a crash at that moment can lose an update. No automatic CWA retries, file uploads, auto-responses to ambient chat, or autonomous conversations.
- Allowed unaddressed group text is buffered locally (last `GROUP_CONTEXT_LIMIT=30` entries by default) and passed to ChatGPT **only when an addressed turn occurs**. Group history is in RAM, truncated per message, and lost at restart. For a more natural call, set `BOT_ALIASES=Ragna,Рагна` on the owner PC and `BOT_ALIASES=Mirri,Мирри` on the peer PC. Disable this context with `GROUP_CONTEXT_LIMIT=0`. Model context-window limits still apply even though ChatGPT retains the chat history.
- Telegram has a 4096 UTF-16-unit text limit; replies are split into safe plain-text chunks. The program requires connectivity and a running PC. Full live E2E testing still requires each owner's credentials and bot token.

### Why Node.js is still used

CWA `BrowserlessHTTPClient` executes a JavaScript protocol module under **Node.js 24+**. `start.cmd` can download a portable Node binary automatically, so no system-wide installation is required. This is **not** a Node-free CWA transport. Node is never downloaded from Telegram or ChatGPT; the archive is retrieved from the official Node.js distribution host and checked against its published SHA-256 checksum. A truly Node-free version would require separate CWA engineering.

Offline tests (no credentials used):

```powershell
python -m unittest discover -s tests -v
```

MIT licensed. CWA, Telegram, and ChatGPT are separate products.
