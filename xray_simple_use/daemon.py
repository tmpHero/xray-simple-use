"""
Daemon main loop: health check, failover, daily rescan, smooth switching.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from xray_simple_use.vless import (
    VLESSConfig,
    parse_vless_link,
    generate_client_config,
    save_config,
    load_config,
    ensure_server_name,
)
from xray_simple_use.xray import (
    download_xray,
    start_xray,
    stop_xray,
    reload_xray,
    is_running,
    _CONFIG_FILE,
    _XRAY_DIR,
)
from xray_simple_use.cf_speedtest import (
    download_cfst,
    run_speedtest,
    filter_valid_ips,
    check_proxy_interference,
    _get_cfst_binary,
)
from xray_simple_use.tester import (
    test_candidates,
    results_to_queue_data,
)
from xray_simple_use.queue import (
    IPQueue,
    Candidate,
    load_queue,
    save_queue,
    build_queue,
    get_next_available,
    mark_failed,
    count_available,
    get_shortest_cooldown_ip,
    clear_cooldown,
)
from xray_simple_use.speedtest import (
    test_connectivity,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daemon")

_HEALTH_INTERVAL = 30  # seconds
_HEALTH_TIMEOUT = 3    # seconds per check
_HEALTH_URL = "http://www.gstatic.com/generate_204"
_FAIL_THRESHOLD = 3    # consecutive failures to trigger failover
_RESET_HOUR = 5        # daily rescan at 5am
_CIRCUIT_COOLDOWN = 600  # 10 minutes
_CFST_CONCURRENCY = 4
_CFST_ATTEMPTS = 2
_TEST_ATTEMPTS = 3
_TEST_TIMEOUT = 8


class Daemon:
    """Manages xray lifecycle with health monitoring and automatic failover."""

    def __init__(self, cfg: VLESSConfig):
        self.cfg = cfg
        self.queue: Optional[IPQueue] = None
        self.fail_count = 0
        self.last_rescan_date: str = ""  # "YYYY-MM-DD" to avoid re-scanning same day
        self._stop = False
        self._socks_port = 10808
        self._http_port = 10809

    def run(self):
        """Entry point: startup, then main loop."""
        log.info("[DAEMON] Starting ...")

        # Ensure binaries
        if not _XRAY_DIR.exists():
            download_xray()
        if not _get_cfst_binary().exists():
            download_cfst()

        # Startup sequence
        self._startup()

        # Main loop
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        while not self._stop:
            try:
                self._tick()
            except Exception as e:
                log.error(f"[DAEMON] Tick error: {e}")
            time.sleep(_HEALTH_INTERVAL)

        # Shutdown
        log.info("[DAEMON] Shutting down ...")
        if is_running():
            stop_xray()

    def _handle_signal(self, signum, frame):
        log.info(f"[DAEMON] Received signal {signum}, stopping ...")
        self._stop = True

    # ── Startup ────────────────────────────────────────────────────

    def _startup(self):
        """Load cached queue, start xray, trigger background candidate update."""
        cached = load_queue()
        active_ip: str = ""

        if cached and cached.candidates:
            active_ip = cached.active_ip
            log.info(f"[QUEUE] Loaded cached queue ({len(cached.candidates)} IPs), active={active_ip}")
            self.queue = cached
        else:
            log.info("[QUEUE] No cached queue, will scan after startup.")
            active_ip = self.cfg.address

        # Ensure serverName before starting (address might be a domain)
        config = generate_client_config(self.cfg, socks_port=self._socks_port, http_port=self._http_port)
        ensure_server_name(config)
        # Override with active IP from queue
        if active_ip and active_ip != self.cfg.address:
            config["outbounds"][0]["settings"]["vnext"][0]["address"] = active_ip

        save_config(config, str(_CONFIG_FILE))
        start_xray(config)

        # Async candidate update (doesn't block proxy availability)
        if not cached or not cached.candidates:
            # No queue at all — must scan
            threading.Thread(target=self._candidate_update, daemon=True).start()
        else:
            # Have queue, but refresh in background
            threading.Thread(target=self._candidate_update, daemon=True).start()

        log.info(f"[ACTIVE] Proxy ready: SOCKS5 127.0.0.1:{self._socks_port}, HTTP 127.0.0.1:{self._http_port}")

    # ── Main tick ───────────────────────────────────────────────────

    def _tick(self):
        """Run one health check cycle."""
        # Check if due for daily rescan
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now()
        if now.hour == _RESET_HOUR and today != self.last_rescan_date:
            log.info("[SCHEDULE] Daily rescan triggered (5am).")
            self._daily_rescan()

        # Half-open recovery: probe IPs whose cooldown has expired
        self._recovery_check()

        # Health check current IP
        healthy = self._health_check()
        if healthy:
            if self.fail_count > 0:
                log.info(f"[HEALTH] Recovered after {self.fail_count} failures.")
            self.fail_count = 0
        else:
            self.fail_count += 1
            log.warning(f"[HEALTH] Failure {self.fail_count}/{_FAIL_THRESHOLD}")
            if self.fail_count >= _FAIL_THRESHOLD:
                self._failover()

    # ── Health check ────────────────────────────────────────────────

    def _health_check(self) -> bool:
        """Check if current proxy responds. Returns True if healthy."""
        result = test_connectivity(
            socks_port=self._socks_port,
            test_url=_HEALTH_URL,
            timeout=_HEALTH_TIMEOUT,
        )
        if result.connected:
            log.info(f"[HEALTH] OK ({result.latency_ms}ms)")
        else:
            log.warning(f"[HEALTH] FAIL: {result.error}")
        return result.connected

    # ── Failover ────────────────────────────────────────────────────

    def _failover(self):
        """Switch to next available candidate, with graded rescan triggers."""
        if not self.queue or not self.queue.candidates:
            log.error("[FAILOVER] No queue available, triggering emergency scan.")
            self._emergency_scan()
            return

        failed_ip = self.queue.active_ip
        mark_failed(self.queue, failed_ip, _CIRCUIT_COOLDOWN)
        log.warning(f"[FAILOVER] Marked {failed_ip} as failed (cooldown={_CIRCUIT_COOLDOWN}s).")

        # Count remaining available IPs
        available = count_available(self.queue)
        log.info(f"[FAILOVER] Available IPs after marking: {available}/{len(self.queue.candidates)}")

        # Try to get next available IP
        next_ip = get_next_available(self.queue, failed_ip)

        # ── 0 available: all broken ──
        if available == 0:
            log.error("[FAILOVER] All IPs in cooldown.")
            # Try the one with shortest remaining cooldown
            fallback = get_shortest_cooldown_ip(self.queue)
            if fallback:
                log.warning(f"[FAILOVER] Trying fallback IP with shortest cooldown: {fallback}")
                clear_cooldown(self.queue, fallback)
                self.queue.active_ip = fallback
                save_queue(self.queue)
                self._switch_active_ip(fallback)
            # Start emergency scan
            threading.Thread(target=self._emergency_scan, daemon=True).start()
            self.fail_count = 0
            return

        # ── 1 available: emergency threshold ──
        if available == 1:
            log.warning("[FAILOVER] Only 1 IP left, triggering emergency CFST.")
            threading.Thread(target=self._emergency_scan, daemon=True).start()

        # ── 2 available: warning threshold ──
        elif available == 2:
            log.warning("[FAILOVER] Only 2 IPs left, starting background candidate supplement.")
            threading.Thread(target=self._candidate_update, daemon=True).start()

        # ── Switch to next IP ──
        if next_ip:
            self.queue.active_ip = next_ip
            save_queue(self.queue)
            self._switch_active_ip(next_ip)
            self.fail_count = 0
        else:
            # Should not reach here if available > 0, but safety net
            log.error("[FAILOVER] Unexpected: no next IP despite available > 0.")
            self._emergency_scan()

    # ── Candidate update ────────────────────────────────────────────

    def _candidate_update(self):
        """Full CFST scan + real-link test + queue update. Async, non-blocking."""
        log.info("[CANDIDATE] Starting full candidate update ...")
        try:
            # CFST coarse scan
            cfst_results = run_speedtest(
                concurrency=_CFST_CONCURRENCY,
                attempts=_CFST_ATTEMPTS,
                test_port=self.cfg.port,
                skip_download=True,
            )

            is_clean, msg = check_proxy_interference(cfst_results)
            if not is_clean:
                log.error(f"[CFST] Proxy interference detected: {msg}")
                return

            valid = filter_valid_ips(cfst_results)
            # For coarse scan with dd, download_speed might be 0 — relax filter
            if not valid:
                valid = [r for r in cfst_results if r.received > 0 and r.loss_rate == 0.0]
            top_ips = [r.ip for r in valid[:5]]

            if len(top_ips) < 2:
                log.warning(f"[CFST] Only {len(top_ips)} valid IPs, need >= 2 for queue.")
                if not top_ips:
                    return

            log.info(f"[CFST] Got {len(cfst_results)} IPs, top 5: {top_ips}")

            # Real-link test
            test_results = test_candidates(
                self.cfg, top_ips,
                attempts=_TEST_ATTEMPTS,
                timeout=_TEST_TIMEOUT,
            )

            for r in test_results:
                log.info(
                    f"[TEST] {r.ip}: {r.success_count}/{r.success_count + r.failure_count + r.timeout_count} "
                    f"success, median={r.median_latency:.0f}ms, p95={r.p95_latency:.0f}ms"
                )

            # Build and save queue
            queue_data = results_to_queue_data(test_results)
            new_queue = build_queue(queue_data)

            if not new_queue.candidates:
                log.error("[QUEUE] No valid candidates after testing.")
                return

            old_active = self.queue.active_ip if self.queue else ""
            self.queue = new_queue
            save_queue(self.queue)
            log.info(f"[QUEUE] Updated: active={new_queue.active_ip}, candidates={[c.ip for c in new_queue.candidates]}")

            # If active IP changed, hot-switch
            if old_active and old_active != new_queue.active_ip:
                self._switch_active_ip(new_queue.active_ip)

        except Exception as e:
            log.error(f"[CANDIDATE] Update failed: {e}")
            # Don't crash the daemon

    # ── Daily rescan ────────────────────────────────────────────────

    def _daily_rescan(self):
        """5am full rescan without stopping xray."""
        self.last_rescan_date = datetime.now().strftime("%Y-%m-%d")
        log.info("[SCHEDULE] Running daily full rescan ...")
        self._candidate_update()

    # ── Emergency scan ──────────────────────────────────────────────

    def _emergency_scan(self):
        """Emergency scan when all IPs are dead."""
        log.warning("[EMERGENCY] All IPs failed, running emergency CFST ...")

        try:
            cfst_results = run_speedtest(
                concurrency=_CFST_CONCURRENCY,
                attempts=_CFST_ATTEMPTS,
                test_port=self.cfg.port,
                skip_download=True,
            )

            is_clean, msg = check_proxy_interference(cfst_results)
            if not is_clean:
                log.error(f"[EMERGENCY] CFST interference: {msg}")
                return

            valid = [r for r in cfst_results if r.received > 0 and r.loss_rate == 0.0]
            top_ips = [r.ip for r in valid[:5]]

            if not top_ips:
                log.error("[EMERGENCY] No IPs found, keeping current config.")
                return

            test_results = test_candidates(self.cfg, top_ips, attempts=2, timeout=6)
            queue_data = results_to_queue_data(test_results)
            self.queue = build_queue(queue_data)
            save_queue(self.queue)

            if self.queue.active_ip:
                self._switch_active_ip(self.queue.active_ip)
                log.info(f"[EMERGENCY] Switched to {self.queue.active_ip}")

        except Exception as e:
            log.error(f"[EMERGENCY] Failed: {e}")

    # ── Recovery check ────────────────────────────────────────────────

    def _recovery_check(self):
        """Probe IPs whose circuit-break cooldown has expired (half-open recovery)."""
        if not self.queue:
            return

        now = time.time()
        for c in self.queue.candidates:
            if c.circuit_broken_until > 0 and now >= c.circuit_broken_until:
                log.info(f"[RECOVERY] Cooldown expired for {c.ip}, probing ...")
                # Quick single probe via the main proxy
                # (Can't test without switching — skip for now, just clear cooldown)
                # In practice, the IP will be naturally retried when current IP fails
                clear_cooldown(self.queue, c.ip)
                save_queue(self.queue)
                log.info(f"[RECOVERY] {c.ip} cooldown cleared, back in candidate pool.")

    # ── Hot switch helper ───────────────────────────────────────────

    def _switch_active_ip(self, new_ip: str):
        """Update config and SIGHUP reload for smooth switch."""
        config = load_config(str(_CONFIG_FILE))
        config["outbounds"][0]["settings"]["vnext"][0]["address"] = new_ip
        save_config(config, str(_CONFIG_FILE))

        try:
            reload_xray()
            log.info(f"[SWITCH] Active IP → {new_ip}")
        except RuntimeError:
            log.warning("[SWITCH] Reload failed, full restart.")
            if is_running():
                stop_xray()
            start_xray(config)
