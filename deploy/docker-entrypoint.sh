#!/bin/sh
# App start script: migrate the database, then serve.
set -eu

uv run alembic upgrade head
exec uv run uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000
