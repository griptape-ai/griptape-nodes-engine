#!/usr/bin/env pwsh
# Convert a recording to a small GIF for inline preview in Claude Code chat.
#
#   powershell -File ./gtn-to-gif.ps1 -In IN.mp4 -Out OUT.gif [-Fps 8] [-Width 480]
#
# See gtn-to-gif.sh for the rationale (two-pass palette approach avoids
# banding/dithering on flat-color UI screenshots).

param(
    [Parameter(Mandatory = $true)][string]$In,
    [string]$Out = "$env:TEMP\gtn-preview.gif",
    [int]$Fps = 8,
    [int]$Width = 480
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path $In)) {
    Write-Error "ERROR: input file not found: $In"
    exit 1
}

$Palette = Join-Path $env:TEMP "gtn-gif-palette-$PID.png"
try {
    & ffmpeg -y -loglevel error -i $In `
        -vf "fps=$Fps,scale=${Width}:-1:flags=lanczos,palettegen" `
        $Palette
    # $ErrorActionPreference = "Stop" does not catch a non-zero exit from a
    # native executable in Windows PowerShell 5.1 — check explicitly.
    if ($LASTEXITCODE -ne 0) {
        throw "ffmpeg palettegen failed (exit $LASTEXITCODE)"
    }

    & ffmpeg -y -loglevel error -i $In -i $Palette `
        -filter_complex "fps=$Fps,scale=${Width}:-1:flags=lanczos[x];[x][1:v]paletteuse" `
        $Out
    if ($LASTEXITCODE -ne 0) {
        throw "ffmpeg paletteuse failed (exit $LASTEXITCODE)"
    }
} finally {
    Remove-Item -Path $Palette -ErrorAction SilentlyContinue
}

Write-Output "fps=$Fps width=$Width out=$Out"
