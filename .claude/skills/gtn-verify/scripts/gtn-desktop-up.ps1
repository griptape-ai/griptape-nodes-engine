#!/usr/bin/env pwsh
# Bring Griptape Nodes Desktop up with CDP exposed on port 9222 and wait until
# the editor frame has attached. Idempotent: exits immediately if CDP is
# already live.
#
# Relaunching the app discards unsaved canvas state, so only run this when a
# restart is acceptable.
#
# Windows only — see gtn-desktop-up.sh for macOS/Linux.
#
#   pwsh ./gtn-desktop-up.ps1
#
# Set $env:GTN_DESKTOP_BIN if the app isn't found automatically (Velopack's
# per-user install path below is a best guess, not verified against a real
# install).

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Port = if ($env:GTN_CDP_PORT) { $env:GTN_CDP_PORT } else { "9222" }
$ProcName = "griptape-nodes-desktop"
$Cdp = "http://127.0.0.1:$Port"

function Test-CdpUp {
    try {
        Invoke-WebRequest -Uri "$Cdp/json/version" -TimeoutSec 2 -UseBasicParsing | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Test-EditorAttached {
    try {
        $targets = Invoke-RestMethod -Uri "$Cdp/json/list" -TimeoutSec 3
        return ($targets | Where-Object { $_.url -like "gtn-editor://*" }).Count -gt 0
    } catch {
        return $false
    }
}

function Resolve-DesktopBin {
    if ($env:GTN_DESKTOP_BIN) { return $env:GTN_DESKTOP_BIN }

    $onPath = Get-Command "$ProcName.exe" -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    $candidates = @(
        (Join-Path $env:LocalAppData "ai.griptape.nodes.desktop\current\$ProcName.exe"),
        (Join-Path $env:ProgramFiles "Griptape Nodes\$ProcName.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Get-DesktopProcesses {
    Get-Process -Name $ProcName -ErrorAction SilentlyContinue
}

function Stop-DesktopGracefully {
    $procs = Get-DesktopProcesses
    foreach ($p in $procs) {
        # CloseMainWindow posts WM_CLOSE, giving Electron a chance to run its
        # normal quit handlers. Stop-Process alone is a hard TerminateProcess
        # call and skips that entirely.
        $p.CloseMainWindow() | Out-Null
    }
}

if (Test-CdpUp) {
    Write-Output "cdp=already-live port=$Port"
} else {
    if ((Get-DesktopProcesses).Count -gt 0) {
        Write-Error "quitting running app (no CDP port)..." -ErrorAction Continue
        Stop-DesktopGracefully
        for ($i = 0; $i -lt 20; $i++) {
            if ((Get-DesktopProcesses).Count -eq 0) { break }
            Start-Sleep -Seconds 1
        }
        $remaining = Get-DesktopProcesses
        if ($remaining.Count -gt 0) {
            Write-Error "graceful quit timed out, force killing" -ErrorAction Continue
            $remaining | Stop-Process -Force
            Start-Sleep -Seconds 3
        }
    }

    $bin = Resolve-DesktopBin
    if (-not $bin) {
        Write-Error "ERROR: could not find $ProcName.exe. Set `$env:GTN_DESKTOP_BIN to its full path."
        exit 1
    }

    Write-Error "launching with --remote-debugging-port=$Port..." -ErrorAction Continue
    Start-Process -FilePath $bin -ArgumentList "--remote-debugging-port=$Port"

    $up = $false
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-CdpUp) { $up = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $up) {
        Write-Error "ERROR: CDP never came up on $Port"
        exit 1
    }
    Write-Output "cdp=launched port=$Port"
}

# The editor webview only attaches once the engine has booted. First launch
# after an update can take a while.
$attached = $false
for ($i = 0; $i -lt 90; $i++) {
    if (Test-EditorAttached) { $attached = $true; break }
    Start-Sleep -Seconds 2
}

if ($attached) {
    Write-Output "editor=attached"
} else {
    Write-Error "editor=not-attached (engine may still be starting, or is stopped)" -ErrorAction Continue
}

(Invoke-RestMethod -Uri "$Cdp/json/list" -TimeoutSec 3) | ForEach-Object {
    Write-Output ("target {0} {1}" -f $_.type, $_.url.Substring(0, [Math]::Min(100, $_.url.Length)))
}
