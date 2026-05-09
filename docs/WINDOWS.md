# Sutando on Windows (with GitHub Copilot CLI)

This is a Windows-friendly subset of Sutando that uses **GitHub Copilot CLI**
as the core agent instead of Claude Code. No Gemini API key, no Twilio, no
Mac required.

```
Phone (voice-to-text)
   └─► PC web form (LAN)
       └─► tasks/<id>.txt
            └─► copilot-task-runner.mjs ──► copilot -p ... --allow-all-tools
                 └─► results/<id>.txt
                      └─► edge-tts-watcher.py ──► results/<id>.part-N.mp3 (live chunks)
                                            └──► results/<id>.mp3       (canonical replay)
                           └─► Browser shows text + plays audio (chunks autoplay live)
```

You dictate a task on your phone (using its built-in voice-to-text), tap
Send, and Sutando processes the task with GitHub Copilot CLI. The result
text is shown in the browser and synthesized to MP3 with Microsoft Edge's
free online TTS engine — no audio API key required.

---

## Prerequisites

| Tool | Version | Get it |
|---|---|---|
| Windows | 10 / 11 | — |
| PowerShell | 7+ recommended | https://learn.microsoft.com/powershell/scripting/install/installing-powershell |
| Node.js | 22+ | https://nodejs.org/ |
| Python | 3.10+ | https://python.org/ |
| GitHub Copilot CLI | 1.0+ | `npm install -g @github/copilot` then `copilot login` |
| Active Copilot subscription | — | https://github.com/features/copilot |

That's the full list. No Anthropic key, no Google AI Studio key, no Twilio
account.

---

## Quick start (5 minutes)

```powershell
# 1. Clone
git clone https://github.com/sonichi/sutando.git
cd sutando

# 2. Configure (only needed if you want LAN/phone access)
Copy-Item .env.windows.example .env
notepad .env

# 3. Start everything
.\sutando.cmd
```

> **Even simpler: double-click `sutando.cmd` in Explorer.** The window
> stays open so you can read the URL, then `pause` waits for any key.

The `sutando.cmd` launcher wraps `src\startup.ps1` with a few QoL extras:

```powershell
.\sutando.cmd                   # start (default)
.\sutando.cmd start --lan       # start, accept LAN connections
.\sutando.cmd stop              # stop all 3 services
.\sutando.cmd restart           # stop + start
.\sutando.cmd status            # show which services are running
.\sutando.cmd logs              # tail last 30 lines of every log
.\sutando.cmd open              # open the web form in your default browser
.\sutando.cmd help              # full reference
```

Add `--no-pause` to skip the "Press any key" prompt after `start` when
running from a terminal or CI.

`startup.ps1` (invoked under the hood) will:
- Verify `node`, `python`, `copilot` are on PATH
- Install `edge-tts` via pip (`python -m pip install --user edge-tts`)
- Start three background services:
  - **agent-api** (port 7843) — HTTP server with the web form
  - **task-runner** — polls `tasks/`, runs `copilot -p ...` per task
  - **tts-watcher** — polls `results/`, generates `.mp3` files

> Skipping `npm install`. The Windows path uses only Node's stdlib, so
> no node_modules are needed. (The repo's other Mac/voice deps include a
> bash-style postinstall script that breaks on Windows. To install them
> anyway, set `SUTANDO_NPM_INSTALL=1`.)

When it's up, open the URL it prints — **http://localhost:7843/** for
local-only, or **http://YOUR.LAN.IP:7843/?token=...** if you enabled LAN
access. Paste / dictate a task in the textarea, tap **Send Task**, and
watch the result stream in token-by-token while the audio autoplays.

### Connecting from your phone

1. In `.env`, set `AGENT_API_BIND=0.0.0.0` and `SUTANDO_API_TOKEN=<random>`.
2. Restart: `.\sutando.cmd restart --lan`
3. Open the printed `http://YOUR.LAN.IP:7843/?token=...` on your phone.
4. Bookmark / add to home screen.
5. Tap the textarea, hit the mic icon on your phone keyboard, dictate.

Both your PC and phone need to be on the same Wi-Fi.

> **Why a token is required for LAN.** The task runner runs `copilot -p ...
> --allow-all-tools --no-ask-user`, which gives the agent full access to
> the repo. Without auth, anyone on the LAN could submit arbitrary prompts
> *and* read every result + audio file by guessing `task-<ms-timestamp>`.
> The startup script refuses to bind to a non-loopback interface unless
> `SUTANDO_API_TOKEN` is set. When the token is set, **all three** of
> `/task`, `/result/<id>` and `/media/results/*.mp3` require it (header
> `Authorization: Bearer <tok>` or `?token=<tok>` query — `<audio>` uses
> the latter because HTML elements can't set custom headers).
>
> *Heads-up:* the token appears in the URL (`?token=...`), so it shows up
> in browser history, the address bar, and the agent-api request log.
> Treat it like a session secret — easy to rotate, low-stakes if a household
> member sees it. For higher security, run Sutando over a tunnel
> (e.g. `cloudflared tunnel`) and keep the bind on `127.0.0.1`.

---

## How it works

### Components

| File | Role |
|---|---|
| `sutando.cmd` | Top-level launcher (double-clickable) — wraps `startup.ps1` with `status` / `logs` / `open` / `help` |
| `src/startup.ps1` | Windows orchestrator — start/stop/restart all services |
| `src/agent-api.py` | HTTP server (port 7843) — web form + `POST /task` endpoint |
| `src/copilot-task-runner.mjs` | Node poller — runs Copilot CLI per task |
| `src/edge-tts-watcher.py` | Python poller — generates streaming `<id>.part-N.mp3` chunks and the canonical `<id>.mp3` from result text |
| `tasks/` | Inbox — new `<id>.txt` files trigger the runner |
| `results/` | Outbox — `<id>.txt` (text) + `<id>.mp3` (audio) + `<id>.part-N.mp3` (streamed chunks) + `<id>.parts.jsonl` (chunk manifest) |
| `tasks/archive/YYYY-MM/` | Processed task files (kept for audit / training) |
| `logs/` | One `.log` + `.log.err` per service |

### Task flow

1. Browser POSTs `{"task": "<text>", "from": "web"}` → `agent-api.py` writes
   `tasks/task-<ts>.txt` with format:
   ```
   id: task-1234567890
   timestamp: 2026-05-06T15:00:00
   task: <user text>
   source: api
   from: web
   ```
2. `copilot-task-runner.mjs` polls `tasks/` every 500 ms, sees the new file
   once its size+mtime is stable for two polls (avoids racing the writer).
3. Runner spawns `copilot --output-format json --no-color -p <wrapper>
   --allow-all-tools --no-ask-user --add-dir <repo>`. The wrapper prompt is
   fixed text that tells Copilot to read the task file from disk (so
   arbitrary user content never has to be escaped onto the command line).
   Copilot reads the file via its built-in tools. JSONL output is parsed
   line-by-line: `assistant.message_delta` events stream tokens into
   `results/<id>.partial`, and the canonical text from the final
   `assistant.message` event is written atomically to `results/<id>.txt`
   when the process exits.
4. The task file is moved to `tasks/archive/YYYY-MM/<id>.txt`.
5. `edge-tts-watcher.py` runs in two complementary modes per task:
   - **Streaming mode** — tails `<id>.partial` as it grows, splits the
     text at sentence boundaries (and on idle/max-length timeouts), and
     synthesises each slice into `<id>.part-<N>.mp3`. Each part is
     atomically written and then a JSON record is appended to
     `<id>.parts.jsonl` (the manifest the SSE endpoint streams to the
     browser).
   - **Full-text mode** — once `<id>.txt` is stable, synthesises the
     canonical full-result `<id>.mp3` for replay/download.
   The two modes write to disjoint files and run in parallel.
6. The browser opens **two** EventSource connections per task:
   - `/stream/<id>` — text deltas, rendered live as Copilot generates them.
   - `/audio-stream/<id>` — audio chunks. Each `event: part` adds a part
     URL to a queue; the warmed-up `<audio>` element plays them
     sequentially via its `ended` event. When the manifest emits
     `event: done`, the form HEAD-polls the canonical `<id>.mp3` and
     mounts it (with controls) for replay/download. Autoplay survives
     because the form pre-warms the `<audio>` element with a silent
     placeholder inside the Send-button click handler, claiming
     user-activation that persists across `src` swaps.

### Reliability features

- **Atomic writes** — every `tasks/`, `results/`, `*.mp3` write goes through
  `.tmp` + `rename`, so readers never see partial files.
- **Per-task timeout** — Copilot subprocess is killed after
  `COPILOT_TASK_TIMEOUT_MS` (default 10 min). The runner always writes a
  result file (success or failure), so the web UI never hangs.
- **Serial execution** — one Copilot subprocess at a time. Prevents
  concurrent CLI sessions from colliding and keeps Copilot quota usage
  predictable.
- **Stable-mtime check** — a `.txt` is only processed after its size+mtime
  has been the same for two polls (~1 second). Avoids racing the writer.

---

## Configuration

All optional. See `.env.windows.example` for the full list.

| Var | Default | Effect |
|---|---|---|
| `AGENT_API_BIND` | `127.0.0.1` | Set to `0.0.0.0` for LAN access |
| `SUTANDO_API_TOKEN` | (unset) | Required when binding to non-loopback |
| `COPILOT_BIN` | `copilot` | Path to copilot binary |
| `COPILOT_TASK_TIMEOUT_MS` | `600000` | 10 minutes |
| `COPILOT_POLL_INTERVAL_MS` | `500` | Task poll cadence |
| `EDGE_TTS_VOICE` | `en-US-AriaNeural` | Run `python -m edge_tts --list-voices` for the full list |
| `EDGE_TTS_RATE` | `+0%` | E.g. `+10%` for faster speech |
| `EDGE_TTS_MAX_CHARS` | `4000` | Result text is truncated for audio past this. Also caps total chars spoken across streamed chunks. |
| `EDGE_TTS_STREAM_DISABLE` | (unset) | Set to `1` to disable streaming chunks (only the canonical full-text mp3 is generated). Saves a few TTS calls per task at the cost of "audio plays after text completes" UX. |
| `EDGE_TTS_MIN_CHUNK_CHARS` | `80` | Minimum buffered chars before flushing a streamed chunk at a sentence boundary. Lower = lower latency, choppier audio. |
| `EDGE_TTS_MAX_CHUNK_CHARS` | `350` | Force flush after this many buffered chars even without a sentence terminator. |
| `EDGE_TTS_IDLE_FLUSH_MS` | `1500` | If text stops streaming for this long, flush whatever complete sentences are buffered (so the user hears trailing thoughts without waiting for the next sentence). |

### Picking a different voice

```powershell
python -m edge_tts --list-voices | Select-String "Neural"
# pick one, e.g. en-GB-SoniaNeural, then in .env:
# EDGE_TTS_VOICE=en-GB-SoniaNeural
```

---

## Common operations

```powershell
# Start
.\sutando.cmd                          # or: pwsh src\startup.ps1

# Stop
.\sutando.cmd stop                     # or: pwsh src\startup.ps1 -Stop

# Restart
.\sutando.cmd restart                  # or: pwsh src\startup.ps1 -Restart

# Health check (which services are running)
.\sutando.cmd status

# Tail logs
.\sutando.cmd logs                     # quick: last 30 lines of each
Get-Content -Wait logs\agent-api.log   # follow-mode
Get-Content -Wait logs\task-runner.log
Get-Content -Wait logs\tts-watcher.log

# Submit a task from CLI (write JSON to a temp file to avoid PowerShell escaping)
'{"from":"cli","task":"What is 2+2?"}' | Out-File -Encoding ascii -NoNewline q.json
curl.exe -X POST http://localhost:7843/task `
    -H "Content-Type: application/json" `
    --data-binary "@q.json"
Remove-Item q.json

# Check the result
curl.exe http://localhost:7843/result/task-<id>

# Process pending tasks once (no daemon)
node src\copilot-task-runner.mjs --once
```

---

## Troubleshooting

**`copilot not found on PATH`**
Run `npm install -g @github/copilot`, then start a new PowerShell window
(PATH is cached per shell).

**`Task timed out after 600 seconds`**
Either the task was genuinely too long, or Copilot is waiting on auth.
Open a new terminal and run `copilot -p "hi"` — if it prompts for login,
run `copilot login` and retry.

**`edge-tts install failed`**
Sutando still works — you just won't get audio. Try manually:
`python -m pip install --user edge-tts`, then restart Sutando.

**Audio doesn't play in the browser**
- Check `logs\tts-watcher.log.err` — most failures are transient network
  hiccups; the watcher retries.
- Browser autoplay is intentionally OFF; tap the play button on the
  `<audio>` element.

**`/result/<id>` returns 404 forever**
- Check `logs\task-runner.log.err` for Copilot errors.
- The task file should have moved to `tasks\archive\YYYY-MM\`. If it's
  still in `tasks/`, the runner isn't picking it up — make sure
  `pwsh src\startup.ps1` is running and `task-runner.pid` exists in
  `state\`.

**`AGENT_API_BIND=0.0.0.0 but SUTANDO_API_TOKEN is not set`**
Set a token in `.env`. This is a hard refusal — see the security note
above.

**Phone can't reach the form**
- Make sure both devices are on the same Wi-Fi.
- Add a Windows Firewall inbound rule for TCP 7843:
  `New-NetFirewallRule -DisplayName "Sutando 7843" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 7843`
- Some routers block client-to-client traffic ("AP isolation"); check your
  router settings.

---

## What's NOT included on Windows

This Windows port covers the **core text-task loop** only. Mac-specific
features that the upstream README documents are not wired up here:

- Voice agent (Gemini Live, real-time browser voice)
- Phone calls (Twilio + ngrok)
- Meeting join (Zoom / Google Meet)
- macOS-only skills (Reminders, iMessage, contacts via Contacts.app, etc.)
- Sutando.app menu-bar app (Swift/Cocoa)

Most skills under `skills/` won't run on Windows without modification.
The Windows path uses Copilot CLI's built-in capabilities (file edit,
shell, web fetch via the GitHub MCP server) directly.
