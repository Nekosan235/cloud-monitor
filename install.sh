#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Idempotent installer for the cloud-monitor push agent.
# Re-running updates the script and the systemd unit. An existing env file is
# left in place unless --force-env is set.

set -euo pipefail
umask 077

SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SOURCE_PY="${SOURCE_DIR}/cloud-monitor.py"
SOURCE_UNIT="${SOURCE_DIR}/deploy/cloud-monitor.service"

INSTALL_ROOT="/opt/personal-site"
ENV_DIR="/etc/personal-site"
ENV_FILE="${ENV_DIR}/cloud-monitor.env"
UNIT_DEST="/etc/systemd/system/cloud-monitor.service"
SERVICE_USER="personal-monitor"
SERVICE_GROUP="personal-monitor"

ENDPOINT=""
SECRET=""
SECRET_FILE=""
SECRET_FROM_FLAG=0
DEVICE_ID=""
DEVICE_NAME=""
DEVICE_REGION=""
DEVICE_IP=""
INTERVAL="15"
TIMEOUT="10"
PATHS=""
FORCE_ENV=0
DRY_RUN=0
NO_ENABLE=0

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [options]

Install cloud-monitor.py, the systemd unit, and a mode-600 env file.
Safe to re-run. An existing env file is not replaced unless --force-env is set.

Required when creating or replacing the env file:
  --endpoint URL          HTTPS report URL
  --device-id ID          [A-Za-z0-9_.:-]{1,64}
  --secret SECRET         HMAC secret, at least 32 characters
  --secret-file PATH      Read the secret from a single-line file (preferred)

Optional:
  --name NAME             Defaults to the device id
  --region REGION         Reported region (may be empty)
  --ip ADDRESS            Reported address; the agent does not discover one
  --interval SECONDS      Default 15, minimum 5
  --timeout SECONDS       Default 10, minimum 1
  --paths LIST            Comma-separated extra mount points
  --force-env             Replace an existing env file
  --no-enable             Install files but do not enable or start the service
  --dry-run               Validate and print the plan. Does not change the system.
  -h, --help

The secret is not printed. Prefer --secret-file so it does not sit in the
shell history. --secret is visible to other users in the process list while
the installer is running.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoint) ENDPOINT=${2:?missing value for --endpoint}; shift 2 ;;
    --secret) SECRET=${2:?missing value for --secret}; SECRET_FROM_FLAG=1; shift 2 ;;
    --secret-file) SECRET_FILE=${2:?missing value for --secret-file}; shift 2 ;;
    --device-id) DEVICE_ID=${2:?missing value for --device-id}; shift 2 ;;
    --name) DEVICE_NAME=${2:?missing value for --name}; shift 2 ;;
    --region) DEVICE_REGION=${2:?missing value for --region}; shift 2 ;;
    --ip) DEVICE_IP=${2:?missing value for --ip}; shift 2 ;;
    --interval) INTERVAL=${2:?missing value for --interval}; shift 2 ;;
    --timeout) TIMEOUT=${2:?missing value for --timeout}; shift 2 ;;
    --paths) PATHS=${2:?missing value for --paths}; shift 2 ;;
    --force-env) FORCE_ENV=1; shift ;;
    --no-enable) NO_ENABLE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "$SOURCE_PY" || ! -f "$SOURCE_UNIT" ]]; then
  echo "missing ${SOURCE_PY} or ${SOURCE_UNIT}" >&2
  exit 1
fi

write_env=1
if [[ -f "$ENV_FILE" && "$FORCE_ENV" -ne 1 ]]; then
  write_env=0
fi

read_secret_file() {
  local file=$1
  if [[ ! -f "$file" ]]; then
    echo "secret file not found: $file" >&2
    exit 1
  fi
  local -a lines=()
  mapfile -t lines < "$file"
  if [[ "${#lines[@]}" -ne 1 ]]; then
    echo "secret file must contain a single line" >&2
    exit 1
  fi
  SECRET=${lines[0]}
  SECRET=${SECRET#$'\ufeff'}
}

if [[ "$write_env" -eq 1 ]]; then
  if [[ -n "$SECRET_FILE" && -n "$SECRET" ]]; then
    echo "pass only one of --secret and --secret-file" >&2
    exit 1
  fi
  if [[ -n "$SECRET_FILE" ]]; then
    read_secret_file "$SECRET_FILE"
  elif [[ -z "$SECRET" && -n "${CLOUD_MONITOR_SECRET:-}" ]]; then
    SECRET=${CLOUD_MONITOR_SECRET}
  fi
  if [[ "$SECRET_FROM_FLAG" -eq 1 ]]; then
    echo "warning: --secret is visible in the process list; prefer --secret-file" >&2
  fi
  if [[ -z "$DEVICE_NAME" ]]; then
    DEVICE_NAME=$DEVICE_ID
  fi
  if [[ -z "$DEVICE_REGION" || -z "$DEVICE_IP" ]]; then
    echo "warning: region or ip is empty; the report will carry a blank field" >&2
  fi
fi

require_python() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required" >&2
    exit 1
  fi
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "Python 3.11 or newer is required" >&2
    exit 1
  fi
}

# Validate with the agent so installer rules cannot drift from the runtime.
# When dest is non-empty, also write the env file there (mode 600).
apply_env() {
  local dest=${1:-}
  CLOUD_MONITOR_SOURCE="$SOURCE_PY" \
  CLOUD_MONITOR_ENDPOINT="$ENDPOINT" \
  CLOUD_MONITOR_SECRET="$SECRET" \
  CLOUD_MONITOR_INTERVAL="$INTERVAL" \
  CLOUD_MONITOR_TIMEOUT="$TIMEOUT" \
  CLOUD_DEVICE_ID="$DEVICE_ID" \
  CLOUD_DEVICE_NAME="$DEVICE_NAME" \
  CLOUD_DEVICE_REGION="$DEVICE_REGION" \
  CLOUD_DEVICE_IP="$DEVICE_IP" \
  CLOUD_MONITOR_PATHS="$PATHS" \
  CLOUD_MONITOR_ENV_DEST="$dest" \
  python3 - <<'PY'
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("cloud_monitor", os.environ["CLOUD_MONITOR_SOURCE"])
module = importlib.util.module_from_spec(spec)
sys.modules["cloud_monitor"] = module
spec.loader.exec_module(module)

try:
    interval = int(os.environ["CLOUD_MONITOR_INTERVAL"])
    timeout = int(os.environ["CLOUD_MONITOR_TIMEOUT"])
except ValueError:
    print("CLOUD_MONITOR_INTERVAL and CLOUD_MONITOR_TIMEOUT must be integers", file=sys.stderr)
    sys.exit(1)

paths = os.environ.get("CLOUD_MONITOR_PATHS", "")
volumes = tuple(item.strip() for item in paths.split(",") if item.strip())
config = module.AgentConfig(
    endpoint=os.environ.get("CLOUD_MONITOR_ENDPOINT", ""),
    secret=os.environ.get("CLOUD_MONITOR_SECRET", ""),
    device_id=os.environ.get("CLOUD_DEVICE_ID", ""),
    name=os.environ.get("CLOUD_DEVICE_NAME", ""),
    region=os.environ.get("CLOUD_DEVICE_REGION", ""),
    ip=os.environ.get("CLOUD_DEVICE_IP", ""),
    interval=interval,
    timeout=timeout,
    volumes=volumes,
)
try:
    module.validate_config(config)
except module.ConfigError as error:
    print(error, file=sys.stderr)
    sys.exit(1)

dest = os.environ.get("CLOUD_MONITOR_ENV_DEST", "")
if not dest:
    sys.exit(0)

values = {
    "CLOUD_MONITOR_ENDPOINT": config.endpoint,
    "CLOUD_MONITOR_SECRET": config.secret,
    "CLOUD_MONITOR_INTERVAL": str(config.interval),
    "CLOUD_MONITOR_TIMEOUT": str(config.timeout),
    "CLOUD_DEVICE_ID": config.device_id,
    "CLOUD_DEVICE_NAME": config.name,
    "CLOUD_DEVICE_REGION": config.region,
    "CLOUD_DEVICE_IP": config.ip,
}
if paths:
    values["CLOUD_MONITOR_PATHS"] = paths
text = module.format_environment(values)
with open(dest, "w", encoding="utf-8") as handle:
    handle.write(text)
os.chmod(dest, 0o600)
PY
}

require_python

if [[ "$write_env" -eq 1 ]]; then
  apply_env ""
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "status: dry-run"
else
  echo "status: install"
fi
echo "script: ${INSTALL_ROOT}/cloud-monitor.py"
echo "unit: ${UNIT_DEST}"
echo "service-user: ${SERVICE_USER}"
if [[ "$write_env" -eq 1 ]]; then
  echo "env: write ${ENV_FILE} mode 600"
  echo "endpoint: ${ENDPOINT}"
  echo "secret: <redacted, ${#SECRET} characters>"
  echo "device-id: ${DEVICE_ID}"
  echo "name: ${DEVICE_NAME}"
  echo "region: ${DEVICE_REGION}"
  echo "ip: ${DEVICE_IP}"
  echo "interval: ${INTERVAL}"
  echo "timeout: ${TIMEOUT}"
  echo "paths: ${PATHS:-<unset>}"
else
  echo "env: leave existing ${ENV_FILE} (pass --force-env to replace it)"
fi

if [[ "$DRY_RUN" -eq 1 ]]; then
  if [[ "$NO_ENABLE" -eq 1 ]]; then
    echo "service: would install files and not enable"
  else
    echo "service: would enable --now on first install, or restart only if already active"
  fi
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install.sh must run as root (or pass --dry-run)" >&2
  exit 2
fi

if ! getent group "$SERVICE_GROUP" >/dev/null; then
  groupadd --system "$SERVICE_GROUP"
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  nologin=$(command -v nologin || true)
  if [[ -z "$nologin" ]]; then
    echo "nologin shell not found" >&2
    exit 1
  fi
  useradd \
    --system \
    --gid "$SERVICE_GROUP" \
    --no-create-home \
    --home-dir /nonexistent \
    --shell "$nologin" \
    "$SERVICE_USER"
fi

previously=0
active=0
if systemctl cat cloud-monitor.service >/dev/null 2>&1; then
  previously=1
fi
if systemctl is-active --quiet cloud-monitor.service; then
  active=1
fi

install -d -m 0755 -o root -g root "$INSTALL_ROOT" "$ENV_DIR"
install -m 0644 -o root -g root "$SOURCE_PY" "${INSTALL_ROOT}/cloud-monitor.py"
install -m 0644 -o root -g root "$SOURCE_UNIT" "$UNIT_DEST"

if [[ "$write_env" -eq 1 ]]; then
  tmp_env=$(mktemp "${ENV_FILE}.XXXXXX")
  chmod 600 "$tmp_env"
  if ! apply_env "$tmp_env"; then
    rm -f "$tmp_env"
    exit 1
  fi
  chown root:root "$tmp_env"
  mv -f "$tmp_env" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  chown root:root "$ENV_FILE"
fi

systemctl daemon-reload

if [[ "$NO_ENABLE" -eq 1 ]]; then
  echo "installed; service not enabled (--no-enable)"
  exit 0
fi

if [[ "$previously" -eq 0 ]]; then
  systemctl enable --now cloud-monitor.service
elif [[ "$active" -eq 1 ]]; then
  systemctl restart cloud-monitor.service
else
  echo "updated files; cloud-monitor.service is installed but not running"
fi

systemctl status cloud-monitor.service --no-pager || true
echo "check: journalctl -u cloud-monitor.service -n 50 --no-pager"
