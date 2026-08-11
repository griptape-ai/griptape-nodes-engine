#!/usr/bin/env pwsh
# Open the hosted Griptape Nodes editor in the persistent `gtn-web` Chromium
# profile and report whether it is signed in.
#
# Prints one of:
#   state=ready   url=<editor url>     -> drive it
#   state=login   url=<auth0 url>      -> a human must sign in once, in the
#                                         headed window this leaves open

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Url = if ($env:GTN_WEB_URL) { $env:GTN_WEB_URL } else { "https://app.nodes.griptape.ai" }
$WebProfile = if ($env:GTN_WEB_PROFILE) { $env:GTN_WEB_PROFILE } else { "gtn-web" }

try {
    Invoke-WebRequest -Uri "http://127.0.0.1:8124/" -TimeoutSec 2 -UseBasicParsing | Out-Null
} catch {
    Write-Error "warn: local engine not reachable on 127.0.0.1:8124 — the editor will load but have no engine" -ErrorAction Continue
}

$script = @"
const page = await browser.getPage("hosted");
await page.goto("$Url", { waitUntil: "domcontentloaded", timeout: 60000 });
await page.waitForTimeout(9000);
const url = page.url();
const body = await page.evaluate(() => document.body.innerText.slice(0, 300));
const needsLogin = /auth\.cloud\.griptape\.ai|\/u\/login/.test(url) || /Log in to Griptape Nodes/.test(body);
console.log((needsLogin ? "state=login" : "state=ready") + " url=" + url);
if (needsLogin) {
  console.log("A human must sign in once in the headed '$WebProfile' Chromium window. Cookies persist afterwards.");
} else {
  console.log("SHOT: " + await saveScreenshot(await page.screenshot(), "gtn-web-up.png"));
}
"@

$script | & dev-browser --browser $WebProfile --timeout 90

# A PowerShell script's own exit code defaults to 0 regardless of the last
# native command's exit code unless propagated explicitly — without this, a
# failed dev-browser run would still report success to any caller checking
# $LASTEXITCODE/%ERRORLEVEL%.
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
