"""
IP priority queue persistence: load, save, failover, circuit breaker.
"""

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).parent.parent
_QUEUE_FILE = _PROJECT_ROOT / "queue.json"

# Minimum success rate for a candidate to enter the queue
_MIN_SUCCESS_RATE = 2 / 3
_MIN_SUCCESS_COUNT = 2

_write_lock = threading.Lock()


@dataclass
class Candidate:
    """Single candidate IP entry in the queue."""
    ip: str
    median_latency_ms: float = 0.0   # warm TTFB median
    cold_ttfb_ms: float = 0.0
    success_rate: float = 0.0
    p95_latency_ms: float = 0.0
    jitter_ms: float = 0.0
    failures: int = 0
    circuit_broken_until: float = 0.0

    def is_available(self) -> bool:
        """Check if this candidate is not in circuit-break cooldown."""
        if self.circuit_broken_until <= 0:
            return True
        return time.time() >= self.circuit_broken_until


@dataclass
class IPQueue:
    """Persistent IP priority queue."""
    generated_at: str = ""
    active_ip: str = ""
    candidates: list[Candidate] = field(default_factory=list)

    def get_active_index(self) -> int:
        """Return index of active_ip in candidates, or -1."""
        for i, c in enumerate(self.candidates):
            if c.ip == self.active_ip:
                return i
        return -1


def load_queue() -> Optional[IPQueue]:
    """
    Load cached IP queue from queue.json.

    Returns:
        IPQueue if file exists and valid, None otherwise.
    """
    if not _QUEUE_FILE.exists():
        return None

    try:
        data = json.loads(_QUEUE_FILE.read_text(encoding="utf-8"))
        candidates = [
            Candidate(
                ip=c["ip"],
                median_latency_ms=c.get("median_latency_ms", 0.0),
                cold_ttfb_ms=c.get("cold_ttfb_ms", 0.0),
                success_rate=c.get("success_rate", 0.0),
                p95_latency_ms=c.get("p95_latency_ms", 0.0),
                jitter_ms=c.get("jitter_ms", 0.0),
                failures=c.get("failures", 0),
                circuit_broken_until=c.get("circuit_broken_until", 0.0),
            )
            for c in data.get("candidates", [])
        ]
        return IPQueue(
            generated_at=data.get("generated_at", ""),
            active_ip=data.get("active_ip", ""),
            candidates=candidates,
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def save_queue(queue: IPQueue) -> None:
    """
    Persist IPQueue to queue.json atomically (tmp → flush → fsync → rename).

    Args:
        queue: IPQueue to save.
    """
    data = {
        "generated_at": queue.generated_at,
        "active_ip": queue.active_ip,
        "candidates": [
            {
                "ip": c.ip,
                "median_latency_ms": c.median_latency_ms,
                "cold_ttfb_ms": c.cold_ttfb_ms,
                "success_rate": c.success_rate,
                "p95_latency_ms": c.p95_latency_ms,
                "jitter_ms": c.jitter_ms,
                "failures": c.failures,
                "circuit_broken_until": c.circuit_broken_until,
            }
            for c in queue.candidates
        ],
    }
    tmp_path = str(_QUEUE_FILE) + ".tmp"
    with _write_lock:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(_QUEUE_FILE))


def get_next_available(queue: IPQueue, failed_ip: str) -> Optional[str]:
    """
    Get the next available candidate IP after a failure.

    Skips the failed IP and any IPs in circuit-break cooldown.

    Args:
        queue: Current IPQueue.
        failed_ip: The IP that just failed.

    Returns:
        Next available IP string, or None if all exhausted.
    """
    for c in queue.candidates:
        if c.ip == failed_ip:
            continue
        if c.is_available():
            return c.ip
    return None


def mark_failed(queue: IPQueue, ip: str, cooldown_sec: int = 600) -> None:
    """
    Mark an IP as failed and put it into circuit-break cooldown.

    Args:
        queue: Current IPQueue (mutated in place).
        ip: The failed IP.
        cooldown_sec: Cooldown duration in seconds (default 10 min).
    """
    for c in queue.candidates:
        if c.ip == ip:
            c.failures += 1
            c.circuit_broken_until = time.time() + cooldown_sec
            return


def count_available(queue: IPQueue) -> int:
    """
    Count how many candidates are not in circuit-break cooldown.

    Args:
        queue: Current IPQueue.

    Returns:
        Number of available (non-broken) candidates.
    """
    return sum(1 for c in queue.candidates if c.is_available())


def get_shortest_cooldown_ip(queue: IPQueue) -> Optional[str]:
    """
    Get the candidate IP with the shortest remaining cooldown.
    Used as last resort when all candidates are broken.

    Args:
        queue: Current IPQueue.

    Returns:
        IP with soonest cooldown expiry, or None if queue is empty.
    """
    broken = [c for c in queue.candidates if not c.is_available()]
    if not broken:
        return None
    broken.sort(key=lambda c: c.circuit_broken_until)
    return broken[0].ip


def clear_cooldown(queue: IPQueue, ip: str) -> None:
    """
    Clear circuit-break cooldown for an IP (half-open recovery).

    Args:
        queue: Current IPQueue (mutated in place).
        ip: The IP to recover.
    """
    for c in queue.candidates:
        if c.ip == ip:
            c.circuit_broken_until = 0.0
            return


def _is_qualified(result: dict) -> bool:
    """
    Check if a test result qualifies for entering the queue.

    Requires: total >= 3, success >= 2, success_rate >= 2/3, latency > 0.

    Args:
        result: Test result dict with success_count, failure_count, median_latency.

    Returns:
        True if the result meets minimum quality threshold.
    """
    total = result["success_count"] + result["failure_count"]
    if total < 3 or result["success_count"] < _MIN_SUCCESS_COUNT:
        return False
    if total > 0 and result["success_count"] / total < _MIN_SUCCESS_RATE:
        return False
    if result.get("median_latency", 0.0) <= 0:
        return False
    return True


def sort_candidates(results: list[dict]) -> list[Candidate]:
    """
    Sort test results into ranked Candidate list.
    Only includes qualified results (success_rate >= 2/3, success >= 2).

    Ranking priority:
        1. success_rate (higher is better)
        2. median_latency_ms (lower is better)
        3. p95_latency_ms (lower is better)
        4. jitter_ms (lower is better)

    Args:
        results: List of dicts with keys: ip, success_count, failure_count,
                 median_latency, p95_latency, jitter.

    Returns:
        Sorted list of Candidate objects (qualified only).
    """
    candidates = []
    for r in results:
        if not _is_qualified(r):
            continue
        total = r["success_count"] + r["failure_count"]
        rate = r["success_count"] / total if total > 0 else 0.0
        candidates.append(Candidate(
            ip=r["ip"],
            median_latency_ms=r.get("median_latency", 0.0),  # warm median
            cold_ttfb_ms=r.get("cold_ttfb", 0.0),
            success_rate=rate,
            p95_latency_ms=r.get("p95_latency", 0.0),
            jitter_ms=r.get("jitter", 0.0),
        ))

    candidates.sort(key=lambda c: (
        -c.success_rate,
        c.median_latency_ms,   # warm median
        c.cold_ttfb_ms,        # cold secondary
        c.p95_latency_ms,
        c.jitter_ms,
    ))
    return candidates


def build_queue(results: list[dict]) -> IPQueue:
    """
    Build a new IPQueue from test results.

    Only includes candidates meeting minimum quality threshold.
    Returns empty IPQueue (no candidates) if none qualify — caller
    must retain the old queue in that case.

    Args:
        results: List of test result dicts.

    Returns:
        IPQueue with sorted qualified candidates, or empty if none qualify.
    """
    candidates = sort_candidates(results)
    active_ip = candidates[0].ip if candidates else ""
    return IPQueue(
        generated_at=datetime.now(timezone.utc).isoformat(),
        active_ip=active_ip,
        candidates=candidates,
    )
