# cloud-monitor

A small Linux agent that samples one VM and pushes a signed JSON report to a personal-site compatible monitor. It is the collector only. The HTTPS ingest, device slots, and dashboard stay in personal-site.

Deployment steps are in [DEPLOY.md](DEPLOY.md) (Chinese). This file is the protocol and security contract.

## What it does

On a Debian or Ubuntu VM the agent reads `/proc` and `statvfs`, then POSTs one sample every 15 seconds (minimum 5):

- CPU busy percent, from two `/proc/stat` samples
- load average and uptime
- memory used / total / available, in GiB
- root disk used / total / available / reserved, plus optional extra mount points
- the device id, name, region, and address you configured

It does not listen on a port. It only makes outbound HTTPS requests.

## What it does not do

- It does not sample the machine that runs personal-site. That host reads its own `/proc` inside the site process (`MONITORING_MODE=live`, refresh 5 seconds). `HISTORY_LIMIT=60` is an in-memory ring of `memory.used` and `disk.used` on that host. The ring is cleared on process restart. It is not a time-series database.
- It does not implement the NAS sibling. That report URL is `/api/monitor/nas/report`. The site also accepts the legacy `X-NAS-Device-Id`, `X-NAS-Timestamp`, `X-NAS-Nonce`, and `X-NAS-Signature` headers. This agent sends `X-Monitor-*` only. A NAS script can use the same signature later.
- It does not discover the VM's public address. `server.host` and `server.ip` are both `CLOUD_DEVICE_IP`.

## Wire protocol

`POST https://<site>/api/monitor/remote/report`

The URL must be `https://`. The path should end in `/api/monitor/remote/report`. Another path is allowed only when it is a compatible ingest; the agent prints a warning otherwise.

Headers:

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `User-Agent` | `personal-site-cloud-monitor/1` |
| `X-Monitor-Device-Id` | device id |
| `X-Monitor-Timestamp` | Unix time in seconds, decimal string |
| `X-Monitor-Nonce` | random token, new on every request |
| `X-Monitor-Signature` | `sha256=` + lowercase HMAC-SHA256 hex |

Signature message, UTF-8, four fields joined by `.`:

```text
deviceId.timestamp.nonce.sha256(body)
```

`sha256(body)` is the lowercase hex SHA-256 of the raw request body. The HMAC key is the shared secret as UTF-8. The site must hash those exact bytes. Parsing the JSON and serializing it again will not match, even when the object is equivalent.

Body bytes are `json.dumps(..., separators=(",", ":"), sort_keys=True)` with the default `\u` escapes for non-ASCII. `--dry-run` prints an indented, human-readable report and does not sign it. Do not recompute the HMAC from dry-run text.

```json
{
  "deviceId": "cloud-example-a",
  "sampledAt": "2026-01-02T03:04:05Z",
  "server": {
    "id": "cloud-example-a",
    "name": "Example cloud VM A",
    "host": "203.0.113.10",
    "ip": "203.0.113.10",
    "region": "example-region-a",
    "os": "Linux 6.1.0",
    "vcpu": 2,
    "uptimeSeconds": 3600,
    "cpuPercent": 12.5,
    "loadAverage": [0.2, 0.15, 0.1]
  },
  "metrics": {
    "memory": {"used": 0.5, "total": 1.0, "available": 0.5, "reserved": null, "unit": "GiB"},
    "disk": {
      "used": 3.0,
      "total": 20.0,
      "available": 16.0,
      "reserved": 1.0,
      "unit": "GiB",
      "volumes": [],
      "missingVolumes": [],
      "partial": false
    }
  }
}
```

`sampledAt` is UTC `YYYY-MM-DDTHH:MM:SSZ`. `cpuPercent` is `null` on the first sample after the process starts, and again if the counters go backwards. Memory `reserved` is always `null`; it is kept so the payload matches the agent already deployed. Disk `reserved` is the root-only gap between free and available blocks (`df` semantics: used = total − free). Extra volumes repeat that shape and add `path` and `device`. `device` is the `st_dev` of this boot, used only to skip a second view of the same filesystem.

When a configured extra path is missing or not readable, `partial` is true, the path is listed in `missingVolumes`, and the report still includes the root disk plus `error.code = partial_volumes`.

Checks the agent enforces before it sends:

- secret length at least 32, no newlines
- device id matches `[A-Za-z0-9_.:-]{1,64}`
- interval at least 5 seconds (default 15)
- timeout at least 1 second (default 10)

## Site registration

personal-site owns ingest. This repo only has to agree with it:

- `REMOTE_CLOUD_MONITOR_SECRET` is the default HMAC secret.
- `REMOTE_CLOUD_N_*` is one slot per cloud VM, N from 1 to 8. A slot may set its own secret. When it does, that device's `CLOUD_MONITOR_SECRET` must be the slot secret, not the default.
- The agent's `deviceId` is the lookup key. Two VMs must not share one id.
- Site stale/skew defaults are 15s, 60s, and 120s, the same knobs as the NAS channel. Keep `CLOUD_MONITOR_INTERVAL` at 15 so a healthy VM stays inside that window. Sync the VM clock; `X-Monitor-Timestamp` is the VM's Unix time.

The site returns HTTP 200 on success. Timeouts and HTTP 502 are logged. The process keeps running and retries on the next interval. If collection succeeded, the CPU sample is kept so the next report still has a real delta.

## Security

- HMAC authenticates the body. It does not encrypt it. HTTPS is required, and TLS certificates are verified. There is no insecure switch. For a private CA, install it in the system trust store or set `SSL_CERT_FILE` in a systemd drop-in.
- Do not commit a filled-in env file. `deploy/cloud-monitor.env.example` uses `monitor.example.com` and the documentation address `203.0.113.10` (RFC 5737).
- `/etc/personal-site/cloud-monitor.env` is mode `600`, owner `root`. systemd reads it before dropping to `personal-monitor`. The service user cannot read the secret, and it has no login shell.
- The agent never prints the secret or the signature. `install.sh --secret` is visible in the process list; use `--secret-file`.
- The nonce is unique per request. Replay rejection is the site's job.
- A quiet journal is the success signal. Errors can include a short HTTP response snippet. They do not include the request signature.

## Layout

```text
cloud-monitor.py                 agent, Python 3.11+, standard library only
deploy/cloud-monitor.service     systemd unit
deploy/cloud-monitor.env.example env template
install.sh                       idempotent installer
DEPLOY.md                        Chinese install guide
tests/test_cloud_monitor.py      unittest, no network
```

Installed paths, chosen so the unit matches hosts that already run this agent:

| Path | Mode | Purpose |
| --- | --- | --- |
| `/opt/personal-site/cloud-monitor.py` | `0644` root | program |
| `/etc/personal-site/cloud-monitor.env` | `0600` root | secret and device identity |
| `/etc/systemd/system/cloud-monitor.service` | `0644` root | unit |
| user `personal-monitor` | nologin | service account |

`PYTHONUNBUFFERED=1` is set so journald sees a line as soon as it is printed. The rest of the unit (64 MiB memory cap, `ProtectSystem=strict`, `NoNewPrivileges`, private tmp, protected home) matches the unit already enabled on those VMs.

## Develop

From the repository root:

```bash
python3 -m unittest discover -s tests -v
```

No third-party packages. Tests build `/proc` fixtures, check the HMAC vector, and exercise `install.sh --dry-run`. They do not open sockets.

```bash
python3 cloud-monitor.py --help
```

## Publishing a public GitHub mirror

This tree is already a git repository. A public GitHub repo is an extra remote, not a different project.

1. Create an empty GitHub repository. Skip the GitHub README, license, and gitignore. This tree already has them.
2. Confirm the only env file that is tracked is the example:

   ```bash
   git ls-files | grep -E 'env|secret'
   ```

   The only line should be `deploy/cloud-monitor.env.example`.
3. Push `main`:

   ```bash
   git remote add github git@github.com:Nekosan235/cloud-monitor.git
   git push -u github main
   ```

4. Point the GitHub description at `DEPLOY.md`. The MIT copyright line is 卢涵煜 (Nekosan235).
5. If a real secret was ever committed, rotate it on the site and on every VM. Rewriting git history is not a substitute for rotation.

## Choices

- One script, not a package. `install.sh` copies it into `/opt`. There is nothing to `pip install`.
- Standard library only, so a VM does not need a virtualenv.
- The reported address is configuration, not a network probe, because a cloud VM often sees only a private NIC.
- Extra mount points on the same filesystem as `/` are dropped. A missing mount does not drop the root sample.
- The NAS agent is intentionally absent. The signature string above is the contract it should reuse.
