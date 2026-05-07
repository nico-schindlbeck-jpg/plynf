# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Worker process settings — env-driven, with CLI overrides on top."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Configuration for the durable workflow worker.

    Reads from ``PLINTH_*`` environment variables; the CLI in
    ``__main__.py`` overrides any of these on a per-flag basis.

    Attributes:
        workspace_url: Base URL of the workspace service.
        gateway_url: Base URL of the gateway service.
        identity_url: Optional identity service URL.
        api_key: Bearer token used for both services.
        concurrency: Number of in-flight steps a single worker process
            executes simultaneously. Each slot polls + leases + executes
            independently.
        lease_ttl: Seconds the worker requests when acquiring a lease.
            The reaper will reclaim a lease that goes ``ttl`` past its
            last heartbeat.
        heartbeat_interval: Seconds between automatic per-lease heartbeats.
            Should be < ``lease_ttl / 3`` so a missed beat doesn't expire
            the lease.
        worker_heartbeat_interval: Seconds between worker-level (not
            lease-level) heartbeats sent to the workspace.
        poll_interval: Seconds the worker sleeps when no pending steps
            are available. Lower = lower latency but more polling.
        handlers_module: Importable module path that registers handlers
            via the ``@plinth.Plinth.workflow_handler`` decorator. The
            worker imports this at startup to populate the dispatch
            table.
        log_level: Logging verbosity.
        log_format: ``console`` (dev) or ``json`` (prod).
    """

    model_config = SettingsConfigDict(
        env_prefix="PLINTH_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    workspace_url: str = Field(default="http://localhost:7421")
    gateway_url: str = Field(default="http://localhost:7422")
    identity_url: str | None = Field(default=None)
    api_key: str = Field(default="local-dev")

    concurrency: int = Field(default=4, ge=1, le=64)
    lease_ttl: int = Field(default=60, ge=5)
    heartbeat_interval: int = Field(default=15, ge=1)
    worker_heartbeat_interval: int = Field(default=30, ge=1)
    poll_interval: float = Field(default=2.0, ge=0.1)

    handlers_module: str = Field(default="")

    log_level: str = Field(default="INFO")
    log_format: Literal["console", "json"] = Field(default="console")
