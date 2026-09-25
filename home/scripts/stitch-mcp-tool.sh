#!/usr/bin/env bash
# stitch-mcp tool wrapper for Hermes.
# Usage: stitch-mcp-tool <tool_name> <json_arguments>
set -euo pipefail
TOOL_NAME="${1:-}"
ARG_JSON="${2:-null}"
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-hermes-agents-stitch}"
TMPDIR="$(mktemp -d)"
PIPE_IN="$TMPDIR/pipe.in"
PIPE_OUT="$TMPDIR/pipe.out"
mkfifo "$PIPE_IN" "$PIPE_OUT"
npx -y stitch-mcp < "$PIPE_IN" > "$PIPE_OUT" &
PID=$!
printf '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"%s","arguments":%s},"id":1}\n' \
  "$TOOL_NAME" "$ARG_JSON" > "$PIPE_IN"
cat "$PIPE_OUT"
kill "$PID" 2>/dev/null || true
rm -rf "$TMPDIR"
