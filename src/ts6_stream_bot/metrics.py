"""Prometheus metrics. Kept tiny and lazy so importing it is free of side effects.

If `prometheus_client` is not installed (e.g. minimal dev install), the module
degrades to a no-op shim so the rest of the app keeps working.
"""

from __future__ import annotations

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        generate_latest,
    )

    _registry = CollectorRegistry()

    PLAY_REQUESTS = Counter(
        "ts6_stream_bot_play_requests_total",
        "Total /play API calls received.",
        registry=_registry,
    )
    PLAY_FAILURES = Counter(
        "ts6_stream_bot_play_failures_total",
        "Total /play API calls that failed (source open / capture start).",
        registry=_registry,
    )
    _STATE = Gauge(
        "ts6_stream_bot_state",
        "Current StreamController state (1 = active for this label).",
        labelnames=("state",),
        registry=_registry,
    )

    _STATES = ("idle", "loading", "playing", "paused")

    def observe_state(state: str) -> None:
        for s in _STATES:
            _STATE.labels(state=s).set(1.0 if s == state else 0.0)

    def render() -> tuple[bytes, str]:
        return generate_latest(_registry), CONTENT_TYPE_LATEST

except ImportError:  # pragma: no cover - optional dep

    class _NoopCounter:
        def inc(self, amount: float = 1.0) -> None:
            return None

    PLAY_REQUESTS = _NoopCounter()  # type: ignore[assignment]
    PLAY_FAILURES = _NoopCounter()  # type: ignore[assignment]

    def observe_state(state: str) -> None:
        return None

    def render() -> tuple[bytes, str]:
        return b"# prometheus_client not installed\n", "text/plain; version=0.0.4"
