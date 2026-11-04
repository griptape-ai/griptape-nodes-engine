# Using the Admin Server

Once your [Admin Server](admin_server.md) is up and running, users connect to it from the Griptape Nodes desktop application by activating with a license instead of logging in through Griptape Cloud. This page walks through that flow from the user's perspective.

## Before you start

Each user needs:

- **The Griptape Nodes desktop application** installed.
- **A license key.** These are issued to your organization; users should request one from their organization admin.
- **The Admin Server address.** The URL where your Admin Server is reachable on your network, for example `http://admin.internal.example.com:8080`. Your Admin Server operator can provide this.

## Activate with a license

### 1. Open the license form

Launch the desktop application. On the login screen, instead of signing in with Griptape Cloud, click **Activate with a License**.

![The login screen with the Activate with a License button below the Login or Sign-Up button](../assets/img/enterprise/admin_server-login.png)

### 2. Paste your license key

In the **License Key** field, paste the license key you received from your organization admin.

![The license activation form with the License Key field and Griptape Server Endpoint field](../assets/img/enterprise/admin_server-license_form.png)

### 3. Point at the Admin Server

In the **Griptape Server Endpoint** field, replace the default Griptape Cloud address (`https://cloud.griptape.ai`) with your Admin Server's address:

```text
http://admin.internal.example.com:8080
```

This routes the application's Griptape Cloud API requests through the Admin Server. The endpoint alone does not route downloads or previews. See [Route HTTPS traffic through the Admin Server](#route-https-traffic-through-the-admin-server) to enable desktop HTTPS routing.

![The license form with a license key pasted and the server endpoint pointed at an on-prem Admin Server](../assets/img/enterprise/admin_server-license_filled.png)

### 4. Activate

Click **Activate License**. The application validates the license and launches the engine as normal, taking you to the editor. Licensing, sessions, and permitted Cloud API requests use the configured Admin Server endpoint.

## Route HTTPS traffic through the Admin Server

**Route HTTPS traffic through this server** routes HTTPS downloads and previews through the Admin Server. Ask your operator to confirm that it runs Admin Server 0.4.0 or later and has the required destinations in [`tunnel.allowed_hosts`](admin_server.md#tunnel).

The endpoint and checkbox appear only with an active license. Routing is off by default and requires an Admin Server endpoint other than `https://cloud.griptape.ai`.

1. Open the account or license menu in the top-right corner, then choose **App Settings**.
1. In **License**, check **Griptape Server Endpoint**. If you change it, click **Save**.
1. Enable **Route HTTPS traffic through this server**.
1. Restart the running engine.

## Reusing a saved license

Licenses you have activated before are remembered. If you log out (or the license is deactivated), the login screen shows a **Saved licenses** section after you click **Activate with a License**. Click **Use** next to a saved license to re-activate it without pasting the key again. The configured server endpoint is also remembered, so you only need to enter the Admin Server address once.

## Troubleshooting

- **Activation fails immediately.** Check that the license key was pasted completely and has not expired. If the key looks right, confirm the **Griptape Server Endpoint** is correct and that the machine can reach it — try opening `http://<admin-server-address>/health` in a browser; a healthy server responds with `{"status":"ok"}`.
- **Activation worked before but fails now.** The license may have expired. Ask your organization admin for a new one.
- **Some features return errors after activating.** If the Admin Server is configured with [forwarding rules](admin_server.md#forwarding), paths that are not permitted are rejected with `403 {"error":"path not permitted"}`. Ask your Admin Server operator whether the feature's routes are allowed to egress.
- **Activation succeeds, but downloads or previews fail.** Check that **Route HTTPS traffic through this server** is enabled and restart the engine. Ask your operator to confirm Admin Server 0.4.0 or later and the allowed tunnel destinations. See [Route HTTPS traffic through the Admin Server](#route-https-traffic-through-the-admin-server).
- **A download reports `tunnel host not permitted` or `tunnel host unreachable`.** Give your operator the error and the destination host, if shown. These indicate a blocked tunnel destination or a connection failure from the Admin Server, not a license problem. See [Admin Server troubleshooting](admin_server.md#troubleshooting).
- **Everything returns errors.** If every request fails, the Admin Server itself may be unable to reach Griptape Cloud. This is an operator-side issue — see the [Admin Server troubleshooting section](admin_server.md#troubleshooting).
