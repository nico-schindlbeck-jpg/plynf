# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""Command groups for the ``plinth`` CLI.

Each module exposes a ``group`` attribute (a :class:`click.Group`) that
:func:`plinth_cli.main._attach_subcommands` mounts onto the root command.
"""

from __future__ import annotations
