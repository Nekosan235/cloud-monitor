"""Unit tests for the cloud-monitor push agent.

These tests mock /proc and the signer. They do not open a network connection.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
AGENT_PATH = ROOT / "cloud-monitor.py"

SPEC = importlib.util.spec_from_file_location("cloud_monitor", AGENT_PATH)
agent = importlib.util.module_from_spec(SPEC)
sys.modules["cloud_monitor"] = agent
SPEC.loader.exec_module(agent)

SECRET = "0123456789abcdef0123456789abcdef"
ENDPOINT = "https://monitor.example.com/api/monitor/remote/report"
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
DOCUMENTATION_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")


def make_config(**overrides) -> agent.AgentConfig:
    values = {
        "endpoint": ENDPOINT,
        "secret": SECRET,
        "device_id": "cloud-example-a",
        "name": "Example cloud VM A",
        "region": "example-region-a",
        "ip": "203.0.113.10",
        "interval": 15,
        "timeout": 10,
        "volumes": (),
        "once": False,
        "dry_run": False,
    }
    values.update(overrides)
    return agent.AgentConfig(**values)


class TestSignature(unittest.TestCase):
    def test_frozen_vector(self):
        body = b'{"deviceId":"cloud-example-a"}'
        signature = agent.sign(SECRET, "cloud-example-a", "1760000000", "fixed-nonce", body)
        self.assertEqual(
            signature,
            "sha256=f160f3a836a94e5f67f67147163bbb470464f4eb9b17563265f12b28a44a7f10",
        )

    def test_message_is_device_timestamp_nonce_and_body_hash(self):
        body = b'{"deviceId":"cloud-example-a"}'
        digest = hashlib.sha256(body).hexdigest()
        message = agent.signature_message("cloud-example-a", "1760000000", "fixed-nonce", body)
        self.assertEqual(
            message.decode("utf-8"),
            f"cloud-example-a.1760000000.fixed-nonce.{digest}",
        )
        expected = hmac.new(SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()
        self.assertEqual(
            agent.sign(SECRET, "cloud-example-a", "1760000000", "fixed-nonce", body),
            "sha256=" + expected,
        )
        self.assertNotIn(body.decode("utf-8"), message.decode("utf-8"))

    def test_canonical_body_sorts_keys_and_escapes_non_ascii(self):
        body = agent.canonical_body({"b": 1, "a": {"d": 1, "c": 2}, "name": "华东"})
        self.assertEqual(body, b'{"a":{"c":2,"d":1},"b":1,"name":"\\u534e\\u4e1c"}')

    def test_request_headers_and_signed_body(self):
        report = {"deviceId": "cloud-example-a", "sampledAt": "2026-01-02T03:04:05Z"}
        request = agent.build_request(
            make_config(),
            report,
            timestamp="1760000000",
            nonce="fixed-nonce",
        )
        headers = {name.lower(): value for name, value in request.header_items()}
        body = request.data
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["user-agent"], "personal-site-cloud-monitor/1")
        self.assertEqual(headers["x-monitor-device-id"], "cloud-example-a")
        self.assertEqual(headers["x-monitor-timestamp"], "1760000000")
        self.assertEqual(headers["x-monitor-nonce"], "fixed-nonce")
        self.assertEqual(
            headers["x-monitor-signature"],
            agent.sign(SECRET, "cloud-example-a", "1760000000", "fixed-nonce", body),
        )
        self.assertEqual(json.loads(body), report)
        self.assertNotIn(b" ", body)

    def test_http_error_includes_status_without_calling_the_network(self):
        def opener(request, timeout):
            self.assertEqual(timeout, 10)
            raise urllib.error.HTTPError(
                request.full_url,
                502,
                "Bad Gateway",
                hdrs=None,
                fp=io.BytesIO(b"bad gateway"),
            )

        with self.assertRaises(RuntimeError) as caught:
            agent.send_report(
                make_config(),
                {"ok": True},
                timestamp="1760000000",
                nonce="fixed-nonce",
                opener=opener,
            )
        self.assertIn("HTTP 502", str(caught.exception))
        self.assertIn("bad gateway", str(caught.exception))


class TestCpuAndMemory(unittest.TestCase):
    def test_cpu_percent_math(self):
        self.assertIsNone(agent.cpu_percent(None, {"idle": 1, "total": 2}))
        self.assertIsNone(agent.cpu_percent({"idle": 5, "total": 5}, {"idle": 5, "total": 5}))
        self.assertIsNone(agent.cpu_percent({"idle": 5, "total": 10}, {"idle": 4, "total": 20}))
        self.assertEqual(
            agent.cpu_percent({"idle": 0, "total": 0}, {"idle": 2, "total": 3}),
            33.3,
        )
        self.assertEqual(
            agent.cpu_percent({"idle": 0, "total": 0}, {"idle": 0, "total": 10}),
            100.0,
        )
        self.assertEqual(
            agent.cpu_percent({"idle": 0, "total": 10}, {"idle": 20, "total": 20}),
            0.0,
        )

    def test_read_cpu_uses_aggregate_line_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stat"
            path.write_text(
                "cpu0 1 0 1 1 1 0 0 0\n"
                "cpu  100 0 100 700 100 0 0 0 9 9\n",
                encoding="utf-8",
            )
            self.assertEqual(agent.read_cpu(str(path)), {"idle": 800, "total": 1000})

    def test_read_cpu_rejects_missing_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stat"
            path.write_text("cpu0 1 0 1 1 1 0 0 0\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                agent.read_cpu(str(path))

    def test_read_meminfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meminfo"
            path.write_text(
                "MemTotal:       1048576 kB\n"
                "MemFree:         100000 kB\n"
                "MemAvailable:    524288 kB\n",
                encoding="utf-8",
            )
            memory = agent.read_meminfo(str(path))
        self.assertEqual(memory["total"], 1.0)
        self.assertEqual(memory["available"], 0.5)
        self.assertEqual(memory["used"], 0.5)
        self.assertIsNone(memory["reserved"])
        self.assertEqual(memory["unit"], "GiB")

    def test_read_meminfo_rejects_available_above_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meminfo"
            path.write_text(
                "MemTotal:       100 kB\nMemAvailable:    200 kB\n",
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                agent.read_meminfo(str(path))

    def test_uptime_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "uptime"
            path.write_text("12345.67 9.0\n", encoding="utf-8")
            self.assertEqual(agent.uptime_seconds(str(path)), 12345)


class TestDisk(unittest.TestCase):
    def test_volume_metric_matches_statvfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            metric = agent.volume_metric(tmp)
            stats = os.statvfs(tmp)
            block = stats.f_frsize or stats.f_bsize
            self.assertEqual(metric["total"], agent.as_gib(stats.f_blocks * block))
            self.assertEqual(metric["path"], tmp)
            self.assertEqual(metric["unit"], "GiB")
            self.assertGreater(metric["total"], 0)

    def test_read_volumes_skips_root_and_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good"
            bad = Path(tmp) / "bad"
            good.mkdir()
            bad.mkdir()

            def fake(path):
                if path == str(bad):
                    raise OSError("denied")
                return {
                    "path": path,
                    "device": "dev-good",
                    "used": 1.0,
                    "total": 2.0,
                    "available": 0.5,
                    "reserved": 0.25,
                    "unit": "GiB",
                }

            with mock.patch.object(agent, "volume_metric", fake):
                stderr = io.StringIO()
                with mock.patch("sys.stderr", stderr):
                    volumes, missing = agent.read_volumes(
                        (str(good), "  ", "/", str(bad), str(good)),
                        set(),
                    )
            self.assertEqual([item["path"] for item in volumes], [str(good)])
            self.assertEqual(missing, [{"path": str(bad), "error": "OSError"}])
            self.assertIn("skip volume", stderr.getvalue())

    def test_missing_path_is_not_a_directory(self):
        volumes, missing = agent.read_volumes(("/this/path/does/not/exist-cloud-monitor",), set())
        self.assertEqual(volumes, [])
        self.assertEqual(
            missing,
            [{"path": "/this/path/does/not/exist-cloud-monitor", "error": "not_directory"}],
        )

    def test_read_disk_marks_partial_and_keeps_root(self):
        disk = agent.read_disk(("/this/path/does/not/exist-cloud-monitor",))
        self.assertTrue(disk["partial"])
        self.assertEqual(disk["error"]["code"], "partial_volumes")
        self.assertEqual(disk["unit"], "GiB")
        self.assertGreater(disk["total"], 0)
        self.assertEqual(disk["volumes"], [])

    def test_same_filesystem_extra_path_is_not_a_second_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp) / "extra"
            extra.mkdir()
            disk = agent.read_disk((str(extra), str(tmp)), root_path=tmp)
            self.assertEqual(disk["volumes"], [])
            self.assertFalse(disk["partial"])


class TestReportAndLoop(unittest.TestCase):
    def test_build_report_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meminfo").write_text(
                "MemTotal:       1048576 kB\nMemAvailable:    524288 kB\n",
                encoding="utf-8",
            )
            (root / "stat").write_text("cpu  10 0 10 50 10 0 0 0\n", encoding="utf-8")
            (root / "uptime").write_text("100.9 1\n", encoding="utf-8")
            missing = root / "no-such-mount"
            paths = agent.ProcPaths(
                meminfo=str(root / "meminfo"),
                stat=str(root / "stat"),
                uptime=str(root / "uptime"),
                root=str(root),
            )
            report, sample = agent.build_report(
                make_config(volumes=(str(root), str(missing))),
                {"idle": 0, "total": 10},
                paths=paths,
                sampled_at="2026-01-02T03:04:05Z",
                os_name="Linux test",
                vcpu=2,
                loadavg=[0.5, 0.4, 0.3],
            )
        self.assertEqual(sample["idle"], 60)
        self.assertEqual(sample["total"], 80)
        self.assertEqual(report["deviceId"], "cloud-example-a")
        self.assertEqual(report["sampledAt"], "2026-01-02T03:04:05Z")
        server = report["server"]
        self.assertEqual(server["id"], "cloud-example-a")
        self.assertEqual(server["name"], "Example cloud VM A")
        self.assertEqual(server["host"], "203.0.113.10")
        self.assertEqual(server["ip"], "203.0.113.10")
        self.assertEqual(server["region"], "example-region-a")
        self.assertEqual(server["os"], "Linux test")
        self.assertEqual(server["vcpu"], 2)
        self.assertEqual(server["uptimeSeconds"], 100)
        self.assertEqual(server["cpuPercent"], 14.3)
        self.assertEqual(server["loadAverage"], [0.5, 0.4, 0.3])
        self.assertEqual(report["metrics"]["memory"]["used"], 0.5)
        self.assertTrue(report["metrics"]["disk"]["partial"])
        self.assertEqual(report["metrics"]["disk"]["volumes"], [])
        self.assertEqual(report["metrics"]["disk"]["missingVolumes"][0]["error"], "not_directory")

    def test_first_sample_has_null_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meminfo").write_text(
                "MemTotal:       1048576 kB\nMemAvailable:    524288 kB\n",
                encoding="utf-8",
            )
            (root / "stat").write_text("cpu  1 0 1 1 1 0 0 0\n", encoding="utf-8")
            (root / "uptime").write_text("1 1\n", encoding="utf-8")
            paths = agent.ProcPaths(
                meminfo=str(root / "meminfo"),
                stat=str(root / "stat"),
                uptime=str(root / "uptime"),
                root=str(root),
            )
            report, _sample = agent.build_report(
                make_config(),
                None,
                paths=paths,
                sampled_at="2026-01-02T03:04:05Z",
                os_name="Linux test",
                vcpu=1,
                loadavg=None,
            )
        self.assertIsNone(report["server"]["cpuPercent"])
        self.assertIsNone(report["server"]["loadAverage"])

    def test_dry_run_does_not_send(self):
        captured = io.StringIO()

        def send(_config, _report):
            raise AssertionError("dry-run posted a report")

        code = agent.run(make_config(dry_run=True), send=send, stdout=captured)
        self.assertEqual(code, 0)
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["deviceId"], "cloud-example-a")
        self.assertIn("memory", payload["metrics"])
        self.assertIn("disk", payload["metrics"])
        self.assertIn("cpuPercent", payload["server"])

    def test_once_sends_a_single_report(self):
        sent = []

        def send(_config, report):
            sent.append(report)

        def fail_sleep(_seconds):
            raise AssertionError("once-mode slept")

        code = agent.run(make_config(once=True), send=send, sleep=fail_sleep)
        self.assertEqual(code, 0)
        self.assertEqual(len(sent), 1)

    def test_failed_post_keeps_cpu_sample(self):
        samples = iter(
            (
                {"idle": 10, "total": 20},
                {"idle": 30, "total": 50},
            )
        )
        seen = []

        def send(_config, report):
            seen.append(report["server"]["cpuPercent"])
            if len(seen) == 1:
                raise urllib.error.URLError("timed out")
            raise StopIteration

        with mock.patch.object(agent, "read_cpu", lambda path="/proc/stat": next(samples)):
            with mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(StopIteration):
                    agent.run(make_config(), send=send, sleep=lambda _seconds: None)
        self.assertEqual(seen, [None, 33.3])

    def test_once_post_failure_returns_one(self):
        def send(_config, _report):
            raise urllib.error.URLError("timed out")

        with mock.patch("sys.stderr", io.StringIO()):
            code = agent.run(make_config(once=True), send=send, sleep=lambda _seconds: None)
        self.assertEqual(code, 1)

    def test_seconds_until_next_has_a_one_second_floor(self):
        self.assertEqual(agent.seconds_until_next(0, 15, 0), 15)
        self.assertEqual(agent.seconds_until_next(0, 15, 10), 5)
        self.assertEqual(agent.seconds_until_next(0, 15, 15), 1)
        self.assertEqual(agent.seconds_until_next(0, 15, 40), 1)


class TestConfig(unittest.TestCase):
    def argv(self, **overrides) -> list[str]:
        values = {
            "--endpoint": ENDPOINT,
            "--secret": SECRET,
            "--device-id": "cloud-example-a",
        }
        values.update(overrides)
        argv = []
        for key, value in values.items():
            argv.extend([key, value])
        return argv

    def test_parse_defaults_name_to_device_id(self):
        config = agent.parse_args(self.argv())
        self.assertEqual(config.name, "cloud-example-a")
        self.assertEqual(config.interval, 15)
        self.assertEqual(config.timeout, 10)
        self.assertFalse(config.once)

    def test_rejects_bad_endpoint_secret_interval_and_device(self):
        cases = (
            {"--endpoint": "http://monitor.example.com/api/monitor/remote/report"},
            {"--endpoint": "https://"},
            {"--secret": "short"},
            {"--secret": "x" * 31},
            {"--device-id": "has space"},
            {"--device-id": "a" * 65},
            {"--device-id": "../etc"},
            {"--interval": "4"},
            {"--timeout": "0"},
            {"--interval": "fast"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(agent.ConfigError):
                    agent.parse_args(self.argv(**overrides))

    def test_accepts_boundary_values(self):
        config = agent.parse_args(
            self.argv(**{"--secret": "y" * 32, "--interval": "5", "--timeout": "1", "--device-id": "a"})
        )
        self.assertEqual(config.secret, "y" * 32)
        self.assertEqual(config.interval, 5)
        self.assertEqual(config.device_id, "a")

    def test_device_id_punctuation(self):
        config = agent.parse_args(self.argv(**{"--device-id": "A_b-c.d:e"}))
        self.assertEqual(config.device_id, "A_b-c.d:e")

    def test_volume_flag_replaces_env_paths(self):
        with mock.patch.dict(os.environ, {"CLOUD_MONITOR_PATHS": "/from-env"}, clear=False):
            from_env = agent.parse_args(self.argv())
            from_flag = agent.parse_args(self.argv() + ["--volume", "/data", "--volume", " /mnt/backup "])
        self.assertEqual(from_env.volumes, ("/from-env",))
        self.assertEqual(from_flag.volumes, ("/data", "/mnt/backup"))

    def test_env_interval_and_comma_paths(self):
        with mock.patch.dict(
            os.environ,
            {
                "CLOUD_MONITOR_ENDPOINT": ENDPOINT,
                "CLOUD_MONITOR_SECRET": SECRET,
                "CLOUD_DEVICE_ID": "cloud-example-a",
                "CLOUD_MONITOR_INTERVAL": "20",
                "CLOUD_MONITOR_PATHS": "/data, /mnt/backup,",
            },
            clear=False,
        ):
            config = agent.parse_args([])
        self.assertEqual(config.interval, 20)
        self.assertEqual(config.volumes, ("/data", "/mnt/backup"))

    def test_cli_interval_overrides_bad_env_default(self):
        with mock.patch.dict(os.environ, {"CLOUD_MONITOR_INTERVAL": "abc"}, clear=False):
            config = agent.parse_args(self.argv() + ["--interval", "9"])
        self.assertEqual(config.interval, 9)

    def test_main_turns_config_errors_into_system_exit(self):
        with self.assertRaises(SystemExit):
            agent.main(["--endpoint", "http://monitor.example.com/report", "--secret", SECRET, "--device-id", "vm"])

    def test_endpoint_path_warning(self):
        self.assertIsNone(agent.endpoint_path_warning(ENDPOINT))
        self.assertIsNone(agent.endpoint_path_warning(ENDPOINT + "/"))
        warning = agent.endpoint_path_warning("https://monitor.example.com/api/monitor/nas/report")
        self.assertIn("/api/monitor/remote/report", warning)

    def test_environment_file_quotes_special_characters(self):
        text = agent.format_environment(
            {
                "CLOUD_MONITOR_ENDPOINT": ENDPOINT,
                "CLOUD_MONITOR_SECRET": r'abc"def\ghi$jkl' + ("z" * 20),
                "CLOUD_MONITOR_INTERVAL": "15",
                "CLOUD_MONITOR_TIMEOUT": "10",
                "CLOUD_DEVICE_ID": "cloud-example-a",
                "CLOUD_DEVICE_NAME": "Example #1",
                "CLOUD_DEVICE_REGION": "example-region-a",
                "CLOUD_DEVICE_IP": "203.0.113.10",
            }
        )
        self.assertIn(r'CLOUD_MONITOR_SECRET="abc\"def\\ghi\$jklzzzzzzzzzzzzzzzzzzzz"', text)
        self.assertIn('CLOUD_DEVICE_NAME="Example #1"', text)
        self.assertNotIn("CLOUD_MONITOR_PATHS", text)
        with self.assertRaises(agent.ConfigError):
            agent.format_environment({"CLOUD_MONITOR_SECRET": "line\nbreak"})


class TestDeployFiles(unittest.TestCase):
    def test_unit_matches_install_paths(self):
        unit = (ROOT / "deploy" / "cloud-monitor.service").read_text(encoding="utf-8")
        self.assertIn("User=personal-monitor", unit)
        self.assertIn("Group=personal-monitor", unit)
        self.assertIn("EnvironmentFile=/etc/personal-site/cloud-monitor.env", unit)
        self.assertIn("ExecStart=/usr/bin/python3 /opt/personal-site/cloud-monitor.py", unit)
        self.assertIn("MemoryMax=64M", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("PYTHONUNBUFFERED=1", unit)
        exec_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
        self.assertEqual(
            exec_lines,
            ["ExecStart=/usr/bin/python3 /opt/personal-site/cloud-monitor.py"],
        )

    def test_example_env_uses_documentation_addresses(self):
        text = (ROOT / "deploy" / "cloud-monitor.env.example").read_text(encoding="utf-8")
        self.assertIn("https://monitor.example.com/api/monitor/remote/report", text)
        self.assertIn("203.0.113.10", text)
        self.assertIn("203.0.113.20", text)
        self.assertIn("replace-with-at-least-32-character-secret", text)
        self.assertGreaterEqual(len("replace-with-at-least-32-character-secret"), 32)
        self.assertIsNotNone(agent.DEVICE_ID_RE.fullmatch("cloud-example-a"))
        self.assert_only_documentation_addresses(text, "cloud-monitor.env.example")

    def test_docs_and_source_use_documentation_addresses_only(self):
        files = (
            ROOT / "README.md",
            ROOT / "DEPLOY.md",
            ROOT / "cloud-monitor.py",
            ROOT / "install.sh",
            ROOT / "deploy" / "cloud-monitor.service",
            ROOT / "deploy" / "cloud-monitor.env.example",
            ROOT / "LICENSE",
            Path(__file__),
        )
        for path in files:
            self.assert_only_documentation_addresses(path.read_text(encoding="utf-8"), path.name)

    def assert_only_documentation_addresses(self, text, label):
        for address in IPV4_RE.findall(text):
            self.assertTrue(
                address.startswith(DOCUMENTATION_PREFIXES),
                f"{label} contains non-documentation address {address}",
            )

    def test_install_script_syntax_and_dry_run_redacts_secret(self):
        syntax = subprocess.run(
            ["bash", "-n", str(ROOT / "install.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        secret = "s" * 40
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "install.sh"),
                "--dry-run",
                "--endpoint",
                ENDPOINT,
                "--secret",
                secret,
                "--device-id",
                "cloud-example-a",
                "--name",
                "Example cloud VM A",
                "--region",
                "example-region-a",
                "--ip",
                "203.0.113.10",
                "--paths",
                "/data",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertIn("secret: <redacted, 40 characters>", result.stdout)
        self.assertIn("203.0.113.10", result.stdout)
        self.assertIn("/opt/personal-site/cloud-monitor.py", result.stdout)

    def test_install_script_rejects_http_and_short_secret(self):
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "install.sh"),
                "--dry-run",
                "--endpoint",
                "http://monitor.example.com/api/monitor/remote/report",
                "--secret",
                "short-secret",
                "--device-id",
                "bad id",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("short-secret", result.stdout)


if __name__ == "__main__":
    unittest.main()
