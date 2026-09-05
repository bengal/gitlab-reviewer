"""Fernet secret encryption and PBKDF2 password hashing."""

import pytest
from cryptography.fernet import InvalidToken

from app.security import decrypt_secret, encrypt_secret, hash_password, verify_password

KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_fernet_round_trip():
    token = encrypt_secret("s3cret-gitlab-token", KEY)
    assert token != "s3cret-gitlab-token"
    assert decrypt_secret(token, KEY) == "s3cret-gitlab-token"


def test_empty_string_passthrough():
    assert encrypt_secret("", KEY) == ""
    assert decrypt_secret("", KEY) == ""


def test_decrypt_garbage_raises():
    with pytest.raises(InvalidToken):
        decrypt_secret("not-a-fernet-token", KEY)


def test_missing_key_raises(monkeypatch):
    # Empty (not unset): a process env var shadows the repo's .env, which
    # pydantic-settings still loads via env_file.
    monkeypatch.setenv("SECRET_ENC_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="SECRET_ENC_KEY"):
            encrypt_secret("x")
    finally:
        get_settings.cache_clear()


def test_hash_password_format():
    stored = hash_password("hunter2")
    parts = stored.split("$")
    assert len(parts) == 4
    assert parts[0] == "pbkdf2"
    assert int(parts[1]) > 0
    bytes.fromhex(parts[2])  # valid salt hex
    bytes.fromhex(parts[3])  # valid digest hex


def test_verify_password_round_trip_and_reject():
    stored = hash_password("hunter2")
    assert verify_password("hunter2", stored)
    assert not verify_password("hunter3", stored)


def test_hash_is_salted():
    assert hash_password("same") != hash_password("same")


def test_verify_malformed_stored():
    assert not verify_password("x", "")
    assert not verify_password("x", "pbkdf2$1$2")
    assert not verify_password("x", "bcrypt$1$aa$bb")
    assert not verify_password("x", "pbkdf2$notanumber$aa$bb")
    assert not verify_password("x", "pbkdf2$1$zz$bb")
