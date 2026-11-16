"""Synthetic consumer-level security regressions for private exports."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.utils import timezone

from django_ergo.bots import messaging
from django_ergo.bots import orchestrator
from django_ergo.bots.registry import BotRegistry
from django_ergo.conversation.attachments import read_text
from django_ergo.conversation.attachments import save_session_file
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import PrivateExport
from django_ergo.conversation.models import SessionMessage
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.models import ThreadMessage
from django_ergo.conversation.toolkit import approval_decision
from django_ergo.conversation.toolkit import approval_pending_digest
from tests.test_bots import write_bot
from tests.test_conversation_structured import claude_engine

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def world(tmp_path, settings):
    settings.DJANGO_ERGO = {
        **getattr(settings, "DJANGO_ERGO", {}),
        "THREAD_MESSAGE_RUNNER": "tests.test_bots.record_message",
    }
    settings.MEDIA_ROOT = tmp_path / "media"
    user = get_user_model().objects.create_user(username="synthetic-export-user")
    folder = write_bot(
        tmp_path,
        "name: hub\nplugins: [{name: attachments}]\n",
        name="hub",
    )
    write_bot(
        folder,
        "name: personal\nprivate: true\nthreads: {allow_create: true}\n"
        "plugins: [{name: attachments}, {name: private_exports}]\n"
        "permissions: {call_bots: [worker]}\n",
        name="personal",
    )
    write_bot(
        folder,
        "name: worker\nthreads: {allow_create: true}\nplugins: [{name: attachments}]\n",
        name="worker",
    )
    registry = BotRegistry.discover(folder, engine_factory=claude_engine)
    personal, worker, hub = (registry.get(n) for n in ("personal", "worker", "hub"))
    source = async_to_sync(personal.root_session)(user)
    destination = async_to_sync(worker.create_session)(user, title="Approved recipient")
    sibling = async_to_sync(worker.create_session)(user, title="Unapproved sibling")
    public = async_to_sync(hub.root_session)(user)
    artifact = save_session_file(
        source, "synthetic.txt", b"synthetic selected bytes", media_type="text/plain"
    )
    secret = save_session_file(
        source, "unselected.txt", b"synthetic private bytes", media_type="text/plain"
    )
    return SimpleNamespace(
        user=user,
        registry=registry,
        personal=personal,
        worker=worker,
        hub=hub,
        source=source,
        destination=destination,
        sibling=sibling,
        public=public,
        artifact=artifact,
        secret=secret,
        ctx=personal.tool_context(source),
        plugin=personal.plugin("private_exports"),
    )


def prepared(w):
    return w.plugin.prepare(
        w.ctx, str(w.destination.id), "Exact synthetic brief.\n", [str(w.artifact.id)]
    )


def authorize(w, arguments):
    """Model the durable pending batch after the authenticated resume claim."""
    pending = [
        {
            "id": "export-tool",
            "name": "ergo_private_export",
            "input": deepcopy(arguments),
        }
    ]
    initial = SessionMessage.objects.create(session=w.source, sequence=0, role="user")
    MessageBlock.objects.create(
        message=initial,
        sequence=0,
        block_type="text",
        text="Export the synthetic brief",
    )
    assistant = SessionMessage.objects.create(
        session=w.source, sequence=1, role="assistant"
    )
    MessageBlock.objects.create(
        message=assistant,
        sequence=0,
        block_type="tool_use",
        tool_use_id="export-tool",
        tool_name="ergo_private_export",
        tool_input=deepcopy(arguments),
    )
    call = StructuredCall.objects.create(
        session=w.source,
        user=w.user,
        kind="chat_reply",
        status="in_progress",
        first_sequence=0,
        last_sequence=1,
        metadata={"pending_approvals": pending},
    )
    batch = {
        "id": "synthetic-approval",
        "actor_id": str(w.user.pk),
        "call_id": str(call.pk),
        "decisions": {"export-tool": True},
        "pending_sha256": approval_pending_digest(pending),
        "shown_verified": True,
    }
    call.metadata["authenticated_approval_decisions"] = [batch]
    call.save(update_fields=["metadata"])
    decision = {key: batch[key] for key in ("id", "actor_id", "call_id")}
    decision["tool_use_id"] = "export-tool"
    return call, decision


def execute(w, arguments, decision):
    token = approval_decision.set(decision)
    try:
        return w.plugin.export(w.ctx, **arguments)
    finally:
        approval_decision.reset(token)


def effects():
    return (
        ConversationSession.objects.count(),
        ThreadMessage.objects.count(),
        ConversationAttachment.objects.count(),
        PrivateExport.objects.count(),
    )


def test_prepare_and_unauthenticated_export_have_no_effects(world):
    w = world
    before = effects()
    args = prepared(w)
    assert args["destination"] == str(w.destination.id)
    assert args["message"] == "Exact synthetic brief.\n"
    assert args["artifacts"][0]["id"] == str(w.artifact.id)
    assert effects() == before
    with pytest.raises(ValueError, match="authenticated human approval"):
        execute(w, args, None)
    assert effects() == before


def test_authenticated_export_is_exact_immutable_destination_owned_copy(world):
    w = world
    args = prepared(w)
    _, decision = authorize(w, args)
    result = execute(w, args, decision)
    delivered = ThreadMessage.objects.get(pk=result["message_id"])
    assert delivered.recipient_session_id == w.destination.id
    assert delivered.text == args["message"]
    audit = PrivateExport.objects.get(pk=result["export_id"])
    assert audit.source_id == w.source.pk
    assert audit.destination_id == w.destination.pk
    assert audit.authorization_id == decision["id"]
    assert audit.actor_id == str(w.user.pk)
    assert audit.result_id == delivered.pk
    assert audit.artifacts == args["artifacts"]
    assert audit.manifest_sha256 == approval_pending_digest(args)
    copy = audit.copies.get()
    assert copy.pk != w.artifact.pk
    assert copy.session_id == w.destination.id
    assert read_text(copy) == "synthetic selected bytes"
    files = w.worker.plugin("attachments")
    recipient = w.worker.tool_context(w.destination)
    sibling = w.worker.tool_context(w.sibling)
    assert files.file_for(recipient, str(copy.pk)).pk == copy.pk
    assert files.file_for(recipient, copy.filename).pk == copy.pk
    assert w.worker.linked_source(w.destination, str(w.source.pk)) is None
    for row in (w.artifact, w.secret):
        with pytest.raises(ValueError, match="No file"):
            files.file_for(recipient, str(row.pk))
    with pytest.raises(ValueError, match="No session"):
        files.session_for(recipient, str(w.source.pk))
    with pytest.raises(ValueError, match="No file"):
        files.file_for(sibling, str(copy.pk))
    with pytest.raises(ValueError, match="No file"):
        files.file_for(sibling, copy.filename, session_id=str(w.destination.pk))
    with pytest.raises(ValueError, match="immutable"):
        files.update(recipient, str(copy.pk), "tampered")
    assert read_text(copy) == "synthetic selected bytes"
    w.personal.plugin("attachments").update(
        w.ctx, str(w.artifact.pk), "later source revision"
    )
    assert read_text(copy) == "synthetic selected bytes"
    before = effects()
    with pytest.raises(ValueError, match="cannot be forwarded"):
        orchestrator.ergo_thread_send(
            recipient, "Reshare", thread=str(w.sibling.pk), attachments=[str(copy.pk)]
        )
    assert effects() == before
    # Even a forged historical share never grants access to the private original.
    ThreadMessage.objects.create(
        sender_session=w.source,
        recipient_session=w.destination,
        text="legacy",
        metadata={"attachments": [messaging.shared_file(w.artifact)]},
    )
    with pytest.raises(ValueError, match="No file"):
        files.file_for(recipient, str(w.artifact.pk))
    before = effects()
    with pytest.raises(ValueError, match="cannot be forwarded"):
        messaging.send(
            w.destination,
            w.sibling,
            "Forward exported copy",
            registry=w.registry,
            metadata={"forwarded": True, "attachments": [messaging.shared_file(copy)]},
        )
    assert effects() == before


@pytest.mark.parametrize(
    "attack",
    [
        "message",
        "destination",
        "manifest",
        "bytes",
        "expired",
        "new_call",
        "new_message",
        "executed",
        "unverified",
        "actor",
        "forged_batch",
        "pending_mutation",
        "tool_result",
    ],
)
def test_authenticated_export_rejects_changed_or_stale_authority(world, attack):  # noqa: C901, PLR0912 - independent authority mutations
    w = world
    args = prepared(w)
    call, decision = authorize(w, args)
    if attack == "message":
        args["message"] += "changed"
    elif attack == "destination":
        args["destination"] = str(w.sibling.pk)
    elif attack == "manifest":
        args["artifacts"][0]["filename"] = "changed.txt"
    elif attack == "bytes":
        w.personal.plugin("attachments").update(
            w.ctx, str(w.artifact.pk), "changed bytes"
        )
    elif attack == "expired":
        StructuredCall.objects.filter(pk=call.pk).update(
            created_at=timezone.now() - timedelta(minutes=16)
        )
    elif attack == "new_call":
        StructuredCall.objects.create(session=w.source, user=w.user, kind="chat_reply")
    elif attack == "new_message":
        SessionMessage.objects.create(session=w.source, sequence=2, role="user")
    elif attack == "executed":
        call.metadata["tool_invocations"] = {"export-tool": {"status": "completed"}}
        call.save(update_fields=["metadata"])
    elif attack == "unverified":
        call.metadata["authenticated_approval_decisions"][0]["shown_verified"] = False
        call.save(update_fields=["metadata"])
    elif attack == "actor":
        decision["actor_id"] = "999999"
    elif attack == "forged_batch":
        decision["id"] = "not-a-persisted-approval"
    elif attack == "pending_mutation":
        call.metadata["pending_approvals"][0]["input"]["message"] = (
            "Changed after review"
        )
        call.save(update_fields=["metadata"])
    elif attack == "tool_result":
        assistant = w.source.messages.get(sequence=1)
        MessageBlock.objects.create(
            message=assistant,
            sequence=1,
            block_type="tool_result",
            tool_result_for="export-tool",
            tool_result_content="already executed",
        )
    before = effects()
    with pytest.raises(ValueError, match="approval|content changed"):
        execute(w, args, decision)
    assert effects() == before


def test_successful_export_cannot_be_replayed(world):
    w = world
    args = prepared(w)
    _, decision = authorize(w, args)
    execute(w, args, decision)
    before = effects()
    with pytest.raises(ValueError, match="stale, replayed"):
        execute(w, args, decision)
    assert effects() == before


@pytest.mark.parametrize("operation", ["send", "forward"])
@pytest.mark.parametrize("target", ["main", "new", "existing"])
def test_private_outbound_bypass_fails_before_recipient_or_delivery_effects(
    world, operation, target
):
    w = world
    args = prepared(w)
    authorize(w, args)
    thread = str(w.destination.pk) if target == "existing" else target
    before = effects()
    operation_fn = (
        (
            lambda: orchestrator.ergo_thread_send(
                w.ctx, "leak", bot="worker", thread=thread, title="Leak", interrupt=True
            )
        )
        if operation == "send"
        else lambda: orchestrator.ergo_thread_forward(
            w.ctx, bot="worker", thread=thread, title="Leak"
        )
    )
    with pytest.raises(ValueError, match="require ergo_private_export"):
        operation_fn()
    assert effects() == before


def test_private_same_bot_send_and_legitimate_child_reply_remain_available(world):
    w = world
    local = async_to_sync(w.personal.create_session)(w.user, title="Private local")
    result = orchestrator.ergo_thread_send(
        w.ctx,
        "Local synthetic message",
        thread=str(local.pk),
        attachments=[str(w.artifact.pk)],
    )
    assert (
        ThreadMessage.objects.get(pk=result["message_id"]).recipient_session_id
        == local.pk
    )
    same_bot = orchestrator.ergo_thread_send(
        w.personal.tool_context(local),
        "Return selected private file",
        thread=str(w.source.pk),
        attachments=[str(w.artifact.pk)],
    )
    returned = ThreadMessage.objects.get(pk=same_bot["message_id"])
    assert returned.metadata["attachments"][0]["id"] == str(w.artifact.pk)
    child = async_to_sync(w.worker.create_session)(
        w.user,
        title="Requested worker",
        metadata={"started_by": str(w.source.pk), "started_by_bot": "personal"},
    )
    result = orchestrator.ergo_thread_send(
        w.worker.tool_context(child),
        "Synthetic child reply",
        bot="personal",
        thread=str(w.source.pk),
    )
    assert (
        ThreadMessage.objects.get(pk=result["message_id"]).recipient_session_id
        == w.source.pk
    )


def test_public_ordinary_attachment_sharing_is_unchanged(world):
    w = world
    artifact = save_session_file(
        w.public, "ordinary.txt", b"public synthetic brief", media_type="text/plain"
    )
    result = orchestrator.ergo_thread_send(
        w.hub.tool_context(w.public),
        "Public share",
        bot="worker",
        thread=str(w.destination.pk),
        attachments=[str(artifact.pk)],
    )
    delivered = ThreadMessage.objects.get(pk=result["message_id"])
    assert delivered.recipient_session_id == w.destination.pk
    assert (
        w.worker.plugin("attachments")
        .file_for(w.worker.tool_context(w.destination), str(artifact.pk))
        .pk
        == artifact.pk
    )


def test_authenticated_export_runs_once_through_claimed_dispatcher(world):
    import json

    from django_ergo.conversation.tool_dispatch import dispatch_tool_once

    w = world
    args = prepared(w)
    call, decision = authorize(w, args)
    token = approval_decision.set(decision)
    try:
        result, error = dispatch_tool_once(
            call, "export-tool", "ergo_private_export", args, w.plugin.toolkits(w.ctx)
        )
        assert not error, result
        receipt = PrivateExport.objects.get(pk=json.loads(result)["export_id"])
        assert receipt.result.text == args["message"]
        assert read_text(receipt.copies.get()) == "synthetic selected bytes"
        before = effects()
        assert dispatch_tool_once(
            call, "export-tool", "ergo_private_export", args, w.plugin.toolkits(w.ctx)
        ) == (result, False)
        assert effects() == before
    finally:
        approval_decision.reset(token)


@pytest.mark.parametrize("viewer", ["destination", "sibling"])
@pytest.mark.parametrize("consumer", ["telegram", "orca", "local_shell"])
def test_official_file_transfer_tools_cannot_forward_snapshots(
    world, monkeypatch, viewer, consumer
):
    from django_ergo.bots.tools import FunctionToolkit
    from django_ergo.plugins.local_shell import LocalShellPlugin
    from django_ergo.plugins.orca import OrcaPlugin
    from django_ergo.plugins.telegram import TelegramPlugin

    w = world
    args = prepared(w)
    _, decision = authorize(w, args)
    result = execute(w, args, decision)
    snapshot = PrivateExport.objects.get(pk=result["export_id"]).copies.get()
    ctx = w.worker.tool_context(getattr(w, viewer))
    if consumer == "telegram":
        plugin = TelegramPlugin(w.worker, {})
        plugin.on_load()
        monkeypatch.setattr(
            plugin, "chat_link", lambda session: {"chat_id": "synthetic"}
        )
        toolkit = FunctionToolkit(plugin._tools(), ctx)

        def transfer():
            return toolkit.execute_tool(
                "ergo_telegram_file",
                {
                    "attachment_id": str(snapshot.pk),
                    "session_id": str(w.destination.pk),
                },
            )
    elif consumer == "orca":
        plugin = OrcaPlugin(w.worker, {})

        def transfer():
            return plugin.upload(
                ctx,
                "synthetic-worktree",
                [str(snapshot.pk)],
                session_id=str(w.destination.pk),
            )
    else:
        plugin = LocalShellPlugin(w.worker, {})

        def transfer():
            return plugin._upload_file(ctx, str(snapshot.pk), str(w.destination.pk))

    with pytest.raises(ValueError, match="No file|cannot be forwarded"):
        transfer()


def test_forwarded_snapshot_refused_before_creating_destination(world, monkeypatch):
    w = world
    args = prepared(w)
    _, decision = authorize(w, args)
    result = execute(w, args, decision)
    snapshot = PrivateExport.objects.get(pk=result["export_id"]).copies.get()
    monkeypatch.setattr(
        orchestrator,
        "user_message",
        lambda session: {
            "text": "Synthetic forwarded request",
            "author": {"kind": "django_user", "id": str(w.user.pk)},
            "origin": {},
            "attachments": [messaging.shared_file(snapshot)],
        },
    )
    before = effects()
    with pytest.raises(ValueError, match="cannot be forwarded"):
        orchestrator.ergo_thread_forward(
            w.worker.tool_context(w.destination),
            thread="new",
            title="Refused",
        )
    assert effects() == before
