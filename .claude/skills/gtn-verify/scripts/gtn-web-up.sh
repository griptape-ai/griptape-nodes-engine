#!/usr/bin/env bash
# Open the hosted Griptape Nodes editor in the persistent `gtn-web` Chromium
# profile and report whether it is signed in.
#
# Prints one of:
#   state=ready   url=<editor url>     -> drive it
#   state=login   url=<auth0 url>      -> a human must sign in once, in the
#                                         headed window this leaves open
set -euo pipefail

URL="${GTN_WEB_URL:-https://app.nodes.griptape.ai}"
PROFILE="${GTN_WEB_PROFILE:-gtn-web}"

if ! curl -s -m 2 -o /dev/null http://127.0.0.1:8124/ ; then
  echo "warn: local engine not reachable on 127.0.0.1:8124 — the editor will load but have no engine" >&2
fi

dev-browser --browser "${PROFILE}" --timeout 90 <<EOF
const page = await browser.getPage("hosted");
await page.goto("${URL}", { waitUntil: "domcontentloaded", timeout: 60000 });
await page.waitForTimeout(9000);
const url = page.url();
const body = await page.evaluate(() => document.body.innerText.slice(0, 300));
const needsLogin = /auth\.cloud\.griptape\.ai|\/u\/login/.test(url) || /Log in to Griptape Nodes/.test(body);
console.log((needsLogin ? "state=login" : "state=ready") + " url=" + url);
if (needsLogin) {
  console.log("A human must sign in once in the headed '${PROFILE}' Chromium window. Cookies persist afterwards.");
} else {
  console.log("SHOT: " + await saveScreenshot(await page.screenshot(), "gtn-web-up.png"));
}
EOF
