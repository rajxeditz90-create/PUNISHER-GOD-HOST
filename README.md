# Telegram Hoster — Deployment Guide

A Flask panel that hosts Telegram bots: users sign up, add their own bot tokens,
and start/stop their bots (up to `MAX_BOTS_PER_USER` each). All bots run the
shared `custom_script.py`, which only the owner can upload.

> Note: this panel runs the script the owner uploads. Treat it as a trusted
> tool — put it behind HTTPS and a strong password, and don't expose it openly.

---

## 1. What you need

- A machine that stays on: a small VPS (DigitalOcean, Hetzner, Lightsail, EC2…),
  a home server, or a PaaS that supports long-running processes and a disk.
- Port `8080` reachable (or put it behind a reverse proxy).
- Docker + Docker Compose (recommended), **or** Python 3.11+ if you run it bare.

Before deploying, edit the two owner lines at the top of `app.py`:

```python
OWNER_USERNAME = os.environ.get("OWNER_USERNAME") or "admin"
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD") or "changeme123"   # <-- change
```

…or leave them and set `OWNER_USERNAME` / `OWNER_PASSWORD` in the environment.

---

## 2. Quick start with Docker (recommended)

```bash
cd telegram-hoster

# 1. Create a .env with a fixed session secret (so logins survive restarts)
cat > .env <<EOF
PANEL_SECRET=$(openssl rand -hex 32)
MAX_BOTS_PER_USER=3
EOF

# 2. Build and start
docker compose up -d --build

# 3. Open the panel
#    http://<server-ip>:8080
```

Sign in as the owner (`admin` / your password), then open the panel. Accounts,
bot tokens, and logs persist in the `host_data` volume mounted at `/data`.

Useful commands:

```bash
docker compose logs -f          # watch panel logs
docker compose restart          # restart
docker compose down             # stop (data stays in the volume)
docker compose up -d --build    # rebuild after editing code
```

---

## 3. Without Docker (bare Python + systemd)

```bash
cd telegram-hoster
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export PANEL_SECRET=$(openssl rand -hex 32)
export DATA_DIR=/var/lib/telegram-hoster     # writable dir for the DB + logs
python app.py
```

To keep it running, use a systemd unit (`/etc/systemd/system/telegram-hoster.service`):

```ini
[Unit]
Description=Telegram Hoster panel
After=network.target

[Service]
WorkingDirectory=/opt/telegram-hoster
Environment=PANEL_SECRET=replace-with-a-fixed-secret
Environment=DATA_DIR=/var/lib/telegram-hoster
ExecStart=/opt/telegram-hoster/.venv/bin/python app.py
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-hoster
```

---

## 4. On a VPS with a domain + HTTPS (Caddy)

Caddy gives you automatic TLS in two lines. Install Docker + Caddy, run the
panel with `docker compose up -d`, then add a `/etc/caddy/Caddyfile`:

```
hoster.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

```bash
sudo systemctl reload caddy
```

Point your domain's DNS at the server, and Caddy handles the certificate. Now
the panel is at `https://hoster.example.com`. If you don't use Caddy, Nginx +
certbot works the same way — just proxy to `127.0.0.1:8080`.

---

## 5. On a PaaS (Render / Railway / Fly.io)

These work, with one caveat: **you must attach a persistent disk at `/data`**,
or accounts and logs vanish on every deploy. On Render, disks need a **paid
instance** (Starter and up) — the free plan has no disk.

### Render (step by step)

The repo ships a `render.yaml` Blueprint, so this is mostly click-through:

1. Push the `telegram-hoster` folder to a GitHub or GitLab repo.
2. In Render: **New → Blueprint**, pick the repo. Render reads `render.yaml`
   and creates the web service plus a 1 GB disk mounted at `/data`.
3. It prompts for `OWNER_PASSWORD` (marked secret) — set a strong one.
   `PANEL_SECRET` is generated for you and `OWNER_USERNAME` defaults to `admin`.
4. Deploy. Render builds the Dockerfile and health-checks `/healthz`.
5. Open the service URL and sign in as the owner.

To change the per-user bot limit, edit `MAX_BOTS_PER_USER` in `render.yaml` (or
the dashboard) and redeploy. To close sign-ups later, set `ALLOW_REGISTRATION=0`.

Doing it by hand instead? **New → Web Service → Docker**, add a **Disk** mounted
at `/data`, and set the same env vars — `DATA_DIR=/data` is the important one.

### Railway / Fly.io

- **Railway** — Deploy from the Dockerfile, add a **Volume** mounted at `/data`,
  set the same env vars.
- **Fly.io** — `fly launch`, then:

  ```bash
  fly volumes create host_data --size 1
  ```

  and in `fly.toml`:

  ```toml
  [[mounts]]
    source = "host_data"
    destination = "/data"

  [env]
    DATA_DIR = "/data"
  ```

  Then `fly secrets set PANEL_SECRET=...` and `fly deploy`.

The panel spawns child processes, which all these platforms allow — but a
1 GB instance is plenty for a handful of bots.

---

## 6. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OWNER_USERNAME` | `admin` | Owner login (overrides the value in `app.py`) |
| `OWNER_PASSWORD` | `changeme123` | Owner password — **change it** |
| `PANEL_SECRET` | random per start | Flask session key — set a fixed value so logins survive restarts |
| `PANEL_PORT` | `8080` | Port to listen on |
| `MAX_BOTS_PER_USER` | `3` | How many bots each user may host |
| `ALLOW_REGISTRATION` | `1` | Set to `0` to close public sign-ups |
| `DATA_DIR` | app folder | Where `auth.sqlite3` and `logs/` live |
| `SCRIPT_PATH` | `./custom_script.py` | The shared bot script |

---

## 7. Data & persistence

Everything stateful lives under `DATA_DIR`:

```
$DATA_DIR/
├── auth.sqlite3     # users, password hashes, bot records + tokens
└── logs/<bot_id>.log
```

Mount a volume at `DATA_DIR` (the compose file does this at `/data`). Back up
`auth.sqlite3` if you care about the accounts.

---

## 8. First-run checklist

1. Set a real `OWNER_PASSWORD` (edit `app.py` or set the env var).
2. Set a fixed `PANEL_SECRET`.
3. `docker compose up -d --build` (or start via systemd).
4. Sign in as the owner and upload your `custom_script.py`.
5. Put it behind HTTPS before sharing the URL.
6. Optionally set `ALLOW_REGISTRATION=0` once your users have signed up.

---

## 9. Troubleshooting

- **Port already in use** — change the host side of the mapping
  (`"9090:8080"` in `docker-compose.yml`) or set `PANEL_PORT`.
- **Users disappear after redeploy** — no volume mounted at `DATA_DIR`.
- **Logins drop after restart** — `PANEL_SECRET` isn't set to a fixed value.
- **Bots won't start** — check the bot's log pane in the panel; a missing or
  invalid `BOT_TOKEN` is the usual cause.
- **Can't reach the panel** — check the firewall/security group allows the port
  (and that the reverse proxy is pointing at the right port).
