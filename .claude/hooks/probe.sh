#!/usr/bin/env bash
# Probe: does a SessionStart hook finish before project MCP servers start?
echo "hook-begin $(date +%s.%N) remote=${CLAUDE_CODE_REMOTE:-unset}" >> /tmp/probe-hook.log
V="$HOME/.greenlight/venv"
python3 -m venv "$V" >/dev/null 2>&1 && "$V/bin/pip" install -q "greenlight @ git+https://github.com/rathojohn/greenlight" >/dev/null 2>&1 \
  && ln -sf "$V/bin/greenlight-mcp" /usr/local/bin/greenlight-mcp 2>/dev/null
echo "hook-end $(date +%s.%N) installed=$(command -v greenlight-mcp)" >> /tmp/probe-hook.log
echo "probe hook ran"
