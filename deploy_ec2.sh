#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="ec2-user"
FFMPEG_BIN="/usr/local/bin/ffmpeg"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "$(id -un)" == "$SERVICE_USER" ]] || fail "Run this script as ${SERVICE_USER}, not root or another user."
[[ "$APP_DIR" == "/home/${SERVICE_USER}/MoneyPrinterTurbo" ]] \
  || fail "Expected project at /home/${SERVICE_USER}/MoneyPrinterTurbo; found ${APP_DIR}."

if ! command -v python3.11 >/dev/null 2>&1; then
  command -v dnf >/dev/null 2>&1 || fail "Python 3.11 is missing and dnf is unavailable."
  sudo dnf install -y python3.11 python3.11-pip python3.11-devel gcc gcc-c++ make
fi
command -v python3.11 >/dev/null 2>&1 || fail "Install Python 3.11 before continuing."

[[ -x "$FFMPEG_BIN" ]] || fail "FFmpeg is not executable at ${FFMPEG_BIN}."
"$FFMPEG_BIN" -version >/dev/null 2>&1 || fail "FFmpeg at ${FFMPEG_BIN} did not start successfully."
[[ -s "${APP_DIR}/.env" && -r "${APP_DIR}/.env" ]] \
  || fail "Missing, empty, or unreadable ${APP_DIR}/.env. Create it without exposing secrets."
if grep -Eq '^[[:space:]]*export[[:space:]]+' "${APP_DIR}/.env"; then
  fail ".env is loaded by systemd; use KEY=value lines without shell 'export' prefixes."
fi
[[ -r "${APP_DIR}/requirements.txt" ]] || fail "requirements.txt is missing or unreadable."
[[ -d "${APP_DIR}/storage" && -w "${APP_DIR}/storage" ]] \
  || fail "storage/ must exist and be writable by ${SERVICE_USER}."

chmod 600 "${APP_DIR}/.env"
python3.11 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

sudo install -m 0644 "${APP_DIR}/deploy/systemd/srz-autoflow.service" \
  /etc/systemd/system/srz-autoflow.service
sudo install -m 0644 "${APP_DIR}/deploy/systemd/srz-autoflow-scheduler-ec2.service" \
  /etc/systemd/system/srz-autoflow-scheduler.service
sudo systemctl daemon-reload
sudo systemctl enable --now srz-autoflow.service srz-autoflow-scheduler.service

printf '\nDeployment complete. Service status:\n'
sudo systemctl --no-pager --full status srz-autoflow.service srz-autoflow-scheduler.service
