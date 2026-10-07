from __future__ import annotations

import subprocess
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage

from django_ergo.conversation.agent_history import canonical_remote
from django_ergo.conversation.agent_history import derive_project_identity
from django_ergo.conversation.agent_history import estimate_cost
from django_ergo.conversation.agent_history import ingest_omp_file
from django_ergo.conversation.models import AgentSessionThreadLink
from django_ergo.conversation.models import AgentTranscriptArtifact
from django_ergo.conversation.models import AgentUsageEvent
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import Worker


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    return repo


def test_canonical_remote_normalizes_ssh_and_https():
    assert (
        canonical_remote("git@GitHub.COM:Owner/Repo.git")
        == "ssh://github.com/Owner/Repo"
    )
    assert (
        canonical_remote("https://user:secret@GitHub.COM/Owner/Repo.git?token=x#anchor")
        == "https://github.com/Owner/Repo"
    )


def test_project_identity_remote_and_local_fallback(git_repo):
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "remote",
            "add",
            "origin",
            "git@github.com:boundcorp/repo.git",
        ],
        check=True,
    )
    remote = derive_project_identity(str(git_repo), "host-a")
    assert remote.canonical_remote == "ssh://github.com/boundcorp/repo"
    assert remote.project_key.startswith("remote:")

    subprocess.run(
        ["git", "-C", str(git_repo), "remote", "remove", "origin"], check=True
    )
    local = derive_project_identity(str(git_repo), "host-a")
    assert local.project_key.startswith("local:")
    assert local.confidence == "local"


def test_project_identity_cwd_change_has_independent_worktree_observation(git_repo):
    nested = git_repo / "nested"
    nested.mkdir()
    root = derive_project_identity(str(git_repo))
    changed = derive_project_identity(str(nested))
    assert root.project_key == changed.project_key
    assert root.worktree_path == changed.worktree_path == str(git_repo)


def test_estimate_is_api_equivalent_not_reported_charge():
    estimate, snapshot = estimate_cost(
        "gpt-6-luna",
        {"input": 1_000_000, "cache_write": 0, "cache_read": 0, "output": 0},
    )
    assert estimate == 0.1
    assert snapshot["basis"] == "api_equivalent_list_price"


@pytest.mark.django_db
def test_omp_ingestion_is_idempotent_stores_private_redacted_transcript_and_links(
    tmp_path, settings
):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    cwd = tmp_path / "untracked"
    cwd.mkdir()
    native = tmp_path / "native.jsonl"
    native.write_text(
        "\n".join(
            [
                '{"type":"title","title":"Title before session"}',
                f'{{"type":"session","id":"omp-1","cwd":"{cwd}"}}',
                '{"id":"event-1","type":"message","timestamp":"2026-10-04T12:00:00Z","message":{"id":"message-1","role":"assistant","model":"gpt-6-luna","usage":{"input":100,"output":20}}}',
                '{"type":"message","message":{"role":"user","content":"api_key=private"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    user = get_user_model().objects.create_user(username="history")
    conversation = ConversationSession.objects.create(
        user=user, engine_type="openai", transport_type="api", status="active"
    )
    worker = Worker.objects.create(
        session=conversation,
        bot_name="bot",
        title="omp: history",
        function="agent:orca",
    )

    session = ingest_omp_file(native, worker=worker)
    again = ingest_omp_file(native, worker=worker)

    assert session.pk == again.pk
    [event] = AgentUsageEvent.objects.all()
    assert event.source_event_key == "event-1"
    assert event.reported_usd is None
    assert event.estimated_usd == Decimal("0.00002000")
    assert event.cost_status == "estimated"
    assert AgentUsageEvent.objects.count() == 1
    assert (
        AgentSessionThreadLink.objects.get(session=session).conversation == conversation
    )
    artifacts = AgentTranscriptArtifact.objects.filter(session=session).order_by(
        "classification"
    )
    assert list(artifacts.values_list("classification", flat=True)) == [
        "raw",
        "redacted",
    ]
    redacted = artifacts.get(classification="redacted")
    assert b"[REDACTED]" in default_storage.open(redacted.storage_key, "rb").read()
