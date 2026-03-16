# Proxy Manager

A Flask web application for managing nginx stream proxy configurations with Pterodactyl panel allocation sync.

## Features

- Create, edit, and delete nginx stream proxy config files
- TCP, UDP, or combined TCP & UDP proxy support
- Pterodactyl Application API integration — allocations stay in sync automatically
- JWT-based authentication with login/logout
- Bootstrap 5 dark mode UI with native dialog modals
- Background hourly sync between local configs and Pterodactyl

## Requirements

- Python 3.10+
- nginx (with stream module)
- Pterodactyl panel with Application API access

## Setup

### 1. Clone and create a virtual environment

```bash
cd /opt
git clone https://github.com/cheekysim/proxy-manager
cd proxy-manager
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|---|---|---|
| `SECRET_KEY` | Flask/JWT secret key | `bd2ba57e-...` |
| `ADMIN_USERNAME` | Admin login email | `admin` |
| `ADMIN_PASSWORD` | Admin login password | `admin` |
| `CONFIG_FILES_PATH` | Path to nginx config directory | `./configs` |
| `DEVELOPMENT` | Skip nginx reload when `true` | `false` |
| `PTERODACTYL_API_URL` | Base URL of your Pterodactyl panel | — |
| `PTERODACTYL_API_KEY` | Pterodactyl Application API key | — |

### 3. Run

**Development:**
```bash
flask --app main.py run --debug
```
Or use the included VS Code launch profile **Python: Flask (Hot Reload)**.

**Production:**
```bash
gunicorn main:app -w 4 -b 0.0.0.0:5000
```
`-w` is the number of worker processes. A common starting point is `(2 × CPU cores) + 1`.

### 4. Web Config

You will need to set the IP address of your nodes.
Through the web app, click "Nodes".
From here, you can configure the IP addresses, otherwise you will not be able to create any entries.

## nginx Configuration

Place generated config files in your nginx stream `conf.d` directory. Your main nginx config should include:

```nginx
stream {
    include /etc/nginx/stream.d/*.conf;
}
```

Set `CONFIG_FILES_PATH` in `.env` to point to that directory.

## Config File Format

Each proxy is stored as a single `.conf` file named `<IP-with-dashes>_<PORT>_<PROTOCOL>.conf`.

**TCP only** (`192-168-1-0_25565_tcp.conf`):
```nginx
server { listen 25565; proxy_pass 192.168.1.0:25565; }
```

**UDP only** (`192-168-1-0_25565_udp.conf`):
```nginx
server { listen 25565 udp; proxy_pass 192.168.1.0:25565; }
```

**TCP & UDP** (`192-168-1-0_25565_both.conf`):
```nginx
server { listen 25565; proxy_pass 192.168.1.0:25565; }
server { listen 25565 udp; proxy_pass 192.168.1.0:25565; }
```

## Pterodactyl Sync

On startup the application performs a bidirectional sync:

- Allocations in Pterodactyl but missing locally → config files are created
- Config files locally but missing from Pterodactyl → allocations are created

A background thread repeats this sync every hour.

The Pterodactyl node ID is configured via the `PTERODACTYL_NODE_ID` environment variable (defaults to `6`).

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/proxies` | List all local proxy configs |
| `POST` | `/api/add` | Add a new proxy |
| `POST` | `/api/edit` | Edit an existing proxy |
| `POST` | `/api/remove` | Remove a proxy |
| `GET` | `/api/allocations` | List Pterodactyl allocations |
| `GET/POST` | `/login` | Login |
| `POST` | `/logout` | Logout |

All `/api/*` routes require a valid JWT cookie (`jwt_token`).

## Production Deployment

It is recommended to run the app behind a reverse proxy (e.g. nginx) with Gunicorn as the WSGI server, managed by systemd.

### systemd service

Create `/etc/systemd/system/proxy-manager.service`:

```ini
[Unit]
Description=Proxy Manager
After=network.target

[Service]
User=www-data
WorkingDirectory=/root/proxy-manager
EnvironmentFile=/root/proxy-manager/.env
ExecStart=/root/proxy-manager/.venv/bin/gunicorn main:app \
    -w 4 \
    -b 127.0.0.1:5000 \
    --access-logfile /var/log/proxy-manager/access.log \
    --error-logfile /var/log/proxy-manager/error.log
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo mkdir -p /var/log/proxy-manager
sudo chown www-data: /var/log/proxy-manager
sudo chown www-data: /opt/proxy-manager
sudo systemctl daemon-reload
sudo systemctl enable --now proxy-manager
```

Check status:

```bash
sudo systemctl status proxy-manager
journalctl -u proxy-manager -f
```

### 4. nginx reverse proxy

Add a server block to proxy HTTP traffic to Gunicorn. Place this in `/etc/nginx/sites-available/proxy-manager` and symlink it into `/etc/nginx/sites-enabled/` — **not** in the stream `conf.d` directory used for TCP/UDP proxies.

```bash
sudo ln -s /etc/nginx/sites-available/proxy-manager /etc/nginx/sites-enabled/proxy-manager
```

```nginx
server {
    listen 80;
    server_name proxy.domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## Development

Set `DEVELOPMENT=true` in `.env` to disable nginx test and reload calls, allowing the app to run without nginx installed.
