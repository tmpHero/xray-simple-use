"""
Test proxy connectivity, latency, and download speed through SOCKS5 proxy.
"""

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
        "--socks5", f"127.0.0.1:{socks_port}",
        "-s", "-o", "/dev/null",
        "-w", "%{time_total}",
        "--max-time", str(timeout),
        test_url,
    ]

    try:
        start = time.monotonic()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        elapsed = time.monotonic() - start

        if proc.returncode == 0:
            try:
                latency = float(proc.stdout.strip()) * 1000  # Convert seconds to ms
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
            "--socks5", f"127.0.0.1:{socks_port}",
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
        "--socks5", f"127.0.0.1:{socks_port}",
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
