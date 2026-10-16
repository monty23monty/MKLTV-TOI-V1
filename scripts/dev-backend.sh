#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
.venv/bin/python -m alembic -c backend/alembic.ini upgrade head
exec .venv/bin/python -m uvicorn toi.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
