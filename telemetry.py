"""Optional Application Insights initialization and per-agent tracing."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

from engine.state import MatchState
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode


_LOGGER_NAME = "overseer"
_initialized = False


def initialize_telemetry() -> None:
    """Enable Azure Monitor export only when a connection string is configured."""
    global _initialized
    if _initialized or not os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return
    from microsoft.opentelemetry import use_microsoft_opentelemetry

    use_microsoft_opentelemetry(
        enable_azure_monitor=True,
        logger_name=_LOGGER_NAME,
    )
    logging.getLogger(_LOGGER_NAME).setLevel(logging.INFO)
    _initialized = True


@contextmanager
def traced_agent_call(
    agent_name: str, state: MatchState
) -> Generator[Span, None, None]:
    """Record bounded state metadata and propagate agent failures to the span."""
    tracer = trace.get_tracer("overseer.agents")
    with tracer.start_as_current_span(f"agent.{agent_name}") as span:
        span.set_attribute("overseer.agent.name", agent_name)
        span.set_attribute("overseer.match.minute", float(state.minute))
        span.set_attribute("overseer.match.source", str(state.source))
        span.set_attribute("overseer.match.event_count", len(state.events))
        span.set_attribute("overseer.match.frame_count", len(state.frames))
        try:
            yield span
        except Exception as error:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, error.__class__.__name__))
            raise
