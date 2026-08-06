#!/usr/bin/env pwsh
# Check for and install the CLI prerequisites this skill needs on Windows.
#
#   pwsh ./gtn-install-prereqs.ps1 [-DryRun]
#
# Installs: ffmpeg, python3, node/npm, the dev-browser CLI (+ its bundled
# Chromium). Prefers winget (built into Windows 10 1709+/11); falls back to
# Chocolatey if winget isn't available and choco is.
#
# Does NOT install:
#   - Chocolatey, if neither package manager is present (its installer runs a
#     script fetched from the internet with elevated access — left to the
#     user to opt into).
#   - The Griptape Nodes Desktop app itself (a GUI installer download, not a
#     package-manager package). This script only checks whether it's present
#     and points at https://griptapenodes.com otherwise.
#
# On macOS/Linux, use gtn-install-prereqs.sh instead.

param(
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Log($msg) { Write-Host $msg }

function Invoke-Run {
    param([string[]]$CommandAndArgs)
    if ($DryRun) {
        Write-Log "would run: $($CommandAndArgs -join ' ')"
        return $true
    }
    $cmd = $CommandAndArgs[0]
    # Splatting (@var) only works on a variable, and the 1..(N-1) range would
    # go descending (not empty) when there are no extra args — guard both.
    $cmdArgs = @()
    if ($CommandAndArgs.Length -gt 1) {
        $cmdArgs = $CommandAndArgs[1..($CommandAndArgs.Length - 1)]
    }
    & $cmd @cmdArgs
    return $LASTEXITCODE -eq 0
}

$PkgMgr = $null
if (Get-Command winget -ErrorAction SilentlyContinue) {
    $PkgMgr = "winget"
} elseif (Get-Command choco -ErrorAction SilentlyContinue) {
    $PkgMgr = "choco"
}

function Install-Package {
    param([string]$WingetId, [string]$ChocoId)
    switch ($PkgMgr) {
        "winget" { return Invoke-Run @("winget", "install", "-e", "--id", $WingetId, "--accept-source-agreements", "--accept-package-agreements") }
        "choco"  { return Invoke-Run @("choco", "install", "-y", $ChocoId) }
        default {
            Write-Log "ERROR: no supported package manager found (winget/choco) to install '$WingetId'."
            return $false
        }
    }
}

function Test-AndInstall {
    param([string]$Name, [scriptblock]$Probe, [scriptblock]$InstallFn)
    if (& $Probe) {
        Write-Log "[ok] $Name"
    } else {
        Write-Log "[missing] $Name — installing..."
        & $InstallFn | Out-Null
        if (& $Probe) {
            Write-Log "[installed] $Name"
        } else {
            Write-Log "[FAILED] $Name — install it manually"
        }
    }
}

Test-AndInstall -Name "ffmpeg" `
    -Probe { [bool](Get-Command ffmpeg -ErrorAction SilentlyContinue) } `
    -InstallFn { Install-Package -WingetId "Gyan.FFmpeg" -ChocoId "ffmpeg" }

Test-AndInstall -Name "python3" `
    -Probe { [bool](Get-Command python -ErrorAction SilentlyContinue) -or [bool](Get-Command python3 -ErrorAction SilentlyContinue) } `
    -InstallFn { Install-Package -WingetId "Python.Python.3" -ChocoId "python3" }

Test-AndInstall -Name "npm" `
    -Probe { [bool](Get-Command npm -ErrorAction SilentlyContinue) } `
    -InstallFn { Install-Package -WingetId "OpenJS.NodeJS.LTS" -ChocoId "nodejs-lts" }

if (Get-Command dev-browser -ErrorAction SilentlyContinue) {
    Write-Log "[ok] dev-browser"
} elseif (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Log "[missing] dev-browser — installing via npm..."
    Invoke-Run @("npm", "install", "-g", "dev-browser") | Out-Null
    if (Get-Command dev-browser -ErrorAction SilentlyContinue) {
        Write-Log "[installed] dev-browser — fetching its bundled Chromium..."
        Invoke-Run @("dev-browser", "install") | Out-Null
    } else {
        Write-Log "[FAILED] dev-browser"
    }
} else {
    Write-Log "[FAILED] dev-browser — npm is not available, install Node.js first"
}

$desktopBin = $env:GTN_DESKTOP_BIN
if (-not $desktopBin) {
    $onPath = Get-Command "griptape-nodes-desktop.exe" -ErrorAction SilentlyContinue
    if ($onPath) { $desktopBin = $onPath.Source }
}
if (-not $desktopBin) {
    $default = Join-Path $env:LocalAppData "ai.griptape.nodes.desktop\current\griptape-nodes-desktop.exe"
    if (Test-Path $default) { $desktopBin = $default }
}
if ($desktopBin) {
    Write-Log "[ok] Griptape Nodes Desktop app"
} else {
    Write-Log "[missing] Griptape Nodes Desktop app — download it from https://griptapenodes.com, or set `$env:GTN_DESKTOP_BIN if it's already installed somewhere non-standard"
}

if (Get-Command claude -ErrorAction SilentlyContinue) {
    try {
        $mcpList = & claude mcp list 2>$null
    } catch {
        $mcpList = ""
    }
    if ($mcpList -match "griptape-nodes") {
        Write-Log "[ok] griptape-nodes MCP server registered"
    } else {
        Write-Log "[missing] griptape-nodes MCP server — with the engine running, register it:"
        Write-Log "    claude mcp add --transport http griptape-nodes http://localhost:8125/mcp/ --scope user"
    }
} else {
    Write-Log "[skip] claude CLI not found, cannot check MCP server registration"
}

Write-Log "done."
