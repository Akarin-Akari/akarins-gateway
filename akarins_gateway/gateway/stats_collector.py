"""
Request Statistics Collector

Lightweight in-memory statistics collection for the management panel.
Tracks per-backend request counts, success rates, and response times.

Author: fufu-chan (Claude Opus 4.6)
Date: 2026-03-14
"""

import time
import threading
from typing import Dict, Any
from dataclasses import dataclass, field

__all__ = ["StatsCollector", "get_stats_collector"]


@dataclass
class BackendStats:
    """Per-backend request statistics."""
    total_requests: int = 0
    success_count: int = 0
    error_count: int = 0
    total_response_time_ms: float = 0.0
    last_request_time: float = 0.0
    last_error_time: float = 0.0
    last_error_code: int = 0
    status_code_counts: Dict[int, int] = field(default_factory=dict)

    def record_request(self, success: bool, response_time_ms: float, status_code: int = 200):
        """Record a single request outcome."""
        self.total_requests += 1
        self.total_response_time_ms += response_time_ms
        self.last_request_time = time.time()

        if success:
            self.success_count += 1
        else:
            self.error_count += 1
            self.last_error_time = time.time()
            self.last_error_code = status_code

        self.status_code_counts[status_code] = self.status_code_counts.get(status_code, 0) + 1

    @property
    def success_rate(self) -> float:
        """Calculate success rate as percentage."""
        if self.total_requests == 0:
            return 0.0
        return round((self.success_count / self.total_requests) * 100, 1)

    @property
    def avg_response_time_ms(self) -> float:
        """Calculate average response time in milliseconds."""
        if self.total_requests == 0:
            return 0.0
        return round(self.total_response_time_ms / self.total_requests, 1)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "total_requests": self.total_requests,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "success_rate": self.success_rate,
            "avg_response_time_ms": self.avg_response_time_ms,
            "last_request_time": self.last_request_time,
            "last_error_time": self.last_error_time,
            "last_error_code": self.last_error_code,
            "status_code_counts": dict(self.status_code_counts),
        }


class StatsCollector:
    """Thread-safe request statistics collector."""

    def __init__(self):
        self._stats: Dict[str, BackendStats] = {}
        self._lock = threading.Lock()
        self._start_time = time.time()

    def record(self, backend_key: str, success: bool, response_time_ms: float, status_code: int = 200):
        """
        Record a request outcome for a backend.

        Args:
            backend_key: Backend identifier (e.g., "zerogravity", "copilot")
            success: Whether the request succeeded
            response_time_ms: Response time in milliseconds
            status_code: HTTP status code
        """
        with self._lock:
            if backend_key not in self._stats:
                self._stats[backend_key] = BackendStats()
            self._stats[backend_key].record_request(success, response_time_ms, status_code)

    def get_backend_stats(self, backend_key: str) -> Dict[str, Any]:
        """Get statistics for a specific backend."""
        with self._lock:
            stats = self._stats.get(backend_key)
            if stats is None:
                return BackendStats().to_dict()
            return stats.to_dict()

    def get_all_stats(self) -> Dict[str, Any]:
        """Get statistics for all backends."""
        with self._lock:
            result = {}
            total_requests = 0
            total_success = 0

            for key, stats in self._stats.items():
                result[key] = stats.to_dict()
                total_requests += stats.total_requests
                total_success += stats.success_count

            return {
                "backends": result,
                "global": {
                    "total_requests": total_requests,
                    "total_success": total_success,
                    "total_errors": total_requests - total_success,
                    "global_success_rate": round(
                        (total_success / total_requests * 100) if total_requests > 0 else 0, 1
                    ),
                    "uptime_seconds": round(time.time() - self._start_time, 0),
                },
            }

    def reset(self):
        """Reset all statistics."""
        with self._lock:
            self._stats.clear()
            self._start_time = time.time()


# Singleton instance
_collector: StatsCollector = None
_collector_lock = threading.Lock()


def get_stats_collector() -> StatsCollector:
    """Get the singleton StatsCollector instance."""
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = StatsCollector()
    return _collector
