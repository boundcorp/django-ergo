"""Safety checks for the operator-run Hindsight importer."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "hindsight_ingest_named",
    Path(__file__).resolve().parents[1] / "scripts/hindsight_ingest_named.py",
)
ingest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ingest)


def test_uncertain_submission_never_replayed(tmp_path):
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "batches": [{"status": "intent", "ids": ["ergonaut-turn-1"]}],
                "skipped": [],
            }
        )
    )
    with (
        patch.object(
            sys,
            "argv",
            ["ingest", "--export", str(tmp_path), "--checkpoint", str(checkpoint)],
        ),
        patch.object(ingest, "request") as request,
    ):
        with pytest.raises(SystemExit, match="Uncertain"):
            ingest.main()
        request.assert_not_called()


def test_protected_bank_rejected_before_requests(tmp_path):
    (tmp_path / "summary.json").write_text(
        json.dumps([{"bank": "personal", "role": "chat"}])
    )
    with (
        patch.object(
            sys,
            "argv",
            [
                "ingest",
                "--export",
                str(tmp_path),
                "--checkpoint",
                str(tmp_path / "checkpoint.json"),
            ],
        ),
        patch.object(ingest, "request") as request,
    ):
        with pytest.raises(SystemExit, match="named chats only"):
            ingest.main()
        request.assert_not_called()


def test_existing_id_skipped_and_intent_saved_before_post(tmp_path):
    from urllib.error import HTTPError

    rows = [
        {
            "bank": "ergonaut",
            "role": "chat",
            "bot": "b",
            "session": "s",
            "title": "Example",
            "documents": 2,
        }
    ]
    (tmp_path / "summary.json").write_text(json.dumps(rows))
    items = [
        {"document_id": "ergonaut-turn-existing"},
        {"document_id": "ergonaut-turn-new", "timestamp": "2026-01-01"},
    ]
    (tmp_path / "b-chat-s.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in items)
    )
    checkpoint = tmp_path / "checkpoint.json"
    posts = []

    def fake_request(url, payload=None):
        if url.endswith("/documents/ergonaut-turn-existing"):
            return {}
        if url.endswith("/documents/ergonaut-turn-new"):
            raise HTTPError(url, 404, "missing", {}, None)
        if payload is not None:
            state = json.loads(checkpoint.read_text())
            assert state["batches"][0]["status"] == "intent"
            assert state["skipped"] == ["ergonaut-turn-existing"]
            posts.append(payload)
            return {"success": True, "operation_id": payload["operation_id"]}
        if "/operations?" in url:
            return {"total": 0}
        return {"status": "completed"}

    with (
        patch.object(
            sys,
            "argv",
            ["ingest", "--export", str(tmp_path), "--checkpoint", str(checkpoint)],
        ),
        patch.object(ingest, "request", side_effect=fake_request),
    ):
        ingest.main()
        ingest.main()
    assert [p["items"] for p in posts] == [[items[1]]]
    assert json.loads(checkpoint.read_text())["batches"][0]["status"] == "completed"
