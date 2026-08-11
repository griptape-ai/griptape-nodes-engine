#!/usr/bin/env pwsh
# Encode frames captured by a dev-browser script into an mp4, then clear them.
#
#   powershell -File ./gtn-encode.ps1 [-Fps N | -DurationMs N] [-Out PATH] [-Prefix NAME] [-Keep]
#
# See gtn-encode.sh for the full rationale behind -DurationMs vs -Fps — the
# behaviour here mirrors it exactly.

param(
    [double]$Fps = 4,
    [Nullable[double]]$DurationMs = $null,
    [string]$Out = "$env:TEMP\gtn-storyboard.mp4",
    [string]$Prefix = "gtnrec_",
    [switch]$Keep
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Tmp = "$HOME\.dev-browser\tmp"

# @() forces array context — a bare Get-ChildItem result is $null for zero
# matches or a single non-array object for exactly one, and Set-StrictMode
# throws PropertyNotFoundStrict on .Count for either without this.
$count = @(Get-ChildItem -Path "$Tmp\$Prefix*.jpg" -ErrorAction SilentlyContinue).Count
if ($count -eq 0) {
    Write-Error "ERROR: no frames matching $Tmp\$Prefix*.jpg"
    exit 1
}

if ($DurationMs) {
    $Fps = $count / ($DurationMs / 1000)
}

# Windows ffmpeg builds frequently lack glob support (-pattern_type glob), so
# use the numbered-sequence input instead — this matches the zero-padded,
# index-from-0 naming every frame producer in this skill uses.
& ffmpeg -y -loglevel error `
    -framerate $Fps -start_number 0 -i "$Tmp\$Prefix%05d.jpg" `
    -c:v libx264 -pix_fmt yuv420p -vf "scale=1280:-2" `
    $Out

# See gtn-record-screen.ps1 for why this check matters — without it, a failed
# ffmpeg run would still fall through and delete the source frames below.
if ($LASTEXITCODE -ne 0) {
    Write-Error "ERROR: ffmpeg failed (exit $LASTEXITCODE)"
    exit 1
}

if (-not $Keep) {
    Remove-Item -Path "$Tmp\$Prefix*.jpg" -ErrorAction SilentlyContinue
}
Write-Output "frames=$count fps=$Fps out=$Out"
