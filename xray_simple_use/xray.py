"""
Download and manage Xray-core binary process lifecycle.
"""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
_THIRD_PARTY = _PROJECT_ROOT / "third_party"
_XRAY_DIR = _THIRD_PARTY / "xray-core"
_PID_FILE = _PROJECT_ROOT / "xray.pid"
_CONFIG_FILE = _PROJECT_ROOT / "config.json"

XRAY_DOWNLOAD_URL = "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"


def _get_xray_binary() -> Path:
    """Get path to xray binary based on platform."""
    sysname = platform.system().lower()
    if sysname == "windows":
        return _XRAY_DIR / "xray.exe"
    return _XRAY_DIR / "xray"


def download_xray(url: str = XRAY_DOWNLOAD_URL) -> Path:
    """
    Download and extract Xray-core binary.

    Args:
        url: Download URL for Xray-core zip.

    Returns:
        Path to the extracted xray binary.

    Raises:
        RuntimeError: If download or extraction fails.
    """
    _THIRD_PARTY.mkdir(parents=True, exist_ok=True)

    if _XRAY_DIR.exists():
        shutil.rmtree(_XRAY_DIR)
    _XRAY_DIR.mkdir(parents=True)

    print(f"Downloading Xray-core from {url} ...")
    tmp_path = _XRAY_DIR / "xray.zip"
    try:
        urllib.request.urlretrieve(url, tmp_path)
    except Exception as e:
        raise RuntimeError(f"Failed to download Xray-core: {e}") from e

    print("Extracting ...")
    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(_XRAY_DIR)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"Failed to extract Xray-core: {e}") from e
    finally:
        tmp_path.unlink(missing_ok=True)

    xray_bin = _get_xray_binary()
    if not xray_bin.exists():
        raise RuntimeError(f"Xray binary not found after extraction: {xray_bin}")

    xray_bin.chmod(0o755)
    print(f"Xray-core installed to {_XRAY_DIR}")
    return xray_bin


def start_xray(config: dict | None = None, config_path: str | None = None) -> subprocess.Popen:
    """
    Start xray-core as a background process.

    Args:
        config: Config dict. If None, loads from config_path.
        config_path: Path to config JSON file. Defaults to project config.json.

    Returns:
        The Popen process handle.

    Raises:
        RuntimeError: If xray binary not found or already running.
    """
    if is_running():
        raise RuntimeError("Xray is already running. Stop it first.")

    xray_bin = _get_xray_binary()
    if not xray_bin.exists():
        raise RuntimeError(
            f"Xray binary not found at {xray_bin}. Run download first."
        )

    if config_path is None:
        config_path = str(_CONFIG_FILE)

    if config is not None:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

    if not os.path.exists(config_path):
        raise RuntimeError(f"Config file not found: {config_path}")

    log_path = _PROJECT_ROOT / "xray.log"
    with open(log_path, "w") as log_f:
        proc = subprocess.Popen(
            [str(xray_bin), "run", "-c", config_path],
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )

    _PID_FILE.write_text(str(proc.pid))
    print(f"Xray started (pid={proc.pid}), config={config_path}")
    return proc


def stop_xray() -> None:
    """
    Stop the running xray-core process.

    Reads PID from PID file, sends SIGTERM, then SIGKILL if needed.
    """
    if not is_running():
        print("Xray is not running.")
        _PID_FILE.unlink(missing_ok=True)
        return

    pid = int(_PID_FILE.read_text().strip())
    try:
        os.kill(pid, 15)  # SIGTERM
        print(f"Xray (pid={pid}) stopped.")
    except ProcessLookupError:
        print(f"Xray process {pid} not found, cleaning up PID file.")
    except PermissionError:
        raise RuntimeError(f"No permission to stop xray process {pid}.")

    _PID_FILE.unlink(missing_ok=True)


def is_running() -> bool:
    """
    Check if xray-core is currently running.

    Returns:
        True if PID file exists and the process is alive.
    """
    if not _PID_FILE.exists():
        return False
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def status() -> dict:
    """
    Get running status of xray-core.

    Returns:
        Dict with keys running, pid, config.
    """
    if not is_running():
        return {"running": False, "pid": None, "config": str(_CONFIG_FILE)}
    pid = int(_PID_FILE.read_text().strip())
    return {"running": True, "pid": pid, "config": str(_CONFIG_FILE)}
