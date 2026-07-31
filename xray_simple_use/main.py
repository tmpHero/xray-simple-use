"""
CLI entry point for xray_simple_use — lightweight Xray deployment tool.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent

from xray_simple_use.config import load_ini, Config
from xray_simple_use.stability_test import StabilityTest

from xray_simple_use.vless import (
    parse_vless_link,
    parse_share_link,
    generate_client_config,
    save_config,
    load_config,
    ensure_server_name,
)
from xray_simple_use.xray import (
    download_xray,
    start_xray,
    stop_xray,
    is_running,
    status,
    _CONFIG_FILE,
    _XRAY_DIR,
)
from xray_simple_use.cf_speedtest import (
    download_cfst,
    run_speedtest,
    filter_valid_ips,
    get_best_ip,
    get_top_ips,
    IPResult,
    _get_cfst_binary,
)
from xray_simple_use.speedtest import (
    probe_socks as _probe_socks,
    test_latency,
    test_download_speed,
    check_curl_available,
)
from xray_simple_use.daemon import Daemon


def main():
    parser = argparse.ArgumentParser(
        prog="xray-simple",
        description="Lightweight Xray-core deployment tool for Linux",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # parse
    p_parse = subparsers.add_parser("parse", help="Parse vless link and show config")
    p_parse.add_argument("url", help="vless:// share link URL")

    # start
    p_start = subparsers.add_parser("start", help="Parse link and start xray-core")
    p_start.add_argument("url", help="vless:// share link URL")
    p_start.add_argument(
        "--socks-port", type=int, default=10808, help="Local SOCKS5 port (default: 10808)"
    )
    p_start.add_argument(
        "--http-port", type=int, default=10809, help="Local HTTP proxy port (default: 10809)"
    )

    # stop
    subparsers.add_parser("stop", help="Stop xray-core")

    # status
    subparsers.add_parser("status", help="Show xray-core running status")

    # optimize
    p_opt = subparsers.add_parser("optimize", help="Run CloudflareSpeedTest and update config")
    p_opt.add_argument("--concurrency", type=int, default=4, help="CFST concurrency threads -n (default: 4)")
    p_opt.add_argument("--attempts", type=int, default=4, help="CFST tests per IP -t (default: 4)")
    p_opt.add_argument("--threshold", type=float, default=10.0, help="Min latency improvement in ms to switch (default: 10)")
    p_opt.add_argument("--restart", action="store_true", help="Restart xray after updating address")
    p_opt.add_argument("--verify", action="store_true", help="Verify proxy connectivity after restart (requires --restart)")
    p_opt.add_argument("--url", type=str, default="", help="vless:// link (if no existing config)")

    # speedtest
    p_speed = subparsers.add_parser("speedtest", help="Test proxy speed through HTTP proxy")
    p_speed.add_argument("--http-port", type=int, default=10809, help="HTTP proxy port (default: 10809)")
    p_speed.add_argument(
        "--mode",
        choices=["all", "latency", "download"],
        default="all",
        help="Test mode (default: all)",
    )

    # run
    p_run = subparsers.add_parser("run", help="Start daemon (runs in background by default)")
    p_run.add_argument("url", nargs="?", default="", help="vless:// share link URL (optional if --config is used)")
    p_run.add_argument("--config", type=str, default="", help="Path to config.ini (default: auto-detect)")
    p_run.add_argument("-f", "--foreground", action="store_true", help="Run in foreground (don't daemonize)")
    p_run.add_argument("--socks-port", type=int, default=10808, help="SOCKS5 port (default: 10808)")
    p_run.add_argument("--http-port", type=int, default=10809, help="HTTP proxy port (default: 10809)")

    # log
    subparsers.add_parser("log", help="Tail daemon logs (follow mode)")

    # stability-test
    p_stab = subparsers.add_parser("stability-test", help="Run 24h stability test (daemon must be running)")
    p_stab.add_argument("--duration", type=str, default="24h", help="Test duration (e.g. 1h, 24h, 30m)")
    p_stab.add_argument("--download-url", type=str, default=None, help="Custom download URL (default: Cloudflare 5MiB)")

    # setup
    p_setup = subparsers.add_parser("setup", help="Download xray-core and CloudflareSpeedTest")
    p_setup.add_argument("--mirror", nargs="?", const="https://gh-proxy.com/", default="", help="Use GitHub mirror (default: https://gh-proxy.com/)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    command_map = {
        "parse": cmd_parse,
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "optimize": cmd_optimize,
        "speedtest": cmd_speedtest,
        "run": cmd_run,
        "stability-test": cmd_stability_test,
        "log": cmd_log,
        "setup": cmd_setup,
    }

    handler = command_map.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()


# ── command handlers ──────────────────────────────────────────────


def cmd_parse(args):
    """Parse vless link and display config summary."""
    cfg = parse_share_link(args.url)
    print("=== VLESS Config ===")
    for key, val in cfg.to_dict_safe().items():
        print(f"  {key}: {val}")

    config = generate_client_config(cfg)
    save_config(config, str(_CONFIG_FILE))
    print(f"\nConfig saved to {_CONFIG_FILE}")


def cmd_start(args):
    """Parse vless link, generate config, start xray."""
    cfg = parse_share_link(args.url)
    print("=== VLESS Config ===")
    for key, val in cfg.to_dict_safe().items():
        print(f"  {key}: {val}")

    config = generate_client_config(cfg, socks_port=args.socks_port, http_port=args.http_port)
    save_config(config, str(_CONFIG_FILE))
    print(f"Config saved to {_CONFIG_FILE}")

    if not _XRAY_DIR.exists():
        print("Xray-core not found, downloading ...")
        download_xray()

    start_xray(config)
    print(f"SOCKS5 proxy: 127.0.0.1:{args.socks_port}")
    print(f"HTTP proxy:   127.0.0.1:{args.http_port}")


def cmd_stop(args):
    """Stop daemon and xray-core."""
    # Stop daemon first (it will stop xray on shutdown)
    daemon_pid = None
    if _DAEMON_PID_FILE.exists():
        try:
            daemon_pid = int(_DAEMON_PID_FILE.read_text().strip())
        except ValueError:
            pass

    if daemon_pid:
        print(f"Stopping daemon (pid={daemon_pid}) ...")
        try:
            os.kill(daemon_pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(daemon_pid, 0)
                os.kill(daemon_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ProcessLookupError, PermissionError):
            pass
        _DAEMON_PID_FILE.unlink(missing_ok=True)

    # Fallback: stop xray directly if still running
    stop_xray()
    print("Stopped.")


def cmd_status(args):
    """Display xray-core running status."""
    st = status()
    if st["running"]:
        print(f"Xray is running (pid={st['pid']})")
        print(f"Config: {st['config']}")
    else:
        print("Xray is not running.")


_DAEMON_PID_FILE = _PROJECT_ROOT / "daemon.pid"


def cmd_run(args):
    """Start daemon. Detaches to background unless -f/--foreground."""
    if not args.foreground and sys.platform != "win32":
        # Setup file logging before forking so child logs persist
        import logging
        log_file = _PROJECT_ROOT / "daemon.log"
        file_handler = logging.FileHandler(str(log_file))
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
        ))
        logging.getLogger().addHandler(file_handler)

        pid = os.fork()
        if pid != 0:
            print(f"Daemon started (pid={pid}). Use 'log' to view output.")
            _DAEMON_PID_FILE.write_text(str(pid))
            return
        # Child: detach from terminal
        os.setsid()
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")

    # Load config
    config_path = Path(args.config) if args.config else None
    try:
        app_config = load_ini(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}")
        sys.exit(1)

    vless_url = args.url or app_config.vless_url
    if not vless_url:
        print("Error: No VLESS URL provided.")
        sys.exit(1)

    cfg = parse_share_link(vless_url)
    daemon = Daemon(cfg, app_config)
    daemon._socks_port = args.socks_port
    daemon._http_port = args.http_port
    daemon.run()


def cmd_log(args):
    """Tail daemon logs."""
    log_files = sorted(_PROJECT_ROOT.glob("*.log"))
    if not log_files:
        print("No log files found.")
        return
    try:
        subprocess.run(["tail", "-f"] + [str(f) for f in log_files])
    except KeyboardInterrupt:
        pass


def cmd_optimize(args):
    """Run CloudflareSpeedTest, pick best IP, update config with rollback support.

    DEPRECATED: Use 'run' command instead for full daemon with health monitoring
    and automatic failover. This command is kept for quick one-shot optimization.
    """
    print("Warning: 'optimize' is deprecated. Use 'run' for daemon mode with failover.")

    if args.verify and not args.restart:
        print("Error: --verify requires --restart (need running proxy to test against)")
        sys.exit(1)

    if args.url:
        cfg = parse_share_link(args.url)
        config = generate_client_config(cfg)
        save_config(config, str(_CONFIG_FILE))
        print(f"Config generated from vless link: {_CONFIG_FILE}")

    if not _CONFIG_FILE.exists():
        print(f"Config file not found: {_CONFIG_FILE}")
        print("Run 'start <vless_url>' first, or use 'optimize --url <vless_url>'")
        sys.exit(1)

    if _get_cfst_binary() is None:
        print("CloudflareSpeedTest not found, downloading ...")
        download_cfst()

    # Load current config
    config = load_config(str(_CONFIG_FILE))
    old_address = config["outbounds"][0]["settings"]["vnext"][0]["address"]
    vless_port = config["outbounds"][0]["settings"]["vnext"][0]["port"]

    # Step 1: ensure serverName is set before we replace address with IP
    ensure_server_name(config)
    save_config(config, str(_CONFIG_FILE))

    # Step 2: run CloudflareSpeedTest on the actual VLESS port
    results = run_speedtest(
        concurrency=args.concurrency,
        attempts=args.attempts,
        test_port=vless_port,
    )
    valid = filter_valid_ips(results)
    top5 = valid[:5]

    print(f"\n=== CFST Top 5 (filtered: no loss, speed>0) ===")
    for i, r in enumerate(top5):
        print(f"  {i+1}. {r.ip}  latency={r.latency:.1f}ms  speed={r.download_speed:.2f}MB/s")

    best_ip = get_best_ip(valid)
    if not best_ip:
        print("No valid IP found after filtering.")
        sys.exit(1)

    # Step 3: threshold check — only switch if improvement is significant
    if old_address == best_ip:
        print(f"\nBest IP is same as current ({best_ip}), no change needed.")
        return

    improvement = top5[0].latency if top5 else float("inf")
    print(f"\nBest IP: {best_ip} (CFST latency={improvement:.1f}ms, speed={top5[0].download_speed:.2f}MB/s)")

    # Step 4: backup old config, then update
    backup_config = json.loads(json.dumps(config))  # deep copy
    config["outbounds"][0]["settings"]["vnext"][0]["address"] = best_ip
    save_config(config, str(_CONFIG_FILE))
    print(f"Address updated: {old_address} -> {best_ip}")

    # Step 5: restart and optionally verify
    if args.restart:
        was_running = is_running()
        if was_running:
            stop_xray()
        start_xray(config)
        time.sleep(1)
        print("Xray restarted.")

    if args.verify:
        success, latency, err = _do_verify_with_result()
        if success:
            print(f"Verify OK: proxy latency={latency}ms")
        else:
            print(f"Verify FAILED: {err}")
            print("Rolling back to previous config ...")
            save_config(backup_config, str(_CONFIG_FILE))
            if was_running:
                if is_running():
                    stop_xray()
                start_xray(backup_config)
            print("Rollback complete, previous config restored.")
            sys.exit(1)


def _do_verify_with_result() -> tuple[bool, float, str]:
    """Quick connectivity check through proxy, return (success, latency, error)."""
    if not check_curl_available():
        return False, 0.0, "curl not available"
    result = _probe_socks(10808, "https://www.gstatic.com/generate_204")
    if result.ok:
        return True, result.total_time_ms or 0.0, ""
    return False, 0.0, result.error or "unknown"


def _do_verify():
    """Quick connectivity check through the proxy (print only)."""
    success, latency, err = _do_verify_with_result()
    if success:
        print(f"Proxy OK: latency={latency}ms")
    else:
        print(f"Proxy FAILED: {err}")


def cmd_speedtest(args):
    """Test proxy latency and download speed through HTTP proxy."""
    if not check_curl_available():
        print("curl is required. Install: apt install curl")
        sys.exit(1)

    http_port = args.http_port

    if args.mode in ("all", "latency"):
        print("=== Proxy Latency Test ===")
        result = test_latency(http_port=http_port, attempts=5, timeout=5)
        if result.connected:
            print(f"  Proxy:     http://127.0.0.1:{http_port}")
            print(f"  Success:   {result.successes}/{result.attempts}")
            print(f"  Median:    {result.median_ms} ms")
            print(f"  Minimum:   {result.minimum_ms} ms")
            print(f"  Maximum:   {result.maximum_ms} ms")
        else:
            print(f"  Failed: {result.error}")
            if args.mode == "latency":
                return

    if args.mode in ("all", "download"):
        print("\n=== Download Speed Test ===")
        dl_result = test_download_speed(http_port=http_port, timeout=60)
        if dl_result.connected:
            print(f"  Proxy:     http://127.0.0.1:{http_port}")
            print(f"  Speed:     {dl_result.speed_mbps} Mbps ({dl_result.speed_mb_s} MiB/s)")
            print(f"  Size:      {dl_result.size_mb} MiB")
            print(f"  Duration:  {dl_result.duration_s} s")
        else:
            print(f"  Failed: {dl_result.error}")


def cmd_stability_test(args):
    """Run 24-hour stability test against the HTTP proxy."""
    duration_s = _parse_duration(args.duration)
    if args.download_url:
        test = StabilityTest(duration_seconds=duration_s, download_url=args.download_url)
    else:
        test = StabilityTest(duration_seconds=duration_s)
    test.run()


def _parse_duration(s: str) -> int:
    """Parse duration string like '24h', '1h30m', '30m' into seconds."""
    import re
    total = 0
    m = re.match(r"(\d+)h", s)
    if m:
        total += int(m.group(1)) * 3600
    m = re.match(r".*?(\d+)m", s)
    if m:
        total += int(m.group(1)) * 60
    m = re.match(r".*?(\d+)s", s)
    if m:
        total += int(m.group(1))
    return total if total > 0 else 86400  # default 24h


def cmd_setup(args):
    """Download xray-core and CloudflareSpeedTest."""
    mirror = args.mirror or ""

    print("=== Downloading Xray-core ===")
    if mirror:
        download_xray(url=mirror + "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip")
    else:
        download_xray()

    print("\n=== Downloading CloudflareSpeedTest ===")
    if mirror:
        download_cfst(url=mirror + "https://github.com/XIU2/CloudflareSpeedTest/releases/latest/download/cfst_linux_amd64.tar.gz")
    else:
        download_cfst()

    print("\nSetup complete.")


if __name__ == "__main__":
    main()
