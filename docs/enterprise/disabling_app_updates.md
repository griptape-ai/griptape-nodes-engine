# Disabling App Updates

Griptape Nodes Desktop updates itself, and each user decides how in
[App Settings](../guides/desktop/app_settings.md#updates). On a machine you
deploy and manage, that is usually the wrong arrangement: you want every
workstation on the version you qualified, and you do not want a user turning
updates back on.

A **policy file** on the machine settles it. Write `policy.json` outside the
application, and the app reads it at launch and turns updates off. Nothing has
to be clicked in Settings after the install, and nobody can click it back.

Requires Griptape Nodes Desktop 0.24.0 or later. Earlier versions ignore the
file.

## The policy file

The file holds a single setting:

```json
{ "disableUpdates": true }
```

Its location depends on the platform. These are the operating system's own
locations for machine-wide application data, so writing there takes
administrator rights:

| Platform | Path                                                                 |
| -------- | -------------------------------------------------------------------- |
| macOS    | `/Library/Application Support/ai.griptape.nodes.desktop/policy.json` |
| Windows  | `%ProgramData%\GriptapeNodes\policy.json`                            |
| Linux    | `/etc/griptape-nodes-desktop/policy.json`                            |

The app never creates the directory or the file. Create both yourself, whether
by hand on a single machine or from an imaging or MDM pipeline that runs on
every machine you deploy.

`disableUpdates` is the only setting the file supports today.

## Turn updates off

Do this before the user first launches the app, or have them quit it first.

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

On Windows, from an elevated PowerShell prompt:

```powershell
$dir = "$env:ProgramData\GriptapeNodes"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Content -Path "$dir\policy.json" -Value '{ "disableUpdates": true }'
```

Windows needs one more step:
[restrict who can write to that folder](#restrict-the-folder-on-windows).
Otherwise a standard user can replace the file you just wrote.

Start the app. The policy is read once at launch, so a file you write or edit
while the app is running applies the next time it starts.

## What the user sees

The **Updates** section of App Settings reports that the decision was made
elsewhere: **Updates are disabled by your organization**. **Check for
Updates**, **Update Behavior**, and **Release Channel** are all disabled, so
the version and the channel are fixed at what you deployed. **Show release
notes after updates** still works, and the user still sees notes after you push
a new version out yourself.

Choosing **Check for Updates…** from the app menu on macOS, or the **Help**
menu on Windows and Linux, answers with a dialog: *Updates are managed by your
organization*.

No update check runs and no update request leaves the machine while the policy
is set, whether the check would have been automatic at launch or a user asked
for it.

## Keeping managed machines current

With updates off, the app will not move off the version it is on, so new
versions are yours to distribute the way you distribute any other application:
qualify a release, then push its installer to your machines. Installing over an
existing install leaves the policy file alone, since it lives outside the
application, and the new version comes up with updates still disabled.

Nothing else about the app is frozen. Node libraries and models are still
installed and updated from inside the app, and the engine that ships with a
given app version is the one you get with it.

## Turn updates back on

Delete `policy.json`, or set `disableUpdates` to `false`, then restart the app.
The Updates section becomes editable again, at whatever **Update Behavior** and
**Release Channel** the machine last had.

Only a literal `true` disables updates. Any other value leaves updates fully
user-controlled, and so does a missing file or one that is not valid JSON. An
unmanaged machine needs no policy file at all.

## Restrict the folder on Windows

Filesystem permissions are what make the policy binding, so only an
administrator should be able to write to the folder holding it. On macOS and
Linux this is already true: `/Library/Application Support` and `/etc` both
require root.

Windows is different. `%ProgramData%` is writable by standard users, so until
`GriptapeNodes\` exists with administrator-only write permissions, any user on
the machine can create that folder and drop their own `policy.json` into it, or
replace yours. What the folder needs: full control for Administrators and
SYSTEM, read-only for everyone else, and no inherited entries that widen that.
One way to set it when your installer or machine image creates the folder:

```powershell
$dir = "$env:ProgramData\GriptapeNodes"
icacls $dir /inheritance:r `
  /grant "*S-1-5-32-544:(OI)(CI)F" `
  /grant "*S-1-5-18:(OI)(CI)F" `
  /grant "*S-1-5-11:(OI)(CI)RX"
```

`/inheritance:r` drops the entries inherited from `%ProgramData%`, and the SIDs
are the built-in Administrators group, SYSTEM, and Authenticated Users, named
by SID so the command works on a machine in any display language. Check the
result with `icacls $dir` and confirm no entry grants write access to a group a
standard user belongs to. Your organization may have its own hardening baseline
for machine-wide folders; that takes precedence over this example.

Until the folder is locked down, treat the policy on Windows as advisory.

## Confirm the policy took effect

Beyond the banner in Settings, the application log records what was read at
launch. Turn on **Write application logs to file** in
[Logging and Diagnostics](../guides/desktop/app_settings.md#logging-and-diagnostics),
restart the app, and export the log. A machine with the policy in place logs
the path it read and the verdict:

```text
Policy: /Library/Application Support/ai.griptape.nodes.desktop/policy.json read, app updates disabled
```

A file that could not be parsed says so too, and updates stay enabled:

```text
Policy: /etc/griptape-nodes-desktop/policy.json is not valid JSON, ignoring
```

There is no log line when no policy file exists, which is the ordinary case.

## Related

- [App Settings: Updates](../guides/desktop/app_settings.md#updates) — the
    controls this policy overrides, and what the release channels are.
- [Admin Server](admin_server.md) — keep managed machines off the public
    internet entirely.
