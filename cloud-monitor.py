#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Push one Linux VM's CPU, memory, disk, and uptime to a personal-site monitor.

Wire contract (do not change casually):

* POST compact JSON to ``CLOUD_MONITOR_ENDPOINT`` (https, normally
  ``/api/monitor/remote/report``).
* Sign the raw body bytes. The header ``X-Monitor-Signature`` is
  ``sha256=`` plus HMAC-SHA256 of ``deviceId.timestamp.nonce.sha256(body)``.
* ``sha256(body)`` is the lowercase hex digest of those exact bytes.
* The site must hash the raw request body. Re-serializing JSON breaks the
  signature even when the object is equivalent.

The sister NAS ingest (``/api/monitor/nas/report``, legacy ``X-NAS-*``
headers) is the same signature and is not implemented here.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

__version__ = "1.0.0"

GIB = 1024**3
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MIN_SECRET_LENGTH = 32
MIN_INTERVAL_SECONDS = 5
DEFAULT_INTERVAL_SECONDS = 15
DEFAULT_TIMEOUT_SECONDS = 10
USER_AGENT = "personal-site-cloud-monitor/1"
REMOTE_REPORT_PATH = "/api/monitor/remote/report"
_AUTO = object()

ENV_KEYS = (
    "CLOUD_MONITOR_ENDPOINT",
    "CLOUD_MONITOR_SECRET",
    "CLOUD_MONITOR_INTERVAL",
    "CLOUD_MONITOR_TIMEOUT",
    "CLOUD_DEVICE_ID",
    "CLOUD_DEVICE_NAME",
    "CLOUD_DEVICE_REGION",
    "CLOUD_DEVICE_IP",
    "CLOUD_MONITOR_PATHS",
)


class ConfigError(ValueError):
    """The process environment or CLI flags cannot produce a report."""


@dataclass
class AgentConfig:
    endpoint: str
    secret: str
    device_id: str
    name: str
    region: str = ""
    ip: str = ""
    interval: int = DEFAULT_INTERVAL_SECONDS
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    volumes: tuple[str, ...] = ()
    once: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class ProcPaths:
    meminfo: str = "/proc/meminfo"
    stat: str = "/proc/stat"
    uptime: str = "/proc/uptime"
    root: str = "/"


def as_gib(value: int) -> float:
    return round(value / GIB, 2)


def read_meminfo(path: str = "/proc/meminfo") -> dict:
    values: dict[str, int] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            parts = rest.strip().split()
            if len(parts) >= 2 and parts[1] == "kB":
                try:
                    values[key] = int(parts[0])
                except ValueError as error:
                    raise RuntimeError(f"invalid meminfo value for {key}") from error
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None or total <= 0 or available < 0 or available > total:
        raise RuntimeError("invalid meminfo")
    total_bytes = total * 1024
    available_bytes = available * 1024
    # `reserved` stays null so the payload matches the agent already deployed.
    # Reservation is a disk concept; memory reports used/total/available.
    return {
        "used": as_gib(total_bytes - available_bytes),
        "total": as_gib(total_bytes),
        "available": as_gib(available_bytes),
        "reserved": None,
        "unit": "GiB",
    }


def read_cpu(path: str = "/proc/stat") -> dict[str, int]:
    """Return aggregate idle and total jiffies from the ``cpu `` line.

    Idle includes iowait (the 5th numeric field), matching the usual
    "busy = total - idle - iowait" reading of ``/proc/stat``.
    """

    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("cpu "):
                continue
            parts = line.split()[1:]
            if len(parts) < 5:
                raise RuntimeError("aggregate cpu line is too short")
            try:
                values = [int(value) for value in parts[:8]]
            except ValueError as error:
                raise RuntimeError("invalid aggregate cpu counters") from error
            idle = values[3] + values[4]
            return {"idle": idle, "total": sum(values)}
    raise RuntimeError("missing aggregate cpu counters")


def cpu_percent(previous: dict[str, int] | None, current: dict[str, int]) -> float | None:
    """Percent busy between two aggregate samples, or None the first time.

    A zero or backwards delta is reported as None so a counter reset does
    not become a fake 0% or 100% spike. Values are clamped to 0..100 and
    rounded to one decimal.
    """

    if not previous:
        return None
    total_delta = current["total"] - previous["total"]
    idle_delta = current["idle"] - previous["idle"]
    if total_delta <= 0 or idle_delta < 0:
        return None
    value = ((total_delta - idle_delta) / total_delta) * 100
    return round(min(100.0, max(0.0, value)), 1)


def volume_metric(path: str) -> dict:
    stats = os.statvfs(path)
    stat_info = os.stat(path)
    block_size = stats.f_frsize or stats.f_bsize
    total = stats.f_blocks * block_size
    free = stats.f_bfree * block_size
    available = stats.f_bavail * block_size
    if total <= 0 or free < 0 or available < 0 or available > free:
        raise RuntimeError(f"invalid statvfs values for {path}")
    # `used` follows df: blocks that are not free. `reserved` is the gap
    # between free and available (root-reserved blocks). `device` is only a
    # dedupe key for this boot, not a stable disk serial.
    return {
        "path": path,
        "device": str(stat_info.st_dev),
        "used": as_gib(total - free),
        "total": as_gib(total),
        "available": as_gib(available),
        "reserved": as_gib(free - available),
        "unit": "GiB",
    }


def read_volumes(paths: tuple[str, ...] | list[str], skip_devices: set[str]) -> tuple[list[dict], list[dict]]:
    seen_devices = set(skip_devices)
    volumes: list[dict] = []
    missing: list[dict] = []
    for raw_path in paths:
        path = raw_path.strip()
        if not path or path == "/":
            continue
        if not os.path.isdir(path):
            missing.append({"path": path, "error": "not_directory"})
            continue
        try:
            metric = volume_metric(path)
        except OSError as error:
            print(f"skip volume {path}: {error}", file=sys.stderr)
            missing.append({"path": path, "error": error.__class__.__name__})
            continue
        if metric["device"] in seen_devices:
            continue
        seen_devices.add(metric["device"])
        volumes.append(metric)
    return volumes, missing


def read_disk(extra_paths: tuple[str, ...] | list[str], root_path: str = "/") -> dict:
    root = volume_metric(root_path)
    volumes, missing = read_volumes(extra_paths, {root["device"]})
    disk = {
        "used": root["used"],
        "total": root["total"],
        "available": root["available"],
        "reserved": root["reserved"],
        "unit": "GiB",
        "volumes": volumes,
        "missingVolumes": missing,
        "partial": bool(missing),
    }
    if missing:
        disk["error"] = {
            "code": "partial_volumes",
            "message": "Some configured volumes were unavailable",
        }
    return disk


def load_average() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def uptime_seconds(path: str = "/proc/uptime") -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return int(time.monotonic())


def build_report(
    config: AgentConfig,
    previous_cpu: dict[str, int] | None,
    *,
    paths: ProcPaths | None = None,
    sampled_at: str | None = None,
    os_name: str | None = None,
    vcpu: int | None | object = _AUTO,
    loadavg: list[float] | None | object = _AUTO,
) -> tuple[dict, dict[str, int]]:
    paths = paths or ProcPaths()
    current_cpu = read_cpu(paths.stat)
    if sampled_at is None:
        sampled_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if os_name is None:
        os_name = f"{platform.system()} {platform.release()}"
    if vcpu is _AUTO:
        vcpu = os.cpu_count()
    if loadavg is _AUTO:
        loadavg = load_average()
    report = {
        "deviceId": config.device_id,
        "sampledAt": sampled_at,
        "server": {
            "id": config.device_id,
            "name": config.name or config.device_id,
            "host": config.ip,
            "ip": config.ip,
            "region": config.region,
            "os": os_name,
            "vcpu": vcpu,
            "uptimeSeconds": uptime_seconds(paths.uptime),
            "cpuPercent": cpu_percent(previous_cpu, current_cpu),
            "loadAverage": loadavg,
        },
        "metrics": {
            "memory": read_meminfo(paths.meminfo),
            "disk": read_disk(config.volumes, root_path=paths.root),
        },
    }
    return report, current_cpu


def canonical_body(report: dict) -> bytes:
    """Bytes that are posted and hashed.

    Keys are sorted, separators have no extra whitespace, and non-ASCII
    characters use ``\\u`` escapes (``json.dumps`` default). The site must
    HMAC this byte string, not a re-encoded object.
    """

    return json.dumps(report, separators=(",", ":"), sort_keys=True).encode("utf-8")


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signature_message(device_id: str, timestamp: str, nonce: str, body: bytes) -> bytes:
    digest = body_sha256(body)
    return f"{device_id}.{timestamp}.{nonce}.{digest}".encode("utf-8")


def sign(secret: str, device_id: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = signature_message(device_id, timestamp, nonce, body)
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def quote_env_value(value: str) -> str:
    """Quote one systemd EnvironmentFile value.

    systemd strips unquoted whitespace and treats a raw ``#`` as a comment.
    A backslash escapes the next character, so ``\\``, ``"``, and ``$`` are
    escaped. Escaping ``$`` keeps the secret intact on parsers that also
    expand variables; on systemd itself ``\\$`` round-trips back to ``$``.
    """

    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return '"' + escaped + '"'


def format_environment(values: dict[str, str]) -> str:
    """Serialize agent settings for ``/etc/personal-site/cloud-monitor.env``."""

    lines = [
        "# cloud-monitor agent environment",
        "# Mode 600, owner root:root. Never commit the filled-in file.",
    ]
    for key in ENV_KEYS:
        if key not in values:
            continue
        value = values[key]
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ConfigError(f"{key} must be a single line")
        lines.append(f"{key}={quote_env_value(value)}")
    lines.append("")
    return "\n".join(lines)


def build_request(
    config: AgentConfig,
    report: dict,
    *,
    timestamp: str,
    nonce: str,
) -> urllib.request.Request:
    body = canonical_body(report)
    signature = sign(config.secret, config.device_id, timestamp, nonce, body)
    return urllib.request.Request(
        config.endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Monitor-Device-Id": config.device_id,
            "X-Monitor-Timestamp": timestamp,
            "X-Monitor-Nonce": nonce,
            "X-Monitor-Signature": signature,
        },
        method="POST",
    )


def _http_error_message(error: urllib.error.HTTPError) -> str:
    raw = b""
    try:
        raw = error.read() or b""
    except Exception:
        raw = b""
    snippet = raw.decode("utf-8", "replace").strip().replace("\n", " ")[:300]
    if snippet:
        return f"HTTP {error.code}: {snippet}"
    reason = error.reason or ""
    return f"HTTP {error.code}: {reason}".rstrip(": ")


def send_report(
    config: AgentConfig,
    report: dict,
    *,
    timestamp: str | None = None,
    nonce: str | None = None,
    opener=None,
) -> None:
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or secrets.token_urlsafe(18)
    request = build_request(config, report, timestamp=timestamp, nonce=nonce)
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=config.timeout) as response:
            response.read()
            if response.status != 200:
                raise RuntimeError(f"unexpected status {response.status}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(_http_error_message(error)) from error


def validate_config(config: AgentConfig) -> None:
    endpoint = config.endpoint or ""
    if not endpoint.startswith("https://") or len(endpoint) <= len("https://"):
        raise ConfigError("CLOUD_MONITOR_ENDPOINT must be an https:// URL")
    if any(character.isspace() for character in endpoint):
        raise ConfigError("CLOUD_MONITOR_ENDPOINT must not contain whitespace")
    parsed = urlparse(endpoint)
    if not parsed.hostname:
        raise ConfigError("CLOUD_MONITOR_ENDPOINT must include a host")
    secret = config.secret or ""
    if "\n" in secret or "\r" in secret or "\x00" in secret or len(secret) < MIN_SECRET_LENGTH:
        raise ConfigError(
            f"CLOUD_MONITOR_SECRET must be at least {MIN_SECRET_LENGTH} characters "
            "and must not contain newlines"
        )
    if not config.device_id or DEVICE_ID_RE.fullmatch(config.device_id) is None:
        raise ConfigError("CLOUD_DEVICE_ID must match [A-Za-z0-9_.:-]{1,64}")
    if config.interval < MIN_INTERVAL_SECONDS:
        raise ConfigError(f"interval must be at least {MIN_INTERVAL_SECONDS} seconds")
    if config.timeout < 1:
        raise ConfigError("timeout must be at least 1 second")
    for label, value in (
        ("CLOUD_DEVICE_NAME", config.name),
        ("CLOUD_DEVICE_REGION", config.region),
        ("CLOUD_DEVICE_IP", config.ip),
    ):
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ConfigError(f"{label} must not contain newlines")
    for volume in config.volumes:
        if "\n" in volume or "\r" in volume or "\x00" in volume:
            raise ConfigError("volume paths must not contain newlines")


def endpoint_path_warning(endpoint: str) -> str | None:
    path = urlparse(endpoint).path.rstrip("/")
    if path.endswith(REMOTE_REPORT_PATH):
        return None
    return (
        "endpoint path does not end with /api/monitor/remote/report "
        "(NAS ingest /api/monitor/nas/report is a different agent)"
    )


def _parse_int(label: str, raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{label} must be an integer") from error


def parse_args(argv: list[str] | None = None) -> AgentConfig:
    parser = argparse.ArgumentParser(
        description="Push Linux cloud VM metrics to a personal-site compatible HTTPS endpoint.",
    )
    parser.add_argument("--version", action="version", version=f"cloud-monitor {__version__}")
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("CLOUD_MONITOR_ENDPOINT", ""),
        help="HTTPS report URL, normally ending in /api/monitor/remote/report",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("CLOUD_MONITOR_SECRET", ""),
        help="shared or per-device HMAC secret (at least 32 characters)",
    )
    parser.add_argument("--device-id", default=os.environ.get("CLOUD_DEVICE_ID", ""))
    parser.add_argument("--name", default=os.environ.get("CLOUD_DEVICE_NAME", ""))
    parser.add_argument("--region", default=os.environ.get("CLOUD_DEVICE_REGION", ""))
    parser.add_argument(
        "--ip",
        default=os.environ.get("CLOUD_DEVICE_IP", ""),
        help="address to report; the agent does not discover a public IP",
    )
    parser.add_argument(
        "--interval",
        default=os.environ.get("CLOUD_MONITOR_INTERVAL", str(DEFAULT_INTERVAL_SECONDS)),
    )
    parser.add_argument(
        "--timeout",
        default=os.environ.get("CLOUD_MONITOR_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)),
    )
    parser.add_argument(
        "--volume",
        dest="volumes",
        action="append",
        help="extra mount point; repeat to set more than one (replaces CLOUD_MONITOR_PATHS)",
    )
    parser.add_argument("--once", action="store_true", help="send one report and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print one human-readable report and do not sign or send it",
    )
    args = parser.parse_args(argv)

    interval = _parse_int("CLOUD_MONITOR_INTERVAL", str(args.interval))
    timeout = _parse_int("CLOUD_MONITOR_TIMEOUT", str(args.timeout))
    if args.volumes is None:
        configured = os.environ.get("CLOUD_MONITOR_PATHS", "")
        volumes = tuple(item.strip() for item in configured.split(",") if item.strip())
    else:
        volumes = tuple(item.strip() for item in args.volumes if item and item.strip())

    device_id = (args.device_id or "").strip()
    name = (args.name or "").strip() or device_id
    config = AgentConfig(
        endpoint=(args.endpoint or "").strip(),
        secret=args.secret or "",
        device_id=device_id,
        name=name,
        region=(args.region or "").strip(),
        ip=(args.ip or "").strip(),
        interval=interval,
        timeout=timeout,
        volumes=volumes,
        once=bool(args.once),
        dry_run=bool(args.dry_run),
    )
    validate_config(config)
    return config


def seconds_until_next(started: float, interval: int, now: float) -> float:
    """Sleep long enough to honor the interval, but never less than one second.

    The one-second floor keeps a hung clock or a very slow sample from turning
    into a hot loop. It does not change the configured interval when the
    sample itself is fast.
    """

    return max(1.0, interval - (now - started))


def run(
    config: AgentConfig,
    *,
    sleep=time.sleep,
    monotonic=time.monotonic,
    send=send_report,
    stdout=None,
    paths: ProcPaths | None = None,
) -> int:
    output = stdout or sys.stdout
    previous_cpu: dict[str, int] | None = None
    while True:
        started = monotonic()
        next_cpu: dict[str, int] | None = None
        try:
            report, next_cpu = build_report(config, previous_cpu, paths=paths)
            if config.dry_run:
                json.dump(report, output, indent=2, sort_keys=True, ensure_ascii=False)
                output.write("\n")
                output.flush()
                return 0
            send(config, report)
            previous_cpu = next_cpu
            if config.once:
                return 0
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            print(f"cloud-monitor error: {error}", file=sys.stderr)
            # Keep the CPU sample when collection worked and the POST failed,
            # so the next interval still has a real delta after a timeout or 502.
            if next_cpu is not None:
                previous_cpu = next_cpu
            if config.once or config.dry_run:
                return 1
        sleep(seconds_until_next(started, config.interval, monotonic()))


def main(argv: list[str] | None = None) -> int:
    try:
        config = parse_args(argv)
    except ConfigError as error:
        raise SystemExit(str(error)) from error
    warning = endpoint_path_warning(config.endpoint)
    if warning:
        print(f"cloud-monitor warning: {warning}", file=sys.stderr)
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
