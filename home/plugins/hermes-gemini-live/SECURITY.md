# Security policy

**Open an issue** for anything you can demonstrate. For anything that could be abused before a
fix lands, email the maintainer through their GitHub profile instead.

## What this plugin touches

- **Your `GEMINI_API_KEY`.** Read from the Hermes home's `.env` (or the environment) inside the
  backend process, and written into the upstream websocket URL — the Live API takes the
  credential that way, which is precisely why audio relays through the backend instead of the
  renderer. The key is redacted from every error string the plugin can produce
  (`hermes_gemini_live/live_client.py`), and `GET /status` reports presence, never the value.
- **Microphone audio.** Sent to `generativelanguage.googleapis.com` for the duration of a call
  and to your own localhost backend. Nothing is written to disk and nothing is sent anywhere else.
- **A Hermes run, in your profile's memory.** `POST /v1/runs` on the local api server means the
  voice model can ask Hermes to read files, run commands and browse. Runs are routed to
  `/p/<profile>/` with that profile's own `API_SERVER_KEY`, and a run that stops for an approval
  is reported to you in voice and on the panel rather than answered automatically; the plugin
  never calls `approve` on its own initiative.
- **No telemetry.** No usage reporting, no analytics, no remote self-updates. The plugin only
  changes when you pull it.

## Known bounds

- The Desktop relay route delegates authorisation to core's own dashboard gate
  (`hermes_cli.web_server_chat._ws_auth_ok`) and **fails closed**: if that gate is missing or
  raises, the websocket is refused rather than trusted.
- Run output is bounded to 4000 characters of speakable text, with markdown, code blocks and
  bare paths removed before the model reads it aloud.
- A call is a metered, always-listening socket: leaving it open keeps streaming audio upstream.
  Mute stops the uplink; Stop ends the session.

## Version support

Tested against Hermes Agent v0.21.x on Windows; CI runs the suite on ubuntu and Windows with
Python 3.11 and 3.12.
