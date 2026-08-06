#!/usr/bin/env pwsh
# Sweep this skill's own scratch files out of dev-browser's tmp dir.
#
#   pwsh ./gtn-clean-scratch.ps1
#
# See gtn-clean-scratch.sh for why this exists — this is the same sweep,
# scoped to the same "gtn"/"gtnrec_" prefixes.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Tmp = "$HOME\.dev-browser\tmp"

$files = Get-ChildItem -Path "$Tmp\*" -Include "gtn*.png", "gtn*.jpg", "gtnrec_*.jpg" -File -ErrorAction SilentlyContinue

if (-not $files) {
    Write-Output "nothing to clean"
    exit 0
}

$sizeBytes = ($files | Measure-Object -Property Length -Sum).Sum
$sizeHuman = "{0:N1}MB" -f ($sizeBytes / 1MB)
$files | Remove-Item -Force
Write-Output "removed=$($files.Count) freed=$sizeHuman"
