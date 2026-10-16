#!/bin/sh
set -eu

# Keep one API worker until the in-process WebSocket bus is replaced with pub/sub.
python -m alembic -c backend/alembic.ini upgrade head
exec python -m uvicorn toi.main:app --app-dir backend --host 0.0.0.0 --port 8000 --workers 1
