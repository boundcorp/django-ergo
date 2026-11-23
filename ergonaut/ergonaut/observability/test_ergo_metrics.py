import socket
import urllib.request
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone
from django_ergo import signals
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.tool_dispatch import dispatch_tool
from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram, values
from prometheus_client.multiprocess import MultiProcessCollector

from ergonaut.observability import ergo, exporter
from ergonaut.observability.collector import ErgoStateCollector, read_state


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_receivers_are_connected_and_count_a_finished_call():
    call = StructuredCall(kind="chat_reply", engine_type="claude", model_name="claude-test-model", status="completed")
    labels = {"bot": "metrics-bot", "engine": "claude", "model": "claude-test-model"}
    before_tokens = sample("ergonaut_llm_tokens_total", **labels, type="input")
    before_cost = sample("ergonaut_llm_cost_usd_total", **labels)
    before_calls = sample("ergonaut_llm_calls_total", **labels, kind="chat_reply", status="completed")

    signals.structured_call_started.send(sender=StructuredCall, call=call, bot_name="metrics-bot")
    assert sample("ergonaut_llm_calls_in_progress", bot="metrics-bot", kind="chat_reply") == 1
    signals.structured_call_finished.send(
        sender=StructuredCall,
        call=call,
        bot_name="metrics-bot",
        duration=2.5,
        tokens={"input": 120, "output": 30, "cache_write": 0, "cache_read": 900, "reasoning": 10},
        cost_usd=0.0125,
        requests=2,
    )

    assert sample("ergonaut_llm_calls_in_progress", bot="metrics-bot", kind="chat_reply") == 0
    assert sample("ergonaut_llm_tokens_total", **labels, type="input") - before_tokens == 120
    assert sample("ergonaut_llm_tokens_total", **labels, type="cache_read") >= 900
    assert sample("ergonaut_llm_cost_usd_total", **labels) - before_cost == pytest.approx(0.0125)
    assert sample("ergonaut_llm_calls_total", **labels, kind="chat_reply", status="completed") - before_calls == 1
    assert sample("ergonaut_llm_requests_total", **labels) >= 2


def test_failed_call_counts_its_error_category():
    call = StructuredCall(kind="chat_reply", engine_type="openai", model_name="gpt-test", status="failed")
    call.error_category = "api"
    labels = {"bot": "err-bot", "engine": "openai", "model": "gpt-test", "category": "api"}
    before = sample("ergonaut_llm_call_errors_total", **labels)
    ergo.call_finished(None, call=call, bot_name="err-bot", duration=1, tokens={}, cost_usd=None, requests=0)
    assert sample("ergonaut_llm_call_errors_total", **labels) - before == 1


def test_library_observes_the_loop_with_token_deltas():
    from django_ergo.conversation.structured import _observed

    call = StructuredCall(kind="probe", input_tokens=100, output_tokens=5, cost_usd=Decimal("0.5"), turns_used=1)
    seen = []

    def receiver(sender, **kwargs):
        seen.append(kwargs)

    signals.structured_call_finished.connect(receiver)
    try:
        with _observed(call):
            call.input_tokens += 40
            call.output_tokens += 2
            call.cost_usd += Decimal("0.25")
            call.turns_used += 1
    finally:
        signals.structured_call_finished.disconnect(receiver)
    (event,) = seen
    assert event["bot_name"] == ""
    assert event["tokens"]["input"] == 40
    assert event["tokens"]["output"] == 2
    assert event["cost_usd"] == pytest.approx(0.25)
    assert event["requests"] == 1


def test_tool_dispatch_counts_ok_and_error_and_unknown():
    class Kit:
        def has_tool(self, name):
            return name in ("good_tool", "bad_tool")

        def execute_tool(self, name, args):
            if name == "bad_tool":
                raise ValueError("nope")
            return "ok"

    before = {
        key: sample("ergonaut_tool_calls_total", tool=tool, outcome=outcome)
        for key, (tool, outcome) in {
            "ok": ("good_tool", "ok"),
            "err": ("bad_tool", "error"),
            "unknown": ("unknown", "error"),
        }.items()
    }
    assert dispatch_tool("good_tool", {}, [Kit()]) == ("ok", False)
    assert dispatch_tool("bad_tool", {}, [Kit()])[1] is True
    assert dispatch_tool("made_up_by_model", {}, [Kit()])[1] is True
    assert sample("ergonaut_tool_calls_total", tool="good_tool", outcome="ok") - before["ok"] == 1
    assert sample("ergonaut_tool_calls_total", tool="bad_tool", outcome="error") - before["err"] == 1
    assert sample("ergonaut_tool_calls_total", tool="unknown", outcome="error") - before["unknown"] == 1


def test_worker_lifecycle_from_post_save():
    now = timezone.now()
    worker = SimpleNamespace(
        bot_name="w-bot",
        function="agent:orca",
        state={"agent": "claude", "model": "claude-opus-test"},
        status="queued",
        started_at=None,
        created_at=now - timedelta(minutes=10),
        completed_at=None,
    )
    labels = {"bot": "w-bot", "function": "agent:orca", "agent": "claude", "model": "claude-opus-test"}
    started = sample("ergonaut_workers_started_total", **labels)
    finished = sample("ergonaut_workers_finished_total", **labels, status="failed")
    polls = sample("ergonaut_worker_polls_total", bot="w-bot", function="agent:orca")

    ergo.worker_saved(None, worker, created=True)
    worker.status, worker.started_at = "running", now - timedelta(minutes=5)
    ergo.worker_saved(None, worker, created=False, update_fields=["status", "polls", "started_at"])
    worker.status, worker.completed_at = "failed", now
    ergo.worker_saved(None, worker, created=False, update_fields=["status", "completed_at"])

    assert sample("ergonaut_workers_started_total", **labels) - started == 1
    assert sample("ergonaut_worker_polls_total", bot="w-bot", function="agent:orca") - polls == 1
    assert sample("ergonaut_workers_finished_total", **labels, status="failed") - finished == 1
    assert sample("ergonaut_worker_duration_seconds_sum", bot="w-bot", function="agent:orca", status="failed") >= 300


def test_async_receivers():
    wake = SimpleNamespace(bot_name="a-bot", status="fired")
    run = SimpleNamespace(bot_name="a-bot", schedule="morning", status="completed")
    message = SimpleNamespace(metadata={"wake": "x"}, in_reply_to_id=None, sender_session_id=None)
    before = (
        sample("ergonaut_wakes_fired_total", bot="a-bot", status="fired"),
        sample("ergonaut_schedule_runs_total", bot="a-bot", schedule="morning"),
        sample("ergonaut_schedule_runs_finished_total", bot="a-bot", schedule="morning", status="completed"),
        sample("ergonaut_thread_messages_total", source="wake"),
    )
    ergo.wake_saved(None, wake, created=False, update_fields=["status", "thread_message", "error", "updated_at"])
    ergo.wake_saved(None, wake, created=False, update_fields=["status"])  # not a fire
    ergo.schedule_run_saved(None, run, created=True)
    ergo.schedule_run_saved(None, run, created=False, update_fields=["status", "completed_at"])
    ergo.thread_message_saved(None, message, created=True)
    after = (
        sample("ergonaut_wakes_fired_total", bot="a-bot", status="fired"),
        sample("ergonaut_schedule_runs_total", bot="a-bot", schedule="morning"),
        sample("ergonaut_schedule_runs_finished_total", bot="a-bot", schedule="morning", status="completed"),
        sample("ergonaut_thread_messages_total", source="wake"),
    )
    assert [a - b for a, b in zip(after, before, strict=True)] == [1, 1, 1, 1]


def test_receivers_never_raise():
    ergo.worker_saved(None, object(), created=True)  # no attributes at all
    ergo.call_finished(None, call=None, bot_name="", duration=0, tokens={}, cost_usd=None, requests=0)


def test_state_collector_serves_cached_snapshot_and_survives_failures():
    calls = []

    def reader():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("database down")
        return {
            "workers": [{"bot_name": "b", "function": "agent:orca", "status": "running", "n": 3}],
            "wakes": [{"bot_name": "b", "n": 2}],
            "recurring": [{"bot_name": "b", "status": "active", "n": 1}],
            "messages": [{"recipient_session__bot_name": "b", "status": "queued", "n": 4}],
            "agent_usage": [
                {
                    "bot_name": "b",
                    "source": "orca",
                    "agent": "codex",
                    "model": "gpt-test",
                    "requests": 7,
                    "input": 10,
                    "output": 20,
                    "cache_write": 0,
                    "cache_read": 30,
                    "reasoning": 5,
                }
            ],
            "queues": {"celery": 6},
        }

    collector = ErgoStateCollector(ttl=60, reader=reader)
    registry = CollectorRegistry()
    registry.register(collector)
    assert (
        registry.get_sample_value("ergonaut_workers", {"bot": "b", "function": "agent:orca", "status": "running"}) == 3
    )
    assert registry.get_sample_value("ergonaut_celery_queue_length", {"queue": "celery"}) == 6
    labels = {"bot": "b", "source": "orca", "agent": "codex", "model": "gpt-test", "type": "cache_read"}
    assert registry.get_sample_value("ergonaut_agent_tokens_total", labels) == 30
    assert registry.get_sample_value("ergonaut_state_up") == 1
    assert len(calls) == 1  # cached between scrapes

    collector.ttl = 0
    assert registry.get_sample_value("ergonaut_wakes_pending", {"bot": "b"}) == 2  # last good snapshot
    assert registry.get_sample_value("ergonaut_state_up") == 0


@pytest.mark.django_db
def test_read_state_queries_run(settings):
    settings.CELERY_BROKER_URL = ""
    state = read_state()
    assert set(state) == {"workers", "wakes", "recurring", "messages", "agent_usage", "queues"}


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_exporter_serves_its_own_port_once(settings, monkeypatch):
    monkeypatch.setattr(exporter, "_server", None)
    settings.TELEMETRY_EXPORTER_PORT = _free_port()
    settings.TELEMETRY_STATE_COLLECTOR = "off"
    try:
        assert exporter.start_exporter("celery") is True
        assert exporter.start_exporter("celery") is True  # already serving
        body = urllib.request.urlopen(f"http://127.0.0.1:{settings.TELEMETRY_EXPORTER_PORT}/metrics", timeout=5).read()
        assert b"ergonaut_celery_task_started_total" in body
    finally:
        server = exporter._server
        if server is not None:
            server[0].shutdown()
            server[0].server_close()


def test_exporter_skips_a_port_in_use(settings, monkeypatch):
    monkeypatch.setattr(exporter, "_server", None)
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        sock.listen()
        settings.TELEMETRY_EXPORTER_PORT = sock.getsockname()[1]
        assert exporter.start_exporter("bots") is False


def test_exporter_off_without_port(settings, monkeypatch):
    monkeypatch.setattr(exporter, "_server", None)
    settings.TELEMETRY_EXPORTER_PORT = 0
    assert exporter.start_exporter("web") is False


def test_archiving_a_dead_child_keeps_totals(tmp_path, monkeypatch):
    """Two 'processes' write; one exits and is archived; the summed totals don't change."""
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    registry = CollectorRegistry()
    totals = {}
    for pid in (101, 102, 103):
        monkeypatch.setattr(values, "ValueClass", values.MultiProcessValue(lambda pid=pid: pid))
        counter = Counter("probe_total", "probe", ["k"], registry=None)
        histogram = Histogram("probe_seconds", "probe", registry=None, buckets=(1.0, float("inf")))
        counter.labels(k="a").inc(pid)
        histogram.observe(0.5)
    collector = exporter._LockedMultiProcessCollector(registry, path=str(tmp_path))

    def snapshot():
        return {
            (s.name, tuple(sorted(s.labels.items()))): s.value for family in collector.collect() for s in family.samples
        }

    totals = snapshot()
    assert totals[("probe_total", (("k", "a"),))] == 306
    exporter.archive_process(101, str(tmp_path))
    exporter.archive_process(102, str(tmp_path))
    assert snapshot() == totals
    names = sorted(p.name for p in tmp_path.glob("*.db"))
    assert names == ["counter_103.db", "counter_archive.db", "histogram_103.db", "histogram_archive.db"]
    assert MultiProcessCollector  # the base reader is what Prometheus' client ships


def test_memory_events_are_counted():
    labels = {"bot": "mem-bot"}
    before = {
        "ok": sample("ergonaut_memory_prefetch_total", **labels, outcome="ok", reason=""),
        "skipped": sample("ergonaut_memory_prefetch_total", **labels, outcome="skipped", reason="provenance"),
        "retain": sample("ergonaut_memory_retain_total", **labels, outcome="queued", reason=""),
        "facts": sample("ergonaut_memory_prefetch_facts_count", **labels),
    }
    send = signals.memory_event.send
    send(
        None,
        plugin="hindsight_memory",
        bot_name="mem-bot",
        event="prefetch",
        outcome="ok",
        reason="",
        duration=0.4,
        count=3,
    )
    send(
        None,
        plugin="hindsight_memory",
        bot_name="mem-bot",
        event="prefetch",
        outcome="skipped",
        reason="provenance",
        duration=0,
        count=0,
    )
    send(
        None,
        plugin="hindsight_memory",
        bot_name="mem-bot",
        event="retain",
        outcome="queued",
        reason="",
        duration=0,
        count=0,
    )
    assert sample("ergonaut_memory_prefetch_total", **labels, outcome="ok", reason="") - before["ok"] == 1
    assert (
        sample("ergonaut_memory_prefetch_total", **labels, outcome="skipped", reason="provenance") - before["skipped"]
        == 1
    )
    assert sample("ergonaut_memory_retain_total", **labels, outcome="queued", reason="") - before["retain"] == 1
    # skipped prefetches carry no latency or fact count
    assert sample("ergonaut_memory_prefetch_facts_count", **labels) - before["facts"] == 1


def test_pr_sync_events_are_counted():
    labels = {"bot": "mem-bot"}
    before = {
        o: sample("ergonaut_memory_pr_sync_total", **labels, outcome=o, reason=r)
        for o, r in (("written", ""), ("skipped", "unchanged"), ("failed", "write"))
    }
    send = signals.memory_event.send
    for outcome, reason, count in (("written", "", 3), ("skipped", "unchanged", 2), ("failed", "write", 1)):
        send(
            None,
            plugin="hindsight_memory",
            bot_name="mem-bot",
            event="pr_sync",
            outcome=outcome,
            reason=reason,
            duration=0.0,
            count=count,
        )
    for outcome, reason, count in (("written", "", 3), ("skipped", "unchanged", 2), ("failed", "write", 1)):
        after = sample("ergonaut_memory_pr_sync_total", **labels, outcome=outcome, reason=reason)
        assert after - before[outcome] == count
