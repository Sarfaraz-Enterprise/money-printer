# Amazon Linux 2023 EC2 deployment

The Streamlit dashboard and autonomous scheduler run as separate systemd
services under `ec2-user`. The project requires Python 3.11+. The setup script
installs Python 3.11 and build tools via `dnf` if Python 3.11 is not present;
it verifies the existing `/usr/local/bin/ffmpeg` installation rather than
installing a different FFmpeg binary.

## 1. Prepare the project and `.env`

Place the complete project at `/home/ec2-user/MoneyPrinterTurbo`, including
`webui.py`, `requirements.txt`, `storage/`, and the `deploy/` directory. In the
EC2 shell:

```bash
cd /home/ec2-user/MoneyPrinterTurbo
if [ ! -e .env ]; then install -m 600 /dev/null .env; fi
nano .env
chmod 600 .env
test -s .env && echo ".env exists and is non-empty"
test -x /usr/local/bin/ffmpeg && /usr/local/bin/ffmpeg -version | head -n 1
```

Enter secrets in `.env` with a text editor; do not paste them into shell
commands or check the file into git. Use `KEY=value` lines (no `export`
prefix), because systemd reads the same file using `EnvironmentFile`. The
script verifies the file without printing its contents.

## 2. Create the virtualenv, install requirements, and start services

```bash
cd /home/ec2-user/MoneyPrinterTurbo
bash deploy_ec2.sh
```

The script builds `.venv` with Python 3.11, installs `requirements.txt`,
validates FFmpeg and `.env`, installs both systemd units, and enables/starts
them. `srz-autoflow.service` runs Streamlit headlessly on port 8501;
`srz-autoflow-scheduler.service` runs the autonomous worker independently.
Do not run an additional FastAPI process that owns the same scheduler.

## 3. Verify, inspect logs, and restart

```bash
sudo systemctl status srz-autoflow.service srz-autoflow-scheduler.service --no-pager
sudo journalctl -u srz-autoflow.service -f
sudo journalctl -u srz-autoflow-scheduler.service -f
curl -I http://127.0.0.1:8501
```

Both units use `Restart=always` and are enabled for system startup. After
changing `.env`, restart both services:

```bash
sudo systemctl restart srz-autoflow.service srz-autoflow-scheduler.service
```

The requested Streamlit bind address is `0.0.0.0`, so the port listens on all
interfaces. Restrict TCP 8501 in the EC2 security group to trusted source IPs,
or preferably place the app behind an authenticated HTTPS reverse proxy.
Do not expose this port publicly without access control.
