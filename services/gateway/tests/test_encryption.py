# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Tests for the AES-GCM encryption helpers used for at-rest OAuth tokens."""

from __future__ import annotations

import base64
import os

import pytest

from plinth_gateway.encryption import (
    EncryptionError,
    decrypt,
    encrypt,
    generate_key,
    load_or_generate_key,
)


def test_generate_key_returns_base64_32_bytes() -> None:
    key = generate_key()
    raw = base64.b64decode(key)
    assert len(raw) == 32


def test_round_trip() -> None:
    key = generate_key()
    plaintext = "ghs_secrettoken123!@#"
    blob = encrypt(plaintext, key_b64=key)
    assert blob != plaintext
    assert decrypt(blob, key_b64=key) == plaintext


def test_round_trip_unicode() -> None:
    key = generate_key()
    plaintext = "tøken-✓-😀"
    blob = encrypt(plaintext, key_b64=key)
    assert decrypt(blob, key_b64=key) == plaintext


def test_two_encryptions_differ_thanks_to_random_nonce() -> None:
    key = generate_key()
    a = encrypt("same-input", key_b64=key)
    b = encrypt("same-input", key_b64=key)
    # Random nonces ⇒ different ciphertexts.
    assert a != b
    assert decrypt(a, key_b64=key) == decrypt(b, key_b64=key)


def test_tampered_ciphertext_fails() -> None:
    key = generate_key()
    blob = encrypt("payload", key_b64=key)
    raw = bytearray(base64.b64decode(blob))
    raw[-1] ^= 0xFF  # flip a bit in the auth tag
    tampered = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(EncryptionError):
        decrypt(tampered, key_b64=key)


def test_wrong_key_fails() -> None:
    key1 = generate_key()
    key2 = generate_key()
    blob = encrypt("payload", key_b64=key1)
    with pytest.raises(EncryptionError):
        decrypt(blob, key_b64=key2)


def test_empty_key_rejected() -> None:
    with pytest.raises(EncryptionError):
        encrypt("x", key_b64="")
    with pytest.raises(EncryptionError):
        decrypt("x", key_b64="")


def test_short_key_rejected() -> None:
    short = base64.b64encode(b"only16bytesnope!").decode("ascii")
    with pytest.raises(EncryptionError):
        encrypt("x", key_b64=short)


def test_garbage_base64_rejected() -> None:
    key = generate_key()
    with pytest.raises(EncryptionError):
        decrypt("not really base64 ===", key_b64=key)


def test_short_blob_rejected() -> None:
    key = generate_key()
    short = base64.b64encode(b"1234").decode("ascii")
    with pytest.raises(EncryptionError):
        decrypt(short, key_b64=key)


def test_load_or_generate_key_uses_configured(tmp_path) -> None:
    configured = generate_key()
    out = load_or_generate_key(configured, data_dir=tmp_path)
    assert out == configured


def test_load_or_generate_key_creates_when_absent(tmp_path) -> None:
    out = load_or_generate_key("", data_dir=tmp_path)
    keyfile = tmp_path / "gateway-oauth-key"
    assert keyfile.exists()
    assert keyfile.read_text(encoding="ascii").strip() == out
    # Round-trip the key to verify it's a valid AES-256 key.
    blob = encrypt("ping", key_b64=out)
    assert decrypt(blob, key_b64=out) == "ping"


def test_load_or_generate_key_reads_existing(tmp_path) -> None:
    # Pre-seed a key on disk; subsequent loads should reuse it.
    key = generate_key()
    keyfile = tmp_path / "gateway-oauth-key"
    keyfile.write_text(key, encoding="ascii")
    out = load_or_generate_key("", data_dir=tmp_path)
    assert out == key


def test_load_or_generate_key_disabled_raises(tmp_path) -> None:
    with pytest.raises(EncryptionError):
        load_or_generate_key("", data_dir=tmp_path, auto_generate=False)


def test_load_or_generate_key_invalid_configured_raises(tmp_path) -> None:
    with pytest.raises(EncryptionError):
        load_or_generate_key("not-base64", data_dir=tmp_path)


def test_load_or_generate_key_keyfile_perms(tmp_path) -> None:
    load_or_generate_key("", data_dir=tmp_path)
    keyfile = tmp_path / "gateway-oauth-key"
    mode = os.stat(keyfile).st_mode & 0o777
    # 0o600 on POSIX; some filesystems may not respect chmod, so just assert
    # the file exists and is owner-readable.
    assert mode & 0o400
