#!/usr/bin/env pwsh
# Collapse static "thinking pause" stretches out of an already-recorded video,
# without touching moving sections.
#
#   powershell -File ./gtn-trim-pauses.ps1 -In IN.mp4 -Out OUT.mp4 [-Sensitivity low|medium|high]
#
# See gtn-trim-pauses.sh for the mechanism (mpdecimate + setpts) and the
# meaning of the per-sensitivity thresholds — identical here.

param(
    [Parameter(Mandatory = $true)][string]$In,
    [string]$Out = "$env:TEMP\gtn-trimmed.mp4",
    [ValidateSet("low", "medium", "high")]
    [string]$Sensitivity = "medium"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not (Test-Path $In)) {
    Write-Error "ERROR: input file not found: $In"
    exit 1
}

switch ($Sensitivity) {
    "low"    { $Hi = 768; $Lo = 320; $Frac = 0.5 }
    "medium" { $Hi = 512; $Lo = 256; $Frac = 0.33 }
    "high"   { $Hi = 256; $Lo = 128; $Frac = 0.2 }
}

& ffmpeg -y -loglevel error -i $In `
    -vf "mpdecimate=hi=${Hi}:lo=${Lo}:frac=${Frac},setpts=N/FRAME_RATE/TB" `
    -an `
    $Out

# $ErrorActionPreference = "Stop" does not catch a non-zero exit from a
# native executable in Windows PowerShell 5.1 — check explicitly.
if ($LASTEXITCODE -ne 0) {
    Write-Error "ERROR: ffmpeg failed (exit $LASTEXITCODE)"
    exit 1
}

Write-Output "sensitivity=$Sensitivity out=$Out"
