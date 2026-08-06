#!/usr/bin/env pwsh
# Record page content from Griptape Nodes to an mp4, by capturing a JPEG frame
# loop over CDP and encoding with ffmpeg.
#
#   pwsh ./gtn-record.ps1 [-Target desktop|web] [-Secs N] [-Fps N] [-Out PATH]
#
# Captures page pixels only — no native window chrome. Use gtn-record-screen.ps1
# for that.
#
# To record an interaction rather than a static screen, run this in the
# background and drive the app with a separate dev-browser script while it
# captures.

param(
    [ValidateSet("desktop", "web")]
    [string]$Target = "desktop",
    [int]$Secs = 8,
    [int]$Fps = 6,
    [string]$Out = "$env:TEMP\gtn-recording.mp4"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Port = if ($env:GTN_CDP_PORT) { $env:GTN_CDP_PORT } else { "9222" }
$WebProfile = if ($env:GTN_WEB_PROFILE) { $env:GTN_WEB_PROFILE } else { "gtn-web" }
$Tmp = "$HOME\.dev-browser\tmp"

$Frames = $Secs * $Fps
$DelayMs = [int][Math]::Floor(1000 / $Fps)
# Screenshots are not instant, so allow generous headroom over the nominal
# duration before the daemon kills the script.
$ScriptTimeout = $Secs * 3 + 60

# Stale frames from a previous run would be swept into this recording by the
# glob, so clear them first.
Remove-Item -Path "$Tmp\gtnrec_*.jpg" -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $Tmp -Force | Out-Null

$loop = @"
const N = $Frames;
for (let i = 0; i < N; i++) {
  const buf = await target.screenshot({ type: "jpeg", quality: 70 });
  await saveScreenshot(buf, "gtnrec_" + String(i).padStart(5, "0") + ".jpg");
  await target.waitForTimeout($DelayMs);
}
console.log("frames=" + N);
"@

if ($Target -eq "desktop") {
    $script = @"
const pages = await browser.listPages();
const target = await browser.getPage(pages[0].id);
$loop
"@
    $script | & dev-browser --connect "http://localhost:$Port" --timeout $ScriptTimeout
} else {
    $script = @"
const target = await browser.getPage("hosted");
$loop
"@
    $script | & dev-browser --browser $WebProfile --timeout $ScriptTimeout
}

$count = (Get-ChildItem -Path "$Tmp\gtnrec_*.jpg" -ErrorAction SilentlyContinue).Count
if ($count -eq 0) {
    Write-Error "ERROR: no frames captured"
    exit 1
}

# Windows ffmpeg builds frequently lack glob support (-pattern_type glob), so
# use the numbered-sequence input instead — frames are already zero-padded
# from index 0, so this is equivalent for this script's own naming scheme.
& ffmpeg -y -loglevel error `
    -framerate $Fps -start_number 0 -i "$Tmp\gtnrec_%05d.jpg" `
    -c:v libx264 -pix_fmt yuv420p -vf "scale=1280:-2" `
    $Out

Remove-Item -Path "$Tmp\gtnrec_*.jpg" -ErrorAction SilentlyContinue
Write-Output "frames=$count fps=$Fps out=$Out"
