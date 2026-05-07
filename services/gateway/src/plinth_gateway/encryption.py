# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""AES-GCM encryption helpers for at-rest OAuth tokens.

Tokens stored in the ``oauth_connections`` table are encrypted at rest using a
server-held AES-256-GCM key. The same key encrypts both access and refresh
tokens, and is loaded from the ``PLINTH_OAUTH_ENCRYPTION_KEY`` setting (32
random bytes, base64-encoded).

The wire format for ciphertexts is ``base64(nonce || ciphertext || tag)``,
single-step base64 over the concatenation. ``cryptography``'s AESGCM combines
ciphertext+tag for us, so the persisted blob is ``nonce || aesgcm.encrypt(...)``.

Auto-generation: if no key is configured AND ``inbound_auth_required`` is False
(dev mode), :func:`load_or_generate_key` writes a fresh one to disk. Production
deployments must always pass ``oauth_encryption_key`` explicitly — auto-gen
emits a clear warning when it fires.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .logging_config import get_logger

_NONCE_BYTES = 12
_KEY_BYTES = 32

log = get_logger(__name__)


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""


def generate_key() -> str:
    """Return a fresh, base64-encoded 32-byte AES-256 key."""
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode("ascii")


def _decode_key(key_b64: str) -> bytes:
    """Decode and validate a base64-encoded 32-byte key."""
    if not key_b64:
        raise EncryptionError("encryption key is empty")
    try:
        raw = base64.b64decode(key_b64.encode("ascii"), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise EncryptionError(f"invalid base64 key: {exc}") from exc
    if len(raw) != _KEY_BYTES:
        raise EncryptionError(
            f"encryption key must decode to {_KEY_BYTES} bytes (got {len(raw)})"
        )
    return raw


def encrypt(plaintext: str, *, key_b64: str) -> str:
    """Encrypt ``plaintext`` (UTF-8) with AES-256-GCM.

    Args:
        plaintext: The string to encrypt.
        key_b64: Base64-encoded 32-byte AES key.

    Returns:
        Base64-encoded ``nonce || ciphertext || tag``.
    """
    key = _decode_key(key_b64)
    aesgcm = AESGCM(key)
    nonce = os.urandom(_NONCE_BYTES)
    ct_and_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    blob = nonce + ct_and_tag
    return base64.b64encode(blob).decode("ascii")


def decrypt(ciphertext_b64: str, *, key_b64: str) -> str:
    """Decrypt a value produced by :func:`encrypt`.

    Args:
        ciphertext_b64: The base64 blob from :func:`encrypt`.
        key_b64: Base64-encoded 32-byte AES key (same key used to encrypt).

    Raises:
        EncryptionError: If the blob is malformed, the key is wrong, or the
            authentication tag does not verify (tampered ciphertext).
    """
    key = _decode_key(key_b64)
    try:
        blob = base64.b64decode(ciphertext_b64.encode("ascii"), validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise EncryptionError(f"invalid base64 ciphertext: {exc}") from exc
    if len(blob) < _NONCE_BYTES + 16:
        raise EncryptionError("ciphertext too short to contain nonce + tag")
    nonce, ct_and_tag = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    aesgcm = AESGCM(key)
    try:
        return aesgcm.decrypt(nonce, ct_and_tag, associated_data=None).decode("utf-8")
    except InvalidTag as exc:
        raise EncryptionError("ciphertext failed authentication (tampered or wrong key)") from exc
    except UnicodeDecodeError as exc:
        raise EncryptionError(f"decrypted bytes are not valid UTF-8: {exc}") from exc


def load_or_generate_key(
    configured: str,
    *,
    data_dir: Path,
    auto_generate: bool = True,
) -> str:
    """Resolve the at-rest encryption key.

    If ``configured`` is non-empty, it's validated and returned as-is. Otherwise
    we look for a pre-generated key at ``data_dir / "gateway-oauth-key"``; if
    that's missing and ``auto_generate`` is True, we create a fresh one and
    write it there. Production callers should always pass ``configured``;
    auto-generation logs a clear warning.

    Args:
        configured: Value from ``Settings.oauth_encryption_key``.
        data_dir: The gateway's data dir.
        auto_generate: If True, create a key on-disk when none is configured.

    Returns:
        A valid base64-encoded 32-byte key.

    Raises:
        EncryptionError: If no key is configured, no on-disk key exists, and
            ``auto_generate`` is False.
    """
    if configured:
        # Validate the configured key fails loudly on misconfiguration.
        _decode_key(configured)
        return configured

    key_file = data_dir / "gateway-oauth-key"
    if key_file.exists():
        text = key_file.read_text(encoding="ascii").strip()
        _decode_key(text)
        return text

    if not auto_generate:
        raise EncryptionError(
            "PLINTH_OAUTH_ENCRYPTION_KEY is not set and auto-generation is disabled"
        )

    key = generate_key()
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key, encoding="ascii")
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        # On Windows / unusual filesystems the chmod may be a no-op.
        pass
    log.warning(
        "oauth.encryption_key.auto_generated",
        path=str(key_file),
        note="Production deployments MUST set PLINTH_OAUTH_ENCRYPTION_KEY explicitly.",
    )
    return key
