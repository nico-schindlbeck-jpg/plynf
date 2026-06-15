# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plynf Authors
"""Outbound email: the verification + password-reset links.

Two backends, selected by config (same "empty env = safe fallback" rule as the
account store):

  * ConsoleMailer — the default. Records every message and logs the link, so
    the whole flow works in dev / demo / tests with ZERO configuration: the
    action link is right there in the dashboard logs.
  * SmtpMailer    — used when ``PLINTH_DASHBOARD_SMTP_HOST`` is set. Plain
    stdlib ``smtplib`` (no new dependency), STARTTLS by default.

Receipts are intentionally NOT sent from here. Stripe emails a branded receipt
+ invoice PDF natively once "email customers about successful payments" is
enabled in the Stripe dashboard — re-implementing that would be strictly worse.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import TYPE_CHECKING, Protocol

import structlog

if TYPE_CHECKING:
    from .settings import Settings

log = structlog.get_logger(__name__)


class Mailer(Protocol):
    """Anything that can deliver a plain-text message."""

    def send(self, to: str, subject: str, text: str) -> None: ...


class ConsoleMailer:
    """Records messages and logs them. The default when no SMTP host is set —
    dev, the offline demo, and the test suite all run through this."""

    def __init__(self) -> None:
        self.outbox: list[dict[str, str]] = []

    def send(self, to: str, subject: str, text: str) -> None:
        self.outbox.append({"to": to, "subject": subject, "text": text})
        # The body carries the action link; surface it so an operator can click
        # through without a real mailbox. Only reached when SMTP is unset.
        log.info("email.console", to=to, subject=subject, body=text)


class SmtpMailer:
    """Delivers via a real SMTP server using only the standard library."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool,
        from_addr: str,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._from = from_addr

    def send(self, to: str, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            if self._use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if self._username:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)
        log.info("email.sent", to=to, subject=subject)


def build_mailer(settings: Settings) -> Mailer:
    """SMTP when a host is configured, else the console fallback."""
    if settings.smtp_host:
        return SmtpMailer(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_use_tls,
            from_addr=settings.email_from,
        )
    return ConsoleMailer()


# -- message templates -------------------------------------------------------


def _send(mailer: Mailer, to: str, subject: str, text: str) -> bool:
    """Best-effort send: a mail-server hiccup must never 500 the request that
    triggered it (signup still succeeds; the user can re-request the email)."""
    try:
        mailer.send(to, subject, text)
        return True
    except Exception as e:  # mail delivery must never break the caller
        log.warning("email.send_failed", to=to, subject=subject, error=str(e))
        return False


def send_verification_email(mailer: Mailer, site_url: str, to: str, token: str) -> bool:
    link = f"{site_url.rstrip('/')}/app/verify?token={token}"
    text = (
        "Welcome to Plynf!\n\n"
        "Confirm this email address so we can reach you about your account:\n\n"
        f"  {link}\n\n"
        "The link is valid for 24 hours. If you didn't create a Plynf account, "
        "you can safely ignore this message."
    )
    return _send(mailer, to, "Confirm your Plynf email", text)


def send_password_reset_email(mailer: Mailer, site_url: str, to: str, token: str) -> bool:
    link = f"{site_url.rstrip('/')}/app/reset?token={token}"
    text = (
        "We received a request to reset your Plynf password.\n\n"
        "Choose a new password here:\n\n"
        f"  {link}\n\n"
        "The link is valid for 1 hour. If you didn't request this, ignore this "
        "message — your password stays unchanged."
    )
    return _send(mailer, to, "Reset your Plynf password", text)


__all__ = [
    "ConsoleMailer",
    "Mailer",
    "SmtpMailer",
    "build_mailer",
    "send_password_reset_email",
    "send_verification_email",
]
