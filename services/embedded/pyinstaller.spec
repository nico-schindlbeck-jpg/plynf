# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for plynf-embedded.
#
# Builds a single-file binary that wraps all five core services.
# Cross-platform via GitHub Actions matrix in release-embedded.yml.
#
# Local build:
#   pip install pyinstaller
#   pyinstaller pyinstaller.spec --clean
#
# Output:  dist/plynf-embedded  (single file, ~200-280 MB depending on platform)

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
import os
import sys

ROOT = os.path.abspath(os.path.dirname(SPEC) if "SPEC" in globals() else ".")
REPO = os.path.abspath(os.path.join(ROOT, "..", ".."))


# ── Collect static assets from sibling services ──────────────────────
# Each sibling service ships templates, migrations, static dashboard
# assets. PyInstaller needs to know where they live so they end up in
# the bundle's _MEIPASS at runtime.

datas = []

# Dashboard SPA (the entire static/ tree)
datas += collect_data_files("plinth_dashboard", includes=["**/*"])

# Workspace + identity SQL migrations
datas += collect_data_files("plinth_workspace", includes=["migrations/**/*"])
datas += collect_data_files("plinth_identity",  includes=["migrations/**/*"])

# Mock-MCP fixtures (so demos work in embedded mode)
datas += collect_data_files("mock_mcp", includes=["fixtures/**/*"])

# Tiktoken / sentence-piece BPE tables, if those packages are vendored
# at install time. Guarded so a missing tiktoken doesn't fail the build.
try:
    datas += collect_data_files("tiktoken_ext")
except Exception:
    pass


# ── Hidden imports ───────────────────────────────────────────────────
# uvicorn loads protocol implementations dynamically; PyInstaller's
# static analyzer misses them. Same for FastAPI's optional schema
# generators.

hiddenimports = []
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("plinth_workspace")
hiddenimports += collect_submodules("plinth_gateway")
hiddenimports += collect_submodules("plinth_identity")
hiddenimports += collect_submodules("plinth_dashboard")
hiddenimports += collect_submodules("mock_mcp")
hiddenimports += [
    "asyncpg",               # if/when Postgres mode is added
    "aiosqlite",             # the default storage driver
    "httpx",
    "websockets.legacy",     # uvicorn websocket fallback
    "websockets.protocol",
]


# ── Excludes (shrink the binary) ─────────────────────────────────────
# Things that get pulled in transitively but we never use.

excludes = [
    "tkinter",
    "matplotlib",
    "PIL.ImageTk",
    "numpy.testing",
    "pandas",          # we don't use pandas at runtime
    "IPython",
    "jupyter",
    "notebook",
    "pytest",          # test deps must not be in the runtime binary
    "_pytest",
]


# ── Build configuration ──────────────────────────────────────────────

a = Analysis(
    [os.path.join("src", "plynf_embedded", "__main__.py")],
    pathex=[os.path.join("src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="plynf-embedded",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX often breaks signatures on macOS notarization
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,       # we want stdout/stderr for logs
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,   # set per-platform in CI matrix
    codesign_identity=None,  # signing handled outside PyInstaller in CI
    entitlements_file=None,
)
