#!/bin/sh
# Dev start script: sync deps, create .env (first run), migrate the DB, then serve.
set -eu

cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
    exit 1
fi

uv sync

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — fill in the required secrets once:"
    echo "  APP_PASSWORD_HASH  uv run python -c \"from app.security import hash_password; print(hash_password('your-password'))\""
    echo "  SESSION_SECRET     uv run python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    echo "  SECRET_ENC_KEY     uv run python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
elif grep -Eq '^(APP_PASSWORD_HASH|SESSION_SECRET|SECRET_ENC_KEY)=( *)?$' .env; then
    echo "warning: APP_PASSWORD_HASH, SESSION_SECRET or SECRET_ENC_KEY is empty in .env"
fi

uv run alembic upgrade head
exec uv run uvicorn --factory app.main:create_app --reload --port "${PORT:-8000}"
