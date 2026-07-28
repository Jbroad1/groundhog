#!/usr/bin/env pwsh
# Install groundhog into your Claude Code skills directory:
#   ~/.claude/skills/groundhog
# Run from a clone of the repo:  pwsh ./install.ps1
$ErrorActionPreference = 'Stop'

$src  = $PSScriptRoot
$dest = Join-Path $env:USERPROFILE '.claude/skills/groundhog'

New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
Copy-Item -Path $src -Destination $dest -Recurse -Force

# Strip VCS + build cruft from the installed copy.
$git = Join-Path $dest '.git'
if (Test-Path $git) { Remove-Item -Recurse -Force $git }
Get-ChildItem -Path $dest -Recurse -Force -Directory -Filter '__pycache__' |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $dest -Recurse -Force -Filter '*.pyc' |
    Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "Installed groundhog -> $dest"
Write-Host "Verify: python `"$dest/scripts/validate_skill.py`" `"$dest`""
