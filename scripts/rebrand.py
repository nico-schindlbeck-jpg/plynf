"""One-shot rebrand: Plinth → Plynf, plynf.com → plynf.com.

Two scopes:

  AGGRESSIVE — every brand and URL reference (Markdown, Astro, HTML,
  CSS, TOML, YAML, JSON, TXT). Safe because these are text surfaces.

  CONSERVATIVE — Python/TS source only touched for URL strings
  (plynf.com → plynf.com) and obvious brand strings in docstrings /
  comments. Class names (`Plinth`), module identifiers
  (`plinth_workspace`), and import statements stay untouched to avoid
  breaking the SDK API contract and 2,867 tests. Phase 2 of the
  rebrand can rename modules cleanly with test-passing CI.

Run from repo root:
    python scripts/rebrand.py [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Directories never touched.
EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "_astro",
    ".astro", ".idea", ".vscode", "results",
}

# Files never touched (lock files, generated, binary).
EXCLUDE_FILENAMES = {
    "package-lock.json", "yarn.lock", "uv.lock", "poetry.lock",
    "Pipfile.lock", "Cargo.lock",
}

EXCLUDE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".mov", ".webm", ".mp3", ".wav",
    ".zip", ".tar", ".gz", ".bz2", ".whl",
    ".lock", ".lockb", ".jsonl",  # transcript files
}

AGGRESSIVE_SUFFIXES = {
    ".md", ".astro", ".html", ".htm", ".css", ".toml", ".yml", ".yaml",
    ".json", ".txt", ".svg", ".jsx", ".tsx", ".vue", ".svelte",
    ".rst", ".adoc",
}

CONSERVATIVE_SUFFIXES = {
    ".py", ".ts", ".js", ".go", ".swift", ".kt", ".rs", ".java", ".c", ".cpp", ".h",
    ".sh", ".bash", ".zsh", ".fish", ".ps1",
    "Makefile", ".mk", ".dockerfile", "Dockerfile",
}

# Aggressive replacements: applied to text content of qualifying files.
# Order matters — domain first so we don't double-replace.
AGGRESSIVE_RULES: list[tuple[str, str]] = [
    # URLs
    (r"https://plinth\.dev", "https://plynf.com"),
    (r"http://plinth\.dev", "http://plynf.com"),
    (r"\bplinth\.dev\b", "plynf.com"),
    # Email domain
    (r"@plinth\.dev\b", "@plynf.com"),
    # Brand
    (r"\bPLINTH\b", "PLYNF"),
    (r"\bPlinth\b", "Plynf"),
    # Lowercase brand in text contexts (NOT in identifiers)
    # We rely on file-type filtering — Python/TS files take the
    # conservative path.
]

# Conservative replacements: URLs and email only.
# Brand words and identifiers stay.
CONSERVATIVE_RULES: list[tuple[str, str]] = [
    (r"https://plinth\.dev", "https://plynf.com"),
    (r"http://plinth\.dev", "http://plynf.com"),
    (r"\bplinth\.dev\b", "plynf.com"),
    (r"@plinth\.dev\b", "@plynf.com"),
]


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in EXCLUDE_SUFFIXES:
        return False
    if path.name in EXCLUDE_FILENAMES:
        return False
    try:
        # Read first 8KB to sniff binary.
        with path.open("rb") as f:
            chunk = f.read(8192)
        if b"\x00" in chunk:
            return False
    except OSError:
        return False
    return True


def file_rules(path: Path) -> list[tuple[str, str]]:
    suffix = path.suffix.lower()
    name = path.name
    # Conservative: source code that would break on identifier rename.
    if suffix in CONSERVATIVE_SUFFIXES or name in {"Makefile", "Dockerfile"}:
        return CONSERVATIVE_RULES
    # Aggressive: text/doc/marketing.
    if suffix in AGGRESSIVE_SUFFIXES:
        return AGGRESSIVE_RULES
    # No-extension files (LICENSE, README without extension, etc.)
    if suffix == "" and name in {"LICENSE", "README", "AUTHORS", "CHANGELOG", "NOTICE"}:
        return AGGRESSIVE_RULES
    # Default: skip
    return []


def should_process(path: Path) -> bool:
    # Excluded if any ancestor is in EXCLUDE_DIRS.
    for part in path.parts:
        if part in EXCLUDE_DIRS:
            return False
    if not path.is_file():
        return False
    if not is_text_file(path):
        return False
    if not file_rules(path):
        return False
    return True


def process(path: Path, dry_run: bool) -> tuple[int, int]:
    """Return (n_substitutions, did_write)."""
    rules = file_rules(path)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return (0, 0)
    new = text
    n = 0
    for pattern, repl in rules:
        new, k = re.subn(pattern, repl, new)
        n += k
    if n == 0:
        return (0, 0)
    if dry_run:
        return (n, 0)
    path.write_text(new, encoding="utf-8")
    return (n, 1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Just count, don't write")
    parser.add_argument("--verbose", "-v", action="store_true", help="List touched files")
    args = parser.parse_args()

    aggressive_total = 0
    conservative_total = 0
    aggr_files = 0
    cons_files = 0

    for path in sorted(ROOT.rglob("*")):
        if not should_process(path):
            continue
        rules = file_rules(path)
        n, wrote = process(path, args.dry_run)
        if n == 0:
            continue
        rel = path.relative_to(ROOT)
        if rules is AGGRESSIVE_RULES:
            aggressive_total += n
            aggr_files += 1
            if args.verbose:
                print(f"  AGG  {n:>4}  {rel}")
        else:
            conservative_total += n
            cons_files += 1
            if args.verbose:
                print(f"  cons {n:>4}  {rel}")

    print(f"")
    print(f"Aggressive scope:    {aggressive_total:>5} substitutions across {aggr_files} files")
    print(f"Conservative scope:  {conservative_total:>5} substitutions across {cons_files} files")
    print(f"Total:               {aggressive_total + conservative_total:>5}")
    if args.dry_run:
        print(f"(dry-run — no files written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
