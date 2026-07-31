"""
Test proxy latency and download speed through HTTP proxy.
"""

import os
import statistics
import subprocess
from dataclasses import dataclass, field
from typing import Optional

_DEFAULT_HTTP_PORT = 10809
_PROBE_URL = "https://www.gstatic.com/generate_204"
_DOWNLOAD_SIZE_BYTES = 10 * 1024 * 1024
_DOWNLOAD_URL = f"https://speed.cloudflare.com/__down?bytes={_DOWNLOAD_SIZE_BYTES}"

_CURL_BIN = "curl"


# ── results ────────────────────────────────────────────────────────

@dataclass
class ProbeResult:
    """Single-shot proxy probe (cold connect, used for health check)."""
    ok: bool
    http_status: Optional[int] = None
    total_time_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class SessionProbeResult:
    """Multi-request session probe: cold + warm TTFB."""
    connected: bool
    cold_ttfb_ms: float = 0.0
    warm_ttfb_samples_ms: list[float] = field(default_factory=list)
    attempts: int = 0
    successes: int = 0
    error: str = ""

    @property
    def warm_median_ms(self) -> float:
        if not self.warm_ttfb_samples_ms:
            return 0.0
        return statistics.median(self.warm_ttfb_samples_ms)

    @property
    def warm_minimum_ms(self) -> float:
        return min(self.warm_ttfb_samples_ms) if self.warm_ttfb_samples_ms else 0.0

    @property
    def warm_maximum_ms(self) -> float:
        return max(self.warm_ttfb_samples_ms) if self.warm_ttfb_samples_ms else 0.0


@dataclass
class LatencyResult:
    connected: bool
    cold_ttfb_ms: float = 0.0
    warm_median_ms: float = 0.0
    warm_minimum_ms: float = 0.0
    warm_maximum_ms: float = 0.0
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


# ── single-shot probe (health check) ───────────────────────────────

def probe_socks(
    socks_port: int,
    url: str,
    expected_status: int = 204,
    timeout: int = 5,
) -> ProbeResult:
    """Single cold-connect probe through SOCKS5 proxy."""
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


# ── session probe (cold + warm) ────────────────────────────────────

def _probe_session(
    proxy_args: list[str],
    url: str,
    expected_status: int = 204,
    warm_attempts: int = 3,
    timeout: int = 5,
) -> SessionProbeResult:
    """Single curl process: first request = cold, subsequent reuse connection."""
    transfer_count = 1 + warm_attempts

    cmd = [
        _CURL_BIN,
        *proxy_args,
        "--http1.1",
        "--silent", "--show-error",
        "--max-time", str(timeout),
        "--write-out",
        "__XSU__%{http_code},%{num_connects},%{time_starttransfer},%{time_total}\n",
    ]
    for _ in range(transfer_count):
        cmd.extend(["--output", os.devnull, url])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=transfer_count * timeout + 5,
        )
    except subprocess.TimeoutExpired:
        return SessionProbeResult(connected=False, attempts=transfer_count, error="timeout")
    except FileNotFoundError:
        return SessionProbeResult(connected=False, attempts=transfer_count, error="curl not found")
    except Exception as exc:
        return SessionProbeResult(connected=False, attempts=transfer_count, error=str(exc))

    records: list[tuple[int, int, float, float]] = []
    for line in proc.stdout.splitlines():
        if not line.startswith("__XSU__"):
            continue
        try:
            status_text, connects_text, ttfb_text, total_text = line.removeprefix("__XSU__").split(",", 3)
            records.append((
                int(status_text), int(connects_text),
                float(ttfb_text) * 1000, float(total_text) * 1000,
            ))
        except (ValueError, TypeError):
            continue

    if not records:
        return SessionProbeResult(
            connected=False, attempts=transfer_count,
            error=proc.stderr.strip() or "no valid curl results",
        )

    cold_status, _, cold_ttfb, _ = records[0]
    warm_samples = [
        ttfb for status, num_connects, ttfb, _ in records[1:]
        if status == expected_status and num_connects == 0
    ]
    successes = sum(1 for status, _, _, _ in records if status == expected_status)
    required_warm = warm_attempts // 2 + 1

    connected = cold_status == expected_status and len(warm_samples) >= required_warm
    error = ""
    if not connected:
        error = f"cold_status={cold_status}, warm_success={len(warm_samples)}/{warm_attempts}"

    return SessionProbeResult(
        connected=connected,
        cold_ttfb_ms=round(cold_ttfb, 1),
        warm_ttfb_samples_ms=[round(v, 1) for v in warm_samples],
        attempts=transfer_count,
        successes=successes,
        error=error,
    )


def probe_http_session(
    http_port: int,
    url: str = _PROBE_URL,
    expected_status: int = 204,
    warm_attempts: int = 3,
    timeout: int = 5,
) -> SessionProbeResult:
    return _probe_session(
        proxy_args=["--proxy", f"http://127.0.0.1:{http_port}"],
        url=url, expected_status=expected_status,
        warm_attempts=warm_attempts, timeout=timeout,
    )


def probe_socks_session(
    socks_port: int,
    url: str = _PROBE_URL,
    expected_status: int = 204,
    warm_attempts: int = 3,
    timeout: int = 5,
) -> SessionProbeResult:
    return _probe_session(
        proxy_args=["--socks5-hostname", f"127.0.0.1:{socks_port}"],
        url=url, expected_status=expected_status,
        warm_attempts=warm_attempts, timeout=timeout,
    )


# ── public API ─────────────────────────────────────────────────────

def test_latency(
    http_port: int = _DEFAULT_HTTP_PORT,
    warm_attempts: int = 3,
    timeout: int = 5,
) -> LatencyResult:
    """Measure cold TTFB + warm TTFB via HTTP proxy in one curl session."""
    session = probe_http_session(
        http_port=http_port, url=_PROBE_URL,
        expected_status=204, warm_attempts=warm_attempts, timeout=timeout,
    )
    if not session.connected:
        return LatencyResult(
            connected=False, attempts=session.attempts,
            successes=session.successes, error=session.error,
        )
    return LatencyResult(
        connected=True,
        cold_ttfb_ms=session.cold_ttfb_ms,
        warm_median_ms=round(session.warm_median_ms, 1),
        warm_minimum_ms=round(session.warm_minimum_ms, 1),
        warm_maximum_ms=round(session.warm_maximum_ms, 1),
        attempts=session.attempts,
        successes=session.successes,
    )


def test_download_speed(
    http_port: int = _DEFAULT_HTTP_PORT,
    test_url: str = _DOWNLOAD_URL,
    expected_bytes: int = _DOWNLOAD_SIZE_BYTES,
    timeout: int = 60,
) -> DownloadResult:
    """Download fixed-size data via HTTP proxy and measure speed."""
    cmd = [
        _CURL_BIN,
        "--proxy", f"http://127.0.0.1:{http_port}",
        "--fail", "--location",
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
        http_status, size_bytes, duration_s = int(parts[0]), float(parts[1]), float(parts[2])
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
            speed_mbps=round(speed_mbps, 2), speed_mb_s=round(speed_mb_s, 2),
            size_mb=round(size_bytes / 1024 / 1024, 2), duration_s=round(duration_s, 2),
        )
    except subprocess.TimeoutExpired:
        return DownloadResult(connected=False, error="timeout")
    except (ValueError, TypeError) as exc:
        return DownloadResult(connected=False, error=str(exc))
    except FileNotFoundError:
        return DownloadResult(connected=False, error="curl not found")
    except Exception as exc:
        return DownloadResult(connected=False, error=str(exc))


def check_curl_available() -> bool:
    try:
        subprocess.run([_CURL_BIN, "--version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False
