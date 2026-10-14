"""Langfuse tracing (self-hosted, see docker-compose `observability` profile).

Off unless LANGFUSE_TRACING=1 and LANGFUSE_PUBLIC_KEY/SECRET_KEY are set. When on, every voice
or MCP command, model call, planner run, policy decision and PayPal call becomes an
observation. When off, everything here is a no-op and langfuse is never imported, so tests
and offline runs don't depend on it. Exporting is asynchronous: a slow or missing Langfuse
never blocks a payment."""
import logging
import os
from contextlib import contextmanager

log = logging.getLogger("mandate.tracing")
_client = None
_enabled: bool | None = None

# Keys whose values never leave the process, wherever they appear in an observation.
SECRET_KEYS = {"signature", "sig", "client_secret", "secret", "access_token", "authorization", "password",
               "private_key", "onboarding_token", "x-onboarding-token", "transmission_sig", "vault_id"}
TRACE_HEADER, PARENT_HEADER = "x-mandate-trace-id", "x-mandate-parent-observation-id"


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = (os.getenv("LANGFUSE_TRACING", "0") == "1" and bool(os.getenv("LANGFUSE_PUBLIC_KEY"))
                    and bool(os.getenv("LANGFUSE_SECRET_KEY")))
    return _enabled


def mask(data=None, **_):
    if isinstance(data, dict):
        return {k: "[redacted]" if str(k).lower() in SECRET_KEYS else mask(data=v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [mask(data=v) for v in data]
    return data


def client():
    global _client
    if _client is None:
        from langfuse import Langfuse
        _client = Langfuse(mask=mask, environment=os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "local"),
                           release=os.getenv("MANDATE_RELEASE"))
    return _client


class _Noop:
    """Stands in for an observation when tracing is off."""

    def update(self, **_):
        return self


NOOP = _Noop()


@contextmanager
def observe(name: str, as_type: str = "span", trace_context: dict | None = None, **fields):
    """A child observation of whatever is current (or a new trace if nothing is)."""
    if not enabled():
        yield NOOP
        return
    with client().start_as_current_observation(name=name, as_type=as_type, trace_context=trace_context,
                                               **fields) as obs:
        try:
            yield obs
        except Exception as e:
            obs.update(level="ERROR", status_message=f"{type(e).__name__}: {e}"[:500])
            raise


@contextmanager
def trace(name: str, *, session_id: str | None = None, user_id: str | None = None, tags: list[str] | None = None,
          seed: str | None = None, parent: dict | None = None, as_type: str = "span", **fields):
    """A root observation whose user/session/tags propagate to every child.

    seed: derive the trace id from a stable key (a purchase id) so steps that run in different
          processes at different times (create, capture, reconcile, refund) land in one trace.
    parent: {"trace_id", "parent_span_id"} received from another service (MCP -> API)."""
    if not enabled():
        yield NOOP
        return
    from langfuse import Langfuse, propagate_attributes
    ctx = parent or ({"trace_id": Langfuse.create_trace_id(seed=seed)} if seed else None)
    with observe(name, as_type=as_type, trace_context=ctx, **fields) as obs:
        # A continued trace keeps the caller's name (the MCP tool), it only adds detail.
        with propagate_attributes(user_id=user_id, session_id=session_id, tags=tags,
                                  trace_name=None if parent else name):
            yield obs


def generation_usage(model: str | None = None, usage: dict | None = None, metadata: dict | None = None):
    """Called by a provider inside the parser's generation to record what the model reported."""
    if not enabled():
        return
    fields = {k: v for k, v in (("model", model), ("usage_details", usage), ("metadata", metadata)) if v}
    client().update_current_generation(**fields)


def score(name: str, value, data_type: str | None = None, comment: str | None = None):
    if not enabled() or value is None:
        return
    client().score_current_trace(name=name, value=value, data_type=data_type, comment=comment)


def outgoing_headers() -> dict:
    """Carry the current trace into a call to another Mandate service."""
    if not enabled():
        return {}
    c = client()
    tid, oid = c.get_current_trace_id(), c.get_current_observation_id()
    return {TRACE_HEADER: tid, PARENT_HEADER: oid} if tid and oid else {}


def incoming_parent(headers) -> dict | None:
    tid, oid = headers.get(TRACE_HEADER), headers.get(PARENT_HEADER)
    return {"trace_id": tid, "parent_span_id": oid} if tid and oid else None


def flush():
    if enabled() and _client is not None:
        try:
            _client.flush()
        except Exception as e:  # never fail a shutdown over telemetry
            log.warning("langfuse flush failed: %s", e)
