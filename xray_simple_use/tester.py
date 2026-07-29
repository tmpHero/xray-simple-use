"""
Concurrent real-link testing of candidate IPs through Xray VLESS tunnel.
"""

import concurrent.futures
import json
import math
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from xray_simple_use.vless import VLESSConfig, generate_test_config
from xray_simple_use.xray import _get_xray_binary, _CONFIG_FILE
from xray_simple_use.speedtest import probe_socks, ProbeResult

_TEST_URL = "https://www.google.com"
_DEFAULT_ATTEMPTS = 3
_DEFAULT_TIMEOUT = 8
_TEST_BASE_PORT = 11001
_EXPECTED_STATUS = 204


@dataclass
class TestResult:
    """Per-IP real-link test result."""
    ip: str
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    latencies: list[float] = field(default_factory=list)  # ms
    median_latency: float = 0.0
    p95_latency: float = 0.0
    jitter: float = 0.0  # stddev of latencies
    tls_ok: bool = False

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count + self.timeout_count
        return self.success_count / total if total > 0 else 0.0


def _test_single_ip(
    socks_port: int,
    test_url: str = _TEST_URL,
    attempts: int = _DEFAULT_ATTEMPTS,
    timeout: int = _DEFAULT_TIMEOUT,
    expected_status: int = _EXPECTED_STATUS,
) -> tuple[int, int, int, list[float]]:
    """
    Test a single IP through its dedicated SOCKS5 port using unified probe_socks.

    Args:
        socks_port: SOCKS5 port for this candidate.
        test_url: URL to request.
        attempts: Number of test requests.
        timeout: Per-request timeout in seconds.
        expected_status: Expected HTTP status code.

    Returns:
        Tuple of (success_count, failure_count, timeout_count, latencies_ms).
    """
    success = 0
    failure = 0
    timeout_cnt = 0
    latencies: list[float] = []

    for _ in range(attempts):
        result = probe_socks(socks_port, test_url, expected_status, timeout)
        if result.ok:
            success += 1
            if result.total_time_ms is not None:
                latencies.append(result.total_time_ms)
        elif result.error == "timeout":
            timeout_cnt += 1
        else:
            failure += 1

    return success, failure, timeout_cnt, latencies


def _compute_stats(latencies: list[float]) -> tuple[float, float, float]:
    """
    Compute median, P95, and jitter (stddev) from latency samples.

    Uses nearest-rank method for P95.
    When samples < 10, P95 is less meaningful — returns max_latency instead.

    Args:
        latencies: List of latency values in ms.

    Returns:
        Tuple of (median, p95_or_max, jitter) in ms.
    """
    if not latencies:
        return 0.0, 0.0, 0.0

    sorted_lat = sorted(latencies)
    n = len(sorted_lat)

    # Median
    mid = n // 2
    if n % 2 == 0:
        median = (sorted_lat[mid - 1] + sorted_lat[mid]) / 2
    else:
        median = sorted_lat[mid]

    # P95 (nearest-rank): ceil(0.95 * n) - 1
    p95_idx = math.ceil(0.95 * n) - 1
    p95 = sorted_lat[p95_idx]

    # Jitter (stddev)
    if n >= 2:
        jitter = statistics.stdev(latencies)
    else:
        jitter = 0.0

    return median, p95, jitter


def test_candidates(
    cfg: VLESSConfig,
    ips: list[str],
    active_ip: str = "",
    test_url: str = _TEST_URL,
    attempts: int = _DEFAULT_ATTEMPTS,
    timeout: int = _DEFAULT_TIMEOUT,
    test_base_port: int = _TEST_BASE_PORT,
) -> list[TestResult]:
    """
    Test multiple candidate IPs concurrently through real Xray VLESS links.

    Steps:
        1. Generate a multi-outbound test config
        2. Start a separate xray instance with this config
        3. Test each candidate IP via its dedicated SOCKS port in parallel
        4. Stop the test xray instance
        5. Compute stats for each IP

    This does NOT affect the main xray instance (if running).

    Args:
        cfg: Base VLESS configuration (protocol params).
        ips: Candidate IP addresses to test.
        active_ip: Current active IP (for the "proxy" outbound).
        test_url: URL to request through each candidate.
        attempts: Number of test requests per IP.
        timeout: Per-request timeout in seconds.
        test_base_port: Starting SOCKS port for candidate inbounds.

    Returns:
        List of TestResult, one per IP (in input order).

    Raises:
        RuntimeError: If xray binary not found or all tests fail.
    """
    xray_bin = _get_xray_binary()
    if not xray_bin.exists():
        raise RuntimeError("Xray binary not found. Run setup first.")

    if not active_ip:
        active_ip = ips[0] if ips else cfg.address

    # Generate test config
    test_config = generate_test_config(
        cfg, ips, active_ip,
        test_base_port=test_base_port,
    )

    # Write to temp file
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(test_config, tmp, indent=2, ensure_ascii=False)
    tmp.close()

    # Start test xray instance
    print(f"[TESTER] Starting test xray with {len(ips)} candidate outbounds ...")
    proc = subprocess.Popen(
        [str(xray_bin), "run", "-c", tmp.name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Verify the test xray started successfully
    time.sleep(0.5)
    if proc.poll() is not None:
        Path(tmp.name).unlink(missing_ok=True)
        raise RuntimeError(
            f"Test xray exited immediately (code={proc.returncode}). "
            f"Check config or port conflicts."
        )

    # Give it a bit more time for TLS/REALITY handshake setup
    time.sleep(1.0)

    # Test all candidates concurrently
    results: list[TestResult] = []
    futures: dict[str, concurrent.futures.Future] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ips)) as executor:
        for i, ip in enumerate(ips):
            port = test_base_port + i
            future = executor.submit(
                _test_single_ip, port, test_url, attempts, timeout
            )
            futures[ip] = future

        for ip, future in futures.items():
            success, failure, timeout_cnt, latencies = future.result()
            median, p95, jitter = _compute_stats(latencies)
            result = TestResult(
                ip=ip,
                success_count=success,
                failure_count=failure,
                timeout_count=timeout_cnt,
                latencies=latencies,
                median_latency=median,
                p95_latency=p95,
                jitter=jitter,
                tls_ok=success > 0,
            )
            results.append(result)

    # Stop test xray
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Cleanup temp config
    Path(tmp.name).unlink(missing_ok=True)

    return results


def results_to_queue_data(results: list[TestResult]) -> list[dict]:
    """
    Convert TestResult list to queue-compatible dict list.

    Args:
        results: List of TestResult from test_candidates.

    Returns:
        List of dicts sorted by quality (success_rate desc, median_latency asc).
    """
    data = []
    for r in results:
        data.append({
            "ip": r.ip,
            "success_count": r.success_count,
            "failure_count": r.failure_count + r.timeout_count,
            "median_latency": r.median_latency,
            "p95_latency": r.p95_latency,
            "jitter": r.jitter,
        })
    # Sort by success_rate desc, then median_latency asc
    data.sort(key=lambda d: (
        -(d["success_count"] / max(d["success_count"] + d["failure_count"], 1)),
        d["median_latency"],
    ))
    return data
