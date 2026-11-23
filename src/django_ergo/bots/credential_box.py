"""Encrypt a credential before a bot table stores it.

A bot's store function (for example the ``GOOGLE_OAUTH`` hook, docs/google-oauth.md) calls
``seal`` on a refresh token and a bot tool calls ``unseal`` when it needs the value. The key
is a Fernet key read from an environment variable the bot names. Nothing is derived from
``SECRET_KEY`` and a missing or malformed key fails closed: no plaintext fallback.

    sealed = credential_box.seal(token, key_env="PERSONAL_GOOGLE_TOKEN_KEY")
    token = credential_box.unseal(sealed, key_env="PERSONAL_GOOGLE_TOKEN_KEY")

Generate a key with ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.
Errors carry fixed text only: never the key, the value or the ciphertext."""

from __future__ import annotations

import os

PREFIX = "fernet:v1:"


class CredentialBoxError(Exception):
    """The key is missing or wrong, or the value isn't sealed; the message is safe to show."""


def _fernet(key_env: str):
    from cryptography.fernet import Fernet

    key = os.environ.get(key_env, "").strip()
    if not key:
        msg = "The encryption key isn't configured."
        raise CredentialBoxError(msg)
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        msg = "The encryption key isn't valid."
        raise CredentialBoxError(msg) from None


def seal(value: str, *, key_env: str) -> str:
    if not isinstance(value, str) or not value:
        msg = "There is nothing to encrypt."
        raise CredentialBoxError(msg)
    return PREFIX + _fernet(key_env).encrypt(value.encode()).decode()


def unseal(sealed: str, *, key_env: str) -> str:
    from cryptography.fernet import InvalidToken

    if not isinstance(sealed, str) or not sealed.startswith(PREFIX):
        msg = "The stored value isn't encrypted."
        raise CredentialBoxError(msg)
    try:
        return _fernet(key_env).decrypt(sealed[len(PREFIX) :].encode()).decode()
    except InvalidToken:
        msg = "The stored value can't be decrypted with this key."
        raise CredentialBoxError(msg) from None
