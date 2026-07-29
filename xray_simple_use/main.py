"""
CLI entry point for xray_simple_use — lightweight Xray deployment tool.
"""

import argparse
import json
import sys
import time
from pathlib import Path

from xray_simple_use.vless import (
    parse_vless_link,
    generate_client_config,
    save_config,
    load_config,
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
    get_best_ip,
    get_top_ips,
    IPResult,
)
from xray_simple_use.speedtest import (
    test_connectivity,
    test_latency,
    test_download_speed,
    check_curl_available,
)


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
    p_opt.add_argument("--count", type=int, default=200, help="Number of IPs to test (default: 200)")
    p_opt.add_argument("--threads", type=int, default=4, help="Concurrent threads (default: 4)")
    p_opt.add_argument("--restart", action="store_true", help="Restart xray after updating address")
    p_opt.add_argument("--verify", action="store_true", help="Verify proxy connectivity after restart (requires curl)")
    p_opt.add_argument("--url", type=str, default="", help="vless:// link (if no existing config)")

    # speedtest
    p_speed = subparsers.add_parser("speedtest", help="Test proxy speed through SOCKS5")
    p_speed.add_argument("--socks-port", type=int, default=10808, help="SOCKS5 port (default: 10808)")
    p_speed.add_argument(
        "--mode",
        choices=["all", "connect", "latency", "download"],
        default="all",
        help="Test mode (default: all)",
    )

    # setup
    subparsers.add_parser("setup", help="Download xray-core and CloudflareSpeedTest")

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
    cfg = parse_vless_link(args.url)
    print("=== VLESS Config ===")
    for key, val in cfg.to_dict().items():
        print(f"  {key}: {val}")

    config = generate_client_config(cfg)
    save_config(config, str(_CONFIG_FILE))
    print(f"\nConfig saved to {_CONFIG_FILE}")


def cmd_start(args):
    """Parse vless link, generate config, start xray."""
    cfg = parse_vless_link(args.url)
    print("=== VLESS Config ===")
    for key, val in cfg.to_dict().items():
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
    """Stop xray-core."""
    stop_xray()


def cmd_status(args):
    """Display xray-core running status."""
    st = status()
    if st["running"]:
        print(f"Xray is running (pid={st['pid']})")
        print(f"Config: {st['config']}")
    else:
        print("Xray is not running.")


def cmd_optimize(args):
    """Run CloudflareSpeedTest, pick best IP by combined score, optionally verify via proxy."""
    if args.url:
        cfg = parse_vless_link(args.url)
        config = generate_client_config(cfg)
        save_config(config, str(_CONFIG_FILE))
        print(f"Config generated from vless link: {_CONFIG_FILE}")

    if not _CONFIG_FILE.exists():
        print(f"Config file not found: {_CONFIG_FILE}")
        print("Run 'start <vless_url>' first, or use 'optimize --url <vless_url>'")
        sys.exit(1)

    from xray_simple_use.cf_speedtest import _get_cfst_binary
    if not _get_cfst_binary().exists():
        print("CloudflareSpeedTest not found, downloading ...")
        download_cfst()

    # Step 1: run CloudflareSpeedTest
    results = run_speedtest(count=args.count, threads=args.threads)
    top5 = results[:5]

    print(f"\n=== CFST Top 5 (sorted by latency) ===")
    for i, r in enumerate(top5):
        print(f"  {i+1}. {r.ip}  latency={r.latency:.1f}ms  speed={r.download_speed:.2f}MB/s")

    # Step 2: pick best by CFST ranking (already latency-sorted in parse)
    best_ip = get_best_ip(results)
    if not best_ip:
        print("No valid IP found.")
        sys.exit(1)

    print(f"\nBest IP: {best_ip} (latency={results[0].latency:.1f}ms, speed={results[0].download_speed:.2f}MB/s)")

    # Step 3: update config
    config = load_config(str(_CONFIG_FILE))
    old_address = config["outbounds"][0]["settings"]["vnext"][0]["address"]
    config["outbounds"][0]["settings"]["vnext"][0]["address"] = best_ip
    save_config(config, str(_CONFIG_FILE))
    print(f"Address updated: {old_address} -> {best_ip}")

    # Step 4: restart and verify
    if args.restart:
        if is_running():
            stop_xray()
        start_xray(config)
        time.sleep(1)
        print("Xray restarted.")

    if args.verify:
        _do_verify()


def _do_verify():
    """Quick connectivity check through the proxy."""
    if not check_curl_available():
        print("curl not available, skipping verification.")
        return

    result = test_connectivity()
    if result.connected:
        print(f"Proxy OK: latency={result.latency_ms}ms")
    else:
        print(f"Proxy FAILED: {result.error}")


def cmd_speedtest(args):
    """Test proxy connectivity, latency, and download speed."""
    if not check_curl_available():
        print("curl is required for speedtest. Install: apt install curl")
        sys.exit(1)

    socks_port = args.socks_port

    if args.mode in ("all", "connect"):
        print("=== Connectivity Test ===")
        result = test_connectivity(socks_port=socks_port)
        if result.connected:
            print(f"  Connected: YES (latency={result.latency_ms}ms)")
        else:
            print(f"  Connected: NO  ({result.error})")
            if args.mode == "connect":
                return

    if args.mode in ("all", "latency"):
        print("\n=== Latency Test ===")
        latencies = test_latency(socks_port=socks_port)
        for url, lat in latencies.items():
            if lat is not None:
                print(f"  {url}: {lat}ms")
            else:
                print(f"  {url}: FAILED")

    if args.mode in ("all", "download"):
        print("\n=== Download Speed Test ===")
        dl_result = test_download_speed(socks_port=socks_port)
        if dl_result.connected:
            print(f"  Speed: {dl_result.download_speed_mbps} Mbps")
            print(f"  Downloaded: {dl_result.download_size_mb} MB")
            print(f"  Duration: {dl_result.download_duration_s}s")
        else:
            print(f"  Failed: {dl_result.error}")


def cmd_setup(args):
    """Download xray-core and CloudflareSpeedTest."""
    print("=== Downloading Xray-core ===")
    download_xray()
    print("\n=== Downloading CloudflareSpeedTest ===")
    download_cfst()
    print("\nSetup complete. Use 'start <vless_url>' to begin.")


if __name__ == "__main__":
    main()
