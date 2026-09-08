# Disabling App Updates

Administrators can disable Griptape Nodes Desktop updates with a machine-level
`policy.json` file. Use this when your organization distributes approved app
versions instead of letting each installation update itself.

This feature requires Griptape Nodes Desktop 0.24.0 or later. Earlier versions
ignore the file.

## Create the policy file

Create `policy.json` with this content:

```json
{ "disableUpdates": true }
```

Put it at the path for the machine's operating system:

| Platform | Path                                                                 |
| -------- | -------------------------------------------------------------------- |
| macOS    | `/Library/Application Support/ai.griptape.nodes.desktop/policy.json` |
| Windows  | `%ProgramData%\GriptapeNodes\policy.json`                            |
| Linux    | `/etc/griptape-nodes-desktop/policy.json`                            |

The app does not create the directory or the file. Create both manually or
through an imaging or device-management system before the user first launches
the app. If the app is already running, quit it first.

On macOS:

```bash
sudo mkdir -p "/Library/Application Support/ai.griptape.nodes.desktop"
sudo tee "/Library/Application Support/ai.griptape.nodes.desktop/policy.json" >/dev/null <<'EOF'
{ "disableUpdates": true }
EOF
```

On Linux:

```bash
sudo mkdir -p /etc/griptape-nodes-desktop
sudo tee /etc/griptape-nodes-desktop/policy.json >/dev/null <<'EOF'
{ "disableUpdates": true }
EOF
```

On Windows, run this from an elevated PowerShell prompt:

```powershell
$dir = "$env:ProgramData\GriptapeNodes"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Content -Path "$dir\policy.json" -Value '{ "disableUpdates": true }'
```

Then [restrict write access to the folder](#restrict-the-folder-on-windows). A
standard user can replace the file until the folder permissions are changed.

The app reads the policy once at launch. Changes to the file take effect the
next time the app starts.

`disableUpdates` is the only setting the policy file supports.

## What users see

App Settings shows **Updates are disabled by your organization**. The following
controls are disabled:

- **Check for Updates**
- **Update Behavior**
- **Release Channel**

**Show release notes after updates** remains available, and shows the notes
after an administrator installs a new version.

If a user chooses **Check for Updates…** from the app menu on macOS or the
**Help** menu on Windows and Linux, the app shows **Updates are managed by your
organization**.

While the policy is enabled, the app does not run automatic or user-initiated
update checks. It sends no requests to the update feed.

## Install app updates

With the built-in updater disabled, distribute each approved installer to the
managed machines. The policy file is outside the app installation, so
installing a new version over the existing one does not remove it. Updates
remain disabled after installation.

Users can still install and update node libraries and models from inside the
app. Each app version continues to use the engine bundled with that version.

## Re-enable app updates

Delete `policy.json` or change `disableUpdates` to `false`, then restart the
app. The Updates section uses the machine's previous **Update Behavior** and
**Release Channel** settings.

Only a literal `true` disables updates. A missing file, invalid JSON, or any
other value leaves updates under user control. An unmanaged machine does not
need a policy file.

## Restrict the folder on Windows

Only administrators should have write access to the directory containing
`policy.json`. The macOS and Linux paths require root access by default.

Standard users can write to `%ProgramData%` on Windows. Until
`GriptapeNodes\` has administrator-only write permissions, a user can create the
directory or replace its `policy.json`. The directory needs full control for
Administrators and SYSTEM, read-only access for other users, and no inherited
entries that grant additional access.

The following PowerShell example sets those permissions:

```powershell
$dir = "$env:ProgramData\GriptapeNodes"
icacls $dir /inheritance:r `
  /grant "*S-1-5-32-544:(OI)(CI)F" `
  /grant "*S-1-5-18:(OI)(CI)F" `
  /grant "*S-1-5-11:(OI)(CI)RX"
```

`/inheritance:r` removes permissions inherited from `%ProgramData%`. The SIDs
identify the built-in Administrators group, SYSTEM, and Authenticated Users, so
the command does not depend on the machine's display language.

Run `icacls $dir` to inspect the result. No group available to a standard user
should have write access. If your organization has its own permissions policy
for machine-wide folders, use that policy instead.

Until write access is restricted, treat the Windows policy as advisory.

## Check the policy

App Settings shows whether updates are disabled. You can also confirm the
policy in the application log. Enable **Write application logs to file** under
[Logging and Diagnostics](../guides/desktop/app_settings.md#logging-and-diagnostics),
restart the app, and export the log.

A valid policy logs the path and result:

```text
Policy: /Library/Application Support/ai.griptape.nodes.desktop/policy.json read, app updates disabled
```

Invalid JSON leaves updates enabled and produces this message:

```text
Policy: /etc/griptape-nodes-desktop/policy.json is not valid JSON, ignoring
```

The app writes no policy log line when the file does not exist.

## Related

- [App Settings: Updates](../guides/desktop/app_settings.md#updates): update
    behavior and release channels
- [Admin Server](admin_server.md): keep managed machines off the public internet
