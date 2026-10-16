# Ubuntu EC2 deployment with systemd

These units run the Streamlit dashboard and autonomous scheduler as separate
services. This is required for a UI-only deployment: Streamlit does not start
the scheduler. Do not also run a FastAPI service that owns the scheduler, or
the standalone worker; the scheduler's lock prevents a second owner, but the
extra process is unnecessary.

The dashboard listens on `127.0.0.1:8501`, so it is not directly exposed to the
internet. Use an authenticated HTTPS reverse proxy for remote access.

## 1. Install OS packages and create a service account

```bash
sudo apt update
sudo apt install -y ffmpeg git python3-venv build-essential curl ca-certificates
sudo adduser --system --group --home /opt/srz-autoflow srz-autoflow
sudo install -d -o srz-autoflow -g srz-autoflow /opt/srz-autoflow
```

The project requires Python 3.11 or newer. Ubuntu 22.04's system Python is
3.10, so install a managed Python 3.11 with `uv` below; this also gives both
Ubuntu 22.04 and 24.04 the same predictable runtime.

## 2. Put the application in `/opt/srz-autoflow`

Clone the repository/branch that contains your Srz AutoFlow changes. Replace
the URL with your private repository URL as appropriate:

```bash
sudo -u srz-autoflow git clone <YOUR_REPOSITORY_URL> /opt/srz-autoflow
cd /opt/srz-autoflow
```

If you copied the application instead of cloning it, ensure the project files
are directly in `/opt/srz-autoflow` (including `pyproject.toml`, `uv.lock`,
`webui.py`, `app/`, `storage/`, and `deploy/`).

## 3. Create the virtual environment and install dependencies

```bash
sudo install -d -o srz-autoflow -g srz-autoflow /opt/srz-autoflow/.local/bin
sudo -u srz-autoflow sh -c 'curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/opt/srz-autoflow/.local/bin sh'
cd /opt/srz-autoflow
sudo -u srz-autoflow /opt/srz-autoflow/.local/bin/uv python install 3.11
sudo -u srz-autoflow /opt/srz-autoflow/.local/bin/uv venv --python 3.11 .venv
sudo -u srz-autoflow /opt/srz-autoflow/.local/bin/uv sync --frozen --no-dev
```

`uv sync --frozen` installs the versions pinned by the checked-in lockfile.
For an application update, pull the desired commit and run `uv sync --frozen
--no-dev` again before restarting the services.

## 4. Configure files and permissions

Create or copy `config.toml` as appropriate for your deployment. Create the
root `.env` using an editor (do not put secret values in shell history):

```bash
sudo -u srz-autoflow install -m 600 /dev/null /opt/srz-autoflow/.env
sudo -u srz-autoflow nano /opt/srz-autoflow/.env
sudo chown -R srz-autoflow:srz-autoflow /opt/srz-autoflow/storage
sudo chmod 600 /opt/srz-autoflow/.env
```

The application loads `.env` from its project root. Keep it owned by the
service account and mode `600`; never commit it. Ensure profiles, tokens,
outputs, logs, and the SQLite ledger under `storage/` are backed up securely.

## 5. Install and enable both systemd services

```bash
sudo install -m 644 deploy/systemd/srz-autoflow-web.service /etc/systemd/system/
sudo install -m 644 deploy/systemd/srz-autoflow-scheduler.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now srz-autoflow-web.service srz-autoflow-scheduler.service
```

Both services use the project's `.venv`, run from the project root, restart
after an unexpected exit, and start automatically after reboot. The separate
scheduler unit is for deployments where Streamlit is the only application
server; if FastAPI is also running and owns the scheduler, enable only the
dashboard service and keep the FastAPI service enabled instead.

## 6. Check service health and logs

```bash
sudo systemctl status srz-autoflow-web.service srz-autoflow-scheduler.service --no-pager
sudo journalctl -u srz-autoflow-web.service -f
sudo journalctl -u srz-autoflow-scheduler.service -f
```

On the EC2 instance, verify the dashboard listener:

```bash
curl -I http://127.0.0.1:8501
```

For a remote browser, configure an HTTPS reverse proxy (for example, Nginx
with authentication) to proxy to `127.0.0.1:8501`. Do not open inbound port
8501 to the public internet in the EC2 security group. Only expose the proxy's
HTTPS port (normally 443), restricted further to trusted source IPs where
possible.

To update, restart, or stop:

```bash
cd /opt/srz-autoflow
sudo -u srz-autoflow git pull --ff-only
sudo -u srz-autoflow /opt/srz-autoflow/.local/bin/uv sync --frozen --no-dev
sudo systemctl restart srz-autoflow-web.service srz-autoflow-scheduler.service
sudo systemctl disable --now srz-autoflow-web.service srz-autoflow-scheduler.service
```
