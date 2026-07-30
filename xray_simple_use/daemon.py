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
    probe_socks,
    ProbeResult,
)
from xray_simple_use.config import Config
from xray_simple_use.cf_speedtest import filter_cfst_results

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
        if _get_cfst_binary() is None:
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
        result = probe_socks(
            socks_port=self._socks_port,
            url=self.app_config.probe_url,
            expected_status=self.app_config.expected_http_status,
            timeout=_HEALTH_TIMEOUT,
        )
        if result.ok:
            log.debug(f"[HEALTH] OK ({result.total_time_ms:.0f}ms)")
        else:
            log.warning(f"[HEALTH] FAIL: {result.error}")
        return result.ok

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
                # Choose best historical candidate (not just shortest cooldown)
                sorted_candidates = sorted(
                    self.queue.candidates,
                    key=lambda c: (c.failures, -c.success_rate, c.median_latency_ms, c.circuit_broken_until),
                )
                fallback_ip = None
                for c in sorted_candidates:
                    log.info(f"[FAILOVER] Probing fallback {c.ip} before use ...")
                    if self._probe_candidate(c.ip):
                        fallback_ip = c.ip
                        log.warning(f"[FAILOVER] {c.ip} probe OK, activating.")
                        break
                    else:
                        c.circuit_broken_until = time.time() + self.app_config.cooldown_seconds
                        log.warning(f"[FAILOVER] {c.ip} probe failed, extending cooldown.")

                if fallback_ip:
                    clear_cooldown(self.queue, fallback_ip)
                    self.queue.active_ip = fallback_ip
                    save_queue(self.queue)
                    self._switch_active_ip(fallback_ip)
                else:
                    log.critical("[FAILOVER] No fallback passed probe — proxy offline.")
                    save_queue(self.queue)

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
        """Full CFST scan + real-link test + transactional queue update."""
        import xray_simple_use.cf_speedtest as cfst_mod
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        custom_csv = cfst_mod._PROJECT_ROOT / f"cfst_result_{ts}.csv"

        try:
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

            valid = filter_cfst_results(
                cfst_results,
                skip_download=self.app_config.skip_download,
                max_loss_rate=self.app_config.max_loss_rate,
                max_latency_ms=self.app_config.max_latency_ms,
            )

            top_ips = [r.ip for r in valid[:self.app_config.candidate_count]]

            if len(top_ips) < 2 and not emergency:
                log.warning(f"[CFST] Only {len(top_ips)} valid IPs.")
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

            queue_data = results_to_queue_data(test_results)
            new_queue = build_queue(queue_data)

            if not new_queue.candidates:
                log.warning("[QUEUE] No qualified candidates, keeping old queue.")
                return

            # Snapshot old queue for potential rollback
            import copy
            with self._queue_lock:
                old_queue_snapshot = copy.deepcopy(self.queue) if self.queue else None

            # Compare with actual xray config address
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

            # Transaction: switch first, then commit queue
            if need_switch:
                switched = self._switch_active_ip(new_queue.active_ip)
            else:
                switched = True

            with self._queue_lock:
                if switched:
                    self.queue = new_queue
                    save_queue(self.queue)
                    log.info(f"[QUEUE] Updated: active={new_queue.active_ip}, "
                             f"candidates={[c.ip for c in new_queue.candidates]}")
                elif old_queue_snapshot:
                    # Switch failed, restore old queue entirely
                    self.queue = old_queue_snapshot
                    save_queue(self.queue)
                    log.warning("[QUEUE] Switch failed, restored old queue.")
                else:
                    self.queue = new_queue
                    save_queue(self.queue)

        finally:
            custom_csv.unlink(missing_ok=True)

    # ── Recovery check ────────────────────────────────────────────────

    # ── Recovery loop (background thread) ──────────────────────────────

    def _recovery_loop(self):
        """Background thread: snapshot targets, probe outside lock, commit inside lock."""
        while not self._recovery_stop.wait(timeout=self.app_config.health_interval):
            # Lock: get snapshot of expired IPs
            with self._queue_lock:
                if not self.queue:
                    continue
                now = time.time()
                targets = [
                    c.ip for c in self.queue.candidates
                    if 0 < c.circuit_broken_until <= now
                ]

            for ip in targets:
                log.info(f"[RECOVERY] Probing {ip} ...")
                recovered = self._probe_candidate(ip)

                # Lock: commit result against current queue
                with self._queue_lock:
                    if not self.queue:
                        continue
                    candidate = next(
                        (c for c in self.queue.candidates if c.ip == ip), None
                    )
                    if candidate is None:
                        continue

                    if recovered:
                        candidate.circuit_broken_until = 0.0
                        log.info(f"[RECOVERY] {ip} OK, restored to pool.")
                    else:
                        candidate.circuit_broken_until = now + self.app_config.cooldown_seconds
                        log.warning(f"[RECOVERY] {ip} still failing, cooldown extended.")
                    save_queue(self.queue)

    def _probe_candidate(self, ip: str) -> bool:
        """Probe a single candidate IP via temporary xray + probe_socks. Returns True if OK."""
        import json, tempfile, subprocess as sp
        from xray_simple_use.vless import generate_test_config
        from xray_simple_use.xray import _get_xray_binary
        from pathlib import Path as P

        test_cfg = generate_test_config(self.cfg, [ip], test_base_port=11901)
        xray_bin = _get_xray_binary()
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(test_cfg, tmp, indent=2, ensure_ascii=False)
        tmp.close()

        try:
            proc = sp.Popen(
                [str(xray_bin), "run", "-c", tmp.name],
                stdout=sp.DEVNULL, stderr=sp.DEVNULL,
            )
            time.sleep(0.5)
            if proc.poll() is not None:
                return False

            result = probe_socks(
                socks_port=11901,
                url=self.app_config.probe_url,
                expected_status=self.app_config.expected_http_status,
                timeout=5,
            )
            ok = result.ok
        except Exception:
            ok = False
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except sp.TimeoutExpired:
                proc.kill()
                proc.wait()
            P(tmp.name).unlink(missing_ok=True)

        return ok

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
