#!/usr/bin/env bash
# Lançador do lol-coach: lol <comando> [args]. Veja CLAUDE.md.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${LOL_PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python >/dev/null 2>&1 && python -c "import sys; sys.exit(sys.version_info < (3, 10))" >/dev/null 2>&1; then PY=python
  elif [ -x "$HOME/AppData/Local/mise/shims/python.exe" ]; then PY="$HOME/AppData/Local/mise/shims/python.exe"
  else echo "Python 3.10+ não encontrado (defina LOL_PYTHON)" >&2; exit 1; fi
fi
cmd="${1:-help}"; shift || true
case "$cmd" in
  list|fetch|fetch-many|static|icons) exec "$PY" "$ROOT/tools/fetch.py" "$cmd" "$@" ;;
  summarize|sum)                exec "$PY" "$ROOT/tools/summarize.py" "$@" ;;
  find|frames|events|at|series|players|games|lanes|paths|timers|decisions|fights|swings) exec "$PY" "$ROOT/tools/query.py" "$cmd" "$@" ;;
  dashboard|painel)             exec "$PY" "$ROOT/tools/dashboard.py" "$@" ;;
  vision)                       exec "$PY" "$ROOT/tools/vision.py" "$@" ;;
  calibrate)                    exec "$PY" "$ROOT/tools/calibrate.py" "$@" ;;
  winprob)                      exec "$PY" "$ROOT/tools/winprob.py" "$@" ;;
  junglers)                     exec "$PY" "$ROOT/tools/winprob.py" junglers "$@" ;;
  where)                        exec "$PY" "$ROOT/tools/landmarks.py" "$@" ;;
  render)                       exec "$PY" "$ROOT/tools/render.py" "$@" ;;
  spec|new)                     exec "$PY" "$ROOT/tools/scaffold.py" "$cmd" "$@" ;;
  validate)                     exec "$PY" "$ROOT/tools/zones.py" validate ;;
  selftest)                     exec "$PY" "$ROOT/tools/selftest.py" "$@" ;;
  watch)                        exec "$PY" "$ROOT/tools/watch.py" "$@" ;;
  pregame|pre)                  exec "$PY" "$ROOT/tools/pregame.py" "$@" ;;
  fixture)                      exec "$PY" "$ROOT/tools/fixture.py" "$@" ;;
  *) sed -n '/^## Ferramentas/,/^## /p' "$ROOT/CLAUDE.md" | head -60 ;;
esac
