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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daemon")

_HEALTH_INTERVAL = 30
_HEALTH_TIMEOUT = 3
_HEALTH_URL = "https://www.gstatic.com/generate_204"
_FAIL_THRESHOLD = 3
_RESET_HOUR = 5
_CIRCUIT_COOLDOWN = 600  # 10 minutes
_DEFAULT_COOLDOWN = 600
_MAX_COOLDOWN = 7200  # 2 hours max
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
        self.last_rescan_date = ""
        self._stop = False
        self._socks_port = 10808
        self._http_port = 10809
        self._scan_lock = threading.Lock()

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

        while not self._stop:
            try:
                self._tick()
            except Exception as e:
                log.error(f"[DAEMON] Tick error: {e}")
            time.sleep(_HEALTH_INTERVAL)

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
        if now.hour == _RESET_HOUR and today != self.last_rescan_date:
            self.last_rescan_date = today  # Prevent re-triggering within same hour
            log.info("[SCHEDULE] Daily rescan triggered (5am).")
            threading.Thread(target=lambda: self._request_scan("daily 5am"), daemon=True).start()

        # Recovery check for expired cooldowns
        self._recovery_check()

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
        """Check if current proxy responds with expected HTTP status."""
        result = test_connectivity(
            socks_port=self._socks_port,
            test_url=_HEALTH_URL,
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
        if not self.queue or not self.queue.candidates:
            log.error("[FAILOVER] No queue available, triggering emergency scan.")
            threading.Thread(target=lambda: self._request_scan("emergency", emergency=True), daemon=True).start()
            return

        failed_ip = self.queue.active_ip
        # Exponential cooldown based on failure count
        current_failures = 0
        for c in self.queue.candidates:
            if c.ip == failed_ip:
                current_failures = c.failures
                break
        cooldown = min(_DEFAULT_COOLDOWN * (2 ** current_failures), _MAX_COOLDOWN)
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
                self._switch_active_ip(fallback)
            threading.Thread(target=lambda: self._request_scan("emergency-all-dead", emergency=True), daemon=True).start()
            self.fail_count = 0
            return

        # ── 1 available: emergency ──
        if available == 1:
            log.warning("[FAILOVER] Only 1 IP left, triggering emergency CFST.")
            threading.Thread(target=lambda: self._request_scan("emergency-1-left", emergency=True), daemon=True).start()

        # ── 2 available: warning ──
        elif available == 2:
            log.warning("[FAILOVER] Only 2 IPs left, starting background supplement.")
            threading.Thread(target=lambda: self._request_scan("warning-2-left"), daemon=True).start()

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
        # Use timestamp-unique CSV to avoid conflicts with concurrent scans
        import xray_simple_use.cf_speedtest as cfst_mod
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        custom_csv = cfst_mod._PROJECT_ROOT / f"cfst_result_{ts}.csv"

        # CFST coarse scan
        old_csv = cfst_mod._RESULT_CSV
        cfst_mod._RESULT_CSV = custom_csv
        try:
            cfst_results = run_speedtest(
                concurrency=_CFST_CONCURRENCY,
                attempts=_CFST_ATTEMPTS,
                test_port=self.cfg.port,
                skip_download=True,
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
            # Fallback: at least received packets, zero loss
            valid = [r for r in cfst_results if r.received > 0 and r.loss_rate == 0.0]

        top_ips = [r.ip for r in valid[:5]]

        if len(top_ips) < 2 and not emergency:
            log.warning(f"[CFST] Only {len(top_ips)} valid IPs, need >= 2.")
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

        # Build queue (only qualified candidates)
        queue_data = results_to_queue_data(test_results)
        new_queue = build_queue(queue_data)

        if not new_queue.candidates:
            log.warning("[QUEUE] No qualified candidates after testing, keeping old queue.")
            return

        old_active = self.queue.active_ip if self.queue else ""
        self.queue = new_queue
        save_queue(self.queue)
        log.info(f"[QUEUE] Updated: active={new_queue.active_ip}, candidates={[c.ip for c in new_queue.candidates]}")

        if old_active and old_active != new_queue.active_ip:
            self._switch_active_ip(new_queue.active_ip)

        # Cleanup temp CSV
        custom_csv.unlink(missing_ok=True)

    # ── Recovery check ────────────────────────────────────────────────

    def _recovery_check(self):
        """Probe IPs whose cooldown has expired with a real connectivity test."""
        if not self.queue:
            return

        now = time.time()
        for c in self.queue.candidates:
            if not (0 < c.circuit_broken_until <= now):
                continue

            log.info(f"[RECOVERY] Cooldown expired for {c.ip}, probing ...")

            # Build a temporary config with just this IP as a test outbound
            from xray_simple_use.vless import generate_test_config
            test_cfg = generate_test_config(self.cfg, [c.ip], test_base_port=11901)

            # Start temp xray and probe
            import json, tempfile, subprocess
            from xray_simple_use.xray import _get_xray_binary
            xray_bin = _get_xray_binary()
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(test_cfg, tmp, indent=2, ensure_ascii=False)
            tmp.close()

            probe_ok = False
            try:
                proc = subprocess.Popen(
                    [str(xray_bin), "run", "-c", tmp.name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                time.sleep(0.5)
                if proc.poll() is not None:
                    log.warning(f"[RECOVERY] {c.ip} test xray failed to start.")
                else:
                    # Quick probe through SOCKS5
                    import subprocess as sp
                    curl_result = sp.run(
                        ["curl", "--socks5-hostname", "127.0.0.1:11901",
                         "--fail", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                         "--max-time", "5", "https://www.google.com"],
                        capture_output=True, text=True, timeout=8,
                    )
                    if curl_result.returncode == 0:
                        probe_ok = True
                        log.info(f"[RECOVERY] {c.ip} probe OK, restoring to candidate pool.")
                    else:
                        log.warning(f"[RECOVERY] {c.ip} still failing, extending cooldown.")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            except Exception as e:
                log.warning(f"[RECOVERY] {c.ip} probe error: {e}")
            finally:
                from pathlib import Path
                Path(tmp.name).unlink(missing_ok=True)

            if probe_ok:
                c.circuit_broken_until = 0.0
            else:
                c.circuit_broken_until = now + _CIRCUIT_COOLDOWN
            save_queue(self.queue)

    # ── Hot switch helper ───────────────────────────────────────────

    def _switch_active_ip(self, new_ip: str):
        """Update config and restart xray with rollback on failure."""
        config = load_config(str(_CONFIG_FILE))
        old_ip = config["outbounds"][0]["settings"]["vnext"][0]["address"]

        if old_ip == new_ip:
            return

        # Backup
        import json
        backup_config = json.dumps(config)
        backup_active = old_ip

        # Update config
        config["outbounds"][0]["settings"]["vnext"][0]["address"] = new_ip
        save_config(config, str(_CONFIG_FILE))

        try:
            restart_xray(config)
            log.info(f"[SWITCH] Active IP: {old_ip} → {new_ip}")
        except RuntimeError as e:
            log.error(f"[SWITCH] Restart failed: {e}, rolling back.")
            # Rollback config
            save_config(json.loads(backup_config), str(_CONFIG_FILE))
            # Rollback queue
            if self.queue:
                self.queue.active_ip = backup_active
                save_queue(self.queue)
            # Try to restart with old config
            try:
                start_xray(config=json.loads(backup_config))
                log.info(f"[SWITCH] Rollback successful, restored {old_ip}.")
            except RuntimeError:
                log.critical("[SWITCH] Rollback restart also failed — proxy may be down.")
