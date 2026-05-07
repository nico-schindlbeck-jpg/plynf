# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 The Plinth Authors
"""``plinth-bench`` CLI.

Subcommands:

* ``<workload>`` — run a single workload at a target RPS.
* ``all`` — run the standard suite (six workloads).
* ``compare A.json B.json`` — print a markdown comparison.
* ``list`` — list available workloads.

Examples:

    plinth-bench workspace_kv \\
        --base-url http://localhost:7421 --target-rps 500 --hold-seconds 60

    plinth-bench all --output-dir benchmarks/results

    plinth-bench compare results/A.json results/B.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from .compare import compare_runs
from .reporter import render_markdown_table, write_json
from .runner import RunnerConfig, WorkloadResult, run_workload
from .workloads import REGISTRY, STANDARD_SUITE, default_target_url


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plinth-bench",
        description="Stress benchmarks for the Plinth services.",
    )
    sub = parser.add_subparsers(dest="cmd")

    # `list`
    sub.add_parser("list", help="list available workloads")

    # `compare A.json B.json`
    cmp = sub.add_parser("compare", help="compare two run JSONs as markdown")
    cmp.add_argument("baseline", type=Path)
    cmp.add_argument("candidate", type=Path)

    # `all`
    all_p = sub.add_parser("all", help="run the standard suite")
    _add_run_args(all_p)
    all_p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmarks/results"),
        help="directory to write per-workload JSONs to",
    )

    # `<workload>` — register a sub-parser per workload.
    for name in REGISTRY:
        sp = sub.add_parser(name, help=f"run the {name} workload")
        _add_run_args(sp)
        sp.add_argument(
            "--output",
            type=Path,
            default=None,
            help="JSON output path (default: stdout only)",
        )

    return parser


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--base-url", default=None, help="target service URL")
    p.add_argument("--target-rps", type=int, default=500)
    p.add_argument("--ramp-seconds", type=int, default=30)
    p.add_argument("--hold-seconds", type=int, default=60)
    p.add_argument("--cooldown-seconds", type=int, default=10)
    p.add_argument("--initial-rps", type=int, default=10)
    p.add_argument("--inflight-cap", type=int, default=5000)
    p.add_argument("--timeout-seconds", type=float, default=30.0)
    p.add_argument(
        "--no-http2",
        action="store_true",
        help="disable HTTP/2 (default: enabled)",
    )
    p.add_argument(
        "--auth-token",
        default=None,
        help="bearer token sent on every request",
    )


def _config_from_args(args: argparse.Namespace) -> RunnerConfig:
    return RunnerConfig(
        target_rps=args.target_rps,
        ramp_seconds=args.ramp_seconds,
        hold_seconds=args.hold_seconds,
        cooldown_seconds=args.cooldown_seconds,
        initial_rps=args.initial_rps,
        inflight_cap=args.inflight_cap,
        request_timeout_seconds=args.timeout_seconds,
        http2=not args.no_http2,
    )


def _print_summary(result: WorkloadResult) -> None:
    print(f"\n=== {result.workload} ===")  # noqa: T201
    print(f"  url:        {result.target_url}")  # noqa: T201
    print(f"  target_rps: {result.target_rps}")  # noqa: T201
    print(f"  duration:   {result.ramp_seconds + result.hold_seconds + result.cooldown_seconds}s")  # noqa: T201
    print(f"  total:      {result.total_requests}")  # noqa: T201
    print(f"  successful: {result.successful}")  # noqa: T201
    print(f"  failed:     {result.failed} ({result.error_rate * 100:.2f}%)")  # noqa: T201
    lat = result.latency_ms
    print(  # noqa: T201
        f"  latency:    p50={lat['p50']:.2f}  p95={lat['p95']:.2f}  "
        f"p99={lat['p99']:.2f}  max={lat['max']:.2f}  mean={lat['mean']:.2f}"
    )
    if result.errors_by_type:
        print(f"  errors:     {result.errors_by_type}")  # noqa: T201


async def _run_one(
    name: str,
    base_url: str,
    config: RunnerConfig,
    auth_token: str | None,
) -> WorkloadResult:
    if name not in REGISTRY:
        raise SystemExit(f"unknown workload: {name}. Run `plinth-bench list`.")
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    workload_fn = REGISTRY[name].build()
    return await run_workload(
        workload_name=name,
        target_url=base_url,
        workload=workload_fn,
        config=config,
        headers=headers,
    )


def _cmd_list() -> int:
    print("Available workloads:")  # noqa: T201
    for name in REGISTRY:
        print(f"  {name:24s} default URL: {default_target_url(name)}")  # noqa: T201
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    if not args.baseline.exists():
        print(f"baseline not found: {args.baseline}", file=sys.stderr)  # noqa: T201
        return 2
    if not args.candidate.exists():
        print(f"candidate not found: {args.candidate}", file=sys.stderr)  # noqa: T201
        return 2
    md = compare_runs(args.baseline, args.candidate)
    print(md, end="")  # noqa: T201
    return 0


def _cmd_one(args: argparse.Namespace) -> int:
    name = args.cmd
    base_url = args.base_url or default_target_url(name)
    config = _config_from_args(args)

    result = asyncio.run(_run_one(name, base_url, config, args.auth_token))
    _print_summary(result)

    if args.output:
        out = write_json(result, args.output)
        print(f"\nwrote: {out}")  # noqa: T201
    return 0 if result.failed == 0 or result.error_rate < 0.5 else 1


def _cmd_all(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[WorkloadResult] = []
    timestamp = int(time.time())
    config = _config_from_args(args)

    for name in STANDARD_SUITE:
        url = args.base_url or default_target_url(name)
        try:
            result = asyncio.run(_run_one(name, url, config, args.auth_token))
        except Exception as exc:  # noqa: BLE001
            print(  # noqa: T201
                f"[{name}] FAILED to run: {exc}; skipping",
                file=sys.stderr,
            )
            continue
        results.append(result)
        _print_summary(result)
        out = write_json(result, args.output_dir / f"{name}_{timestamp}.json")
        print(f"  wrote: {out}")  # noqa: T201

    # Write a combined suite JSON too — handy for compare(A.json B.json).
    suite_path = args.output_dir / f"suite_{timestamp}.json"
    suite_path.write_text(json.dumps([r.to_dict() for r in results], indent=2))
    print(f"\nsuite: {suite_path}")  # noqa: T201
    print("\n" + render_markdown_table(results))  # noqa: T201
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.cmd == "list":
        return _cmd_list()
    if args.cmd == "compare":
        return _cmd_compare(args)
    if args.cmd == "all":
        return _cmd_all(args)
    return _cmd_one(args)


if __name__ == "__main__":
    raise SystemExit(main())
