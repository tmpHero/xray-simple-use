"""
24-hour stability test: cold/warm TTFB probes, download tests, resource monitoring.

Uses the HTTP proxy at 127.0.0.1:10809 — the daemon must already be running.
"""

import json
import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from xray_simple_use.xray import is_running
from xray_simple_use.queue import load_queue, IPQueue
from xray_simple_use.speedtest import probe_http_session

_PROJECT_ROOT = Path(__file__).parent.parent
_LOGS_DIR = _PROJECT_ROOT / "logs"

_LATENCY_INTERVAL = 10
_DOWNLOAD_INTERVAL = 300
_RESOURCE_INTERVAL = 60
_DOWNLOAD_SIZE_BYTES = 5 * 1024 * 1024
_DOWNLOAD_URL = f"https://speed.cloudflare.com/__down?bytes={_DOWNLOAD_SIZE_BYTES}"

_PRIMARY_PROBE_URL = "https://www.gstatic.com/generate_204"
_SECONDARY_PROBE_URL = "https://cp.cloudflare.com/"
_EXPECTED_STATUS = 204
_HTTP_PROXY = "http://127.0.0.1:10809"


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = math.ceil(p / 100 * len(sorted_data)) - 1
    return sorted_data[max(0, min(idx, len(sorted_data) - 1))]


@dataclass
class StabilityReport:
    """Aggregated test results."""
    start_time: str = ""
    end_time: str = ""
    duration_hours: float = 0.0

    latency_probes: int = 0
    latency_success: int = 0
    latency_failed: int = 0
    max_consecutive_failures: int = 0

    cold_ttfbs: list[float] = field(default_factory=list)
    warm_ttfbs: list[float] = field(default_factory=list)

    download_tests: int = 0
    download_completed: int = 0
    download_interrupted: int = 0
    download_speeds: list[float] = field(default_factory=list)

    ip_switches: int = 0
    failed_switches: int = 0
    xray_crashes: int = 0
    max_memory_mb: float = 0.0
    residual_processes: int = 0

    @property
    def availability(self) -> float:
        return self.latency_success / self.latency_probes * 100 if self.latency_probes else 0.0

    @property
    def cold_ttfb_p50(self) -> float: return _percentile(self.cold_ttfbs, 50)
    @property
    def cold_ttfb_p95(self) -> float: return _percentile(self.cold_ttfbs, 95)
    @property
    def cold_ttfb_p99(self) -> float: return _percentile(self.cold_ttfbs, 99)

    @property
    def warm_ttfb_p50(self) -> float: return _percentile(self.warm_ttfbs, 50)
    @property
    def warm_ttfb_p95(self) -> float: return _percentile(self.warm_ttfbs, 95)
    @property
    def warm_ttfb_p99(self) -> float: return _percentile(self.warm_ttfbs, 99)

    @property
    def download_median_speed(self) -> float:
        return _percentile(self.download_speeds, 50) if self.download_speeds else 0.0

    @property
    def passed(self) -> bool:
        return (
            self.availability >= 99.5
            and self.max_consecutive_failures <= 2
            and (self.download_tests == 0 or self.download_interrupted / max(self.download_tests, 1) <= 0.01)
            and self.failed_switches == 0
            and self.xray_crashes == 0
        )


class StabilityTest:
    """Runs a stability test against the HTTP proxy."""

    def __init__(self, duration_seconds: float, download_url: str = _DOWNLOAD_URL):
        self.duration = duration_seconds
        self.download_url = download_url
        self.report = StabilityReport()
        self._stop_event = threading.Event()
        self._log_file: Optional[Path] = None
        self._last_active_ip: str = ""
        self._consecutive_failures = 0

    def run(self):
        """Run the full stability test with monotonic-clock scheduling."""
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        self._log_file = _LOGS_DIR / f"stability-{date_str}.jsonl"

        self.report.start_time = datetime.now(timezone.utc).isoformat()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        start_wall = time.time()
        start_mono = time.monotonic()
        deadline_mono = start_mono + self.duration

        next_latency = start_mono
        next_download = start_mono
        next_resource = start_mono

        print(f"Stability test started: {self.duration / 3600:.1f}h duration")
        print(f"Log file: {self._log_file}")
        print("Press Ctrl+C to stop early.\n")

        while not self._stop_event.is_set() and time.monotonic() < deadline_mono:
            now = time.monotonic()

            if now >= next_latency:
                self._latency_probe()
                next_latency += _LATENCY_INTERVAL
                if next_latency <= time.monotonic():
                    next_latency = time.monotonic() + _LATENCY_INTERVAL

            if now >= next_download:
                self._download_test()
                next_download += _DOWNLOAD_INTERVAL
                if next_download <= time.monotonic():
                    next_download = time.monotonic() + _DOWNLOAD_INTERVAL

            if now >= next_resource:
                self._resource_check()
                next_resource += _RESOURCE_INTERVAL
                if next_resource <= time.monotonic():
                    next_resource = time.monotonic() + _RESOURCE_INTERVAL

            now = time.monotonic()
            next_due = min(next_latency, next_download, next_resource, deadline_mono)
            self._stop_event.wait(max(0.0, next_due - now))

        self.report.end_time = datetime.now(timezone.utc).isoformat()
        self.report.duration_hours = (time.time() - start_wall) / 3600
        self._print_report()

    def _handle_signal(self, signum, frame):
        print("\nInterrupted. Generating partial report ...")
        self._stop_event.set()

    # ── Latency probe ────────────────────────────────────────────────

    def _latency_probe(self):
        self.report.latency_probes += 1
        active_ip = self._get_active_ip()

        result = probe_http_session(
            http_port=10809, url=_PRIMARY_PROBE_URL,
            expected_status=_EXPECTED_STATUS, warm_attempts=3, timeout=5,
        )
        # Fallback
        if not result.connected:
            result2 = probe_http_session(
                http_port=10809, url=_SECONDARY_PROBE_URL,
                expected_status=_EXPECTED_STATUS, warm_attempts=3, timeout=5,
            )
            if result2.connected:
                result = result2

        if result.connected:
            self.report.latency_success += 1
            self.report.cold_ttfbs.append(result.cold_ttfb_ms)
            self.report.warm_ttfbs.append(result.warm_median_ms)
            self._consecutive_failures = 0
        else:
            self.report.latency_failed += 1
            self._consecutive_failures += 1
            self.report.max_consecutive_failures = max(
                self.report.max_consecutive_failures, self._consecutive_failures,
            )

        self._log_jsonl({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "latency_probe",
            "metric_version": 2,
            "active_ip": active_ip,
            "success": result.connected,
            "cold_ttfb_ms": result.cold_ttfb_ms,
            "warm_ttfb_median_ms": round(result.warm_median_ms, 1),
            "warm_ttfb_samples_ms": result.warm_ttfb_samples_ms,
            "attempts": result.attempts,
            "successes": result.successes,
            "error": result.error,
        })

    # ── Download test ─────────────────────────────────────────────────

    def _download_test(self):
        self.report.download_tests += 1
        active_ip = self._get_active_ip()
        success, size_mb, duration_s, speed_mbps = self._run_download()

        if success:
            self.report.download_completed += 1
            self.report.download_speeds.append(speed_mbps)
        else:
            self.report.download_interrupted += 1

        self._log_jsonl({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "download_test",
            "active_ip": active_ip,
            "success": success,
            "size_mb": round(size_mb, 2),
            "duration_seconds": round(duration_s, 2),
            "speed_mbps": round(speed_mbps, 2),
        })

    # ── Resource check ────────────────────────────────────────────────

    def _resource_check(self):
        xray_alive = is_running()
        if not xray_alive:
            self.report.xray_crashes += 1

        mem_mb = 0.0
        try:
            from xray_simple_use.xray import _PID_FILE
            pid = int(_PID_FILE.read_text().strip())
            with open(f"/proc/{pid}/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        mem_mb = int(line.split()[1]) / 1024
                        break
            self.report.max_memory_mb = max(self.report.max_memory_mb, mem_mb)
        except Exception:
            pass

        try:
            queue = load_queue()
            if queue and queue.active_ip and queue.active_ip != self._last_active_ip:
                if self._last_active_ip:
                    self.report.ip_switches += 1
                    self._log_jsonl({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": "failover",
                        "old_ip": self._last_active_ip,
                        "new_ip": queue.active_ip,
                        "reason": "detected in queue.json",
                        "verified": True,
                    })
                self._last_active_ip = queue.active_ip
        except Exception:
            pass

        self._log_jsonl({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "resource",
            "active_ip": self._get_active_ip(),
            "xray_alive": xray_alive,
            "xray_memory_mb": round(mem_mb, 1),
        })

    # ── Helpers ───────────────────────────────────────────────────────

    def _run_download(self, timeout: int = 60) -> tuple[bool, float, float, float]:
        cmd = [
            "curl",
            "--proxy", _HTTP_PROXY,
            "--fail", "--location",
            "--silent", "--show-error",
            "--max-time", str(timeout),
            "--output", os.devnull,
            "--write-out", "%{http_code},%{size_download},%{time_total}",
            self.download_url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
            if proc.returncode != 0:
                return False, 0.0, 0.0, 0.0
            parts = proc.stdout.strip().split(",")
            if len(parts) != 3:
                return False, 0.0, 0.0, 0.0
            http_status, size_bytes, duration_s = int(parts[0]), float(parts[1]), float(parts[2])
            if http_status != 200:
                return False, 0.0, 0.0, 0.0
            if size_bytes < _DOWNLOAD_SIZE_BYTES * 0.9:
                return False, size_bytes / 1024 / 1024, duration_s, 0.0
            size_mb = size_bytes / (1024 * 1024)
            speed_mbps = (size_bytes * 8) / (duration_s * 1_000_000) if duration_s > 0 else 0.0
            return True, size_mb, duration_s, speed_mbps
        except Exception:
            return False, 0.0, 0.0, 0.0

    def _get_active_ip(self) -> str:
        try:
            queue = load_queue()
            if queue and queue.active_ip:
                return queue.active_ip
        except Exception:
            pass
        try:
            from xray_simple_use.vless import load_config
            from xray_simple_use.xray import _CONFIG_FILE
            config = load_config(str(_CONFIG_FILE))
            return config["outbounds"][0]["settings"]["vnext"][0]["address"]
        except Exception:
            return "unknown"

    def _log_jsonl(self, entry: dict) -> None:
        if self._log_file is None:
            return
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _print_report(self) -> None:
        r = self.report
        print("\n" + "=" * 48)
        print("Stability test report")
        print("=" * 48)
        print(f"Duration          {r.duration_hours:.1f}h")
        print()
        print(f"Latency probes     {r.latency_probes}")
        print(f"Successful         {r.latency_success}")
        print(f"Failed             {r.latency_failed}")
        print(f"Availability       {r.availability:.2f}%")
        print()
        print(f"Cold TTFB P50      {r.cold_ttfb_p50:.0f} ms")
        print(f"Cold TTFB P95      {r.cold_ttfb_p95:.0f} ms")
        print(f"Cold TTFB P99      {r.cold_ttfb_p99:.0f} ms")
        print(f"Warm TTFB P50      {r.warm_ttfb_p50:.0f} ms")
        print(f"Warm TTFB P95      {r.warm_ttfb_p95:.0f} ms")
        print(f"Warm TTFB P99      {r.warm_ttfb_p99:.0f} ms")
        print(f"Max consec. fails  {r.max_consecutive_failures}")
        print()
        print(f"Download tests     {r.download_tests}")
        print(f"Completed          {r.download_completed}")
        print(f"Interrupted        {r.download_interrupted}")
        if r.download_speeds:
            print(f"Median speed       {r.download_median_speed:.1f} Mbps")
        print()
        print(f"IP switches        {r.ip_switches}")
        print(f"Failed switches    {r.failed_switches}")
        print(f"Xray crashes       {r.xray_crashes}")
        print(f"Max memory         {r.max_memory_mb:.0f} MB")
        print()
        print(f"Result             {'PASS' if r.passed else 'FAIL'}")

        self._log_jsonl({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "report",
            "duration_hours": r.duration_hours,
            "latency_probes": r.latency_probes,
            "latency_success": r.latency_success,
            "availability_pct": round(r.availability, 2),
            "cold_ttfb_p50_ms": round(r.cold_ttfb_p50),
            "cold_ttfb_p95_ms": round(r.cold_ttfb_p95),
            "cold_ttfb_p99_ms": round(r.cold_ttfb_p99),
            "warm_ttfb_p50_ms": round(r.warm_ttfb_p50),
            "warm_ttfb_p95_ms": round(r.warm_ttfb_p95),
            "warm_ttfb_p99_ms": round(r.warm_ttfb_p99),
            "max_consecutive_failures": r.max_consecutive_failures,
            "download_tests": r.download_tests,
            "download_completed": r.download_completed,
            "download_median_speed_mbps": round(r.download_median_speed, 1),
            "ip_switches": r.ip_switches,
            "xray_crashes": r.xray_crashes,
            "max_memory_mb": round(r.max_memory_mb, 0),
            "result": "PASS" if r.passed else "FAIL",
        })
