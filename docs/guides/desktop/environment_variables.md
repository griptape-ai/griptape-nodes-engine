# Desktop Environment Variables

Use these variables to configure Griptape Nodes Desktop at launch.

## Where to set them

Set the variables you need before the desktop app starts, through a launching shell,
managed launcher, or user-session environment. Do not put desktop-only variables
in the engine's `.env` file.

On macOS, apps opened from Finder or the Dock inherit the `launchd` environment,
not shell exports. Use a managed launcher or LaunchAgent to supply the variables.

Fully quit and relaunch the app after adding, changing, or removing a variable.
Restarting only the engine is not enough.

## `GTN_DESKTOP_SERVER_URL`

Overrides the saved server endpoint with an `http` or `https` origin, such as
`http://admin-server:8080`. Any path is stripped. A valid value takes precedence
over the saved setting; blank or invalid values are ignored, and invalid values
are logged.

## `GTN_DESKTOP_ROUTE_TRAFFIC_THROUGH_SERVER`

Overrides the saved HTTPS routing setting. Use `1` or `true` to enable routing,
or `0` or `false` to disable it, case-insensitively. Blank or invalid values are
ignored. Routing is off by default.

Routing applies only with a license credential and a non-default server endpoint.
It requires Admin Server 0.4.0 or later with allowed destinations configured. See
[Route HTTPS traffic through the Admin Server](../../enterprise/using_the_admin_server.md#route-https-traffic-through-the-admin-server).

Removing either server override restores its saved user setting on the next launch.

## `GTN_DESKTOP_DISABLE_UPDATES`

Set to `1` or `true`, case-insensitively, to disable automatic and manual app update
checks for that run. Other values do not disable updates. Setting `0` or `false`
cannot re-enable updates when `policy.json` has `disableUpdates: true`.

For enforcement, use the
[machine-level update policy](../../enterprise/disabling_app_updates.md).
An environment override is not binding because users control their launch environment.

## `GRIPTAPE_NODES_LICENSE`

Supplies the license token for that run, taking precedence over the stored active
license. A nonblank value prevents switching the active credential in the app,
even if the token is invalid. Invalid tokens do not fall back to a stored license;
blank values are ignored.

The token is not written to stored licenses or the engine's `.env` file. Removing
the variable restores the stored license selection on the next launch. Treat the
token like a password and keep it out of shared scripts and version control.
