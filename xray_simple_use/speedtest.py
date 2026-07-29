"""
Test proxy connectivity, latency, and download speed through SOCKS5 proxy.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

_DEFAULT_SOCKS_PORT = 10808
_DEFAULT_TEST_URLS = [
    "https://www.google.com",
    "https://www.youtube.com",
    "https://github.com",
]
_SPEED_TEST_URL = "http://speedtest.tele2.net/100MB.zip"

_CURL_BIN = "curl"


@dataclass
class ProbeResult:
    """Unified proxy probe result."""
    ok: bool
    http_status: Optional[int] = None
    total_time_ms: Optional[float] = None
    error: Optional[str] = None


def probe_socks(
    socks_port: int,
    url: str,
    expected_status: int = 204,
    timeout: int = 5,
) -> ProbeResult:
    """
    Probe a URL through SOCKS5 proxy — unified for health check,
    candidate testing, and recovery verification.

    Args:
        socks_port: SOCKS5 proxy port.
        url: URL to request.
        expected_status: HTTP status code expected for success.
        timeout: Request timeout in seconds.

    Returns:
        ProbeResult with ok, http_status, total_time_ms, error.
    """
    cmd = [
        _CURL_BIN,
        "--socks5-hostname", f"127.0.0.1:{socks_port}",
        "--silent", "--show-error",
        "--max-time", str(timeout),
        "--output", os.devnull,
        "--write-out", "%{http_code},%{time_total}",
        url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)

        if proc.returncode != 0:
            return ProbeResult(
                ok=False,
                http_status=None,
                total_time_ms=None,
                error=proc.stderr.strip() or f"curl exit {proc.returncode}",
            )

        parts = proc.stdout.strip().split(",", 1)
        if len(parts) != 2:
            return ProbeResult(ok=False, error=f"unexpected curl output: {proc.stdout.strip()}")

        http_code = int(parts[0].strip())
        time_ms = float(parts[1].strip()) * 1000

        return ProbeResult(
            ok=http_code == expected_status,
            http_status=http_code,
            total_time_ms=time_ms,
            error=None if http_code == expected_status else f"HTTP {http_code}",
        )

    except subprocess.TimeoutExpired:
        return ProbeResult(ok=False, error="timeout")
    except (ValueError, TypeError) as e:
        return ProbeResult(ok=False, error=f"parse error: {e}")
    except Exception as e:
        return ProbeResult(ok=False, error=str(e))


@dataclass
class ProxyTestResult:
    """Result of a proxy speed test."""
    connected: bool
    latency_ms: float = 0.0
    download_speed_mbps: float = 0.0
    download_size_mb: float = 0.0
    download_duration_s: float = 0.0
    error: str = ""


def test_connectivity(
    socks_port: int = _DEFAULT_SOCKS_PORT,
    test_url: str = _DEFAULT_TEST_URLS[0],
    timeout: int = 10,
) -> ProxyTestResult:
    """
    Test proxy connectivity by requesting a URL via SOCKS5.

    Args:
        socks_port: Local SOCKS5 proxy port.
        test_url: URL to test against.
        timeout: Request timeout in seconds.

    Returns:
        ProxyTestResult with connectivity status and latency.
    """
    cmd = [
        _CURL_BIN,
        "--socks5-hostname", f"127.0.0.1:{socks_port}",
        "--fail",
        "-s", "-o", "/dev/null",
        "-w", "%{http_code}\n%{time_total}",
        "--max-time", str(timeout),
        test_url,
    ]

    try:
        start = time.monotonic()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        elapsed = time.monotonic() - start

        if proc.returncode == 0:
            lines = proc.stdout.strip().split("\n")
            if len(lines) >= 2:
                http_code = lines[0].strip()
                time_val = lines[1].strip()
                if http_code not in ("204", "200", "301", "302"):
                    return ProxyTestResult(
                        connected=False,
                        error=f"Unexpected HTTP status: {http_code}",
                    )
                try:
                    latency = float(time_val) * 1000
                except ValueError:
                    latency = elapsed * 1000
            else:
                try:
                    latency = float(proc.stdout.strip()) * 1000
                except ValueError:
                    latency = elapsed * 1000

            return ProxyTestResult(
                connected=True,
                latency_ms=round(latency, 1),
            )
        else:
            return ProxyTestResult(
                connected=False,
                error=f"curl exited with code {proc.returncode}: {proc.stderr.strip()}",
            )

    except subprocess.TimeoutExpired:
        return ProxyTestResult(connected=False, error="Connection timed out")
    except FileNotFoundError:
        return ProxyTestResult(connected=False, error="curl not found. Install curl to test connectivity.")
    except Exception as e:
        return ProxyTestResult(connected=False, error=str(e))


def test_latency(
    socks_port: int = _DEFAULT_SOCKS_PORT,
    urls: list[str] | None = None,
    timeout: int = 10,
) -> dict[str, Optional[float]]:
    """
    Measure latency to multiple target URLs through the proxy.

    Args:
        socks_port: Local SOCKS5 proxy port.
        urls: List of URLs to test. Defaults to common sites.
        timeout: Request timeout in seconds.

    Returns:
        Dict mapping URL to latency in ms (None if failed).
    """
    if urls is None:
        urls = _DEFAULT_TEST_URLS

    results: dict[str, Optional[float]] = {}

    for url in urls:
        cmd = [
            _CURL_BIN,
            "--socks5-hostname", f"127.0.0.1:{socks_port}",
            "-s", "-o", "/dev/null",
            "-w", "%{time_total}",
            "--max-time", str(timeout),
            url,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
            if proc.returncode == 0:
                latency = float(proc.stdout.strip()) * 1000
                results[url] = round(latency, 1)
            else:
                results[url] = None
        except Exception:
            results[url] = None

    return results


def test_download_speed(
    socks_port: int = _DEFAULT_SOCKS_PORT,
    test_url: str = _SPEED_TEST_URL,
    max_size_mb: float = 10.0,
    timeout: int = 30,
) -> ProxyTestResult:
    """
    Measure download speed through the proxy.

    Downloads a portion of a test file and measures speed.

    Args:
        socks_port: Local SOCKS5 proxy port.
        test_url: URL of a file to download for speed test.
        max_size_mb: Maximum bytes to download (avoids downloading huge files).
        timeout: Download timeout in seconds.

    Returns:
        ProxyTestResult with download speed in Mbps.
    """
    max_bytes = int(max_size_mb * 1024 * 1024)

    cmd = [
        _CURL_BIN,
        "--socks5-hostname", f"127.0.0.1:{socks_port}",
        "-s", "-o", "/dev/null",
        "-w", "%{time_total}\n%{size_download}",
        "--max-time", str(timeout),
        "--max-filesize", str(max_bytes),
        "-L",
        test_url,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)

        if proc.returncode not in (0, 63):
            # curl code 63 = max filesize exceeded (this is OK, we got enough data)
            return ProxyTestResult(
                connected=False,
                error=f"curl exited with code {proc.returncode}: {proc.stderr.strip()}",
            )

        lines = proc.stdout.strip().split("\n")
        if len(lines) < 2:
            return ProxyTestResult(connected=False, error="Failed to parse curl output")

        time_total = float(lines[0].strip())
        size_bytes = float(lines[1].strip())

        if time_total <= 0 or size_bytes <= 0:
            return ProxyTestResult(connected=False, error="Download returned no data")

        size_mb = size_bytes / (1024 * 1024)
        speed_mbps = (size_bytes * 8) / (time_total * 1000 * 1000)

        return ProxyTestResult(
            connected=True,
            download_speed_mbps=round(speed_mbps, 2),
            download_size_mb=round(size_mb, 2),
            download_duration_s=round(time_total, 2),
        )

    except subprocess.TimeoutExpired:
        return ProxyTestResult(connected=False, error="Download timed out")
    except FileNotFoundError:
        return ProxyTestResult(connected=False, error="curl not found")
    except Exception as e:
        return ProxyTestResult(connected=False, error=str(e))


def quick_verify(
    socks_port: int = _DEFAULT_SOCKS_PORT,
    attempts: int = 3,
    timeout: int = 10,
) -> tuple[bool, float, str]:
    """
    Verify proxy works: test connectivity multiple times, return median latency.

    Args:
        socks_port: Local SOCKS5 proxy port.
        attempts: Number of connectivity test attempts.
        timeout: Per-attempt timeout in seconds.

    Returns:
        Tuple of (success, median_latency_ms, error_message).
    """
    latencies: list[float] = []
    last_error = ""

    for _ in range(attempts):
        result = test_connectivity(socks_port=socks_port, timeout=timeout)
        if result.connected:
            latencies.append(result.latency_ms)
        else:
            last_error = result.error

    if not latencies:
        return False, 0.0, last_error

    latencies.sort()
    median = latencies[len(latencies) // 2]
    return True, median, ""


def check_curl_available() -> bool:
    """Check if curl is available on the system."""
    try:
        subprocess.run([_CURL_BIN, "--version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False
