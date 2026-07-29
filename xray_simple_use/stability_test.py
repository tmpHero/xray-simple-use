"""
24-hour stability test: latency probes, download tests, resource monitoring.

Uses the HTTP proxy at 127.0.0.1:10809 — the daemon must already be running.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from xray_simple_use.xray import is_running
from xray_simple_use.queue import load_queue, IPQueue

_PROJECT_ROOT = Path(__file__).parent.parent
_LOGS_DIR = _PROJECT_ROOT / "logs"

_LATENCY_INTERVAL = 10     # seconds
_DOWNLOAD_INTERVAL = 300   # 5 minutes
_RESOURCE_INTERVAL = 60    # 1 minute
_DOWNLOAD_SIZE_MB = 5

_PRIMARY_PROBE_URL = "https://www.gstatic.com/generate_204"
_SECONDARY_PROBE_URL = "https://cp.cloudflare.com/"
_DOWNLOAD_URL = "http://speedtest.tele2.net/5MB.zip"
_EXPECTED_STATUS = 204
_HTTP_PROXY = "http://127.0.0.1:10809"


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
    latencies: list[float] = field(default_factory=list)

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
        if self.latency_probes == 0:
            return 0.0
        return self.latency_success / self.latency_probes * 100

    @property
    def latency_p50(self) -> float:
        return _percentile(self.latencies, 50)

    @property
    def latency_p95(self) -> float:
        return _percentile(self.latencies, 95)

    @property
    def latency_p99(self) -> float:
        return _percentile(self.latencies, 99)

    @property
    def latency_max(self) -> float:
        return max(self.latencies) if self.latencies else 0.0

    @property
    def download_median_speed(self) -> float:
        return _percentile(self.download_speeds, 50) if self.download_speeds else 0.0

    @property
    def download_min_speed(self) -> float:
        return min(self.download_speeds) if self.download_speeds else 0.0

    @property
    def passed(self) -> bool:
        return (
            self.availability >= 99.5
            and self.max_consecutive_failures <= 2
            and (self.download_tests == 0 or self.download_interrupted / max(self.download_tests, 1) <= 0.01)
            and self.failed_switches == 0
            and self.xray_crashes == 0
        )


def _percentile(data: list[float], p: float) -> float:
    """Compute percentile using nearest-rank method."""
    if not data:
        return 0.0
    import math
    sorted_data = sorted(data)
    idx = math.ceil(p / 100 * len(sorted_data)) - 1
    return sorted_data[max(0, min(idx, len(sorted_data) - 1))]


class StabilityTest:
    """Runs a 24-hour stability test against the HTTP proxy."""

    def __init__(self, duration_seconds: float, download_url: str = _DOWNLOAD_URL):
        self.duration = duration_seconds
        self.download_url = download_url
        self.report = StabilityReport()
        self._stop = False
        self._log_file: Optional[Path] = None
        self._last_active_ip: str = ""
        self._consecutive_failures = 0
        self._last_queue_check = 0.0
        self._last_download = 0.0

    def run(self):
        """Run the full stability test."""
        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        self._log_file = _LOGS_DIR / f"stability-{date_str}.jsonl"

        self.report.start_time = datetime.now(timezone.utc).isoformat()
        deadline = time.time() + self.duration
        print(f"Stability test started: {self.duration / 3600:.1f}h duration")
        print(f"HTTP proxy: {_HTTP_PROXY}")
        print(f"Log file: {self._log_file}")
        print(f"Deadline: {datetime.fromtimestamp(deadline).strftime('%Y-%m-%d %H:%M:%S')}")
        print("Press Ctrl+C to stop early.\n")

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        cycle = 0
        while not self._stop and time.time() < deadline:
            cycle += 1
            now = time.time()

            # Latency probe every 10s
            if cycle % max(1, _LATENCY_INTERVAL // 10) == 0 or cycle == 1:
                self._latency_probe()

            # Download test every 5min
            if now - self._last_download >= _DOWNLOAD_INTERVAL or cycle == 1:
                self._download_test()
                self._last_download = now

            # Resource monitor every 1min
            if now - self._last_queue_check >= _RESOURCE_INTERVAL or cycle == 1:
                self._resource_check()
                self._last_queue_check = now

            time.sleep(1)

        self.report.end_time = datetime.now(timezone.utc).isoformat()
        self.report.duration_hours = (time.time() - (deadline - self.duration)) / 3600
        self._print_report()

    def _handle_signal(self, signum, frame):
        print("\nInterrupted. Generating partial report ...")
        self._stop = True

    # ── Probe helpers ────────────────────────────────────────────────

    def _latency_probe(self):
        """Single latency probe via HTTP proxy."""
        self.report.latency_probes += 1
        active_ip = self._get_active_ip()

        # Primary
        success, http_status, first_byte_ms, total_ms = self._http_probe(_PRIMARY_PROBE_URL)
        if not success:
            # Secondary fallback
            success, http_status, first_byte_ms, total_ms = self._http_probe(_SECONDARY_PROBE_URL)

        if success:
            self.report.latency_success += 1
            self.report.latencies.append(total_ms)
            self._consecutive_failures = 0
        else:
            self.report.latency_failed += 1
            self._consecutive_failures += 1
            self.report.max_consecutive_failures = max(
                self.report.max_consecutive_failures, self._consecutive_failures
            )

        self._log_jsonl({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "latency_probe",
            "active_ip": active_ip,
            "success": success,
            "http_status": http_status,
            "first_byte_ms": first_byte_ms,
            "total_ms": total_ms,
        })

    def _download_test(self):
        """5MB download test via HTTP proxy."""
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

    def _resource_check(self):
        """Check xray health and resource usage."""
        xray_alive = is_running()
        if not xray_alive:
            self.report.xray_crashes += 1

        mem_mb = 0.0
        cpu_pct = 0.0

        # Try to get xray memory from /proc
        try:
            from xray_simple_use.xray import _PID_FILE
            pid = int(_PID_FILE.read_text().strip())
            with open(f"/proc/{pid}/status", "r") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        mem_mb = int(line.split()[1]) / 1024  # kB to MB
                        break
            self.report.max_memory_mb = max(self.report.max_memory_mb, mem_mb)
        except Exception:
            pass

        # Check for queue changes (failover events)
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

    # ── Internal helpers ─────────────────────────────────────────────

    def _http_probe(self, url: str, timeout: int = 5) -> tuple[bool, int, float, float]:
        """Probe via HTTP proxy. Returns (success, http_code, first_byte_ms, total_ms)."""
        cmd = [
            "curl",
            "--proxy", _HTTP_PROXY,
            "--silent", "--show-error",
            "--max-time", str(timeout),
            "--output", os.devnull,
            "--write-out", "%{http_code},%{time_starttransfer},%{time_total}",
            url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 3)
            if proc.returncode != 0:
                return False, 0, 0.0, 0.0
            parts = proc.stdout.strip().split(",")
            if len(parts) != 3:
                return False, 0, 0.0, 0.0
            http_code = int(parts[0])
            first_byte = float(parts[1]) * 1000
            total = float(parts[2]) * 1000
            return http_code == _EXPECTED_STATUS, http_code, first_byte, total
        except Exception:
            return False, 0, 0.0, 0.0

    def _run_download(self, timeout: int = 60) -> tuple[bool, float, float, float]:
        """Download 5MB test file. Returns (success, size_mb, duration_s, speed_mbps)."""
        cmd = [
            "curl",
            "--proxy", _HTTP_PROXY,
            "--silent", "--show-error",
            "--max-time", str(timeout),
            "--max-filesize", str(_DOWNLOAD_SIZE_MB * 1024 * 1024),
            "--output", os.devnull,
            "--write-out", "%{size_download},%{time_total}",
            "-L",
            self.download_url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
            parts = proc.stdout.strip().split(",")
            if len(parts) != 2:
                return False, 0.0, 0.0, 0.0
            size_bytes = float(parts[0])
            duration_s = float(parts[1])
            size_mb = size_bytes / (1024 * 1024)
            if size_mb < 1.0:
                return False, size_mb, duration_s, 0.0
            speed_mbps = (size_bytes * 8) / (duration_s * 1_000_000) if duration_s > 0 else 0.0
            return True, size_mb, duration_s, speed_mbps
        except Exception:
            return False, 0.0, 0.0, 0.0

    def _get_active_ip(self) -> str:
        """Get current active IP from queue or config."""
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
        """Append a JSON line to the log file."""
        if self._log_file is None:
            return
        try:
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _print_report(self) -> None:
        """Print the final report."""
        r = self.report

        print("\n" + "=" * 48)
        print("24-hour stability report")
        print("=" * 48)
        print(f"Duration          {r.duration_hours:.1f}h")
        print()
        print(f"Latency probes     {r.latency_probes}")
        print(f"Successful         {r.latency_success}")
        print(f"Failed             {r.latency_failed}")
        print(f"Availability       {r.availability:.2f}%")
        print()
        print(f"Latency P50        {r.latency_p50:.0f} ms")
        print(f"Latency P95        {r.latency_p95:.0f} ms")
        print(f"Latency P99        {r.latency_p99:.0f} ms")
        print(f"Maximum            {r.latency_max:.0f} ms")
        print(f"Max consec. fails  {r.max_consecutive_failures}")
        print()
        print(f"Download tests     {r.download_tests}")
        print(f"Completed          {r.download_completed}")
        print(f"Interrupted        {r.download_interrupted}")
        if r.download_speeds:
            print(f"Median speed       {r.download_median_speed:.1f} Mbps")
            print(f"Minimum speed      {r.download_min_speed:.1f} Mbps")
        print()
        print(f"IP switches        {r.ip_switches}")
        print(f"Failed switches    {r.failed_switches}")
        print(f"Xray crashes       {r.xray_crashes}")
        print(f"Max memory         {r.max_memory_mb:.0f} MB")
        print()
        print(f"Result             {'PASS' if r.passed else 'FAIL'}")

        # Also write report to log
        self._log_jsonl({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "report",
            "duration_hours": r.duration_hours,
            "latency_probes": r.latency_probes,
            "latency_success": r.latency_success,
            "latency_failed": r.latency_failed,
            "availability_pct": round(r.availability, 2),
            "latency_p50_ms": round(r.latency_p50),
            "latency_p95_ms": round(r.latency_p95),
            "latency_p99_ms": round(r.latency_p99),
            "max_consecutive_failures": r.max_consecutive_failures,
            "download_tests": r.download_tests,
            "download_completed": r.download_completed,
            "download_interrupted": r.download_interrupted,
            "download_median_speed_mbps": round(r.download_median_speed, 1),
            "ip_switches": r.ip_switches,
            "failed_switches": r.failed_switches,
            "xray_crashes": r.xray_crashes,
            "max_memory_mb": round(r.max_memory_mb, 0),
            "result": "PASS" if r.passed else "FAIL",
        })
