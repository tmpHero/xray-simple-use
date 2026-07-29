"""
Run CloudflareSpeedTest to find the best Cloudflare IP for the proxy.
"""

import csv
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).parent.parent
_THIRD_PARTY = _PROJECT_ROOT / "third_party"
_CFST_DIR = _THIRD_PARTY / "cfst"
_RESULT_CSV = _PROJECT_ROOT / "cfst_result.csv"
_DEFAULT_THREADS = 4
_DEFAULT_COUNT = 200

CFST_DOWNLOAD_URL = (
    "https://github.com/XIU2/CloudflareSpeedTest/releases/latest/download/"
    "CloudflareST_linux_amd64.tar.gz"
)


@dataclass
class IPResult:
    """Single CloudflareSpeedTest result."""
    ip: str
    port: int = 443
    latency: float = 0.0
    download_speed: float = 0.0  # MB/s
    loss_rate: float = 0.0
    sent: int = 0
    received: int = 0

    @property
    def latency_ms(self) -> float:
        """Latency in milliseconds."""
        return self.latency


def _get_cfst_binary() -> Path:
    """Get path to CloudflareSpeedTest binary."""
    sysname = os.name
    if sysname == "nt":
        return _CFST_DIR / "CloudflareST.exe"
    return _CFST_DIR / "CloudflareST"


def download_cfst(url: str = CFST_DOWNLOAD_URL) -> Path:
    """
    Download and extract CloudflareSpeedTest binary.

    Args:
        url: Download URL for the release tar.gz.

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
    try:
        urllib.request.urlretrieve(url, tmp_path)
    except Exception as e:
        raise RuntimeError(f"Failed to download CloudflareSpeedTest: {e}") from e

    print("Extracting ...")
    import tarfile
    try:
        with tarfile.open(tmp_path, "r:gz") as tf:
            tf.extractall(_CFST_DIR)
    except Exception as e:
        raise RuntimeError(f"Failed to extract CloudflareSpeedTest: {e}") from e
    finally:
        tmp_path.unlink(missing_ok=True)

    cfst_bin = _get_cfst_binary()
    if not cfst_bin.exists():
        raise RuntimeError(
            f"CloudflareSpeedTest binary not found after extraction: {cfst_bin}"
        )

    cfst_bin.chmod(0o755)
    print(f"CloudflareSpeedTest installed to {_CFST_DIR}")
    return cfst_bin


def run_speedtest(
    count: int = _DEFAULT_COUNT,
    threads: int = _DEFAULT_THREADS,
    test_url: str = "",
    max_delay: int = 300,
) -> list[IPResult]:
    """
    Run CloudflareSpeedTest and return parsed results.

    Args:
        count: Number of IPs to test.
        threads: Number of concurrent threads.
        test_url: URL for download speed test (default: CFST built-in).
        max_delay: Max acceptable average delay in ms.

    Returns:
        List of IPResult sorted by latency (lowest first).

    Raises:
        RuntimeError: If binary not found or execution fails.
    """
    cfst_bin = _get_cfst_binary()
    if not cfst_bin.exists():
        raise RuntimeError(
            f"CloudflareSpeedTest binary not found. Run download first."
        )

    cmd = [
        str(cfst_bin),
        "-n", str(count),
        "-t", str(threads),
        "-tl", str(max_delay),
        "-o", str(_RESULT_CSV),
    ]
    if test_url:
        cmd.extend(["-url", test_url])

    print(f"Running CloudflareSpeedTest (count={count}, threads={threads}) ...")
    result = subprocess.run(
        cmd,
        cwd=str(_CFST_DIR),
        capture_output=True,
        text=True,
    )

    output = result.stdout
    print(output)

    if not _RESULT_CSV.exists():
        raise RuntimeError(
            f"CloudflareSpeedTest did not produce result.csv. "
            f"stderr: {result.stderr}"
        )

    results = _parse_result_csv(str(_RESULT_CSV))
    if not results:
        raise RuntimeError("CloudflareSpeedTest returned no results.")

    return results


def _parse_result_csv(csv_path: str) -> list[IPResult]:
    """
    Parse CloudflareSpeedTest result CSV into IPResult list.

    Args:
        csv_path: Path to the result CSV file.

    Returns:
        List of IPResult sorted by latency.
    """
    results: list[IPResult] = []

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or not row[0].strip():
                continue
            ip_val = row[0].strip()
            if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip_val):
                continue

            try:
                port = int(row[1]) if len(row) > 1 and row[1].strip() else 443
                sent = int(row[2]) if len(row) > 2 and row[2].strip() else 0
                received = int(row[3]) if len(row) > 3 and row[3].strip() else 0
                loss = float(row[4]) if len(row) > 4 and row[4].strip() else 0.0
                latency = float(row[5]) if len(row) > 5 and row[5].strip() else 0.0
                speed = float(row[6]) if len(row) > 6 and row[6].strip() else 0.0

                results.append(IPResult(
                    ip=ip_val,
                    port=port,
                    latency=latency,
                    download_speed=speed,
                    loss_rate=loss,
                    sent=sent,
                    received=received,
                ))
            except (ValueError, IndexError):
                continue

    results.sort(key=lambda r: r.latency)
    return results


def get_best_ip(results: list[IPResult]) -> Optional[str]:
    """
    Get the IP with lowest latency from results.

    Args:
        results: List of IPResult from run_speedtest.

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
        results: List of IPResult from run_speedtest.
        n: Number of top IPs to return.

    Returns:
        List of IP address strings.
    """
    return [r.ip for r in results[:n]]
