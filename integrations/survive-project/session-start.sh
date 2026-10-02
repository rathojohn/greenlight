# Add to .claude/hooks/session-start.sh in survive-project, after the npm step.
# greenlight: flake history and the gate for playtest failures (docs/TESTING.md)
if command -v greenlight >/dev/null 2>&1; then
  echo "greenlight: ready"
elif pip install -q "greenlight @ git+https://github.com/rathojohn/greenlight" >&2; then
  echo "greenlight: installed. After a playtest run with failures: greenlight playtest gate"
else
  echo "greenlight: install failed. Retry: pip install \"greenlight @ git+https://github.com/rathojohn/greenlight\""
fi
