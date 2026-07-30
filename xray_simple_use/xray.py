"""
Download and manage Xray-core binary process lifecycle.
"""

import json
import os
import platform
import shutil
import signal
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
_THIRD_PARTY = _PROJECT_ROOT / "third_party"
_XRAY_DIR = _THIRD_PARTY / "xray-core"
_PID_FILE = _PROJECT_ROOT / "xray.pid"
_LOG_FILE = _PROJECT_ROOT / "xray.log"
_CONFIG_FILE = _PROJECT_ROOT / "config.json"

XRAY_DOWNLOAD_URL = "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip"

_STOP_TIMEOUT = 5  # seconds to wait after SIGTERM before SIGKILL


def _get_xray_binary() -> Path | None:
    """Find xray binary (may be in a subdirectory after extraction)."""
    suffix = ".exe" if platform.system().lower() == "windows" else ""
    name = f"xray{suffix}"
    for root, _dirs, files in os.walk(_XRAY_DIR):
        for f in files:
            if f == name:
                return Path(root) / f
    return None


def _fetch(url: str, dest: Path, retries: int = 3) -> None:
    """Download a file via curl with retry on failure. Falls back to urllib."""
    if shutil.which("curl"):
        for attempt in range(1, retries + 1):
            retry_flag = ["--retry", "3", "--retry-delay", "2"] if attempt == 1 else []
            result = subprocess.run(
                ["curl", "-L", "-f", "-#", "-o", str(dest)] + retry_flag + [url],
                capture_output=False,
            )
            if result.returncode == 0:
                return
            if attempt < retries:
                print(f"Download failed (curl code {result.returncode}), retrying ({attempt + 1}/{retries}) ...")
                time.sleep(2)
        raise RuntimeError(f"curl failed after {retries} attempts (code {result.returncode})")
    else:
        urllib.request.urlretrieve(url, dest)


def download_xray(url: str = "") -> Path:
    """
    Download and extract Xray-core binary.

    Args:
        url: Download URL (empty = default GitHub releases/latest).

    Returns:
        Path to the extracted xray binary.

    Raises:
        RuntimeError: If download or extraction fails.
    """
    if not url:
        url = XRAY_DOWNLOAD_URL
    _THIRD_PARTY.mkdir(parents=True, exist_ok=True)

    if _XRAY_DIR.exists():
        shutil.rmtree(_XRAY_DIR)
    _XRAY_DIR.mkdir(parents=True)

    print(f"Downloading Xray-core from {url} ...")
    tmp_path = _XRAY_DIR / "xray.zip"
    _fetch(url, tmp_path)

    print("Extracting ...")
    try:
        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.extractall(_XRAY_DIR)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"Failed to extract Xray-core: {e}") from e
    finally:
        tmp_path.unlink(missing_ok=True)

    xray_bin = _get_xray_binary()
    if xray_bin is None:
        raise RuntimeError("xray binary not found after extraction")

    xray_bin.chmod(0o755)
    print(f"Xray-core installed to {_XRAY_DIR}")
    return xray_bin


def _get_xray_binary_or_raise() -> Path:
    """Get xray binary path, raise RuntimeError if not found."""
    p = _get_xray_binary()
    if p is None:
        raise RuntimeError("xray binary not found. Run setup or download first.")
    return p


def start_xray(config: dict | None = None, config_path: str | None = None) -> subprocess.Popen:
    """
    Start xray-core as a background process, verify it stays alive.

    Args:
        config: Config dict. If None, loads from config_path.
        config_path: Path to config JSON file. Defaults to project config.json.

    Returns:
        The Popen process handle.

    Raises:
        RuntimeError: If xray binary not found, already running, or startup fails.
    """
    if is_running():
        raise RuntimeError("Xray is already running. Stop it first.")

    xray_bin = _get_xray_binary_or_raise()

    if config_path is None:
        config_path = str(_CONFIG_FILE)

    if config is not None:
        _save_config_atomic(config, config_path)

    if not os.path.exists(config_path):
        raise RuntimeError(f"Config file not found: {config_path}")

    with open(_LOG_FILE, "w") as log_f:
        proc = subprocess.Popen(
            [str(xray_bin), "run", "-c", config_path],
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )

    # Verify the process stays alive after startup
    time.sleep(0.5)
    if proc.poll() is not None:
        tail = _read_log_tail(str(_LOG_FILE), 10)
        raise RuntimeError(
            f"Xray exited immediately (code={proc.returncode}). "
            f"Check config or logs.\nLast log lines:\n{tail}"
        )

    _PID_FILE.write_text(str(proc.pid))
    print(f"Xray started (pid={proc.pid}), config={config_path}")
    return proc


def stop_xray() -> None:
    """
    Stop the running xray-core process gracefully.

    Sends SIGTERM, waits up to 5s, then SIGKILL if still alive.
    Only deletes PID file after process is confirmed dead.
    """
    if not is_running():
        print("Xray is not running.")
        _PID_FILE.unlink(missing_ok=True)
        return

    pid = int(_PID_FILE.read_text().strip())

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"Xray process {pid} not found, cleaning up.")
        _PID_FILE.unlink(missing_ok=True)
        return
    except PermissionError:
        raise RuntimeError(f"No permission to stop xray process {pid}.")

    # Wait for graceful shutdown
    deadline = time.time() + _STOP_TIMEOUT
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
            time.sleep(0.2)
        except ProcessLookupError:
            print(f"Xray (pid={pid}) stopped gracefully.")
            _PID_FILE.unlink(missing_ok=True)
            return

    # Force kill
    print(f"Xray did not exit after {_STOP_TIMEOUT}s, sending SIGKILL.")
    try:
        os.kill(pid, signal.SIGKILL)
    except PermissionError:
        _PID_FILE.unlink(missing_ok=True)
        print(f"Warning: cannot signal pid {pid} (WSL limitation). PID file removed.")
        return
    time.sleep(0.5)
    try:
        os.kill(pid, 0)
        _PID_FILE.unlink(missing_ok=True)
        print(f"Warning: pid {pid} still alive (WSL limitation). PID file removed.")
    except ProcessLookupError:
        _PID_FILE.unlink(missing_ok=True)
        print("Xray killed.")


def restart_xray(config: dict, config_path: str | None = None) -> subprocess.Popen:
    """
    Gracefully restart xray-core: stop (with wait), then start with new config.

    Args:
        config: New config dict.
        config_path: Config file path (defaults to config.json).

    Returns:
        New Popen process handle.
    """
    if config_path is None:
        config_path = str(_CONFIG_FILE)

    if is_running():
        stop_xray()

    return start_xray(config, config_path=config_path)


def is_running() -> bool:
    """
    Check if xray-core is currently running.

    Verifies both PID existence and that the process is actually xray
    (not a PID reuse by another process).

    Returns:
        True if PID file exists and the process is alive and is xray.
    """
    if not _PID_FILE.exists():
        return False

    try:
        pid = int(_PID_FILE.read_text().strip())
    except ValueError:
        _PID_FILE.unlink(missing_ok=True)
        return False

    # Check process exists
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        _PID_FILE.unlink(missing_ok=True)
        return False

    # Verify it's actually xray (not a reused PID)
    if not _verify_process_is_xray(pid):
        _PID_FILE.unlink(missing_ok=True)
        return False

    return True


def _verify_process_is_xray(pid: int) -> bool:
    """Check if /proc/<pid>/cmdline belongs to an xray process."""
    cmdline_path = f"/proc/{pid}/cmdline"
    try:
        cmdline = Path(cmdline_path).read_bytes()
        return b"xray" in cmdline.lower()
    except (FileNotFoundError, PermissionError, OSError):
        # On Windows or if procfs unavailable, skip verification
        return True


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


def _save_config_atomic(config: dict, filepath: str) -> None:
    """Atomically write config dict as JSON: tmp → fsync → rename."""
    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)
    # Restrict permissions
    try:
        os.chmod(filepath, 0o600)
    except OSError:
        pass


def _read_log_tail(log_path: str, lines: int = 10) -> str:
    """Read the last N lines of a log file."""
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except FileNotFoundError:
        return "(log file not found)"
