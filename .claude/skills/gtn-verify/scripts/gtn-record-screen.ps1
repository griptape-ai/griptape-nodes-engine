#!/usr/bin/env pwsh
# Record the actual Windows screen, including native window chrome, menus and
# file dialogs that CDP screenshots cannot see.
#
#   pwsh ./gtn-record-screen.ps1 [-Secs N] [-Out PATH] [-NoFocus]
#
# Uses ffmpeg's gdigrab. There is no macOS-style permission prompt to probe
# for on Windows, so this just runs.

param(
    [int]$Secs = 10,
    [string]$Out = "$env:TEMP\gtn-screen.mp4",
    [switch]$NoFocus
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Error "ERROR: ffmpeg not installed"
    exit 1
}

if (-not $NoFocus) {
    $proc = Get-Process -Name "griptape-nodes-desktop" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc) {
        # SetForegroundWindow/ShowWindow aren't exposed as PowerShell cmdlets;
        # P/Invoke via Add-Type is the standard way to reach them.
        Add-Type -Name Win32 -Namespace GtnVerify -MemberDefinition '
            [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
            [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
        '
        [GtnVerify.Win32]::ShowWindow($proc.MainWindowHandle, 9) | Out-Null  # SW_RESTORE
        [GtnVerify.Win32]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null
        Start-Sleep -Seconds 1
    }
}

Write-Error "recording screen for ${Secs}s..." -ErrorAction Continue
& ffmpeg -y -loglevel error `
    -f gdigrab -framerate 30 -i desktop `
    -t $Secs -c:v libx264 -preset veryfast -pix_fmt yuv420p -vf "scale=1600:-2" `
    $Out

Write-Output "out=$Out secs=$Secs"
