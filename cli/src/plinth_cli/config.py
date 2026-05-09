# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``~/.plinth/config.toml`` loader and resolved :class:`Config` object.

Resolution order (highest priority wins):

1. ``PLINTH_*`` environment variables
2. The selected profile's section in ``config.toml``
3. The ``[default]`` section in ``config.toml``
4. The hard-coded defaults from :mod:`plinth_cli.settings`

Unknown keys are silently ignored so config files can carry forward
across CLI versions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Python 3.11+ ships ``tomllib``; older interpreters fall back to ``tomli``
# (declared as a conditional dependency in ``pyproject.toml``).
try:  # pragma: no cover - import-fallback shim
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - py<3.11
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

from . import settings as _s

# ---------------------------------------------------------------------------
# Resolved config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """Effective configuration after profile + env-var resolution.

    All URL fields are populated; an empty string means "no URL configured"
    (commands that need it will surface a friendly error). The ``profile``
    field carries the name selected by ``--profile`` for display.
    """

    profile: str = _s.DEFAULT_PROFILE
    workspace_url: str = _s.DEFAULT_WORKSPACE_URL
    gateway_url: str = _s.DEFAULT_GATEWAY_URL
    identity_url: str = _s.DEFAULT_IDENTITY_URL
    dashboard_url: str = _s.DEFAULT_DASHBOARD_URL
    api_key: str = _s.DEFAULT_API_KEY
    output: str = _s.DEFAULT_OUTPUT
    timeout: float = 10.0
    config_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict of the resolved values."""

        return {
            "profile": self.profile,
            "workspace_url": self.workspace_url,
            "gateway_url": self.gateway_url,
            "identity_url": self.identity_url,
            "dashboard_url": self.dashboard_url,
            "api_key": _redact(self.api_key),
            "output": self.output,
            "timeout": self.timeout,
            "config_path": str(self.config_path) if self.config_path else None,
        }


def _redact(value: str) -> str:
    """Return ``value`` with all but the last 4 chars masked."""

    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(
    *,
    profile: str = _s.DEFAULT_PROFILE,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Load + resolve config for ``profile``.

    Args:
        profile: Profile name (``default`` or a key under ``[profiles.X]``).
        config_path: Override the default config location (used by tests).
        env: Override environment variables (used by tests). Defaults to
            ``os.environ``.

    Returns:
        A fully populated :class:`Config`. Missing config files do not
        raise — built-in defaults stand in. Malformed TOML raises
        :class:`ConfigError`.
    """

    env = env if env is not None else dict(os.environ)
    path = config_path if config_path is not None else _s.CONFIG_PATH

    file_data = _read_toml(path) if path.exists() else {}
    cfg = _resolve(file_data, profile=profile, env=env)
    cfg.config_path = path if path.exists() else None
    return cfg


class ConfigError(RuntimeError):
    """Raised on a malformed ``config.toml``."""


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse ``path`` as TOML; raise :class:`ConfigError` on failure."""

    try:
        with path.open("rb") as fp:
            return tomllib.load(fp)
    except tomllib.TOMLDecodeError as exc:  # type: ignore[attr-defined]
        raise ConfigError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc


def _resolve(
    file_data: dict[str, Any],
    *,
    profile: str,
    env: dict[str, str],
) -> Config:
    """Merge file_data, profile, and env into a :class:`Config`."""

    # Layer 1: built-in defaults.
    cfg = Config(profile=profile)

    # Layer 2: ``[default]`` section in the file.
    default_section = file_data.get("default") or {}
    _apply_section(cfg, default_section, env=env)

    # Layer 3: ``[profiles.<name>]`` section if non-default.
    if profile != _s.DEFAULT_PROFILE:
        profile_section = (file_data.get("profiles") or {}).get(profile)
        if profile_section is None:
            # Allow empty profile blocks; surface unknown-name as an error.
            raise ConfigError(
                f"Profile {profile!r} is not defined in the config file. "
                f"Available profiles: {sorted((file_data.get('profiles') or {}).keys())}"
            )
        _apply_section(cfg, profile_section, env=env)

    # Layer 4: env-var overrides have the highest precedence.
    _apply_env(cfg, env)
    return cfg


def _apply_section(cfg: Config, section: dict[str, Any], *, env: dict[str, str]) -> None:
    """Apply a TOML section onto ``cfg`` in place.

    ``api_key_env`` indirects through an environment variable so secrets
    don't have to live in the file (mirrors the spec example).
    """

    if "workspace_url" in section:
        cfg.workspace_url = str(section["workspace_url"])
    if "gateway_url" in section:
        cfg.gateway_url = str(section["gateway_url"])
    if "identity_url" in section:
        cfg.identity_url = str(section["identity_url"])
    if "dashboard_url" in section:
        cfg.dashboard_url = str(section["dashboard_url"])
    if "output" in section:
        cfg.output = str(section["output"])
    if "timeout" in section:
        cfg.timeout = float(section["timeout"])

    # API key precedence: explicit api_key > api_key_env indirection.
    if "api_key" in section and section["api_key"]:
        cfg.api_key = str(section["api_key"])
    elif "api_key_env" in section and section["api_key_env"]:
        var = str(section["api_key_env"])
        if var in env and env[var]:
            cfg.api_key = env[var]

    # Surface anything else under ``extra`` for diagnostics.
    for k, v in section.items():
        if k not in {
            "workspace_url",
            "gateway_url",
            "identity_url",
            "dashboard_url",
            "output",
            "timeout",
            "api_key",
            "api_key_env",
        }:
            cfg.extra[k] = v


def _apply_env(cfg: Config, env: dict[str, str]) -> None:
    """Layer in env-var overrides (top priority)."""

    mapping = {
        "PLINTH_WORKSPACE_URL": "workspace_url",
        "PLINTH_GATEWAY_URL": "gateway_url",
        "PLINTH_IDENTITY_URL": "identity_url",
        "PLINTH_DASHBOARD_URL": "dashboard_url",
        "PLINTH_API_KEY": "api_key",
        "PLINTH_OUTPUT": "output",
    }
    for env_key, attr in mapping.items():
        val = env.get(env_key)
        if val:
            setattr(cfg, attr, val)
    if env.get("PLINTH_TIMEOUT"):
        try:
            cfg.timeout = float(env["PLINTH_TIMEOUT"])
        except ValueError:
            # Don't fail on a bad env value; CLI usage is meant to be ergonomic.
            pass


# ---------------------------------------------------------------------------
# Writer (for ``plinth config init``)
# ---------------------------------------------------------------------------


SAMPLE_CONFIG = """\
# Plinth CLI configuration file.
# Generated by `plinth config init`. Edit freely — see
# https://github.com/your-org/plinth/blob/main/cli/README.md for the schema.

[default]
workspace_url = "{workspace_url}"
gateway_url = "{gateway_url}"
identity_url = "{identity_url}"
api_key = "{api_key}"
output = "{output}"

# Define additional profiles under [profiles.<name>]:
#
# [profiles.production]
# workspace_url = "https://workspace.plinth.example"
# gateway_url   = "https://gateway.plinth.example"
# identity_url  = "https://identity.plinth.example"
# api_key_env   = "PLINTH_PROD_API_KEY"
# output        = "json"
"""


def write_default_config(
    path: Path | None = None,
    *,
    workspace_url: str = _s.DEFAULT_WORKSPACE_URL,
    gateway_url: str = _s.DEFAULT_GATEWAY_URL,
    identity_url: str = _s.DEFAULT_IDENTITY_URL,
    api_key: str = _s.DEFAULT_API_KEY,
    output: str = _s.DEFAULT_OUTPUT,
) -> Path:
    """Write a starter config to ``path`` (defaults to ``~/.plinth/config.toml``).

    The parent directory is created with mode 0700 if missing — the file
    holds an API key, so we follow ssh-like permissions.
    """

    target = path if path is not None else _s.CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(
        SAMPLE_CONFIG.format(
            workspace_url=workspace_url,
            gateway_url=gateway_url,
            identity_url=identity_url,
            api_key=api_key,
            output=output,
        )
    )
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX FS
        pass
    return target


__all__ = [
    "Config",
    "ConfigError",
    "load_config",
    "write_default_config",
]
