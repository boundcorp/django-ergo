"""OpenTelemetry instrumentation for conversation engines.

Follows the GenAI semantic conventions:
https://opentelemetry.io/docs/specs/semconv/gen-ai/

Configuration via Django settings:
    DJANGO_ERGO = {
        "TELEMETRY_ENABLED": True,  # default False
        "TELEMETRY_SERVICE_NAME": "django-ergo",
        "TELEMETRY_EXPORTER": "console",  # or "otlp"
        "TELEMETRY_OTLP_ENDPOINT": "http://localhost:4317",
    }
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

# GenAI semantic convention attribute names
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"

# Custom attributes for ergo
ERGO_SESSION_ID = "ergo.session.id"
ERGO_ENGINE_TYPE = "ergo.engine.type"
ERGO_TRANSPORT_TYPE = "ergo.transport.type"
ERGO_CACHE_CREATION_TOKENS = "ergo.usage.cache_creation_input_tokens"
ERGO_CACHE_READ_TOKENS = "ergo.usage.cache_read_input_tokens"


def _get_setting(name: str, default: Any = None) -> Any:
    """Get a django-ergo telemetry setting."""
    try:
        from django.conf import settings

        ergo_settings = getattr(settings, "DJANGO_ERGO", {})
        return ergo_settings.get(name, default)
    except Exception:  # noqa: BLE001
        return default


def _is_enabled() -> bool:
    return bool(_get_setting("TELEMETRY_ENABLED", False))  # noqa: FBT003


_tracer = None


def get_tracer():
    """Get or initialize the OpenTelemetry tracer."""
    global _tracer  # noqa: PLW0603
    if _tracer is not None:
        return _tracer

    if not _is_enabled():
        return None

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        service_name = _get_setting("TELEMETRY_SERVICE_NAME", "django-ergo")
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)

        exporter_type = _get_setting("TELEMETRY_EXPORTER", "console")
        if exporter_type == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter
            from opentelemetry.sdk.trace.export import SimpleSpanProcessor

            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        elif exporter_type == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            endpoint = _get_setting("TELEMETRY_OTLP_ENDPOINT", "http://localhost:4317")
            provider.add_span_processor(
                SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("django_ergo.conversation")
        return _tracer

    except ImportError:
        logger.debug("opentelemetry packages not installed, telemetry disabled")
        return None


@contextlib.contextmanager
def trace_engine_call(  # noqa: PLR0913
    operation: str,
    engine_type: str,
    model: str,
    session_id: str = "",
    transport_type: str = "",
    max_tokens: int | None = None,
):
    """Context manager that creates an OTel span for an engine call.

    Usage:
        with trace_engine_call("send", "claude", "claude-sonnet-4-6", ...) as span:
            response = await client.messages.create(...)
            if span:
                span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, response.usage.input_tokens)
    """
    tracer = get_tracer()
    if tracer is None:
        yield None
        return

    span_name = f"{engine_type}.{operation}"
    with tracer.start_as_current_span(span_name) as span:
        span.set_attribute(GEN_AI_OPERATION_NAME, operation)
        span.set_attribute(GEN_AI_SYSTEM, engine_type)
        span.set_attribute(GEN_AI_REQUEST_MODEL, model)
        if session_id:
            span.set_attribute(ERGO_SESSION_ID, session_id)
        if transport_type:
            span.set_attribute(ERGO_TRANSPORT_TYPE, transport_type)
        if max_tokens is not None:
            span.set_attribute(GEN_AI_REQUEST_MAX_TOKENS, max_tokens)
        yield span


def record_usage(
    span, input_tokens=None, output_tokens=None, cache_creation=None, cache_read=None
):
    """Record token usage on an active span."""
    if span is None:
        return
    if input_tokens is not None:
        span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, input_tokens)
    if output_tokens is not None:
        span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, output_tokens)
    if cache_creation is not None:
        span.set_attribute(ERGO_CACHE_CREATION_TOKENS, cache_creation)
    if cache_read is not None:
        span.set_attribute(ERGO_CACHE_READ_TOKENS, cache_read)
