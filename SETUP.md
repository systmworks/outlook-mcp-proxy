[← Back to README](README.md)

# Setup

## 1. Register the Azure app

All of this happens in the [Azure Portal](https://portal.azure.com) → **App
registrations**. You do not need a paid Azure subscription — app registration is free.

1. **App registrations → New registration**.
   - Name: anything, e.g. `Outlook MCP Proxy`.
   - **Supported account types: "Personal Microsoft accounts only."** This is the
     single most important setting on this page — it's what restricts sign-in to
     personal outlook.com/hotmail.com/live.com accounts and structurally excludes
     work/school accounts. Do not pick "Accounts in any organizational directory and
     personal Microsoft accounts" — that would let work/school accounts through too.
   - Redirect URI: platform **Web**, value `https://<your-domain>/auth/callback`
     (fill in your real domain once you know it — see step 2; you can come back and
     edit this later under **Authentication** if you don't have it yet).
   - Click **Register**.
2. Note the **Application (client) ID** shown on the app's Overview page — this is
   `MS_CLIENT_ID`.
3. **Certificates & secrets → New client secret**. Give it a description and the
   longest expiry Azure offers (max 24 months — **there is no "never expires" option**,
   unlike Google). Copy the secret **value** immediately; Azure only shows it once —
   this is `MS_CLIENT_SECRET`. **Set a reminder for before the expiry date** — an
   expired secret breaks every connected session until you generate a new one and
   update the running server.
4. No API permissions need to be added manually here — the scopes
   (`Mail.Read`, `Mail.ReadWrite`, `Mail.Send`, `Calendars.Read`, `Calendars.ReadWrite`,
   `User.Read`, `offline_access`) are requested dynamically per-connection at sign-in
   time and Microsoft's consent screen handles the grant.

## 2. Deploy the server

This is written for a Proxmox LXC container reachable over
[Tailscale](https://tailscale.com), which is what avoids opening any public port. Any
host with HTTPS works the same way — substitute your own reverse proxy/TLS setup.

1. Create a new LXC container (Debian/Ubuntu template) with Python 3.12+ available
   (`apt install python3.12-venv` if it isn't already).
2. Clone this repo into it, e.g. `/opt/outlook-mcp-proxy`:
   ```bash
   git clone https://github.com/<you>/outlook-mcp-proxy /opt/outlook-mcp-proxy
   cd /opt/outlook-mcp-proxy
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Install Tailscale in the container and bring it up:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up
   ```
   Note the `*.ts.net` MagicDNS hostname it's assigned — this becomes `BASE_URL`.
4. Create `/opt/outlook-mcp-proxy/.env`:
   ```bash
   MS_CLIENT_ID=<from step 1.2>
   MS_CLIENT_SECRET=<from step 1.3>
   JWT_SECRET=<openssl rand -hex 32>
   BASE_URL=https://<your-tailscale-hostname>
   ```
5. Go back to **Azure Portal → your app → Authentication** and set the redirect URI to
   exactly `https://<your-tailscale-hostname>/auth/callback` — it must match `BASE_URL`
   byte-for-byte.
6. Create a systemd unit, e.g. `/etc/systemd/system/outlook-mcp-proxy.service`:
   ```ini
   [Unit]
   Description=Outlook MCP Proxy
   After=network.target

   [Service]
   Type=simple
   User=outlook-mcp
   WorkingDirectory=/opt/outlook-mcp-proxy
   EnvironmentFile=/opt/outlook-mcp-proxy/.env
   ExecStart=/opt/outlook-mcp-proxy/venv/bin/python server.py
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```
   ```bash
   systemctl daemon-reload
   systemctl enable --now outlook-mcp-proxy
   ```
7. Verify it's up:
   ```bash
   curl https://<your-tailscale-hostname>/.well-known/oauth-authorization-server
   ```
   This should return a small JSON document, not an error page.

**Never run multiple replicas or `uvicorn --workers N`** — session/state stores are
per-process in-memory; a request landing on a different process than the one that
authenticated it fails as if unauthenticated.

## 3. Connect in Claude

Settings → Connectors → **Add → Add custom connector**:

| Field | Value |
|---|---|
| Name | anything, e.g. `Outlook (Personal)` |
| URL | `https://<your-tailscale-hostname>/personal/mcp` |

Expand **Advanced settings** and fill in:

| Field | Value |
|---|---|
| OAuth Client ID | any placeholder text, e.g. `claude` |
| OAuth Client Secret | leave blank |

That last part isn't optional in practice: this server doesn't implement automatic
client registration (it's a single-user proxy, not a multi-tenant OAuth provider), so
without something in the Client ID field, Claude will show a "client registration isn't
supported" warning and refuse to connect. The value itself doesn't matter — the server
never checks it, it only needs to be non-empty.

Click **Add**, then **Connect**, and sign in with your personal Microsoft account. You
should land back in Claude successfully authenticated.

**Second account:** repeat this whole step with a different alias in the URL —
`https://<your-tailscale-hostname>/family/mcp` — and sign in with the second account.
Any alias name works for either connector; `/personal/` and `/family/` are just
examples. Both connectors share the same deployed server and the same Azure app.

## Update loop

```bash
cd /opt/outlook-mcp-proxy
git pull
source venv/bin/activate
pip install -r requirements.txt
systemctl restart outlook-mcp-proxy
```

Every restart wipes the in-memory session store — after restarting, any connected
account needs to be removed and re-added in Claude, then signed in again.

## Self-hosting without Tailscale

The server works the same behind any reverse proxy that terminates HTTPS (Caddy,
nginx, Traefik, …) and forwards to its port (`8000` by default, or whatever `PORT` is
set to):

```bash
pip install -r requirements.txt
set -a && . ./.env && set +a   # load the variables from .env
python server.py

# Or with the included Dockerfile
docker build -t outlook-mcp .
docker run -d --env-file .env -p 8000:8000 outlook-mcp
```

Set `BASE_URL` to whatever public URL your proxy exposes the server under (a bare
domain, or a domain plus path prefix if sharing a host with other services) — the
server builds its OAuth redirect URIs from it, so it must match exactly.
