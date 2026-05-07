# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Revocation helpers — kept thin so other services can import a tiny surface.

The bulk of revocation lives in :mod:`plinth_identity.store`. This module
exposes a stable, side-effect-free helper that callers (notably the workspace
+ gateway middleware) can use to ask "is this JTI revoked?" without pulling in
the full :class:`TokenStore`.
"""

from __future__ import annotations


class RevocationList:
    """An in-memory blocklist of revoked JTIs.

    Designed for downstream services that prefer the more-correct "phone-home
    to identity to validate" path over local secret verification. They can
    keep one of these around, periodically refresh it via
    :meth:`replace`, and consult :meth:`contains` on every request.
    """

    def __init__(self, jtis: set[str] | None = None) -> None:
        self._jtis: set[str] = set(jtis or set())

    def contains(self, jti: str) -> bool:
        return jti in self._jtis

    def add(self, jti: str) -> None:
        self._jtis.add(jti)

    def replace(self, jtis: set[str]) -> int:
        """Atomically swap the list contents. Returns the new size."""

        self._jtis = set(jtis)
        return len(self._jtis)

    def __len__(self) -> int:
        return len(self._jtis)

    def __contains__(self, jti: object) -> bool:
        return jti in self._jtis


__all__ = ["RevocationList"]
