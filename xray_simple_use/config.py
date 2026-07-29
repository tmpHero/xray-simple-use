"""
Configuration file loading for xray-simple-use.

Supports config.ini with interpolation disabled (VLESS URLs contain %XX).
"""

import os
import stat
import sys
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _default_config_dir() -> Path:
    """Get platform-specific default config directory."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", str(Path.home()))
    else:
        base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "xray-simple-use"


DEFAULT_CONFIG_PATH = _default_config_dir() / "config.ini"
FALLBACK_CONFIG_PATH = Path("config.ini")


@dataclass
class Config:
    """All tunable parameters for the daemon."""
    vless_url: str = ""

    # [daemon]
    daily_scan_hour: int = 5
    health_interval: int = 30
    failure_threshold: int = 3
    cooldown_seconds: int = 600
    max_cooldown_seconds: int = 7200
    replenish_threshold: int = 2
    emergency_threshold: int = 1

    # [cfst]
    cfst_concurrency: int = 30
    cfst_attempts: int = 2
    candidate_count: int = 5
    skip_download: bool = True
    max_latency_ms: int = 500

    # [test]
    test_attempts: int = 3
    test_timeout_seconds: int = 5
    probe_url: str = "https://www.gstatic.com/generate_204"


def load_ini(path: Optional[Path] = None) -> Config:
    """
    Load configuration from an INI file.

    Resolution order:
        1. Explicit path argument
        2. --config CLI argument (not handled here)
        3. Platform default (~/.config/xray-simple-use/config.ini)
        4. Fallback: ./config.ini

    Args:
        path: Explicit config file path, or None for auto-detection.

    Returns:
        Config object with all parameters.

    Raises:
        FileNotFoundError: If no config file found.
        ValueError: If config is missing required fields or is malformed.
    """
    if path is None:
        path = _find_config()

    if not path.is_file():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Copy config.example.ini to {path} and edit it."
        )

    _check_permissions(path)

    parser = ConfigParser(interpolation=None)
    loaded = parser.read(path, encoding="utf-8")
    if not loaded:
        raise ValueError(f"Unable to read config file: {path}")

    if not parser.has_option("server", "vless_url"):
        raise ValueError("Missing [server] vless_url in config file.")

    return Config(
        vless_url=parser.get("server", "vless_url"),

        daily_scan_hour=parser.getint("daemon", "daily_scan_hour", fallback=5),
        health_interval=parser.getint("daemon", "health_interval", fallback=30),
        failure_threshold=parser.getint("daemon", "failure_threshold", fallback=3),
        cooldown_seconds=parser.getint("daemon", "cooldown_seconds", fallback=600),
        max_cooldown_seconds=parser.getint("daemon", "max_cooldown_seconds", fallback=7200),
        replenish_threshold=parser.getint("daemon", "replenish_threshold", fallback=2),
        emergency_threshold=parser.getint("daemon", "emergency_threshold", fallback=1),

        cfst_concurrency=parser.getint("cfst", "concurrency", fallback=30),
        cfst_attempts=parser.getint("cfst", "attempts", fallback=2),
        candidate_count=parser.getint("cfst", "candidate_count", fallback=5),
        skip_download=parser.getboolean("cfst", "skip_download", fallback=True),
        max_latency_ms=parser.getint("cfst", "max_latency_ms", fallback=500),

        test_attempts=parser.getint("test", "attempts", fallback=3),
        test_timeout_seconds=parser.getint("test", "timeout_seconds", fallback=5),
        probe_url=parser.get("test", "probe_url", fallback="https://www.gstatic.com/generate_204"),
    )


def _find_config() -> Path:
    """Find config file: platform default first, then cwd fallback."""
    if DEFAULT_CONFIG_PATH.is_file():
        return DEFAULT_CONFIG_PATH
    if FALLBACK_CONFIG_PATH.is_file():
        return FALLBACK_CONFIG_PATH
    return DEFAULT_CONFIG_PATH  # Return default path for error message


def _check_permissions(path: Path) -> None:
    """Warn if config file is readable by others (Linux only)."""
    if sys.platform == "win32":
        return
    try:
        mode = stat.S_IMODE(os.stat(str(path)).st_mode)
        if mode & 0o077:
            import logging
            logging.getLogger("config").warning(
                f"Config file {path} has permissive permissions ({oct(mode)}). "
                f"Run: chmod 600 {path}"
            )
    except OSError:
        pass
