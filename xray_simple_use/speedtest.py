"""
Test proxy latency and download speed through HTTP proxy.
"""

import os
import statistics
import subprocess
from dataclasses import dataclass
from typing import Optional

_DEFAULT_HTTP_PORT = 10809
_PROBE_URL = "https://www.gstatic.com/generate_204"
_DOWNLOAD_SIZE_BYTES = 10 * 1024 * 1024
_DOWNLOAD_URL = f"https://speed.cloudflare.com/__down?bytes={_DOWNLOAD_SIZE_BYTES}"

_CURL_BIN = "curl"


@dataclass
class LatencyResult:
    connected: bool
    median_ms: float = 0.0
    minimum_ms: float = 0.0
    maximum_ms: float = 0.0
    attempts: int = 0
    successes: int = 0
    error: str = ""


@dataclass
class DownloadResult:
    connected: bool
    speed_mbps: float = 0.0
    speed_mb_s: float = 0.0
    size_mb: float = 0.0
    duration_s: float = 0.0
    error: str = ""


# ── unified probe (used by daemon/tester) ──────────────────────────

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
    """Probe a URL through SOCKS5 proxy."""
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
            return ProbeResult(ok=False, error=proc.stderr.strip() or f"curl exit {proc.returncode}")
        parts = proc.stdout.strip().split(",", 1)
        if len(parts) != 2:
            return ProbeResult(ok=False, error="invalid output")
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
        return ProbeResult(ok=False, error=str(e))
    except Exception as e:
        return ProbeResult(ok=False, error=str(e))


# ── public speedtest API ───────────────────────────────────────────

def test_latency(
    http_port: int = _DEFAULT_HTTP_PORT,
    attempts: int = 5,
    timeout: int = 5,
) -> LatencyResult:
    """
    Measure first-byte latency via HTTP proxy, median of successful probes.

    Args:
        http_port: HTTP proxy port.
        attempts: Number of probe attempts.
        timeout: Per-attempt timeout in seconds.

    Returns:
        LatencyResult with median/min/max latencies.
    """
    latencies: list[float] = []
    last_error = ""

    for _ in range(attempts):
        cmd = [
            _CURL_BIN,
            "--proxy", f"http://127.0.0.1:{http_port}",
            "--fail",
            "--silent", "--show-error",
            "--max-time", str(timeout),
            "--output", os.devnull,
            "--write-out", "%{http_code},%{time_starttransfer}",
            _PROBE_URL,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 2)
            if proc.returncode != 0:
                last_error = proc.stderr.strip() or f"curl exit {proc.returncode}"
                continue
            status_text, latency_text = proc.stdout.strip().split(",", 1)
            if int(status_text) != 204:
                last_error = f"HTTP {status_text}"
                continue
            latencies.append(float(latency_text) * 1000)
        except subprocess.TimeoutExpired:
            last_error = "timeout"
        except (ValueError, TypeError) as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)

    if len(latencies) < (attempts // 2 + 1):
        return LatencyResult(
            connected=False,
            attempts=attempts, successes=len(latencies),
            error=f"insufficient successful probes: {len(latencies)}/{attempts}",
        )

    return LatencyResult(
        connected=True,
        median_ms=round(statistics.median(latencies), 1),
        minimum_ms=round(min(latencies), 1),
        maximum_ms=round(max(latencies), 1),
        attempts=attempts,
        successes=len(latencies),
    )


def test_download_speed(
    http_port: int = _DEFAULT_HTTP_PORT,
    test_url: str = _DOWNLOAD_URL,
    expected_bytes: int = _DOWNLOAD_SIZE_BYTES,
    timeout: int = 60,
) -> DownloadResult:
    """
    Download fixed-size data via HTTP proxy and measure speed.

    Args:
        http_port: HTTP proxy port.
        test_url: Download URL (must return exact byte count).
        expected_bytes: Expected download size in bytes.
        timeout: Download timeout in seconds.

    Returns:
        DownloadResult with speed and size.
    """
    cmd = [
        _CURL_BIN,
        "--proxy", f"http://127.0.0.1:{http_port}",
        "--fail",
        "--location",
        "--silent", "--show-error",
        "--max-time", str(timeout),
        "--output", os.devnull,
        "--write-out", "%{http_code},%{size_download},%{time_total}",
        test_url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        if proc.returncode != 0:
            return DownloadResult(connected=False, error=proc.stderr.strip() or f"curl exit {proc.returncode}")

        parts = proc.stdout.strip().split(",")
        if len(parts) != 3:
            return DownloadResult(connected=False, error="invalid output")

        http_status = int(parts[0])
        size_bytes = float(parts[1])
        duration_s = float(parts[2])

        if http_status != 200:
            return DownloadResult(connected=False, error=f"HTTP {http_status}")
        if size_bytes < expected_bytes * 0.9:
            return DownloadResult(connected=False, error=f"incomplete: {size_bytes / 1024 / 1024:.2f} MiB")
        if duration_s <= 0:
            return DownloadResult(connected=False, error="invalid duration")

        speed_mbps = size_bytes * 8 / duration_s / 1_000_000
        speed_mb_s = size_bytes / duration_s / 1024 / 1024

        return DownloadResult(
            connected=True,
            speed_mbps=round(speed_mbps, 2),
            speed_mb_s=round(speed_mb_s, 2),
            size_mb=round(size_bytes / 1024 / 1024, 2),
            duration_s=round(duration_s, 2),
        )
    except subprocess.TimeoutExpired:
        return DownloadResult(connected=False, error="timeout")
    except (ValueError, TypeError) as exc:
        return DownloadResult(connected=False, error=str(exc))
    except FileNotFoundError:
        return DownloadResult(connected=False, error="curl not found")
    except Exception as exc:
        return DownloadResult(connected=False, error=str(exc))


def test_connectivity(
    socks_port: int = 10808,
    test_url: str = _PROBE_URL,
    timeout: int = 5,
) -> ProbeResult:
    """DEPRECATED: use probe_socks or test_latency instead."""
    return probe_socks(socks_port, test_url, timeout=timeout)


def check_curl_available() -> bool:
    """Check if curl is available on the system."""
    try:
        subprocess.run([_CURL_BIN, "--version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False
