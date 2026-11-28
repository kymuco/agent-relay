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

### Natural conversation and Telegram Rich Messages (opt-in)

Ragna now favors natural short replies in everyday chat; for technical
explanations, use structured formatting without forcing headings everywhere.

Enable natural work with `NATURAL_WORK_ENABLED=on` only when both
`WORK_ENABLED=on` and `WORK_EXECUTOR=github` are already configured.
An authorized human can address Ragna, use an imperative, and name a
single allowlisted project alias or its repository short name:

    Рагна, создай калькулятор и тесты в github-workspace

Questions such as 'Рагна, как думаешь, стоит ли создать калькулятор?'
remain ordinary conversation. Ambiguous project references and
unaddressed group chat cannot trigger work. The classifier deliberately
does not treat 'да, давай' or old quoted messages as authority.
The explicit /work command remains available for other phrasings.

While a GitHub job is running, the allowed human can add requirements:

    Рагна, учти деление на ноль
    Рагна, добавь ещё тест ошибок

The Host stores amendments durably until the next worker checkpoint.
The existing persistent coordinator and developer chats attempt the
update on the SAME task branch, and the remote SHA is verified again.
The allowed number of developer passes is WORK_MAX_ROUNDS (default 2).
If an amendment arrives too late or the iteration budget is exhausted,
the job is marked needs_review, and unprocessed changes are explicitly
reported; they are not silently claimed to have been implemented.
No cancelled/ambiguous CWA sends are replayed automatically, and
stopping AgentRelay cannot undo a remote commit already created.

Telegram supports native Rich Messages. Set TELEGRAM_RICH_MODE=auto
to send only structured Markdown replies (headings, lists, code blocks,
tables) using sendRichMessage with rich_message.markdown. Ordinary chat
and inter-bot handoffs keep their existing sendMessage HTML behavior.
Default off is conservative: if Telegram rejects a rich send, AgentRelay
does not automatically resend because an ambiguous write may already
have been delivered. Set the option back to off if needed.

Both options are independent. They add no new CWA model, auth or agents.
All CWA conversations still explicitly request gpt-6 and high.
All local work still requires numeric user and project/repository allowlists.

### Generated images in Telegram (async readback)

Natural requests such as 'Рагна, сгенерируй картинку кота',
'Рагна, сделай мне изображение города' or 'Рагна, нарисуй кота'
now trigger the image-delivery path when MEDIA_ENABLED=on.
The ChatGPT send still requests GPT-6 High; actual image generation is
up to the product's available capabilities. AgentRelay cannot assume
an image was created from the assistant's text alone.

After the CWA chat turn, AgentRelay polls CANONICAL read-only image
observations in a separate CWA client for a bounded period (about
71 seconds total). This does not block normal Ragna replies. Once a
new ready image is observed, its generated artifact bytes are verified
through CWA handoff, then sent as Telegram photo or document.
An immediate empty observation is NOT proof the image failed.
If no ready artifact appears, Ragna reports that it was not confirmed;
the user can later check with /images@ragnacobot without rerunning
generation. The command only checks the image already requested.
Images observed in the conversation before the request are excluded.
The read-only polling does not run image generation again.

Important: if Telegram upload completion is ambiguous, the image is
marked as already attempted and is NOT automatically sent a second
time. A later generation request supersedes any previous monitor;
the initial image may still be present in ChatGPT history.
The monitor stops on /stop, /resume epoch change, or AgentRelay shutdown.
Use LOG_LEVEL=debug for intent recognition, discovery counts and
transfer outcome without logging prompts, generated content or tokens.

This path is experimental because the CWA readback and generated
artifact handoff depend on changing ChatGPT consumer endpoints.
MEDIA_ENABLED=off disables image delivery; MAX_MEDIA_MB limits size.

### Telegram typing presence + GitHub live progress (opt-in)

Use CHAT_TYPING=on to show Telegram's native 'typing...' while the
main Ragna conversation is waiting for CWA to answer. One ephemeral
sendChatAction typing signal is emitted immediately and refreshed at
most once per four seconds, then stopped on reply, error or /stop.
This is not a background GitHub progress indicator. Typing is not
shown for the entire developer job. Telegram itself clears it after
at most five seconds or after a bot message.

Use WORK_LIVE_PROGRESS=on with the existing GitHub work backend to
create ONE editable status message at task admission. WorkProgress
persists the real Telegram message ID in WORK_PROGRESS_FILE, and
changes that exact message using editMessageText/HTML as the Host
advances through its actual states: coordinator planning, developer
working, remote SHA check, optional local test, final review and
completed/failed/needs_review/stopped. No fake percentages and no
new ChatGPT request. The already existing single final result report
is still sent as a separate notification so completion is visible
even if the status message has scrolled out of view.

Status contents are safely HTML escaped and bounded. A failure
to edit a status message does not lead to a duplicate message or
automatic resending of a potentially successful write. /work stop
and /stop mark the active card stopped. On restart, an interrupted
job's old card is marked interrupted, never resumed automatically.
Task progress isn't simulated while no work is happening.

WORK_LIVE_PROGRESS works independently of TELEGRAM_RICH_MODE; it
uses standard HTML sendMessage/editMessageText for broad client
compatibility. Normal Rich Messages for model responses are unchanged.
CHAT_TYPING and WORK_LIVE_PROGRESS are both off by default; enable
one or both in .env. They don't require another model, new CWA
conversations or a Codex API key.

### Three Telegram work-card detail levels (when WORK_LIVE_PROGRESS=on)

New GitHub tasks have a single task card, updated in place.
On success, the compact card itself shows the result, confirmed commit
and any test count explicitly reported by the trusted GitHub Developer.
AgentRelay no longer duplicates the entire Coordinator transcript into
the group after a successful card edit.

Inline buttons below each card:

- Подробнее: edit the SAME card with bounded Developer summary, filenames,
  test results, remote branch/SHA, and verification details.
- Свернуть: return the SAME card to its compact format.
- Журнал: send a separate agentrelay-work-N.txt with saved Host stage
  events, time stamps, a bounded Developer report, and verification metadata.
- GitHub: link to the specific verified commit, shown only for validated
  repository/commit identifiers and verified remote SHA.

Buttons are display-only and never start, stop, merge or modify tasks.
Telegram callback_query updates are independently checked against the
numeric allowlisted human user, the configured group chat, the exact
original Telegram card message_id, and a persisted task ID. Other
users/bots or fake/stale callbacks receive no document and cannot edit
the card. Repeated journal downloads have a 20-second cooldown.

The task-card state file also stores a bounded history of the last
30 task cards, so their buttons survive AgentRelay restart and continue
working after another job begins. Older records are pruned in FIFO ID
order; initial legacy cards without buttons remain as they were.
History is local data and never grants execution authority.

Technical journal contents are intentionally narrow: no CWA secrets,
full prompts, raw source code, or unfiltered model/tool transcripts.
Developer-reported test success is shown naturally in the user card;
the technical journal separately records its source as Developer and
the Host's own remote SHA verification.

If a Telegram status-card edit fails or a card ID was not captured,
AgentRelay sends one concise fallback result instead of retrying
an ambiguous edit. This avoids a missing completion while keeping
ordinary successful task completions to one updated card.

Tip: if WORK_LIVE_PROGRESS=off, AgentRelay continues using ordinary
plain result messages; no callback UI is emitted.

### Task Conversation v1: Reply-to-card and confirmed stop

With WORK_LIVE_PROGRESS=on and the GitHub backend enabled, replying to
an actual Ragna task card binds that Telegram message to the card's exact
persisted work ID, not the most recent task. Telegram sender/bot IDs and
the exact card message_id must match. Replies to arbitrary messages,
forwards or other assistants cannot acquire task authority.

- 'Reply' -> 'Что там?' responds immediately from saved Host state,
  without any ChatGPT model request.
- 'Reply' -> 'Рагна, почему использовала unittest?' routes a normal
  GPT-6 High conversation question to Ragna with bounded, read-only
  context about precisely that card. Questions do not become a new
  work request; they do not call Developer/Coordinator.
- 'Reply' -> 'Добавь ещё проверку деления на ноль' is admitted into
  the existing human amendment queue only when the exact bound task is
  active, the caller is in WORK_ALLOWED_USER_IDS, and the project matches.
  The card shows queued amendments, and existing WORK_MAX_ROUNDS bounds
  whether the Developer will apply them. Late changes are not claimed
  as completed. An old task's Reply can NEVER amend a newer task.
- An explicit Reply mentioning a different allowlisted repository is
  rejected without forwarding it to CWA or GitHub.
- 'Reply' -> 'Останови работу' explains that the Stop button must
  be used; arbitrary text does not stop the task.

While active, a card includes the Telegram button 'Остановить задачу'.
Pressing it shows 'Да, остановить' and 'Отмена' instead of immediately
stopping. Only authorized work users can request/confirm. Confirmation
is bound to the original exact card, task ID and requesting numeric
Telegram human ID; it expires after 60 seconds. WorkState.stop compares
the expected task ID atomically. Old buttons can never stop a later work.
Stop cancels further AgentRelay steps but cannot roll back an existing
remote GitHub commit.

Telegram card edits for buttons and amendment counts are asynchronous,
so a slow Telegram API should not hold up polling or /stop.
No new agents, model providers, repositories, permissions, or automatic
merges are introduced by this feature. Normal conversation and ordinary
peer-bot routing are unchanged.

Manual smoke test: create one tiny work task; while in progress Reply
to its card with a question and then an explicit amendment. Verify the
pending count and resulting review. Press Stop on a fresh long-running
task, cancel once, then confirm; check it never changes a different task.

## Native Agency 0.1 (NA-00 / NA-01)

Baseline: 37ee20b9; 159 passing tests in Actions run 45049284308.
This branch introduces main-assistant action proposals without execution.
The primary ChatGPT turn still returns natural text; no extra classifier or CWA
send. A single final [[agentrelay_action]] JSON trailer carries version 1, a
fresh turn token, action and arguments, followed by [[/agentrelay_action]].
The Host removes it before Telegram/TTS and records a local observation only.
Supported: start_work, get_work_status, get_work_result, amend_work.

To test, use ACTION_MODE=observe and ACTION_OBSERVE_READ_ONLY_ACK=on with a
**dedicated test conversation/account whose write-capable native connectors
are disabled or read-only**. The acknowledgement is an operator statement,
not verification of provider permissions. A prompt alone cannot prevent
native connector writes. Do not test against write-capable main chats.
Keep WORK_ENABLED=on and existing project mappings for discovery.
Observe mode bypasses legacy natural and Reply-based work classification,
blocks explicit /work start too, and never sends proposals to executors.
/stop, /resume, /work status, ordinary Telegram conversation, media and
peer dialogues remain available. No proposals admitted from peers, ambient
media, historical catch-up or users without work permissions. Observe is
about AgentRelay work, not disabling all ChatGPT capabilities.

The local journal .agent-relay-action-observations.json records the last 100
eligible turns: no_action/proposed/rejected, source IDs and proposed arguments.
No full prompt/response or turn token is stored. Arguments can contain sensitive
project details; keep the file local, ignore custom paths in Git, never execute
journal entries. Use separate state files per bot.

Manual smoke: discuss a change, explicitly forbid changes, request a task,
agree to a prior concrete plan, ask about an existing task via Reply. Compare
the journal to expected intent. Verify no new task/Coordinator/Developer call.
Check both bot profiles and /stop during CWA. ACTION_MODE=off restores legacy
behavior, without work-state migration. NA-02 admission, execution, result
feedback and shared tasks are NOT implemented by NA-01.

## NA-01.1: response integrity

This experimental branch pins CWA commit 0c0af75e (PR 259) instead of the
older PyPI build. Run start.cmd after pulling so bootstrap installs changed
requirements in .venv (Git and network access required). Main chat and catch-up
request verify_final_text=True by default. It adds a read, not a model turn;
if the completed message cannot be verified, the existing no-replay error path
is used. Native Agency observe always verifies; CWA_VERIFY_FINAL_TEXT=off is
an escape hatch for ordinary legacy chat only. Empty asynchronous responses
retain the existing media lifecycle. No automatic fallback generation.

Telegram HTML delivery uses source chunks, preferring complete paragraphs and
fenced blocks; oversized or unclosed blocks use literal text. Rich Messages
remain opt-in via TELEGRAM_RICH_MODE=auto. Use off if Telegram Rich rendering
introduces artifacts. No automatic retry of a possibly delivered Telegram
message. No live Telegram or authenticated CWA test is implied.

## NA-02: action admission dry-run

In ACTION_MODE=observe, each proposed action is evaluated by action_policy.py
against Host-owned source identity, current stop generation, configured GitHub
project and current task/card binding. ACTION_AUTO_PROJECTS is an explicit
comma-separated subset of configured project aliases (empty by default).
Bounded reads can be allowed; starts require an idle worker; amendments require
the exact active task and Reply binding for auto eligibility. Writes to projects
without auto eligibility are classified ask; unknown or out-of-scope actions are
denied. No confirmation button is required for eligible proposals. Ask is a
classification only; no approval endpoint exists yet. No execution path is
connected. Journal decisions (allow/ask/deny), proposal digest, source digest
and generation are diagnostic only, never reusable grants. A digest binds the
proposal to the current source, not proof of semantic intent or prior consent.
Recheck permissions, scope, task identity, expiry and generation at any future
dispatch. Native connector restrictions are still an operator prerequisite.

## NA-03: native background execution (opt-in)

The default remains ACTION_MODE=off. First use observe with a dedicated test
chat, verify actual canonical delivery and review proposed actions/decisions.
Only after accepting those smoke tests, set ACTION_MODE=execute,
ACTION_EXECUTE_ACK=on, ACTION_OBSERVE_READ_ONLY_ACK=on, WORK_ENABLED=on,
WORK_EXECUTOR=github, WORK_LOCAL_VERIFY=off and an explicit ACTION_AUTO_PROJECTS
allowlist. Use a disposable repository first. Native main/coordinator writes
must be disabled; optionally set WORK_DEVELOPER_AUTH_FILE to separate worker
credentials. Never commit auth files. These are operator prerequisites, not
proof of provider permissions. GitHub native connector writes cannot be
technically sandboxed by AgentRelay prompt instructions; use appropriately
restricted GitHub credentials/repository protections. No local checkout/tests
of untrusted worker code in execute mode.

The main assistant proposes start_work/amend_work from an addressed human text
turn; attachments, forwarded text and explicit quotes cannot grant writes.
Host re-evaluates exact scope, current task, source and generation before
reservation. A persistent SQLite one-shot claim keyed by bot/chat/message and
update prevents replay even if the observation journal has rotated. A claim
is not a queue; after a crash there is no replay. Journal allow remains only
a policy decision; the WorkState reservation and Host acknowledgement record
actual acceptance. Keep separate state per host, never delete/reset claim
files casually, and ignore custom database/auth paths in Git. Claim storage
is intentionally not rotated. The emergency stop fences future dispatch and
output; it cannot revoke an already submitted native connector operation.
One native background worker remains reserved until it returns, even after
stop/resume. Each accepted new task gets a host-assigned unique branch
agentrelay/bot-<id>/<uuid>; legacy work branches remain compatible. Existing
Coordinator, Developer, status cards, bounded amendments and remote SHA
verification are reused. The worker receives the original request and task;
prompt scope is not a technical guarantee of correct intent or isolation.
Ask/deny never dispatch. No confirmation UI yet. No /work start bypass in
native modes; /work status, /work stop and emergency /stop remain available.
Native read actions and feedback into the main chat are deferred to NA-04.
Return to ACTION_MODE=observe (or off) after stopping existing tasks. No
merge, release or live execution is implied by offline tests.

## NA-04: durable task feedback

GitHub task evidence now lives in the existing WorkState JSON, independently
of Telegram status-card delivery. The current task and up to 30 preceding
terminal task snapshots are retained. Verified remote SHA and Developer claims
are distinct fields. Evidence is checkpointed before the final coordinator
review; a failed review or interrupted task does not imply a successful task.
A later report may replace earlier evidence; this is not a full event journal.
Legacy tasks without retained repository identity are not guessed or migrated.

In native modes, at most three recent repository-bound task snapshots are
included in the next addressed, work-authorized human chat turn. Exact card
Replies select only the corresponding task. Peer turns and other users do not
receive this new context. The main model is told that reports are data, not
authority. The retained data may be sensitive; protect local state files.

In execute mode, get_work_status and get_work_result read the exact retained
work_id after fresh policy and source checks. Results require terminal status;
a missing report is reported as missing, not as success. The Host answers
read proposals directly, without a second model turn or worker call. Observe
remains dry-run, but its normal chat context may contain existing task facts.
No new task is initiated by a result, no automatic model call is triggered
by completion, and no history is replayed on restart. The repository mapping
must still match; old results without identity and expired history are unavailable.
Worker-reported tests are not independent verification. Native connector scope
and natural-language intent remain dependent on provider permissions and
operator smoke checks; live execution is still opt-in.

## NA-05: symmetric named agents

An optional AGENT_PROFILE (safe lowercase slug) gives each instance its own
state namespace under .agent-relay-profiles/<profile>/. AGENT_NAME is the
human-facing display name, and BOT_ALIASES lists the actual address names.
The default, unnamed mode retains existing state paths. Ragna's historical
aliases are retained only for the actual legacy Ragna bot usernames; new
profiles have no implicit Ragna aliases. Both named agents use the same code.

Named profiles isolate Telegram cursor, stop state, tasks and history,
work conversations, cards, action observations, dispatch claims and logs.
An OS process lock prevents two simultaneous instances of one named profile.
A local SQLite ownership registry binds each profile to its Telegram bot ID,
group and main CWA conversation; coordinator/developer lanes cannot overlap
with each other or any named profile's chat. A mismatch is rejected, not silently
rebound. Legacy unnamed instances and different checkouts/hosts are outside
this local registry; do not run the same bot or chat from multiple hosts.
All new named-profile GitHub tasks (including legacy explicit work commands)
use unique bot-ID/UUID branches. Task IDs remain local to each agent.

For two independent instances in one checkout, create .env.ragna and .env.yuki
from .env.example, set AGENT_PROFILE=ragna or yuki and AGENT_NAME=Ragna or Yuki,
provide different Telegram tokens and CWA_CONVERSATION_ID values, and configure
BOT_ALIASES, peer identity and WORK_* allowlists separately. With a profile,
CWA_AUTH_FILE=auth_data.json resolves to the profile's own auth_data.json;
explicit alternate auth paths remain supported. For example, start from two
separate PowerShell terminals after completing dependency setup:

    $env:AGENTRELAY_ENV_FILE = ".env.ragna"
    .\start.cmd

and:

    $env:AGENTRELAY_ENV_FILE = ".env.yuki"
    .\start.cmd

The shell environment takes precedence over the selected file; avoid stale
variables and never commit credentials. Run initial setup once, not concurrently.
Custom state/log paths outside the profile namespace are rejected (legacy
defaults in copied .env files are remapped). Existing unnamed state is NOT
migrated automatically. Stop the old instance before adopting a named profile;
keep its files as backups and do not assume old task IDs, cursor, stop state or
claims have migrated. New profile starts with fresh state and retains the usual
first-launch backlog skip. Do not use this as a way to bypass a stopped task.
Do not delete ownership or claim files to fix configuration mistakes.

Separate state and conversation ownership are not remote connector permission
isolation. Main/coordinator native write restrictions and operator smoke tests
still apply. No shared task ledger, cross-host lease, agent-to-agent work
delegation or live parallel Telegram experiment is implied by this milestone.

### NA-05.1: Windows and resource lifecycle hardening

The Host tracks background card, image, work and typing threads. Shutdown
fences new managed threads and waits a bounded time for pending operations;
only after a successful drain is the named profile lease released explicitly.
A timeout retains the lease until a subsequent successful shutdown or OS
process exit. This does not cancel already-submitted remote calls. The polling
worker exits after shutdown instead of waiting indefinitely on an empty queue.
Logging and SQLite test handles are closed before temporary directory cleanup.
Card tests wait for thread completion, not only an in-memory field change.
CI runs on Linux and Windows with Python 3.11 and 3.14 and repeats with an
explicit profile environment variable. Live Telegram/CWA tests remain manual.

### NA-01.2: canonical turn identity correction

CWA now verifies the exact submitted user message ID, not the inherited
conversation user_id. Transient final read failures may trigger at most three
read attempts; no new generation or write is issued. Native modes continue
to require canonical verification. Debug logs expose fixed stage/reason codes
without raw exceptions, response bodies or credentials. After pulling, run
start.cmd --setup-only to refresh dependencies; if an older same-version CWA
is retained, reinstall the pinned Git dependency explicitly. Live verification
is still required.

### NA-03.1 — Direct Delegation

Native `start_work` accepts optional `delegation: "direct" | "coordinated"`.
Ragna chooses the direct route for clear bounded tasks; complex work may keep
Coordinator. Omitted route defaults to coordinated (legacy compatibility).
Direct uses the same Developer lane and original human scope, isolated branch,
claim, stop fence, remote SHA verification, bounded amendments, work history and
card. Host finalizes directly from observed evidence and the worker report;
no Coordinator planning or review request is sent. No new permissions and no
retry/fallback after an ambiguous write. GitHub remains the source of code.

### NA-06.1 — Capability Registry

The immutable Host registry in `capability_registry.py` describes the four
existing native capabilities, their stable IDs, legacy action names, input
schemas, scope, executor, effects and constraints. The same argument definitions
validate the model trailer and Host policy. Per eligible human turn the Host
advertises a fresh catalog with configured projects, auto-write policy, mode and
worker availability. This is a configuration snapshot, not live GitHub connector
permission proof, a reservation, or an execution grant. Admission, claim and
stop-generation checks remain authoritative. Existing v1 action names, direct
and coordinated routes, settings and task history are unchanged. No new executor,
local code writing, CI dispatch, plugin loading or remote delegation is enabled.

### NA-06.2 — GitHub CI observation

Set `WORK_CI_VERIFY=on` to enable read-only GitHub Actions observations after
Host verifies the task branch and SHA. Public repositories can be observed without
a token; for private repositories optionally set a fine-grained `WORK_CI_TOKEN`
with Actions read access to the explicitly allowed repositories. Never send the
token in Telegram or commit it. The Host uses fixed GitHub API GET endpoints,
rejects redirects, bounds responses and verifies each run's exact SHA and branch.
`WORK_CI_WAIT_SECONDS=60` bounds polling (0 = one snapshot; HTTP socket timeouts
are separate). Stop/generation checks fence further requests; no dispatch/rerun.
CI is separate from worker-reported tests and remote SHA verification. All visible
runs succeeding means passed, not proof all required checks were triggered.
No registered workflow is not_configured; no matching runs visible is not_run.
Failed/incomplete/pending/unavailable/not_run finalize as needs_review while
preserving the verified commit; not_configured and passed can complete normally.
No automatic code repair, local fallback, or later refresh is enabled yet.
Disabled by default for compatibility. Old task records are unchanged.

### NA-06.3 — Local Workspace

Set `WORK_WORKSPACE_ROOT` to an existing dedicated absolute directory outside
AgentRelay and every configured project checkout; leave empty to disable.
Requires GitHub work and `WORK_LOCAL_VERIFY=off`. For each verified final SHA,
Host validates the local clone root and origin, fetches that SHA (not a moving
branch), and creates a unique detached worktree. It checks HEAD and clean status
with checkout hooks, external filters, symlink creation and submodule recursion
disabled for those commands. LFS files remain pointers; submodules are not
initialized. No project scripts, tests or Codex are launched. The primary
checkout's branch/index/files are untouched; Git object and worktree metadata
are shared. Workspace evidence is a snapshot with an ID relative to the root,
not a guarantee of later directory state or an execution sandbox. A failed
preparation preserves verified commit/CI and finalizes as needs_review. Stop
fences subsequent Git commands but may leave partial materialization. No automatic
cleanup, deletion, reuse, replay, or refresh of historical tasks; the operator
must manage retained worktrees and disk usage. Existing trusted Git configuration
and credentials are required; Git credential helpers/transport can still run.

### NA-06.4 — Local Execution

Opt in with `WORK_LOCAL_TESTS=demo=unittest` (multiple entries separated by
semicolons; supported presets: unittest, pytest). Requires a configured GitHub
project and WORK_WORKSPACE_ROOT, with WORK_LOCAL_VERIFY=off. **Tests execute
repository code with the Host user's OS permissions. A worktree and a fixed
command are NOT a sandbox. Only enable trusted repositories and code.**
Host rechecks worktree identity, shared Git directory, detached HEAD, exact SHA
and clean status before executing the fixed Python preset; no shell, no model
commands, no installation. Uses WORK_PYTHON or the active interpreter. Unittest
requires a nonzero test count; pytest must be installed by the operator.
Run when CI is disabled, not configured, not run or unavailable; do not duplicate
passed CI or mask failed/incomplete/pending CI. A successful local fallback can
complete a task with unavailable/not-run CI while preserving that CI evidence.
Local failure, timeout, no tests, output limit or unverifiable execution requires
review. Uses WORK_TIMEOUT_SECONDS, polls stop permission, attempts process-tree
termination, bounds captured output, and persists only exit status/count/time,
not raw logs. This is best-effort process management, not OS isolation; no
protection against malicious child processes or network/file access. No automatic
retry, source repair, dependency install, or Codex invocation yet.

### NA-06.5 — Codex Diagnostics

Opt in with `WORK_CODEX_DIAGNOSTICS=demo` (comma-separated subset of aliases
configured in WORK_LOCAL_TESTS). Requires installed/authenticated Codex CLI.
The Host invokes Codex at most once after unsuccessful local execution; never
on success or skipped local execution. `WORK_DIAGNOSTIC_TIMEOUT_SECONDS=180`
(10–600) bounds the CLI call. Exact clean detached workspace revalidation is
required before and after diagnostics. The existing CodexRunner uses read-only
sandbox and never approval; the diagnostic prompt prohibits modifying source,
rerunning code, installing dependencies, external connectors and network commands.
These instructions are not independent OS/network isolation. Only opt in for
trusted repositories and Codex configuration; Codex can process repository
source and a bounded failure excerpt, which may contain sensitive data despite
best-effort redaction of known environment secrets. No raw test output is saved
in task history, cards or console. Structured category/summary/recommendation
is a model report tied to the failed execution and exact SHA, not evidence of a
fix. Missing CLI, failure, invalid report or changed workspace preserve the
original local failure and needs_review. Stop prevents further stages and uses
existing best-effort Codex process termination. No automated repair, replay,
new native action, or additional permission is enabled. Disabled by default.

### NA-06.5.1 — Intelligent Execution Routing (supersedes automatic local fallback)

Developer receives a per-project configuration catalog and returns a bounded
verification object with route and reason; local/hybrid also require an exact
operator-configured preset. Host checks the final verified SHA and independently
admits the plan. Cloud is preferred; missing workflow, unavailable CI or slow
CI alone never triggers local preparation or execution. Cloud requires observed
successful CI; local requires successful local tests, with no cloud claim; hybrid
requires both, with local deferred until cloud passes. None means Developer
reports execution unnecessary, not a passed check. Unavailable or denied plans
require review. For older reports without a plan, only read-only CI observation
is allowed; if CI is disabled, the legacy completed status means commit-only,
not verified tests. If enabled and not passed, review is required. No additional
model call or workflow creation. WORK_LOCAL_VERIFY=on is retired and rejected;
WORK_WORKSPACE_ROOT and WORK_LOCAL_TESTS now permit but do not trigger local
execution. All new records separate plan, admission and observed evidence. Local
presets still execute trusted repository code with the Host user's OS permissions.

### NA-06.6 — Autonomous Feedback Loop

`WORK_FEEDBACK_PROJECTS=demo` enables automatic repair for configured GitHub
project aliases. No fixed repair iteration cap; WORK_MAX_ROUNDS still controls
legacy Codex and initial human-amendment handling, not autonomous repairs.
A confirmed CI failure or local failed/no-tests result can trigger one new
Developer turn with the exact prior evidence, original human scope and earlier
amendments. Developer may return unchanged (intentional failure, unrelated issue,
no meaningful progress), blocked, or a new commit. No coordinator call per repair.
Pending/missing/unavailable checks and ambiguous tool responses never auto-retry.
Each new commit must have the previous SHA as its sole parent, with a source tree
not previously seen in this repair loop. Independent read-only GitHub Git commit
metadata is required (public repository or WORK_CI_TOKEN with Contents: read).
No local checkout to verify ancestry. Required checks may not be weakened; an
unadmitted or weakened plan stops without further execution. On identity failure
or repeated tree, the prior historical verified SHA is retained; the branch may
have moved. Stop, shutdown, and interrupted tasks fence further calls, no replay.
Late human amendments remain queued for review rather than silently applied by
repair. Up to 12 recent verified SHAs, compact per-round verification outcomes
and attempt count are retained in feedback records; full commit history stays
in the branch. No raw test logs are persisted;
opt-in permits bounded redacted failure context to be sent to Developer, which may
still contain sensitive data. Repeated trees are a deterministic no-progress guard,
not a guarantee of semantic progress for every different tree. Local execution
remains separately opted in. No merge, force push, release, install, or permission
expansion. No new CI workflows are automatically created.

### NA-06.7.1/2 — Artifact Protocol & GitHub Repository Artifacts

`WORK_ARTIFACT_PROJECTS=demo` opts a configured project into artifact metadata and
immutable GitHub file links in the configured Telegram group. This is not an
authorization to share arbitrary repository content or local files. Developer may
return up to five distinct `artifacts` declarations with source github_repository,
canonical repository-relative path, required boolean, delivery link. Host uses
read-only Git commit and tree metadata for the final independently verified SHA;
no clone, blob download, worktree, execution, redirect, arbitrary URL or write.
Only regular Git blobs are registered; symlinks, directories and submodules are
not deliverable. Git blob SHA and size describe the Git object, not a downloaded
file or resolved Git LFS content. Artifact IDs are deterministic for source,
repository, commit and path; task state/history associates them with work IDs.
Required declarations survive repair rounds. Missing required artifacts result
in needs_review; failed verification withholds locators. Optional unavailable
items remain visible as limitations. Metadata observations are historical, not
permanent availability promises. Private repositories may need WORK_CI_TOKEN
with Contents: read; GitHub links retain normal GitHub access checks. Truncated,
oversized or unavailable tree metadata fails closed. Registry is retained with
existing profile-scoped task history (30 prior tasks), no new disk cache.
Telegram cards, journals and Host result reads expose registered locators. Actual
byte download, SHA-256 of payload, Actions archives, native CWA files, local
outputs, and artifact transfer across hosts are later adapters, not implemented.

### NA-06.7.3 — GitHub Actions Artifacts

Existing WORK_ARTIFACT_PROJECTS and WORK_CI_VERIFY=on enable this source; no new
setting or automatic workflow creation. Developer declares source github_actions,
exact upload-artifact name, required boolean, delivery link. Host uses only an
admitted cloud/hybrid plan with passed CI evidence for the final verified SHA.
It rechecks each observed run's repository, head repository, SHA, branch, success
and attempt before and after listing metadata. Only first-attempt runs supported:
per-run artifact listings do not prove which attempt produced an archive. No
implicit rerun, local fallback, guessing a name, or selecting ambiguous matches.
Up to 20 observed runs and 100 artifacts per run, no incomplete pagination;
missing, expired, ambiguous or unavailable required outputs need review.
Archive metadata records exact run ID, artifact ID, archive size, creation and
expiry, and optional GitHub-reported SHA-256 digest (not Host-computed). Links
are normal GitHub authenticated archive links, not signed redirects. Expired
links are withheld when views are regenerated; old Telegram messages cannot be
revoked automatically. No archive download/extraction or inspection of files
inside an archive; no guarantee of continued availability or contents. Private
repos may require Actions: read on WORK_CI_TOKEN. Required declarations survive
feedback revisions by source+locator. Repo artifact links remain compatible.

### NA-06.7.4 — Native ChatGPT Artifacts

`NATIVE_ARTIFACT_REGISTRY=on` with `MEDIA_ENABLED=on` enables native CWA
artifact receipts in ordinary explicitly addressed human chat turns. Native files
are independent of GitHub jobs, with source cwa_native and turn identity instead
of a commit SHA. Use actual sandbox links with unique ASCII basenames. Existing
CWA PR14.0 handoff is reused; Host checks returned conversation, filename, size,
SHA-256 and destination before sending bytes through existing Telegram transport.
Supports DOCX, XLSX, PPTX, PDF, ZIP and existing text/image extensions; up to
three files per turn within MAX_MEDIA_MB (1–10 MiB). No native delivery from
peer, catchup or action-trailer turns; image-intent turns retain the existing
image monitor and legacy media path (not native receipts).
CWA resolves a unique conversation-owned filename, not a per-message file ID:
this is not proof of which exact model message created the file. Reuse of names
can fail closed; ask for unique names. Canonical final text is required in opt-in
mode. Durable SQLite receipts beside the profile cursor record claims before
retrieval and send, byte evidence and Telegram message ID on acknowledgement.
No payload cache, signed URL, product file ID, credentials or absolute path in
receipts. No automatic retry or replay, even after interruption. Recent five
receipts are supplied only to the same human in the same bot/group/conversation.
Claim history is retained without automatic cleanup. This increment does not
integrate native creation into background Developer jobs or implement manual
resend / cross-host artifact transfer. When disabled, legacy media behavior stays.

### NA-06.7.4.1 — Native artifact read recovery

With NATIVE_ARTIFACT_REGISTRY=on, native file delivery retries temporary
discovery/transport/HTTP errors at delays 0, 2, 5, 10 seconds. New temporary
destination per read; no CWA write or Telegram send retry. Fixed stage/reason/HTTP
codes logged and persisted on failure, no raw exception/body/locator. Pinned CWA
does not expose curl exit code. Integrity, ambiguous identity, size, forbidden
locator and local publication failures are terminal. For addressed human turns,
a random host-generated marker is appended before the single CWA send. On an
eligible ambiguous failure, read the public canonical snapshot via read client;
require exact conversation, unique user marker, current completed visible final
and nearest user ancestor. Never execute recovered actions, peer handoffs or voice.
Only linked native files or existing image monitor may be delivered. No model replay.
Bounded synchronous recovery in the turn handler; existing image background monitor
unchanged. Unconfirmed result reported to human. No restart replay. CWA unchanged.

### NA-06.7.5 — Local Runner Artifacts

Existing WORK_ARTIFACT_PROJECTS plus WORK_WORKSPACE_ROOT and WORK_LOCAL_TESTS
opt in to this source; no new command capability or automatic local fallback.
Developer declares source local_runner, exact ASCII basename, required boolean,
delivery retain. Must use admitted local/hybrid verification. Only for authorized
local computation, existing preset executes committed code. Host creates a fresh
<workspace_id>.artifacts sibling directory under WORK_WORKSPACE_ROOT and passes
its path to the child via AGENTRELAY_ARTIFACT_DIR only for declared outputs.
Tests should use that variable only if present, and must not write generated
files into the checkout. After successful execution, Host revalidates the clean
detached worktree, reads only declared regular single-link files, checks
size (1 byte to 10 MiB per file), computes SHA-256. No directory scan, arbitrary local
path, symlink/reparse point/hardlink retrieval, automatic cleanup or execution
of artifacts. Final records bind SHA and workspace to successful execution;
required unavailable outputs need review. Failed and superseded run directories
are retained locally but never registered as final output. Files remain mutable;
metadata is an observation, not immutable storage; future delivery must recheck
bytes. Local file links and Telegram transfer are deferred to NA-06.7.6.
Local tests are trusted code running with the Host user's permissions, not
an OS sandbox, and may themselves access or modify other resources. Limits bound
Host collection, not disk writes of the child. Operator owns cleanup/retention.

### NA-07.1 — Durable Event Runtime

When work is enabled, the Host creates a local SQLite journal next to
`WORK_STATE_FILE` (suffix `.events.sqlite3`). Named profiles inherit their
existing isolated work-state paths; journal identity is bound to Telegram bot
ID. Do not share these files between processes or reset them independently.
No new settings, model calls, network events, autonomous dispatch or authority
are introduced.

Every new persisted WorkState transition captures one Host event:
work.accepted (local reservation), work.started (running phase, not proof of
remote submission), work.completed, work.failed, work.needs_review,
work.interrupted or work.stopped. Repeated running stage updates and result
checkpoints do not emit new lifecycle events. Terminal status is immutable
through update(), so a Telegram presentation error cannot undo finalized work.
Legacy snapshots are not backfilled; a legacy active task marked interrupted
on startup produces a real new transition. Status alone is not proof of tests,
remote writes or achievement of a goal; commit evidence is included only when
the retained verified result matches the work identity. No raw task text,
model summaries, logs or credentials are copied into the event payload.

The event outbox is written in the same atomic JSON replacement as WorkState.
SQLite append is idempotent on the persisted event UUID, with a monotonic
local read cursor. After a successful SQLite transaction, acknowledged events
are dropped from memory and from the next JSON write. If the process exits
before that write, restart replays the same IDs, not the work. Delivery is
retried on WorkState changes, startup and the existing Telegram polling loop;
temporary append failures retain the outbox. Startup journal initialization
or invalid persisted state can fail closed. This protects against process
interruption, not arbitrary disk corruption or independent deletion of files.
No automatic rotation: monitor disk usage; keep JSON and journal backups
together. Custom state paths must be ignored in Git. This is a local event
producer, not yet a mission scheduler, consumer acknowledgement protocol or
cross-host federation. Emergency stop still fences work; events are historical
facts, never permission to act. Existing work is never automatically replayed.

### NA-07.2 — Decision Scheduler (observe only)

Work-enabled hosts classify up to 100 journal events per existing Telegram
polling iteration, without extra threads, CWA calls, external actions or new
environment variables. Completed, failed and needs_review events are potential
cognition candidates; accepted and started are ignored; interrupted requires
reconciliation; stopped is ignored. A candidate is only an observation, not
a pending executable wake or proof that a result is correct. All historical
events, including ones recorded before this upgrade, can be classified for
diagnostics; none can authorize a later action. Future mission admission must
bind fresh events explicitly, not replay these observations.

The scheduler cursor and classification rows commit in one SQLite transaction
in the existing `.events.sqlite3` journal. Restart or failed transactions do
not duplicate decisions. The scheduler pauses observation while emergency
stop is active (event outbox delivery can continue); resume does not replay
work or authorize cognition. Temporary observation errors do not prevent
Telegram polling. No new permissions, mission state, model wakeup, peer
transport or automatic work execution. No additional configuration required.

### NA-07.3 — Mission State & Grant (observe only)

Opt in by adding `MISSION_ALLOWED_USER_IDS=<your numeric Telegram user ID>`
to the existing named profile environment (e.g. .env.ragna). These IDs must
also be in WORK_ALLOWED_USER_IDS and ALLOWED_USER_IDS. Requires named profile,
GitHub work and Native Agency observe or execute mode. Project must be in
WORK_PROJECTS, WORK_REPOS and ACTION_AUTO_PROJECTS. No implicit owner or
permission migration. Empty disables mission commands and invalidates existing
active grants on refresh. Ordinary work settings are unchanged.

Use explicitly addressed, non-forwarded human Telegram commands (replace the
bot username and project alias):

    /mission@ragnacobot start demo 60 10 3 12 | Implement a small tested CLI | CLI works; tests pass
    /mission@ragnacobot status
    /mission@ragnacobot revoke <exact mission_id>

Limits are minutes (1–1440), decision turns (1–100), tasks (1–30), and total
worker turns (1–200). Goal and criteria are mandatory. One active mission per
profile; a fresh owner command is required after expiry/revocation/invalidation.
The grant is immutable, bound to bot identity, owner, group, message, exact
repository and emergency-control generation. A duplicate message cannot
create another grant. The lifecycle is refreshed on polling/status/start/revoke;
changing control generation (including /stop then /resume), owner, group or
repository invalidates it. Any configured mission owner may revoke the exact
active grant. No grant automatically resumes. Historical startup backlog
commands do not create grants; only fresh messages reach the handler.

Mission rows live in the existing work event SQLite database, with a unique
active-mission constraint. Goals and criteria may be sensitive; keep local
state private. Commands do not trigger CWA, developer, Codex, peer messages
or GitHub writes. Budgets are stored but not spent; counters are zero until
later execution stages. No historical WorkState/decision rows are linked to a
mission, no automatic wakeups, no new merge or delegation permission. The
mission is a recorded planning envelope, NOT a grant usable by any existing
dispatcher. Status/revoke work while paused; a new mission cannot be created
while paused. No live verification implied by offline tests.

### NA-07.4 — Autonomous Cognition (initial observation)

Opt in with MISSION_COGNITION=on and MISSION_COGNITION_READ_ONLY_ACK=on
in the named profile, in addition to mission owner configuration. Only enable
if the main CWA account has no mutating native tools/connectors; a prompt and
the acknowledgement are not a technical sandbox. Uses a separate CWA client
with main auth, a separate persistent mission conversation, canonical final
text verification and the existing profile conversation ownership checks.

Only a NEW mission created while enabled arms a durable initial observation
in the same transaction as its grant. The Host polling loop reserves one
decision before starting a managed background thread; no human follow-up is
needed. Exactly one attempt, even if max_decisions is larger. Earlier missions
and unrelated WorkState events do not trigger cognition. The current mission
scope and generation must still be valid when the result is stored. /stop,
revocation and expiry prevent acceptance of stale results, but cannot recall
an already submitted remote request. A claimed turn interrupted by restart
is never retried automatically; disabling cognition discards unclaimed turns.
The decision budget counts the reservation even on failure or interruption.

Use /mission@BOT status to see cognition state, decision and proposed task.
No additional Telegram publication or executor call follows the turn.
Supported decisions: propose_task, wait, propose_finish, escalate. All are
observations only, never actual task creation, verified completion or a grant
for external writes. Existing developer, coordinator and main chat remain
separate. Multi-step missions, mission-bound work events, automatic execution
and result integration are later stages. The persistent mission conversation
may contain earlier mission context; the current grant is authoritative.
