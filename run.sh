#!/usr/bin/env sh
# One command to try Customer Care locally: ./run.sh
# Works without any key (offline rules). Add GEMINI_API_KEY to .env for real Gemma 4.
set -e
cd "$(dirname "$0")"
PY="$(command -v python3.14 || command -v python3.13 || command -v python3.12 || command -v python3)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env
echo "Customer Care: http://localhost:${PORT:-8000}  (панель агента · /chat · /dashboard · /settings)"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
