"""
Daemon main loop: health check, failover, daily rescan, graded triggers.
"""

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
    restart_xray,
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
    _RESULT_CSV,
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
from xray_simple_use.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daemon")

_HEALTH_TIMEOUT = 3


class Daemon:
    """Manages xray lifecycle with health monitoring and automatic failover."""

    def __init__(self, vless_cfg: VLESSConfig, app_config: Config):
        self.cfg = vless_cfg
        self.app_config = app_config
        self.queue: Optional[IPQueue] = None
        self.fail_count = 0
        self.last_rescan_date = ""
        self._stop = False
        self._socks_port = 10808
        self._http_port = 10809
        self._scan_lock = threading.Lock()
        self._queue_lock = threading.RLock()
        self._recovery_stop = threading.Event()

    def run(self):
        """Entry point: startup, then main loop."""
        log.info("[DAEMON] Starting ...")

        if not _XRAY_DIR.exists():
            download_xray()
        if not _get_cfst_binary().exists():
            download_cfst()

        self._startup()

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Start background recovery thread
        recovery_thread = threading.Thread(target=self._recovery_loop, daemon=True)
        recovery_thread.start()

        while not self._stop:
            try:
                self._tick()
            except Exception as e:
                log.error(f"[DAEMON] Tick error: {e}")
            time.sleep(self.app_config.health_interval)

        self._recovery_stop.set()
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

        config = generate_client_config(self.cfg, socks_port=self._socks_port, http_port=self._http_port)
        ensure_server_name(config)
        if active_ip and active_ip != self.cfg.address:
            config["outbounds"][0]["settings"]["vnext"][0]["address"] = active_ip

        save_config(config, str(_CONFIG_FILE))
        start_xray(config)

        # Always refresh candidates in background
        threading.Thread(target=lambda: self._request_scan("startup"), daemon=True).start()

        log.info(f"[ACTIVE] Proxy ready: SOCKS5 127.0.0.1:{self._socks_port}, HTTP 127.0.0.1:{self._http_port}")

    # ── Main tick ───────────────────────────────────────────────────

    def _tick(self):
        """Run one health check cycle."""
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now()
        if now.hour == self.app_config.daily_scan_hour and today != self.last_rescan_date:
            self.last_rescan_date = today
            log.info(f"[SCHEDULE] Daily rescan triggered ({self.app_config.daily_scan_hour}am).")
            threading.Thread(target=lambda: self._request_scan("daily"), daemon=True).start()

        healthy = self._health_check()
        if healthy:
            if self.fail_count > 0:
                log.info(f"[HEALTH] Recovered after {self.fail_count} failures.")
            self.fail_count = 0
        else:
            self.fail_count += 1
            log.warning(f"[HEALTH] Failure {self.fail_count}/{self.app_config.failure_threshold}")
            if self.fail_count >= self.app_config.failure_threshold:
                self._failover()

    # ── Health check ────────────────────────────────────────────────

    def _health_check(self) -> bool:
        """Check if current proxy responds with expected HTTP status."""
        result = test_connectivity(
            socks_port=self._socks_port,
            test_url=self.app_config.probe_url,
            timeout=_HEALTH_TIMEOUT,
        )
        if result.connected:
            log.debug(f"[HEALTH] OK ({result.latency_ms}ms)")
        else:
            log.warning(f"[HEALTH] FAIL: {result.error}")
        return result.connected

    # ── Failover ────────────────────────────────────────────────────

    def _failover(self):
        """Switch to next available candidate, with graded rescan triggers."""
        with self._queue_lock:
            if not self.queue or not self.queue.candidates:
                log.error("[FAILOVER] No queue available, triggering emergency scan.")
                threading.Thread(target=lambda: self._request_scan("emergency", emergency=True), daemon=True).start()
                return

            failed_ip = self.queue.active_ip
            current_failures = 0
            for c in self.queue.candidates:
                if c.ip == failed_ip:
                    current_failures = c.failures
                    break
            cooldown = min(self.app_config.cooldown_seconds * (2 ** current_failures), self.app_config.max_cooldown_seconds)
            mark_failed(self.queue, failed_ip, cooldown)
            log.warning(f"[FAILOVER] Marked {failed_ip} as failed (cooldown={cooldown}s).")

            available = count_available(self.queue)
            log.info(f"[FAILOVER] Available IPs after marking: {available}/{len(self.queue.candidates)}")

            next_ip = get_next_available(self.queue, failed_ip)

            # ── 0 available ──
            if available == 0:
                log.error("[FAILOVER] All IPs in cooldown.")
                fallback = get_shortest_cooldown_ip(self.queue)
                if fallback:
                    log.warning(f"[FAILOVER] Trying fallback: {fallback}")
                    clear_cooldown(self.queue, fallback)
                    self.queue.active_ip = fallback
                    save_queue(self.queue)
                    switch_ip = fallback
                else:
                    switch_ip = None
                if switch_ip:
                    self._switch_active_ip(switch_ip)
                threading.Thread(target=lambda: self._request_scan("emergency-all-dead", emergency=True), daemon=True).start()
                self.fail_count = 0
                return

            # ── 1 available: emergency ──
            if available <= self.app_config.emergency_threshold:
                log.warning(f"[FAILOVER] Only {available} IP(s) left, triggering emergency CFST.")
                threading.Thread(target=lambda: self._request_scan("emergency", emergency=True), daemon=True).start()
            # ── warning threshold ──
            elif available <= self.app_config.replenish_threshold:
                log.warning(f"[FAILOVER] Only {available} IPs left, starting background supplement.")
                threading.Thread(target=lambda: self._request_scan("warning"), daemon=True).start()

            if next_ip:
                self.queue.active_ip = next_ip
                save_queue(self.queue)
                self._switch_active_ip(next_ip)
                self.fail_count = 0
            else:
                log.error("[FAILOVER] Unexpected: no next IP despite available > 0.")
                threading.Thread(target=lambda: self._request_scan("emergency-unexpected", emergency=True), daemon=True).start()

    # ── Unified scan entry ──────────────────────────────────────────

    def _request_scan(self, reason: str, emergency: bool = False):
        """Single-flight entry point for all scan requests."""
        if not self._scan_lock.acquire(blocking=False):
            log.info(f"[SCAN] Already running, skipping '{reason}' request.")
            return

        try:
            log.info(f"[SCAN] Starting ({reason}, emergency={emergency}) ...")
            self._do_scan(emergency)
        except Exception as e:
            log.error(f"[SCAN] Failed: {e}")
        finally:
            self._scan_lock.release()

    def _do_scan(self, emergency: bool = False):
        """Full CFST scan + real-link test + queue update."""
        import xray_simple_use.cf_speedtest as cfst_mod
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        custom_csv = cfst_mod._PROJECT_ROOT / f"cfst_result_{ts}.csv"

        # CFST coarse scan
        old_csv = cfst_mod._RESULT_CSV
        cfst_mod._RESULT_CSV = custom_csv
        try:
            cfst_results = run_speedtest(
                concurrency=self.app_config.cfst_concurrency,
                attempts=self.app_config.cfst_attempts,
                test_port=self.cfg.port,
                skip_download=self.app_config.skip_download,
                max_delay=self.app_config.max_latency_ms,
            )
        finally:
            cfst_mod._RESULT_CSV = old_csv

        is_clean, msg = check_proxy_interference(cfst_results)
        if not is_clean:
            log.error(f"[CFST] Proxy interference: {msg}")
            return

        # Filter: when skip_download is on, only check loss_rate
        valid = [
            r for r in cfst_results
            if r.received > 0 and r.loss_rate == 0.0 and r.download_speed >= 0.0
        ]
        if not valid:
            valid = [r for r in cfst_results if r.received > 0 and r.loss_rate == 0.0]

        top_ips = [r.ip for r in valid[:self.app_config.candidate_count]]

        if len(top_ips) < 2 and not emergency:
            log.warning(f"[CFST] Only {len(top_ips)} valid IPs, need >= 2.")
            if not top_ips:
                return

        log.info(f"[CFST] Got {len(cfst_results)} IPs, top {len(top_ips)}: {top_ips}")

        # Real-link test
        test_results = test_candidates(
            self.cfg, top_ips,
            test_url=self.app_config.probe_url,
            attempts=self.app_config.test_attempts,
            timeout=self.app_config.test_timeout_seconds,
        )

        for r in test_results:
            log.info(
                f"[TEST] {r.ip}: {r.success_count}/{r.success_count + r.failure_count + r.timeout_count} "
                f"success, median={r.median_latency:.0f}ms, p95={r.p95_latency:.0f}ms"
            )

        # Build queue (only qualified candidates)
        queue_data = results_to_queue_data(test_results)
        new_queue = build_queue(queue_data)

        if not new_queue.candidates:
            log.warning("[QUEUE] No qualified candidates after testing, keeping old queue.")
            return

        # Compare with actual xray config address (not old queue.active_ip)
        current_xray_addr = ""
        try:
            current_config = load_config(str(_CONFIG_FILE))
            current_xray_addr = current_config["outbounds"][0]["settings"]["vnext"][0]["address"]
        except Exception:
            pass

        need_switch = (
            (current_xray_addr and current_xray_addr != new_queue.active_ip)
            or not self.queue
            or not self.queue.active_ip
        )

        # Transaction: switch xray first, then save queue
        switched = False
        if need_switch:
            switched = self._switch_active_ip(new_queue.active_ip)

        with self._queue_lock:
            self.queue = new_queue
            if not switched and need_switch:
                # Switch failed — but we still update queue metadata.
                # The active_ip mismatch will be corrected on next scan.
                log.warning("[QUEUE] Switch failed, queue saved but xray may not match.")
            save_queue(self.queue)
            log.info(f"[QUEUE] Updated: active={new_queue.active_ip}, candidates={[c.ip for c in new_queue.candidates]}")

        # Cleanup temp CSV
        custom_csv.unlink(missing_ok=True)

    # ── Recovery check ────────────────────────────────────────────────

    # ── Recovery loop (background thread) ──────────────────────────────

    def _recovery_loop(self):
        """Background thread: periodically probe IPs whose cooldown has expired."""
        while not self._recovery_stop.wait(timeout=self.app_config.health_interval):
            if not self.queue:
                continue
            now = time.time()
            for c in self.queue.candidates:
                if not (0 < c.circuit_broken_until <= now):
                    continue

                log.info(f"[RECOVERY] Cooldown expired for {c.ip}, probing ...")
                from xray_simple_use.vless import generate_test_config
                test_cfg = generate_test_config(self.cfg, [c.ip], test_base_port=11901)

                import json, tempfile, subprocess as sp
                from xray_simple_use.xray import _get_xray_binary
                from pathlib import Path as P

                xray_bin = _get_xray_binary()
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
                json.dump(test_cfg, tmp, indent=2, ensure_ascii=False)
                tmp.close()

                probe_ok = False
                try:
                    proc = sp.Popen(
                        [str(xray_bin), "run", "-c", tmp.name],
                        stdout=sp.DEVNULL, stderr=sp.DEVNULL,
                    )
                    time.sleep(0.5)
                    if proc.poll() is not None:
                        log.warning(f"[RECOVERY] {c.ip} test xray failed to start.")
                    else:
                        curl_result = sp.run(
                            ["curl", "--socks5-hostname", "127.0.0.1:11901",
                             "--fail", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                             "--max-time", "5", self.app_config.probe_url],
                            capture_output=True, text=True, timeout=8,
                        )
                        http_code = curl_result.stdout.strip()
                        probe_ok = (
                            curl_result.returncode == 0
                            and http_code in ("204", "200")
                        )
                        if probe_ok:
                            log.info(f"[RECOVERY] {c.ip} probe OK (HTTP {http_code}), restored.")
                        else:
                            log.warning(f"[RECOVERY] {c.ip} still failing (code={http_code}), extending cooldown.")
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except sp.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                except Exception as e:
                    log.warning(f"[RECOVERY] {c.ip} probe error: {e}")
                finally:
                    P(tmp.name).unlink(missing_ok=True)

                if probe_ok:
                    c.circuit_broken_until = 0.0
                else:
                    c.circuit_broken_until = now + self.app_config.cooldown_seconds
                save_queue(self.queue)

    # ── Hot switch helper ───────────────────────────────────────────

    def _switch_active_ip(self, new_ip: str) -> bool:
        """Update config and restart xray with rollback on failure.

        Returns True on success, False on failure (after rollback).
        """
        config = load_config(str(_CONFIG_FILE))
        old_ip = config["outbounds"][0]["settings"]["vnext"][0]["address"]

        if old_ip == new_ip:
            return True

        # Backup
        import json
        backup_config = json.dumps(config)

        # Update config
        config["outbounds"][0]["settings"]["vnext"][0]["address"] = new_ip
        save_config(config, str(_CONFIG_FILE))

        try:
            restart_xray(config)
            log.info(f"[SWITCH] Active IP: {old_ip} → {new_ip}")
            return True
        except RuntimeError as e:
            log.error(f"[SWITCH] Restart failed: {e}, rolling back.")
            # Rollback config
            save_config(json.loads(backup_config), str(_CONFIG_FILE))
            # Rollback queue active_ip
            with self._queue_lock:
                if self.queue:
                    self.queue.active_ip = old_ip
                    save_queue(self.queue)
            # Try to restart with old config
            try:
                start_xray(config=json.loads(backup_config))
                log.info(f"[SWITCH] Rollback successful, restored {old_ip}.")
            except RuntimeError:
                log.critical("[SWITCH] Rollback restart also failed — proxy may be down.")
            return False
