"""
Run CloudflareSpeedTest to find the best Cloudflare IP for the proxy.
"""

import csv
import os
import shutil
import subprocess
import tarfile
import time
import urllib.request
from dataclasses import dataclass
from ipaddress import ip_address, IPv4Address, IPv6Address
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).parent.parent
_THIRD_PARTY = _PROJECT_ROOT / "third_party"
_CFST_DIR = _THIRD_PARTY / "cfst"
_RESULT_CSV = _PROJECT_ROOT / "cfst_result.csv"
_DEFAULT_CONCURRENCY = 4
_DEFAULT_ATTEMPTS = 4

CFST_DOWNLOAD_URL = (
    "https://github.com/XIU2/CloudflareSpeedTest/releases/latest/download/"
    "cfst_linux_amd64.tar.gz"
)


@dataclass
class IPResult:
    """Single CloudflareSpeedTest result."""
    ip: str
    latency: float = 0.0       # ms
    download_speed: float = 0.0  # MB/s
    loss_rate: float = 0.0
    sent: int = 0
    received: int = 0

    @property
    def latency_ms(self) -> float:
        """Latency in milliseconds."""
        return self.latency


def _get_cfst_binary() -> Path | None:
    """Find the CloudflareSpeedTest binary (may be in a subdirectory)."""
    names = ["cfst", "CloudflareST"]
    if os.name == "nt":
        names = ["cfst.exe", "CloudflareST.exe"]
    for root, _dirs, files in os.walk(_CFST_DIR):
        for f in files:
            if f in names:
                return Path(root) / f
    return None


_IP_FILE: Path | None = None  # Resolved lazily


def _find_ip_file() -> Path | None:
    """Find ip.txt bundled with CFST (may be in a subdirectory)."""
    for root, _dirs, files in os.walk(_CFST_DIR):
        for f in files:
            if f == "ip.txt":
                return Path(root) / f
    return None


def download_cfst(url: str = CFST_DOWNLOAD_URL) -> Path:
    """
    Download and extract CloudflareSpeedTest binary.

    Args:
        url: Download URL for the release tarball.

    Returns:
        Path to the extracted binary.

    Raises:
        RuntimeError: If download or extraction fails.
    """
    _THIRD_PARTY.mkdir(parents=True, exist_ok=True)

    if _CFST_DIR.exists():
        shutil.rmtree(_CFST_DIR)
    _CFST_DIR.mkdir(parents=True)

    print(f"Downloading CloudflareSpeedTest from {url} ...")
    tmp_path = _CFST_DIR / "cfst.tar.gz"
    if shutil.which("curl"):
        for attempt in range(1, 4):
            result = subprocess.run(
                ["curl", "-L", "-f", "-#", "--retry", "3", "--retry-delay", "2",
                 "-o", str(tmp_path), url],
                capture_output=False,
            )
            if result.returncode == 0:
                break
            if attempt < 3:
                print(f"Download failed (curl code {result.returncode}), retrying ({attempt + 1}/3) ...")
                time.sleep(2)
        else:
            raise RuntimeError(f"curl failed after 3 attempts (code {result.returncode})")
    else:
        urllib.request.urlretrieve(url, tmp_path)

    print("Extracting ...")
    try:
        with tarfile.open(tmp_path, "r:gz") as tf:
            tf.extractall(_CFST_DIR)
    except Exception as e:
        raise RuntimeError(f"Failed to extract CloudflareSpeedTest: {e}") from e
    finally:
        tmp_path.unlink(missing_ok=True)

    cfst_bin = _get_cfst_binary()
    if cfst_bin is None:
        # List what was extracted for debugging
        extracted = list(_CFST_DIR.iterdir()) if _CFST_DIR.exists() else []
        raise RuntimeError(
            f"cfst binary not found after extraction. "
            f"Extracted contents: {[p.name for p in extracted]}"
        )

    cfst_bin.chmod(0o755)
    print(f"CloudflareSpeedTest installed to {_CFST_DIR}")
    return cfst_bin


def run_speedtest(
    concurrency: int = _DEFAULT_CONCURRENCY,
    attempts: int = _DEFAULT_ATTEMPTS,
    max_delay: int = 300,
    test_port: int = 443,
    test_url: str = "",
    skip_download: bool = False,
) -> list[IPResult]:
    """
    Run CloudflareSpeedTest and return parsed results.

    CFST flags:
        -n  concurrency: number of concurrent latency test threads
        -t  attempts: number of latency tests per IP
        -tl max_delay: upper limit for acceptable average delay (ms)
        -tp test_port: port to test (default CFST: 443)
        -url test_url: URL for download speed test
        -dd skip download speed test (faster, for coarse filtering)
        -o  output CSV path

    Args:
        concurrency: Concurrent threads (-n).
        attempts: Tests per IP (-t).
        max_delay: Max acceptable delay in ms (-tl).
        test_port: Port to test (-tp).
        test_url: URL for download speed test (-url).
        skip_download: Skip download speed test (-dd) for faster coarse scan.

    Returns:
        List of IPResult filtered and sorted by latency.

    Raises:
        RuntimeError: If binary not found or execution fails.
    """
    cfst_bin = _get_cfst_binary()
    if cfst_bin is None:
        raise RuntimeError(
            "CloudflareSpeedTest binary not found. Run setup or download first."
        )

    # Remove stale result CSV so we don't reuse old data
    _RESULT_CSV.unlink(missing_ok=True)

    cmd = [
        str(cfst_bin),
        "-n", str(concurrency),
        "-t", str(attempts),
        "-tl", str(max_delay),
        "-tp", str(test_port),
        "-o", str(_RESULT_CSV),
    ]

    # Use bundled ip.txt if available
    ip_file = _find_ip_file()
    if ip_file is not None:
        cmd.extend(["-f", str(ip_file)])
    if skip_download:
        cmd.append("-dd")
    if test_url:
        cmd.extend(["-url", test_url])

    print(
        f"Running CloudflareSpeedTest "
        f"(concurrency={concurrency}, attempts={attempts}, port={test_port}"
        f"{', skip_download' if skip_download else ''}) ..."
    )

    start_time = time.monotonic()
    result = subprocess.run(
        cmd,
        cwd=str(_CFST_DIR),
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"CloudflareSpeedTest exited with code {result.returncode}."
        )

    if not _RESULT_CSV.exists():
        raise RuntimeError(
            f"CloudflareSpeedTest did not produce result.csv. "
            f"stderr: {result.stderr}"
        )

    results = _parse_result_csv(str(_RESULT_CSV))
    if not results:
        raise RuntimeError("CloudflareSpeedTest returned no valid results.")

    return results


def _parse_result_csv(csv_path: str) -> list[IPResult]:
    """
    Parse CloudflareSpeedTest result CSV into IPResult list.

    CSV format (current CFST version, no port column):
        IP 地址,已发送,已接收,丢包率,平均延迟,下载速度 (MB/s),地区

    Uses DictReader when header is present; falls back to positional parsing.

    Args:
        csv_path: Path to the result CSV file.

    Returns:
        List of IPResult sorted by latency.
    """
    results: list[IPResult] = []

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        return results

    # Detect header row (first cell contains non-IP text like "IP")
    header_row = rows[0]
    data_start = 0
    if header_row:
        first_cell = header_row[0].strip()
        try:
            ip_address(first_cell)
            # Looks like an IP, no header
            data_start = 0
        except ValueError:
            # Has header row, skip it
            data_start = 1

    for row in rows[data_start:]:
        if not row or not row[0].strip():
            continue

        ip_val = row[0].strip()

        # Validate IP address (IPv4 or IPv6)
        try:
            ip_address(ip_val)
        except ValueError:
            continue

        try:
            # Current CFST CSV: IP, sent, received, loss%, latency, speed, region
            sent = int(row[1]) if len(row) > 1 and row[1].strip() else 0
            received = int(row[2]) if len(row) > 2 and row[2].strip() else 0
            loss = float(row[3]) if len(row) > 3 and row[3].strip() else 0.0
            latency = float(row[4]) if len(row) > 4 and row[4].strip() else 0.0
            speed = float(row[5]) if len(row) > 5 and row[5].strip() else 0.0
        except (ValueError, IndexError):
            continue

        results.append(IPResult(
            ip=ip_val,
            latency=latency,
            download_speed=speed,
            loss_rate=loss,
            sent=sent,
            received=received,
        ))

    results.sort(key=lambda r: r.latency)
    return results


def filter_valid_ips(results: list[IPResult]) -> list[IPResult]:
    """DEPRECATED: use filter_cfst_results instead."""
    return filter_cfst_results(results, skip_download=True, max_loss_rate=0.0)


def filter_cfst_results(
    results: list[IPResult],
    *,
    skip_download: bool = True,
    max_loss_rate: float = 0.0,
    max_latency_ms: float = 0.0,
) -> list[IPResult]:
    """
    Filter CFST results by received, loss rate, latency, and optionally download speed.

    When skip_download is True, download_speed is not checked (CFST returns 0 with -dd).
    When False, download_speed must be > 0.

    Args:
        results: Raw IPResult list from CFST.
        skip_download: Whether CFST was run with -dd (skip download speed test).
        max_loss_rate: Maximum acceptable loss rate (0.0 = no loss).
        max_latency_ms: Maximum acceptable latency (0 = no limit).

    Returns:
        Filtered list sorted by latency.
    """
    valid: list[IPResult] = []

    for r in results:
        if r.received <= 0:
            continue
        if r.loss_rate > max_loss_rate:
            continue
        if r.latency <= 0:
            continue
        if max_latency_ms > 0 and r.latency > max_latency_ms:
            continue
        if not skip_download and r.download_speed <= 0:
            continue
        valid.append(r)

    if not valid:
        # Fallback: at least require received packets and zero loss
        valid = [r for r in results if r.received > 0 and r.loss_rate == 0.0]

    valid.sort(key=lambda r: r.latency)
    return valid


def get_best_ip(results: list[IPResult]) -> Optional[str]:
    """
    Get the best IP from filtered results (lowest latency first).

    Args:
        results: List of IPResult (should be filtered via filter_valid_ips).

    Returns:
        Best IP address string, or None if empty.
    """
    if not results:
        return None
    return results[0].ip


def get_top_ips(results: list[IPResult], n: int = 5) -> list[str]:
    """
    Get top N IPs sorted by latency.

    Args:
        results: List of IPResult.
        n: Number of top IPs to return.

    Returns:
        List of IP address strings.
    """
    return [r.ip for r in results[:n]]


def check_proxy_interference(results: list[IPResult]) -> tuple[bool, str]:
    """
    Check if CFST results look like traffic went through a proxy.

    Signs of proxy interference:
        - All latencies are suspiciously low (< 1ms)
        - All latencies are too close together (std dev < 0.5ms)
        - No valid results (empty list)

    Args:
        results: List of IPResult from CFST.

    Returns:
        Tuple of (is_clean: bool, message: str).
    """
    if not results:
        return False, "No valid results — CSV may be empty or corrupted."

    latencies = [r.latency for r in results if r.latency > 0]
    if not latencies:
        return False, "All latencies are zero — CFST traffic may go through proxy."

    # All results under 1ms is physically impossible without proxy
    if all(l < 1.0 for l in latencies):
        return False, (
            "All latencies < 1ms — CFST traffic likely went through proxy. "
            "Ensure CFST runs directly, not through TUN/transparent proxy."
        )

    # Very low variance with many samples suggests all hitting same proxy
    if len(latencies) >= 3:
        mean = sum(latencies) / len(latencies)
        variance = sum((l - mean) ** 2 for l in latencies) / len(latencies)
        stddev = variance ** 0.5
        if stddev < 0.5 and mean < 5.0:
            return False, (
                f"Abnormally tight latency distribution (mean={mean:.1f}ms, "
                f"stddev={stddev:.2f}ms) — possible proxy interference."
            )

    return True, "OK"

