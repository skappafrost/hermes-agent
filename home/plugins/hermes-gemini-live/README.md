# hermes-gemini-live

[![tests](https://github.com/skappafrost/hermes-gemini-live/actions/workflows/tests.yml/badge.svg)](https://github.com/skappafrost/hermes-gemini-live/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Full-duplex **Gemini Live** voice inside [Hermes Agent](https://github.com/NousResearch/hermes-agent)'s
Desktop app, with real work delegated to Hermes while you keep talking.

Press the mic in the composer, speak, hear Gemini answer. Ask for something that needs files,
a terminal, the web, or memory, and the voice model hands the job to Hermes over the api server,
keeps the conversation going, and reads the outcome aloud when it lands. Several jobs can be in
flight at once; when one stops because it needs *your* permission, the lane says so out loud.

Your `GEMINI_API_KEY` stays server-side. This is not a preference: the Live API carries the key
in the websocket URL, so handing it to a renderer would put a credential in a browser process.
Audio relays through Hermes' own backend, which holds the key.

---

## Requires

- **Hermes Agent with the Desktop app.** The composer control is a Desktop plugin; the CLI,
  TUI and messaging gateways get the delegated work but no microphone.
- **A running gateway with the `api_server` platform** (default port **8642**). Without it the
  call still works as pure conversation and the panel states that Hermes work is unavailable.
- **A Google AI Studio API key.** The free tier works; there is no OAuth path here.
- **Nothing to install on the Python side.** `websockets`, `aiohttp` and `httpx` already ship in
  the Hermes runtime, and this plugin deliberately declares no dependencies (see
  [Why there is no `pyproject.toml`](#why-there-is-no-pyprojecttoml)).

## Install

One command, into the Hermes home your shell points at:

```bash
hermes plugins install skappafrost/hermes-gemini-live
```

It will say *"custom (unreviewed) source — not from the Hermes catalog"* until the catalog entry
below is merged upstream; that warning is about review status, not about the code. Because the
manifest declares `requires_env: GEMINI_API_KEY`, the installer prompts for the key and writes it
into the home's `.env` for you. If you install by hand, add it yourself:

```bash
# macOS/Linux ~/.hermes  ·  Windows %LOCALAPPDATA%\hermes
echo 'GEMINI_API_KEY=your-key' >> ~/.hermes/.env
```

Installing by hand works too, and is what you want if you plan to edit the plugin:

```bash
cd ~/.hermes/plugins && git clone https://github.com/skappafrost/hermes-gemini-live.git
cd ~/.hermes && hermes plugins enable hermes-gemini-live
```

**Restart Hermes Desktop.** Two independent reasons, both real: plugin API routes are mounted
once at web-server import, and the Desktop half is a *copy* under `desktop-plugins/` that is
only refreshed when the app scans at start-up. After any later edit to `desktop/plugin.js` the
app must restart again.

Verify without making a call:

```bash
hermes plugins validate plugins/hermes-gemini-live   # 14 checks, including the SDK surface lint
hermes plugins doctor hermes-gemini-live
python scripts/check_setup.py                        # key, model, and per-profile run lane
```

### One profile is not enough

Profiles are separate homes and **do not inherit the default home's plugins**. `hermes -p alpha
plugins enable hermes-gemini-live` answers *"No plugin named …"* until that profile has a folder
for it. Point each profile at one shared clone so there is a single source of truth:

```bash
# macOS/Linux
ln -s ~/.hermes/plugins/hermes-gemini-live ~/.hermes/profiles/alpha/plugins/hermes-gemini-live

# Windows (PowerShell) — a real junction, not a symlink you lack privileges for
New-Item -ItemType Junction -Target "$env:USERPROFILE\.hermes\plugins\hermes-gemini-live" `
  -Path "$env:USERPROFILE\.hermes\profiles\alpha\plugins\hermes-gemini-live"
```

On Windows, `ln -s` inside Git Bash silently performs a **recursive copy** instead of linking —
prefer PowerShell's `New-Item -ItemType Junction`. Each home then needs its own enable and, for
delegated runs, its own `API_SERVER_KEY` (see [Troubleshooting](#troubleshooting)).

## What you get

| | |
|---|---|
| **Composer control** | One row in the composer's action area: tap to start, tap Stop to hang up. Chrome is copied from core's own voice surfaces, so it reads as part of the app. |
| **State you can see** | `Connecting` · `Listening` · `Speaking` · `Thinking` · `Hermes is working · N tasks` · `Hermes needs you` · `Muted`, each with its own spinner, meter and clock — a long wait never looks like a crash. |
| **Mute without hanging up** | Drops the microphone at the last step before the socket: session, context and running tasks stay alive, and you stop paying audio-in tokens. |
| **Real work, delegated** | Three verbs, not thirty schemas: `hermes_task` starts a job, `hermes_tasks` reads the call's board, `hermes_task_update(task, action)` answers or redirects one of them. |
| **Survives your own clicking** | The call lives outside the React component's lifecycle, so opening a session or switching panes mid-sentence does not end it. |

## Configuration

Every value is read from the environment of the profile serving the call. A variable that is
**set but blank** is a refusal, never a silent fall-through. Put them next to the key in the
same home's `.env`.

| Key | Default | What it does |
|---|---|---|
| `GEMINI_LIVE_API_KEY` → `GEMINI_API_KEY` | — | Live credential, server-side only |
| `GEMINI_LIVE_MODEL` | `gemini-3.8-live` | Any Live model id; `models/` is added once. See [which one to pick](#the-delegate-is-where-the-models-differ-most) |
| `GEMINI_LIVE_THINKING_LEVEL` | `high` | Only for `...-extended-thinking`, which **requires** a level; `high`/`low` accepted, `minimal`/`auto` refused |
| `GEMINI_LIVE_VOICE` | `Aoede` | `Puck` `Charon` `Kore` `Fenrir` `Aoede` — **case-sensitive**. `Aoede` (soft) and `Kore` (firm) are the two documented female voices. Careful: the endpoint answers `setupComplete` to *any* `voiceName`, so this short whitelist is what catches a typo instead of silently giving you the default voice |
| `GEMINI_LIVE_SILENCE_MS` | `1200` | How long silence must run before the turn ends (400–5000) |
| `GEMINI_LIVE_PREFIX_MS` | `300` | Lead-in kept before speech (100–1500) |
| `GEMINI_LIVE_ECHO_CANCELLATION` | on | Keep ON unless you know why — see [loudspeaker note](#limits-stated-rather-than-discovered-mid-call) |
| `GEMINI_LIVE_NOISE_SUPPRESSION` · `GEMINI_LIVE_AUTO_GAIN` | on | Capture tuning |
| `GEMINI_LIVE_HALF_DUPLEX` | off | Stop sending mic while the model speaks; for a speaker that beats the canceller. Costs talk-over interruption |
| `GEMINI_LIVE_API_SERVER_URL` | `http://127.0.0.1:8642` | The gateway the lane posts runs to |
| `GEMINI_LIVE_API_SERVER_PROFILE` | derived from the serving home | Pin which profile delegated runs belong to |
| `GEMINI_LIVE_SESSION_KEY` | unset | Stable memory scope for delegated runs |
| `GEMINI_LIVE_INSTRUCTIONS` | — | Extra persona sentences appended to the system instruction |

## Troubleshooting

| Symptom | What it actually means | Fix |
|---|---|---|
| Panel says `backend: 404 {"detail":"Plugin not found"}` | **This home has the plugin disabled** — not a routing bug. The mount happens once, but the runtime gate re-reads `plugins.enabled` on every request. | `hermes plugins enable hermes-gemini-live` in the home the Desktop is serving |
| `Hermes work is unavailable: no API_SERVER_KEY for profile 'alpha'` | A multiplexed gateway authorises `/p/<profile>/v1/…` with **that profile's own** key and deliberately does not inherit the owner's (`api_server.py:1530-1545`). | Put `API_SERVER_KEY` in *that profile's* `.env` |
| Control call answers 404 `run_not_found` for a run you started | Ownership is by idempotency scope, and the submitting session key **is** that scope. | Send the same `X-Hermes-Session-Key` you submitted with |
| The model chats but never does the work | `gemini-3.8-live-extended-thinking` chooses to answer instead of delegating about 3 times in 5 — measured, not a wiring fault. | Use the default `gemini-3.8-live` |
| Your speaker makes the model interrupt itself | Server-side sensitivity is **not adjustable** (`threshold`, `voiceActivityConfig`, `serverVad`, `pushToTalk` all close 1007). | Keep `GEMINI_LIVE_ECHO_CANCELLATION` on, or set `GEMINI_LIVE_HALF_DUPLEX=1` |
| Edits to `desktop/plugin.js` do nothing | The app copies it at start-up. | Restart Hermes Desktop, then compare the two `plugin.js` mtimes |
| `uv lock` complains about two workspace members named the same | Only affects plugins that declare `pyproject.toml`/`python_dependencies`. | Not this plugin — see [below](#why-there-is-no-pyprojecttoml) |

## Measured against the real endpoint

These are not opinions; each was run against `generativelanguage.googleapis.com` with one key
and one question that a model cannot answer without calling something ("current time in Tokyo —
do not guess").

| Model | Setup | Function calling |
|---|---|---|
| `gemini-3.8-live` | accepted, audio at `audio/pcm;rate=24000` | **8/8** |
| `gemini-3.1-flash-live-preview` | accepted | 4/4, both the delegate and a canonical test function |
| `gemini-2.5-flash-native-audio-latest` | accepted | called |
| `gemini-3.8-live-extended-thinking` | accepted **only** with `thinkingConfig.thinkingLevel` | **2/5** with `behavior: NON_BLOCKING`, **0/5** without |

### The delegate is where the models differ most

Five trials per configuration, same question, same strong order in the system instruction, 90 s
window. Extended-thinking scores higher on speech quality and on agentic benchmarks, but the one
thing a voice lane needs — noticing it cannot know and asking for help — is unreliable there. The
2/5 is a **decision, not a dropped frame**: a second run kept the socket open 90 s past
`turnComplete` (Google warns `turnComplete` no longer means idle for that model) and the rate did
not move, while both successful calls landed *after* `turnComplete` (5.9 s and 13.5 s). In the
misses the model answered within the same ~3 s, i.e. it never hesitated. Nothing on the wire
changes it: `toolConfig`, `mode: ANY` and function scheduling are unsupported on this endpoint,
so a call cannot be mandated. Hence the default.

### There is no thinking knob on `gemini-3.8-live`, and it is already at maximum

Reading `usageMetadata.thoughtsTokenCount` on a trick arithmetic question:

| Sent in `generationConfig` | Thought tokens | Total |
|---|---|---|
| nothing | 941 | 980 |
| `thinkingBudget: -1` (dynamic) | 939 | 1004 |
| `thinkingBudget: -1` + `includeThoughts` | 1055 | 1655 |

`thinkingLevel` is refused outright ("Thinking level is not supported for this model"),
`thinkingBudget` parses but does nothing (941 vs 939 is the same number), and `includeThoughts`
raises only the *reported* count because the thoughts ride along in the response. The model
spends ~940 thought tokens per turn on its own.

### Wire contract, as accepted and as refused

Accepted in `setup`: `contextWindowCompression.slidingWindow`, `sessionResumption`,
`inputAudioTranscription`, `outputAudioTranscription`, `tools.functionDeclarations` (with
`behavior: NON_BLOCKING` on the **declaration** — on the Tool entry the endpoint answers
`Unknown name "behavior" at 'setup.tools[0]'`),
`realtimeInputConfig.automaticActivityDetection.{silenceDurationMs,prefixPaddingMs}`, and
`realtimeInput.text`.

Rejected with close code **1007**: `threshold`, `voiceActivityConfig`, `serverVad`, `pushToTalk`,
`turnCoverage`, `turnComplete` inside `realtimeInput`, `toolConfig` at both `setup` and
`setup.generation_config`, and `responseModalities: ["AUDIO","TEXT"]`.

## Limits, stated rather than discovered mid-call

- **Google owns the context trim.** The sliding window cuts at 80% of the window toward half of
  that and never trims the system instruction. Without it an audio-only session hard-stops near
  15 minutes. There is no way to delete context mid-session (`RemoveContext` has no Live
  equivalent), so a long call is bounded by that trim plus `goAway`.
- **`goAway` is terminal** — the socket dies right after. The panel says so instead of going quiet.
- **No upstream cancel or truncate.** Interruption is the server's VAD; the client can only stop
  its own playback.
- **64k tokens/minute** for these Live models (no RPM/RPD/TPD per Google). Audio tokens stream
  continuously, so the practical limit is call length, not message count.
- **A Hermes run is slow: 75.5 s measured** for a one-word answer, and 88 s for a real web
  lookup. That is why the lane answers the model with a receipt immediately and delivers the
  result later as a spoken note — a tool that waits is a dead microphone.
- **A result never talks over the model.** The note is queued and handed over in the next ~1 s
  gap in its speech, so it finishes the sentence it was saying before reading anything; several
  results queue in order, and a question from a run parked on an approval jumps that queue
  because the run behind it is standing still. The panel sees every transition immediately —
  only the note to the model waits.
- **The report is written for a voice model.** The delegated prompt tells the Hermes run to
  finish in prose that reads well aloud: conclusion first, at most four short sentences, no
  markdown or bare paths. The full answer stays in the Hermes session, so "đọc kỹ phần đó lại"
  is a follow-up question, not a re-run.
- **Multi-task is one Live session plus a board**, not a session per task. Four runs can be in
  flight (a run parked on an approval keeps its worker while it waits), each keeps the id the
  model saw in its receipt, and finished tasks stay on the board for the rest of the call.
  Closing the call is the only thing that forgets them: `sessionResumption` is enabled and its
  handle is forwarded, but nothing resumes from it yet, so a new call starts empty.
- **A delegated run is routed to `/p/<profile>/`** when the serving home is a named profile —
  an unprefixed run would resume in the *default* profile's memory.
- Not supported: mobile/Discord capture, wake word (core's wake reaction is unconditional and
  the microphone lease admits one owner — see below), appearing as a row in core's own voice
  engine dropdown (that menu starts core's engines and offers no plugin seam), and memory
  write-back from the panel.

### Why wake word is not here

Hermes ships a real wake-word listener (`tools/wake_word.py`). It cannot be reused from a
plugin lane for two reasons that are visible in core: `emitGatewayEvent` for `wake.detected`
returns void and core then starts its own chained conversation unconditionally
(`apps/desktop/src/app/contrib/wiring.tsx`), and the cross-process microphone lease
(`runtime/wake-word.lock`) admits exactly one owner. A plugin that tried its own listener would
either double-fire or be refused. Adding it properly is a core change, not a plugin one.

## Why there is no `pyproject.toml`

Deliberate, and load-bearing. Hermes builds a uv workspace out of every plugin that looks
buildable, and a buildable member keeps its declared `[project].name` — so one clone enabled in
two profiles gives `uv lock` *"Two workspace members are both named X"*, which breaks the
lockfile and therefore `hermes update`. Membership is decided by
`pm/plugin_declarations.py`: `is_member = not external and (pyproject or install_requirements)`.
With neither, this plugin is invisible to the resolver, is never written into a generation, and
cannot be blamed for a broken build during an update. Its three runtime imports
(`websockets`, `aiohttp`, `httpx`) already ship in the Hermes runtime.

`hermes update` therefore cannot touch this plugin. One command *can*: `hermes plugins update
hermes-gemini-live` runs `git stash push --include-untracked` and `git reset --hard HEAD` inside
the plugin folder, so keep any local edits committed.

## Development

```
__init__.py                  # register(): fail fast on a missing key, log the serving home
hermes_gemini_live/
  config.py                  # every read, fail-closed; model-conditional thinking; status_dict
  wire.py                    # setup frame + frame normaliser (the only place wire names live)
  live_client.py             # the socket; redacts the key from every error string
  agent_lane.py              # /v1/runs client, task board, worker pool, speakable text
  tools.py                   # the three function declarations
  relay.py                   # browser socket ⇄ Live socket, receipt-first tool handling
dashboard/plugin_api.py      # GET /status, WS /relay — mounted at /api/plugins/<name>/
desktop/plugin.js            # the composer control (uncompiled ESM, no JSX)
tests/                       # 86 tests, no network
```

Two rules that are easy to break by accident:

- **The Desktop half is a single uncompiled ESM file.** Only `@hermes/plugin-sdk`, `react` and
  `react/jsx-runtime` resolve, there is no build step and no JSX — everything goes through
  `createElement`. `hermes plugins validate` lints exactly this.
- **Tests must not touch the network or the microphone.** `tests/test_relay.py` drives the relay
  against a scripted Live peer, and each test ends through exactly one pump so no assertion
  depends on which task the loop finished first.

Run them with any Python that has `pytest`, `pytest-asyncio`, `httpx`, `starlette` and `fastapi`:

```bash
python -m pytest -q   # 86 passed here; 2 of them need a Hermes checkout on PYTHONPATH
                      # and skip cleanly without one, which is what CI sees
```

The renderer path — a real microphone, a real speaker, real interruptions — is only provable by
making a call. CI cannot cover it, and this README does not claim it does.

## How this differs from other voice plugins

The Hermes catalog already carries Gemini Live voice — notably **`gemini-live-bridge`**, which
this README describes from its own catalog entry, not from a guess. It is the better pick if you
want a *transcript-and-meters* experience: a status-bar minibar, model catalog, thinking level,
session token meter and auto-redial through session resumption, and it is documented as having
**no agent tools**.

This plugin's bet is the opposite: a single control in the composer, and the voice model being
**able to make Hermes do things** — files, terminals, the web, memory — while the conversation
continues. That is where almost all of the work here went, and it is why the lane has a task
board, an approval round-trip to the user's voice, and a measured table of which Live models
actually delegate (they do not all). Choose on that axis:

| You want | Pick |
|---|---|
| Transcript, token meter, model picker, reconnect | `gemini-live-bridge` |
| "Add this to the repo, then tell me what changed" by voice | this plugin |
| Both | nothing stops you — they are separate sockets, but each holds a microphone, so run one at a time |

Chained voice modes in core (`hermes-talk`, `hermes-speech`, `deepgram-voice`, …) are a third
thing again: STT → agent → TTS turns, not a duplex model.

## Publishing to the Hermes plugin catalog

`hermes plugins install hermes-gemini-live` needs a catalog entry merged into `hermes-agent` by a
maintainer: a YAML pin under `plugin-catalog/` with a **full 40-character commit SHA**, no
self-updating code, and declared capabilities matching what actually registers.
`plugin-catalog/hermes-gemini-live.yaml` in this repo is that entry, ready to copy into a PR from
the repository owner (rule 5 of the catalog's admission policy).

## License

MIT — see [LICENSE](LICENSE).
