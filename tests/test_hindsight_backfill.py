from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

from django_ergo.plugins import hindsight_backfill as hb


def test_export_pairs_original_request_and_excludes_unverified_spans(monkeypatch):
    now = datetime.now(UTC)

    def call(pk, first, last, request):
        return SimpleNamespace(
            pk=pk,
            first_sequence=first,
            last_sequence=last,
            request=request,
            status="completed",
            metadata={},
            created_at=now,
        )

    calls = [
        call("ok", 1, 3, "hello"),
        call("missing", 4, None, "hi"),
        call("mismatch", 5, 6, "different"),
    ]
    manager = Mock()
    manager.filter.return_value.order_by.return_value = calls
    monkeypatch.setattr(hb.StructuredCall, "objects", manager)

    def message(line, role, value):
        return SimpleNamespace(line=line, role=role, timestamp=now, text=lambda: value)

    histories = {
        1: [
            message(1, "user", "hello"),
            message(2, "user", "tool output"),
            message(3, "assistant", "answer"),
        ],
        5: [message(5, "user", "other"), message(6, "assistant", "answer")],
    }
    monkeypatch.setattr(
        hb,
        "SessionSource",
        lambda session, first_line, **kw: SimpleNamespace(
            load=lambda: histories[first_line]
        ),
    )
    monkeypatch.setattr(hb, "_tools", lambda session, call: set())
    turns = list(hb.turns(object()))
    assert turns[0].content == "User: hello\nAssistant: answer"
    assert turns[0].document_id == "ergonaut-turn-ok"
    assert turns[0].line == 1
    assert [t.skipped for t in turns[1:]] == ["missing_span", "request_mismatch"]
