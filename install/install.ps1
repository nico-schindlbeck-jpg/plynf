# Plynf Windows installer (PowerShell)
# Usage:
#   irm https://plynf.com/install.ps1 | iex
#
# Requirements: Docker Desktop for Windows, PowerShell 5.1+
# For the one-liner path, Plynf runs via Docker Compose on Windows.
# The Go CLI binary is the primary interface — download it from the
# GitHub Release assets or use 'winget install plynf' (coming soon).
#
# This script:
#   1. Checks Docker is running
#   2. Downloads deploy/compose.prod.yml
#   3. Pulls images and starts the stack
#   4. Opens the dashboard at http://localhost:7420

param(
    [string]$Version = "latest",
    [switch]$DryRun = $false,
    [switch]$SkipOpen = $false
)

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/nico-schindlbeck-jpg/plynf"
$ComposeUrl = "$RepoUrl/raw/main/deploy/compose.prod.yml"
$PlynfHome  = Join-Path $env:USERPROFILE ".plynf"

Write-Host "Plynf installer for Windows" -ForegroundColor Cyan
Write-Host "Version : $Version"
Write-Host "PlynfHome : $PlynfHome"

# ── Check Docker ───────────────────────────────────────────────────────────
$dockerCmd = Get-Command "docker" -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Error @"
Docker not found. Install Docker Desktop for Windows first:
  https://docs.docker.com/desktop/install/windows-install/
"@
    exit 1
}

try {
    docker info | Out-Null
} catch {
    Write-Error "Docker daemon is not running. Start Docker Desktop and retry."
    exit 1
}

Write-Host "✔ Docker is running" -ForegroundColor Green

# ── Create install dir ─────────────────────────────────────────────────────
if (-not (Test-Path $PlynfHome)) {
    if (-not $DryRun) {
        New-Item -ItemType Directory -Path $PlynfHome | Out-Null
    }
    Write-Host "Created $PlynfHome"
}

# ── Download compose file ───────────────────────────────────────────────────
$ComposeFile = Join-Path $PlynfHome "compose.prod.yml"
Write-Host "Downloading compose file..."
if (-not $DryRun) {
    $versionTag = if ($Version -eq "latest") { "main" } else { $Version }
    $url = "$RepoUrl/raw/$versionTag/deploy/compose.prod.yml"
    Invoke-WebRequest -Uri $url -OutFile $ComposeFile -UseBasicParsing
}

# ── Set PLYNF_VERSION env var ───────────────────────────────────────────────
if ($Version -ne "latest") {
    $env:PLYNF_VERSION = $Version.TrimStart("v")
}

# ── Pull and start ──────────────────────────────────────────────────────────
Write-Host "Pulling images (first run ~1 GB, may take a minute)..."
if (-not $DryRun) {
    docker compose -f $ComposeFile pull
    docker compose -f $ComposeFile up -d --wait --wait-timeout 180
}

Write-Host "✔ All services started" -ForegroundColor Green
Write-Host ""
Write-Host "Dashboard: http://localhost:7420"
Write-Host "Run 'plynf doctor' (after installing the CLI binary) to verify all services."

if (-not $SkipOpen -and -not $DryRun) {
    Start-Process "http://localhost:7420"
}
