"""Application Insights initialization and fact-level OpenTelemetry spans."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import Generator, Iterable

from engine.state import MatchState
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode


_initialized = False


def initialize_telemetry() -> None:
    """Enable Azure Monitor export only when a connection string is configured."""
    global _initialized
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if _initialized or not connection_string:
        return
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(connection_string=connection_string)
    _initialized = True


@contextmanager
def traced_operation(
    operation_name: str,
    *,
    operation_type: str,
    fact_ids: Iterable[str] = (),
) -> Generator[Span, None, None]:
    """Record operation identity, fact lineage, latency, and failures."""
    tracer = trace.get_tracer("overseer.agents")
    started_at = time.perf_counter()
    with tracer.start_as_current_span(f"{operation_type}.{operation_name}") as span:
        span.set_attribute(f"overseer.{operation_type}.name", operation_name)
        input_fact_ids = sorted(set(fact_ids))
        if input_fact_ids:
            span.set_attribute("overseer.fact.input_ids", input_fact_ids)
        try:
            yield span
        except Exception as error:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, error.__class__.__name__))
            raise
        finally:
            span.set_attribute(
                "overseer.operation.latency_ms",
                (time.perf_counter() - started_at) * 1000,
            )


@contextmanager
def traced_agent_call(
    agent_name: str,
    state: MatchState,
    *,
    fact_ids: Iterable[str] = (),
) -> Generator[Span, None, None]:
    """Record bounded live-match metadata around one agent invocation."""
    with traced_operation(
        agent_name,
        operation_type="agent",
        fact_ids=fact_ids,
    ) as span:
        span.set_attribute("overseer.match.minute", float(state.minute))
        span.set_attribute("overseer.match.source", str(state.source))
        span.set_attribute("overseer.match.event_count", len(state.events))
        span.set_attribute("overseer.match.frame_count", len(state.frames))
        yield span


@contextmanager
def traced_tool_call(
    tool_name: str,
    *,
    fact_ids: Iterable[str] = (),
) -> Generator[Span, None, None]:
    """Record a deterministic engine tool invocation."""
    with traced_operation(
        tool_name,
        operation_type="tool",
        fact_ids=fact_ids,
    ) as span:
        yield span
