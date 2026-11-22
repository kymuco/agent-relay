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


### Ongoing bot-to-bot Reply dialogue (opt-in)

New in v0.3: to let two separate assistants keep replying to each other, both
owners set \`PEER_REPLY_MODE=on\` and optionally
\`PEER_REPLY_WINDOW_SECONDS=900\` in their existing \`.env\`. A configured,
verified peer initiates with \`/relay@TargetBot\`; then the assistants continue
using **Telegram's native Reply** on each other's messages. **/reply is not a
Telegram command.** An addressed human message can trigger an initial
\`[[handoff:PeerBot]]\` marker (or a leading \`@PeerBot\`) to start a session.
A bot's message without a direct Reply or addressed /relay command is ignored.

There is no fixed **message-count** limit, but an interaction has a fixed
wall-clock window (15 minutes by default, 60–7200 seconds supported) and
does not persist across program restarts. To stop, a permitted human types
\`/stoprelay\` in the shared group; both running bots should see this with Group
Privacy off. The responding model may end the dialogue with
\`[[stop_dialogue]]\`. Session expiry and CWA request failure stop further Reply
routing; no automatic retry of ambiguous conversation writes.

When a reply contains multiple Telegram chunks, only its last chunk is sent as a
direct Reply, preventing multiple parallel triggers. This is experimental;
check both bot logs and agree on sharing chat messages with both ChatGPT accounts.
Using GPT-6 High can generate long runs of substantial latency and usage.


### Telegram multimodal bridge (experimental v0.4)

- Images: allowed humans can send photo or image-document; their bytes are passed to
  the existing CWA conversation. Image generation is requested by normal prompts
  ("Рагна, нарисуй ..."), and ready generated images are retrieved through CWA's
  canonical \`get_generated_images\` and verified \`handoff_generated_artifact\`.
  If generation is not ready, send \`/images@YourBotUsername\` later in the same
  running process. No fabricated public image URLs.
- Files: PDF (extractable text, max 80 pages), DOCX, UTF-8 TXT/MD/PY/JSON/CSV/
  JS/TS and other listed source files are read as untrusted document content in
  the model prompt. Scanned PDFs require OCR and are rejected with an explanation.
  CWA-generated sandbox file links are sent back using CWA's verified artifact
  handoff, restricted to filenames/extensions and a 10 MB limit.
- Voice: a Telegram voice message is transcribed by the supplied
  \`cwa_dictate.py\` via the ChatGPT dictation endpoint. Responses to voice
  messages are spoken via supplied \`cwa_tts.py\` (Read Aloud AAC), then converted
  to OGG/Opus using FFmpeg for Telegram \`sendVoice\`. Text responses are also
  retained. To request a voice reply to text, send \`@YourBot /voice расскажи ...\`.
  \`VOICE_REPLY_MODE=off|on_voice|always\` controls automatic speech.
- Formatting: a restricted safe Markdown-to-Telegram-HTML renderer supports
  bold, italics, code blocks, inline code, blockquotes, headings and HTTP links;
  escaping prevents arbitrary model-written HTML from being sent as markup.

Set \`MEDIA_ENABLED=on\`, \`MEDIA_AUTO_REPLY=on\`, \`VOICE_REPLY_MODE=on_voice\`
and \`MAX_MEDIA_MB=10\` in each owner's \`.env\`; group and user allowlists
still apply. CWA module files are included in the repository for reproducibility.
Audio processing requires FFmpeg (Windows installation example:
\`winget install Gyan.FFmpeg\` and restart the PowerShell session).
No new browser extension is required. These ChatGPT endpoints are unofficial and
may change without notice. Inbound documents are sent to the owner's ChatGPT
conversation; users should agree on group sharing and avoid sending secrets.
The local isolated tests mock external requests; successful live Telegram/CWA
image generation, voice and PDF handling remain to be verified by each owner.

### Fix: continuous peer Reply without typed /reply

When `PEER_REPLY_MODE=on`, **either** a verified peer's `/relay@Bot`
command **or** its ordinary `@Bot` mention starts a native Telegram Reply
session. Replies are sent with Bot API `reply_parameters.message_id`; there
is no literal `/reply` command in the message body. Subsequent direct
Replies within the session are accepted without another @mention. The
existing session timeout, `/stoprelay`, actor allowlists and
`[[stop_dialogue]]` behavior still apply.

### Literal /relay@Bot handoff on every active peer turn

For active bot-to-bot conversations (`PEER_REPLY_MODE=on` on both hosts),
AgentRelay now **writes a real visible** `/relay@PeerBot` command at the beginning
of each outgoing bot reply. This is not left to the language model and is
not the literal `/reply` command. Native Telegram Reply metadata is also
attached. When the answer is split into chunks, only the final chunk is
directed to the peer to prevent multiple independent replies.

For example: Ragna sends `/relay@mirrimmannnbot 3/6, твой ход`,
Mirri sends `/relay@ragnacobot 4/6, продолжаем`. Verified bot identity,
conversation timeout, allowed chat, `/stoprelay` and
`[[stop_dialogue]]` still govern routing. With `PEER_REPLY_MODE=off`, no
automatic peer command is inserted.

### Unsolicited GIFs, MP4s, and other unsupported media

Telegram often sends animated GIFs as MP4 documents. AgentRelay now filters
unsupported documents, animations, videos, stickers, and audio from metadata
before download or CWA submission. In regular group chat these are **ignored
silently**, even with `MEDIA_AUTO_REPLY=on`. An explicitly addressed unsupported
attachment receives one explanatory reply instead of an exception.

To prevent auto-replies to ordinary photos, voice and supported documents too,
set `MEDIA_AUTO_REPLY=off` in the existing local `.env` and restart.
The shipped `.env.example` now defaults to `off`; it never overwrites
an existing `.env`.

### Natural spoken answers: /voice and /both (v0.5)

- With `VOICE_REPLY_MODE=on_voice`, `@YourBot /voice Привет` sends **only a Telegram voice message**. `@YourBot /both Привет` sends the **text and voice**. A regular addressed text message remains text-only.
- A Telegram voice input (when accepted by the ordinary addressed-message or `MEDIA_AUTO_REPLY` gate) produces **voice only** by default. If `MEDIA_AUTO_REPLY=off`, voice inputs still need an addressed caption or a Reply to the bot; it is not an ambient listener.
- With `VOICE_REPLY_MODE=always`, ordinary human turns remain text+voice (prior behavior); explicit `/voice` requests voice-only. `VOICE_REPLY_MODE=off` disables voice synthesis, including explicit requests.
- Commands can also target the bot: `/voice@YourBotUsername` and `/both@YourBotUsername`. In a group with two bots, a bare `/voice` does not automatically address either bot. This prevents unintended double replies.
- For speech delivery the model receives **per-turn guidance** to use the listener's language, natural spoken sentences, brief responses, and no Markdown, URLs, code fences, emojis, or delivery markers. The model's generated text stays in the private CWA conversation but is **not sent to Telegram** when voice-only TTS succeeds.
- If TTS synthesis or Telegram voice delivery fails, voice-only mode falls back to **one text answer**. No extra CWA send or automatic retry of uncertain voice delivery occurs.
- Bot-to-bot `/relay@Peer` traffic remains text-only even if `VOICE_REPLY_MODE=always`.

### Reply to someone else's photo or GIF

An authorized human can address the bot in a **text reply** to a previous
Telegram photo, image document, or GIF/MP4 animation:
`@ragnacobot Что происходит на этом GIF?`. AgentRelay reads one
`reply_to_message` level and attaches the quoted photo to CWA. GIF and MP4
replies yield up to three small JPEG frames sampled from the first six seconds
through FFmpeg. Frame samples are **not video understanding**: no audio,
unseen motion, or events outside the sampled seconds are available.
All downloads retain the 10 MB limit and safe Telegram-domain handling.
Unaddressed replies remain silent even with `MEDIA_AUTO_REPLY=on`; ordinary
unaddressed GIFs continue to be ignored. GIF support requires FFmpeg on the
host. If Telegram omits media identifiers in the quoted message, the bot
cannot retrieve those bytes.

### Emergency /stop and /resume (Ragna and Mirri independently)

Send `/stop@ragnacobot` to stop Ragna or `/stop@MirriMmannnBot`
to stop Mirri. Send corresponding `/resume@...` to resume; bare `/stop`
and `/resume` are understood by **each bot** in the group. Only allowlisted
human Telegram IDs can control the bot; peer bots and model output cannot.
Control commands are never sent to ChatGPT. They bypass per-turn cooldown
and are accepted while a separate worker is waiting on CWA/TTS. An in-flight
CWA submission cannot be reliably cancelled, but its response is invalidated:
no new Telegram text, voice, image, or document should be sent from that
generation after the stop has been processed. Network sends already underway
cannot be recalled or guaranteed to be cancelled. Work queued before stop
is invalidated even if resume happens quickly.

Stop state is persisted in `CONTROL_FILE=.agent-relay-control.json` and
survives process restart. The stopped bot discards new requests until resume.
Unlike `/stoprelay` (which only closes a bot-to-bot session), `/stop`
disables all AI responses and aborts session continuity.

### Smart Catch-up after offline downtime

With `CATCHUP_MODE=smart` (default), a previously launched bot drains
Telegram updates waiting at startup **without calling CWA for each old
mention**. It gathers up to 100 recent trusted text/caption messages from
the selected group (within `CATCHUP_MAX_AGE_MINUTES=180`), including subsequent
human and peer messages to determine whether earlier questions were resolved.
It requests exactly one read-only *recovery review* in its existing CWA
conversation, and sends **at most one** plain Telegram reply if the model
returns a valid `{"decision":"reply","text":"..."}` JSON object.
If it returns `{"decision":"silent","text":""}`, it stays silent.
The reply never adds `/relay@` or a handoff marker, so no automatic old
bot-to-bot conversation is restarted.

Historical commands and action requests are redacted from the review prompt,
and are NEVER replayed as actions. Old attachments are not downloaded.
A model instruction asks for zero tools, but CWA Direct HTTP does not
provide a hard no-tools switch; do not rely on this text-only instruction
as a security boundary for connected external tools. If this matters to
your setup, set `CATCHUP_MODE=off` until you have a tool-disabled
read-only model path. An emergency `/stop` or `/resume` sent while
offline is applied silently in chronological order before catch-up.
A first-ever launch retains the old protective behavior (skip backlog).
`CATCHUP_MODE=off` also drains and skips old messages instead of replaying
all past mentions, but it does not summarize them. Newly arriving messages
still receive normal responses.

A human can send `/catchup@YourBot` to retry the stored startup review
if it produced no reply (the in-memory snapshot disappears at restart).
New messages arriving **after** startup are processed normally. Telegram
may not deliver every group message, depending on privacy settings.
Only retained Telegram Bot API updates can be reviewed; full historical
group chat backfill is not available.

### Fast daily startup (no start.cmd, no double Ctrl+C)

After your initial setup, launch AgentRelay directly from PowerShell:

```powershell
cd W:\dev\agent-relay
.\.venv\Scripts\python.exe .\agent_relay.py
```

With an active `(.venv)`, `python agent_relay.py` works too. The new
`.\run.ps1` does the same single-process launch, provided your PowerShell
script policy allows local scripts. If in doubt, use the direct Python command
above: no batch process, no CMD `Terminate batch job? [Y/N]` prompt.
The bot now adds the previously installed `.runtime/node.exe` to PATH itself.
No bootstrap, dependency check, or Node version probe runs at every start.

`Ctrl+C` stops the bot with a single clean shutdown line. Active pending
worker output is invalidated without persisting a Telegram `/stop` state.
`start.cmd --setup-only` remains the initial setup and update command.
Legacy `start.cmd` still exists but may show Windows batch termination prompts.

#### Logs

```ini
LOG_LEVEL=normal
LOG_FILE=logs/agentrelay.log
```

At `normal` the console reports start, stop, catch-up summary, emergency
pauses, and important failures — **not every reply or update**. Use `debug`
temporarily for update IDs and per-reply diagnostics; `quiet` shows just
warnings/errors. The diagnostic file is rotated (three files of up to
512 KiB each), logging event metadata but never Telegram message bodies,
CWA prompts, or credentials. Set `LOG_FILE=off` to disable file logging.

### Ragna + local Codex CLI (experimental, opt-in)

AgentRelay supports explicit, authorized delegation from Ragna to Codex CLI.
Only allowed humans in the configured Telegram group can start local work.
Ordinary messages, peer bots, historical catch-up, and model output cannot start work.

1. Install and sign into the official Codex CLI on this PC; check: `codex --version`.
2. Prepare a separate, disposable or trusted Git checkout with NO `.env`,
   `auth_data.json`, private keys, credentials or private files.
3. Add to AgentRelay's LOCAL `.env` (do not commit this file):

    WORK_ENABLED=on
    WORK_PROJECTS=demo=W:\dev\agentrelay-codex-sandbox
    WORK_STATE_FILE=.agent-relay-work.json
    WORK_TIMEOUT_SECONDS=600
    WORK_MAX_ROUNDS=2

4. Start Ragna with `python agent_relay.py`.
5. In the Telegram group, as an allowed human:

    /work@ragnacobot start demo Найди один небольшой баг и исправь его, затем запусти тесты.

Use `/work@ragnacobot status` to inspect current work and
`/work@ragnacobot stop` to interrupt it. The global `/stop@ragnacobot`
also cancels local work. `/resume` never automatically replays stopped work.

Ragna uses her EXISTING CWA ChatGPT conversation to plan and review each
Codex iteration. Codex receives only bounded delegation text and is
launched non-interactively with `exec --json --sandbox workspace-write`
and `--ask-for-approval never`. At most two coding iterations are permitted
by default. Codex work changes files in the selected local workspace;
there is NO automatic git push, commit, merge, or system-wide shell API.
Inspect git diff before accepting changes.

Security: The operator-selected workspace is a routing allowlist, not
an OS security boundary. Verify Windows Codex sandbox enforcement on your
installed Codex version. Codex prompts forbid network publishing, touching
secrets, and leaving the workspace, but text alone cannot enforce that.
Existing CWA chats may also have live connected tools; the Gateway cannot
hard-disable those ChatGPT connectors by prompting alone. Never opt in
for a checkout containing secrets or for a machine session with excessive
rights. Child Codex processes do NOT inherit AgentRelay's Telegram and CWA
environment variables, and a CMD npm launcher is bypassed for safer argv.

Work status is saved to `.agent-relay-work.json` and never automatically
replayed after an interruption/restart. CWA and Codex work is serialized
through one worker; `/work status`, `/work stop` and `/stop` bypass it.
Mocked CI tests cover this flow; live Codex + Telegram testing is still required.

#### Separate local-execution authority

WORK_ALLOWED_USER_IDS is REQUIRED when WORK_ENABLED=on. Set it to only
your own Telegram numeric user ID. These IDs must also appear in
ALLOWED_USER_IDS, but the two fields are intentionally independent:
someone allowed to chat with Ragna is not automatically allowed to
modify your local files. Unauthorized /work commands are ignored.

Example in local .env: WORK_ALLOWED_USER_IDS=123456789

#### Python test runner and work timings

On Windows, Codex's own shell may not resolve `python` or `py`, even when
AgentRelay is started in an activated `.venv`. The gateway now supplies
an exact trusted Python executable to the Codex worker, defaulting to
the Python running AgentRelay itself. To override, configure an existing
absolute interpreter path in local `.env`:

    WORK_PYTHON=W:\dev\agent-relay\.venv\Scripts\python.exe

Codex is told to use that executable for tests, and to create files
directly in the configured project workspace, not a nested alias folder.
OS sandbox rules may still block an external interpreter; verify this
on your local Windows version rather than disabling isolation.

With `LOG_LEVEL=debug`, log entries now separate **Ragna decision latency**
from **Codex execution duration**. Normal logging only shows the final
total duration and the number of Codex rounds. The 2-round budget is a
maximum, not a requirement: Ragna can finish after the first round.
Set `WORK_MAX_ROUNDS=1` for simpler tasks at the cost of losing an
automatic correction pass. `CATCHUP_MODE=off` skips startup catch-up
and its additional ChatGPT review request, if that trade-off is desired.

Successful orchestration means the workflow finished, **not** that tests
passed: check Codex's exact test results. A report of zero tests passed
must not be treated as a verified implementation.

### CWA request lifecycle diagnostics (debug)

`LOG_LEVEL=debug` now reports each actual outbound CWA **chat turn** (not
low-level HTTP calls) with a local request number, purpose (`chat`,
`catchup`, `work#N.planM`, `work#N.final`), dispatch time, completion
status, and duration. It does **not** print prompt text, model response,
conversation IDs, auth data, or exception messages. Normal logs remain quiet.
The local rotating file also includes these debug events. This is useful
for seeing whether a slowdown is in Ragna/CWA or the subsequent Codex task.
Note: this does not capture CWA-internal tool invocations or HTTP retries.

### Ragna background GitHub developer + coordinator (opt-in)

The primary Telegram/ChatGPT Ragna conversation remains responsive while a
**separate Ragna-work coordinator** delegates an authorized task to a
**separate GitHub developer ChatGPT conversation**. Both are persistent:
their IDs are saved in `.agent-relay-work-chats.json` (ignored by Git).
Each lane has its own BrowserlessHTTPClient instance. The main Ragna chat,
work coordinator, developer, and catch-up all explicitly request
`model="gpt-6", reasoning_effort="high"` on every ChatGPT send.
An existing conversation's historical creation model cannot be retroactively
changed. The CWA request flags are explicit but platform/server model
selection has not been independently attested.

To try the new worker route, provide a **real GitHub repository you own**
and a dedicated clean local checkout of that very repository with a matching
GitHub `origin`. Do NOT authorize a directory with secrets or unrelated data.

    WORK_ENABLED=on
    WORK_EXECUTOR=github
    WORK_ALLOWED_USER_IDS=<your-numeric-telegram-user-id>
    WORK_PROJECTS=demo=W:\dev\your-separate-github-test-checkout
    WORK_REPOS=demo=<owner>/<actual-repository-name>
    WORK_CHAT_STATE_FILE=.agent-relay-work-chats.json
    WORK_LOCAL_VERIFY=off

Then launch `python agent_relay.py` and send:

    /work@ragnacobot start demo Create a small harmless feature in a new
    branch and report its exact GitHub commit SHA. Do not merge or publish.

The worker conversation is **created lazily on the first task**, not at
process startup. The coordinator gives the worker one authorized task;
the developer is instructed to use its GitHub connector and return strict
`status`, `repository`, `branch`, `commit_sha`, `summary` JSON. An unavailable
connector or required new approval is a **blocked** result, never success.
AgentRelay independently compares the claimed branch/SHA with the remote
ref through `git ls-remote` using the local checkout's allowlisted GitHub
origin. No worker statement alone is treated as proof.

With `WORK_LOCAL_VERIFY=off` (default) no checkout, local code execution
or Codex run occurs. A successfully verified GitHub branch is reported to
Ragna and you for review. With `WORK_LOCAL_VERIFY=on`, AgentRelay first
rejects a dirty working tree, fetches the EXACT branch and SHA, switches
the separate checkout to that commit, and invokes Codex with a `read-only`
sandbox to attempt tests. This opt-in may execute UNTRUSTED REPO CODE;
only use it in an isolated machine/workspace that you control. A sandbox
or prompts alone cannot guarantee arbitrary test code is harmless.
Run the new GitHub mode in a separate disposable checkout, not in AgentRelay
or another sensitive project.

Background work is independent from ordinary Ragna chat, but CWA's shared
account and private protocol have not been verified for simultaneous
high-effort requests on the user's Windows system. Cancellation discards
any late response; it cannot reverse a GitHub commit already produced by
the remote worker. No automatic retry after ambiguous CWA writes, no
automatic merge/push-to-default-branch, and no automatic resumed task on restart.
Only your explicitly listed `WORK_ALLOWED_USER_IDS` can start or stop local
tasks, independently of other people allowed to chat with Ragna.

**Tool boundary:** consumer ChatGPT connectors might be present in the new
worker conversation, but CWA's Direct HTTP path does not independently
guarantee connector execution/approval parity with the website. Validate
the first real branch creation and GitHub SHA before trusting live execution.
