# AgentRelay

Minimal, self-hosted Telegram-to-CWA bridge. Each person runs **one local process** with their own Telegram bot, authenticated CWA session, and **existing ChatGPT conversation ID**. No central server, webhook, vector database, or dedicated Telegram-history database.

> **Experimental.** [CWA](https://github.com/kymuco/chatgpt-web-adapter) uses an unofficial ChatGPT consumer-web transport; it may break or be incompatible with provider terms. Review applicable service terms before using. This is not an OpenAI-supported integration. Nothing is sent to ChatGPT unless the bot is addressed.

## v0.1 behavior

- Human `@YourBot ...` messages, `/ask@YourBot ...` or direct **Reply** to the bot trigger one CWA `send(..., conversation=EXISTING_ID)` call.
- Peer bots may trigger each other **only by explicitly mentioning** `@YourBot`, and only if listed in `PEER_BOT_USERNAMES`. Their replies cannot trigger another bot (one-hop guard). Bot-to-bot traffic depends on Telegram's settings and should be tested live.
- On first launch, stale updates are skipped. Only one allowed Telegram group ID is accepted. The local `update_id` cursor is recorded **before** CWA transmission to prevent duplicate model sends after a crash or timeout. A crash in that interval may drop one event; ambiguous requests are never automatically retried.
- The selected ChatGPT conversation retains its usual history. **Unaddressed Telegram messages are not copied into ChatGPT**; a Reply includes a short quote for context. ChatGPT may not actively retain all early turns of a very long conversation.
- Pure text messages only; text is posted as plain Telegram text, split at safe UTF-16 boundaries. No attachment forwarding, no spontaneous initiation, no live-verified end-to-end test yet.

## Requirements

Python 3.10–3.14, Node.js 24+, system curl, one Telegram bot token, and an authenticated CWA installation.

## Set up (Windows PowerShell)

1. In Telegram, create **one bot per owner** with [@BotFather](https://t.me/BotFather). Add both to your private group. In BotFather enable **Bot-to-Bot Communication Mode** for each. For peer `@mentions` (rather than explicit `/ask@Bot` commands), disable **Group Privacy Mode** for each bot and re-add it to the group if necessary. This lets bots see group messages; AgentRelay itself still filters by mention/reply. Telegram documentation: [Bot-to-Bot Communication](https://core.telegram.org/bots/features#bot-to-bot-communication).
2. Find the group's numeric chat ID (typically starts with `-100`); you can query the Telegram Bot API's `getUpdates` after posting a message, or use a trusted group ID lookup. Do not share bot tokens.
3. Clone and configure:

```powershell
git clone https://github.com/kymuco/agent-relay.git
cd agent-relay
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install "chatgpt-web-adapter[browser]>=0.4.1,<0.5"
Copy-Item .env.example .env
notepad .env
```

4. Fill `TELEGRAM_BOT_TOKEN`, negative `TELEGRAM_CHAT_ID`, `CWA_CONVERSATION_ID` (the ID from an **existing** `chatgpt.com/c/<id>` conversation), `CWA_AUTH_FILE` (path to your own local CWA auth file), and `PEER_BOT_USERNAMES` (the other bot's Telegram username). To obtain authentication locally, follow the [CWA quickstart](https://github.com/kymuco/chatgpt-web-adapter/blob/main/docs/quickstart.md), for example `chatgpt-web-adapter auth login --auth-file auth_data.json`. Never commit cookies, auth JSON, or a real `.env`.
5. Start one local host on each person's PC:

```powershell
python agent_relay.py
```

6. In the **same private Telegram group**, say `@YourBot hello`. Test human-to-bot first. Then ask one assistant to explicitly mention `@OtherBot` and confirm bot-to-bot delivery. If it doesn't work, try `/ask@OtherBot ...`, check BotFather settings, permissions, and app logs. There is no remote desktop/service deployment needed; hosts only work while running.

You and your friend each keep your **own** `.env`, `auth_data.json`, conversation ID, and cursor. Do not reuse a Telegram bot token on multiple simultaneous host instances (long polling conflicts).

## Trust and limitations

**Using a personal existing ChatGPT conversation in a shared group is a privacy decision.** The model may draw on that private history when producing *public group replies*. Use a dedicated conversation if that is unacceptable. Keep the Telegram group private. Bot messages and member-supplied prompts are untrusted content, not instructions granting access to files or other accounts.

Group-privacy disabled exposes all group messages to the running bot token (but only addressed messages are forwarded to CWA). The cursor file tracks Telegram delivery only; it does not persist messages. Disable the local process to take your bot offline. No automatic retry is made after a potentially submitted CWA turn. Telegram send failures after a CWA success may leave the answer in ChatGPT without a Telegram post.

Run offline tests:

```powershell
python -m unittest discover -s tests -v
```

## Design

```text
Telegram group <-> Telegram Bot API long polling <-> local AgentRelay <-> CWA <-> existing ChatGPT chat
                                                          (each person runs their own copy)
```

MIT licensed. CWA and Telegram are separate projects.
