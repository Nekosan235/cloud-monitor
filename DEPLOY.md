# 云主机监控推送代理部署

中文部署说明。协议、字段和安全边界的英文说明在 [README.md](README.md)。

本仓库只包含采集和上报。personal-site 负责接收 `POST /api/monitor/remote/report`、校验 HMAC、登记设备。跑 personal-site 的那台机器自己读 `/proc`，不要再装这个代理。

示例地址 `203.0.113.10` 和 `203.0.113.20` 来自 RFC 5737，只用于文档。示例域名是 `monitor.example.com`。

## 你需要准备的

- Debian 或 Ubuntu，Python 3.11 或更新（系统自带的 `python3` 即可）
- 能出站访问站点的 TCP 443。本代理不监听任何端口
- 站点上的 HTTPS 地址，路径以 `/api/monitor/remote/report` 结尾
- 至少 32 个字符的 HMAC 密钥
- 每台 VM 一个不重复的 `deviceId`，字符集 `[A-Za-z0-9_.:-]{1,64}`

站点侧（不在本仓库）：

- 默认密钥是 `REMOTE_CLOUD_MONITOR_SECRET`
- 云主机槽位是 `REMOTE_CLOUD_N_*`，最多 8 个。某个槽位如果写了自己的 SECRET，这台机器的 `CLOUD_MONITOR_SECRET` 必须用槽位密钥
- 站点用 `deviceId` 找到槽位。两台 VM 不能共用一个 id
- 站点与 NAS 通道共用的 stale/skew 默认值是 15 秒、60 秒、120 秒。代理默认每 15 秒推一次，最小值是 5 秒。间隔拉到几十秒以上时，面板可能把还在正常推送的机器标成陈旧

密钥可以用下面的命令生成。不要把结果写进 git：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## 装到哪里

| 路径 | 权限 | 作用 |
| --- | --- | --- |
| `/opt/personal-site/cloud-monitor.py` | `0644` root:root | 程序。服务用户能读，不能改 |
| `/etc/personal-site/cloud-monitor.env` | `0600` root:root | 密钥和设备身份。systemd 以 root 读取 |
| `/etc/systemd/system/cloud-monitor.service` | `0644` root:root | 单元 |
| 用户 `personal-monitor` | 系统用户，`nologin` | 进程身份 |

服务用户读不到 env 是预期行为。密钥不需要交给它。

## 用 install.sh

在仓库根目录执行。脚本可以重复跑：它会更新 Python 文件和 unit，但**不会覆盖已经存在的 env**，除非加上 `--force-env`。第一次安装会 `enable --now`。已经在跑则会重启以加载新脚本。已经装好但被停掉的服务会保持停止。

`--secret` 会出现在进程列表和 shell 历史里。优先用 `--secret-file`。

```bash
umask 077
python3 -c 'import secrets, pathlib; pathlib.Path("cloud-monitor.secret").write_text(secrets.token_urlsafe(32) + "\n")'

sudo ./install.sh --dry-run \
  --endpoint "https://monitor.example.com/api/monitor/remote/report" \
  --secret-file ./cloud-monitor.secret \
  --device-id cloud-example-a \
  --name "Example cloud VM A" \
  --region example-region-a \
  --ip 203.0.113.10

sudo ./install.sh \
  --endpoint "https://monitor.example.com/api/monitor/remote/report" \
  --secret-file ./cloud-monitor.secret \
  --device-id cloud-example-a \
  --name "Example cloud VM A" \
  --region example-region-a \
  --ip 203.0.113.10

rm -f ./cloud-monitor.secret
```

第二台机器换掉设备身份，地址仍用文档地址举例：

```bash
sudo ./install.sh \
  --endpoint "https://monitor.example.com/api/monitor/remote/report" \
  --secret-file ./cloud-monitor.secret \
  --device-id cloud-example-b \
  --name "Example cloud VM B" \
  --region example-region-b \
  --ip 203.0.113.20
```

两台机器可以共用站点的默认密钥，也可以各用各的槽位密钥。密钥不同就各写各的 `--secret-file`。

只更新程序、不动 env：

```bash
sudo ./install.sh
```

装好文件但先不启动：

```bash
sudo ./install.sh --no-enable --secret-file ./cloud-monitor.secret \
  --endpoint "https://monitor.example.com/api/monitor/remote/report" \
  --device-id cloud-example-a --ip 203.0.113.10 --region example-region-a
```

干跑会打印计划，并把密钥打成 `<redacted, N characters>`。

## 手动安装

不跑 `install.sh` 时，在仓库根目录执行：

```bash
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "need Python 3.11+")'

sudo install -d -m 0755 -o root -g root /opt/personal-site /etc/personal-site

if ! id personal-monitor >/dev/null 2>&1; then
  sudo useradd --system --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin --user-group personal-monitor
fi

sudo install -m 0644 -o root -g root cloud-monitor.py /opt/personal-site/cloud-monitor.py
sudo install -m 0644 -o root -g root deploy/cloud-monitor.service /etc/systemd/system/cloud-monitor.service
sudo install -m 0600 -o root -g root deploy/cloud-monitor.env.example /etc/personal-site/cloud-monitor.env
sudoedit /etc/personal-site/cloud-monitor.env
```

上面复制 example 的那一行会覆盖 `/etc/personal-site/cloud-monitor.env`。机器已经填过密钥时不要再执行它，用 `sudoedit` 改现有文件。

编辑 env 时至少改这几项：

- `CLOUD_MONITOR_ENDPOINT`：必须是 `https://`，通常以 `/api/monitor/remote/report` 结尾
- `CLOUD_MONITOR_SECRET`：换成上面生成的随机串。示例文件里的占位符长度够，但站点不会接受它
- `CLOUD_DEVICE_ID`、`CLOUD_DEVICE_NAME`、`CLOUD_DEVICE_REGION`、`CLOUD_DEVICE_IP`

`CLOUD_MONITOR_INTERVAL` 保持 `15`。`CLOUD_MONITOR_TIMEOUT` 保持 `10`。

确认权限，然后启用：

```bash
sudo stat -c '%a %U %G %n' /etc/personal-site/cloud-monitor.env /opt/personal-site/cloud-monitor.py
sudo systemctl daemon-reload
sudo systemctl enable --now cloud-monitor.service
```

`stat` 应看到 env 是 `600 root root`，脚本是 `644 root root`。

## 确认它在跑

成功时进程保持运行，日志里**没有**连续的 `cloud-monitor error:`。代理不会在每次成功上报时写日志。

```bash
systemctl is-enabled cloud-monitor.service
systemctl is-active cloud-monitor.service
systemctl status cloud-monitor.service --no-pager
journalctl -u cloud-monitor.service -n 50 --no-pager
id personal-monitor
```

`is-active` 应输出 `active`。内存应低于 unit 里的 `MemoryMax=64M`。

看本机会采到什么（不发网）。`--dry-run` 的 JSON 有缩进，中文也不转义，**不能**拿它去重算 HMAC：

```bash
sudo bash -c 'set -a; . /etc/personal-site/cloud-monitor.env; set +a; python3 /opt/personal-site/cloud-monitor.py --dry-run'
```

向站点发一笔然后退出。单元文件里不要加 `--once`，`Restart=always` 会马上再拉起一个：

```bash
sudo systemctl stop cloud-monitor.service
sudo bash -c 'set -a; . /etc/personal-site/cloud-monitor.env; set +a; python3 /opt/personal-site/cloud-monitor.py --once'
sudo systemctl start cloud-monitor.service
```

启动后的第一笔 `cpuPercent` 是 `null`，第二笔才有百分比。这不是故障。

## 和站点对一下

| 代理 env | 站点 |
| --- | --- |
| `CLOUD_MONITOR_ENDPOINT` | `https://<站点>/api/monitor/remote/report` |
| `CLOUD_MONITOR_SECRET` | `REMOTE_CLOUD_MONITOR_SECRET`，或该设备槽位上的 SECRET |
| `CLOUD_DEVICE_ID` | 槽位里登记的 device id，1 到 8 号 |
| `CLOUD_MONITOR_INTERVAL=15` | stale/skew 默认 15s / 60s / 120s（与 NAS 共用，本仓库不实现） |

签名字符串是 `deviceId.timestamp.nonce.sha256(body)`。`sha256(body)` 是原始 body 的小写十六进制 SHA-256。头是 `X-Monitor-Device-Id`、`X-Monitor-Timestamp`、`X-Monitor-Nonce`、`X-Monitor-Signature`（值以 `sha256=` 开头）。时间戳是 Unix 秒，不是毫秒。

站点也接受旧的 `X-NAS-*` 头。那是 NAS 通道用的。本代理不发送它们。NAS 的路径是 `/api/monitor/nas/report`，以后可以另写一个脚本，签名算法不用改。

跑 personal-site 的主机使用进程内采样：`MONITORING_MODE=live`，每 5 秒读一次本机 `/proc`，`HISTORY_LIMIT=60` 的内存环只保存 `memory.used` 和 `disk.used`。进程重启后环被清空。那不是本代理的存储，也不是时序数据库。

## 常见故障

### 时钟偏差

`X-Monitor-Timestamp` 取自这台 VM 的时钟。VM 和站点差得太多时，站点会拒绝这份上报，即使 HMAC 算法本身是对的。

```bash
timedatectl status
sudo timedatectl set-ntp true
```

需要的话安装并启用 `systemd-timesyncd`。对时之后：

```bash
sudo systemctl restart cloud-monitor.service
journalctl -u cloud-monitor.service -n 20 --no-pager
```

### 密钥不一致

日志里出现 HTTP 401 或 403 时，先核对密钥，再核对时钟。

- 长度至少 32。比较两边长度，不要把密钥贴到工单里
- 槽位如果配置了自己的 SECRET，代理不能再用全局 `REMOTE_CLOUD_MONITOR_SECRET`
- env 里不要有多余的引号、空格或换行。用 `sudoedit` 改，改完确认仍是 `600`
- 示例文件里的 `replace-with-at-least-32-character-secret` 只能让代理进程启动，站点会拒绝它

改完 env 后执行 `sudo systemctl restart cloud-monitor.service`。只改文件不会让已经启动的进程重新读 env。

### HTTPS

- endpoint 不是 `https://` 时，进程直接退出，journal 里每 10 秒重复一次 `CLOUD_MONITOR_ENDPOINT must be an https:// URL`（`Restart=always`，`RestartSec=10`）
- 证书不受信时，日志是 `certificate verify failed`。把 CA 装进系统信任库，或加一个 drop-in：

```bash
sudo systemctl edit cloud-monitor.service
```

```ini
[Service]
Environment=SSL_CERT_FILE=/etc/ssl/certs/custom-ca.pem
```

没有关闭证书校验的开关。

### 超时和 HTTP 502

`cloud-monitor error: ... timed out` 或 `HTTP 502` 表示这一轮没送到。进程不退出，下一个间隔会重试。采集已经成功时，这一轮的 CPU 计数会留下来，下一笔仍能算出 `cpuPercent`。

先看失败是不是连续的：

```bash
journalctl -u cloud-monitor.service -f
```

偶发一次 502 或超时，下一笔又安静了，就不用改代理。连续失败时看站点入口、安全组和出站 443，而不是把 `CLOUD_MONITOR_TIMEOUT` 改得很长。

### 配置写错导致重启循环

`deviceId` 不合法、密钥太短、间隔小于 5，进程都会马上退出。`systemctl status` 里能看到重启计数，journal 里是同一句 `ConfigError`。改 env，再 `sudo systemctl restart cloud-monitor.service`。

### 数据盘不完整

`CLOUD_MONITOR_PATHS` 里的路径不存在，或者 `personal-monitor` 无法进入该目录时，根分区仍然上报，`disk.partial` 为 true，路径出现在 `missingVolumes`。和 `/` 同一文件系统的路径会被丢掉，这是去重，不是故障。

### 不该安装的机器

不要在已经跑 personal-site 的站点主机上启用这个单元。那台机器的本机曲线来自站点进程自己的采样。

## 额外挂载点

根分区总是会报。额外路径用逗号分隔，不要加空格以外的引号问题；`install.sh` 会把值写成 systemd 能读的引号形式。

```bash
# 在 env 中：
CLOUD_MONITOR_PATHS="/data,/mnt/backup"
```

或：

```bash
sudo ./install.sh --force-env --paths /data,/mnt/backup ...
```

`--force-env` 会换掉整个 env，包括密钥。不换密钥时，直接 `sudoedit` 那一行然后重启即可。

## 升级

```bash
git pull
sudo ./install.sh
```

不带 `--force-env` 时，现有 `/etc/personal-site/cloud-monitor.env` 保持不变。服务如果正在运行，会被重启。

## 卸掉

```bash
sudo systemctl disable --now cloud-monitor.service
sudo rm -f /etc/systemd/system/cloud-monitor.service
sudo systemctl daemon-reload
sudo rm -f /opt/personal-site/cloud-monitor.py
sudo rm -f /etc/personal-site/cloud-monitor.env
sudo userdel personal-monitor
sudo groupdel personal-monitor
```

机器退役或 env 曾经被拷走时，在站点上轮换这把密钥。删文件代替不了轮换。

## NAS

本仓库不包含 NAS 采集脚本。NAS 上报走 `/api/monitor/nas/report`，签名字符串与这里相同，头可以用 `X-Monitor-*` 或旧的 `X-NAS-*`。云主机继续用本代理和 `/api/monitor/remote/report`。
