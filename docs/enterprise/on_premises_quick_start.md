# On-Premises Quick Start

This is the shortest path from nothing to a working on-premises deployment: one [Admin Server](admin_server.md) inside your network, licensed seats issued from the [Admin Dashboard](admin_dashboard.md), and desktop applications that reach Griptape Cloud only through that server. Instances point at the Admin Server instead of `cloud.griptape.ai`, so you get one egress rule to manage and one place to audit outbound traffic.

You do not need any of this if your instances can already reach `cloud.griptape.ai` directly and that is acceptable in your environment.

Steps 1 to 4 are done once, by the administrator. Steps 5 to 7 are the per-user rollout.

## Before you start

You need:

- **The Admin Server binary.** It is provided to enterprise customers — [contact Foundry](https://www.foundry.com/products/griptape/request-demo) to obtain it.
- **A Griptape Cloud account that owns the organization** you are deploying for. The Admin Dashboard only appears for organization owners.
- **A host inside your network** that is allowed outbound HTTPS to `cloud.griptape.ai`, and that your workstations can reach on its listen port (`8080` by default). No other Griptape Nodes instance needs internet access.
- **An internet-connected machine for administration.** The Admin Dashboard talks to Griptape Cloud directly, so it cannot be used from a locked-down on-premises machine — see step 1.

## 1. Create a Griptape Cloud API key

The Admin Server needs a **Griptape Cloud API key** to start. This is not the same thing as the **license keys** you issue to users in step 5: the API key authenticates the Admin Server itself to Griptape Cloud, is created once, and belongs to you rather than to a seat.

The key comes from the **Admin Dashboard**, so nothing needs to be installed yet — sign in to the web editor from any internet-connected browser. The desktop application works identically if you already run it; both use the same interface.

1. Sign in with **Login or Sign-Up**. You must own the organization you are deploying for.
1. Open the **Admin Dashboard**:
    - **Web editor** — the user menu in the bottom-left corner. The dashboard replaces the editor view; **Back to Editor** returns you.
    - **Desktop application** — the profile menu in the top-right corner. The dashboard opens in its own window.
1. The dashboard opens on **License Keys**. You are not issuing seats yet, so ignore the license table and click the **API Keys** tile in the stats bar.
1. Create a key. Its value is shown **once** — copy it immediately.

![The stats bar on the License Keys page with the API Keys tile highlighted](../assets/img/enterprise/on_premises_quick_start-api_keys_tile.png)

The key is not used on the request path. It only confirms that the operator owns a Griptape organization — user applications still send their own `Authorization` header, which the Admin Server forwards untouched.

## 2. Install the Admin Server

Unpack the archive on your chosen host. It contains:

- `server` — the binary
- `config.example.yaml` — a template you can copy, edit, and use
- `README` and `LICENSE`

## 3. Configure the Admin Server

The only thing you must set is the API key environment variable:

```bash
export GT_CLOUD_API_KEY="gt-..."
```

The server validates the key at startup and will not boot without it. A config file only ever names the variable to read the key from; the key itself never goes in a file.

A config file is **optional**. `./server` with no config starts fine — a missing `config.yaml` is not an error, and the server falls through to built-in defaults plus any environment overrides. If the defaults suit you, skip to step 4 and start it.

!!! note "Check your version before trusting the defaults"

    Run `./server -version`. The defaults are only safe unconfigured on Admin Server 0.3.0 and later. Earlier versions default `read_timeout` and `write_timeout` to `30s`, which cuts streamed replies off mid-reply — see [Admin Server: Configuration](admin_server.md#configuration) for what to set instead.

To change something, copy the shipped template rather than writing one from scratch:

```bash
cp config.example.yaml config.yaml
```

Settings resolve in order: built-in defaults, then the config file, then environment variables. Environment variables always win.

The file has four blocks — `server`, `upstream`, `logging`, and `forwarding`. For the key-by-key reference, the defaults, and the environment variable equivalents, see [Admin Server: Configuration](admin_server.md#configuration).

## 4. Start and verify

```bash
./server                      # defaults plus environment overrides
./server -config config.yaml  # if you created a config file
```

The server writes to stdout and stderr. It does not write a log file — redirect the streams if you need one, or let your service manager collect them.

Check that it is live:

```bash
curl http://<admin-server-address>:8080/health
# {"status":"ok"}
```

If the API key is missing or invalid, or the upstream is unreachable, the server logs the reason and exits rather than serving. Configuration problems surface at boot.

## 5. Issue license keys

Back in the **Admin Dashboard**, go to **License Keys → + Create License Key**:

- **License Name(s)** — one per seat. Enter several names to create a batch in one step.
- **License Type** — `Interactive` for a person in the editor, `Headless` for automated use. This **cannot be changed** after creation.
- **Expiration Date** — required, between 1 and 730 days out.
- **Access Groups** — add the key to the right groups now.

Each token is revealed **exactly once**. Copy it and deliver it to the user. If a token is lost, use **Reissue** on that license to generate a new one.

If you intend to restrict what seats can do — which libraries, nodes, projects, and models they may use — build your **permission templates** and **access groups** first, in the sidebar sections of the same name. Both are assignable while creating a key, which is less work than retrofitting. Griptape also ships read-only managed templates you can attach as they are. See [Admin Dashboard: Permission Editor](admin_dashboard.md#permission-editor).

## 6. Roll out Griptape Nodes to workstations

Install the **desktop application** on each user machine — see [Installation](../installation.md).

Desktop is required here, not optional. The web editor is Cloud-hosted and reaches its engine over the Cloud relay, which is not the on-premises path; on-premises runs the desktop application with a direct WebSocket to its engine. The web editor is fine as the administrator's tool for issuing keys, but it is not the artists' editor in this deployment.

These machines do **not** need internet access. They only need to reach the Admin Server.

Send each user two things:

1. Their license token.
1. The Admin Server address, for example `http://admin.internal.example.com:8080`.

## 7. Activate on each workstation

1. Launch the desktop application.
1. On the login screen, click **Activate with a License** rather than signing in.
1. Paste the token into **License Key**.
1. In **Griptape Server Endpoint**, replace `https://cloud.griptape.ai` with your Admin Server address. This is the step that makes the deployment on-premises.
1. Click **Activate License**.

The endpoint and the license are both remembered. On later logins, users pick their key from **Saved licenses**. For screenshots of this flow, see [Using the Admin Server](using_the_admin_server.md).

License-activated users get the editor only. The Admin Dashboard never appears for them, because activation creates a session with no Cloud account behind it.

## Troubleshooting

**The Admin Dashboard menu entry is missing.** You are signed in with a license key rather than a Cloud account, or the active organization is one you do not own. Sign in with **Login or Sign-Up** and check the organization switcher.

Everything else — `502` responses, `403 {"error":"path not permitted"}`, startup failures, failed uploads, and activation problems — is covered in:

- [Admin Server: Troubleshooting](admin_server.md#troubleshooting): operator-side
- [Using the Admin Server: Troubleshooting](using_the_admin_server.md#troubleshooting): user-side activation

## Related

- [Installation](../installation.md): installing Griptape Nodes
- [Admin Server](admin_server.md): configuration, environment variables, and troubleshooting
- [Admin Dashboard](admin_dashboard.md): license keys, access groups, and permission templates
- [Using the Admin Server](using_the_admin_server.md): the end-user activation flow
- [Architecture: On-premises configuration](../architecture.md#on-premises-configuration): where the Admin Server sits
