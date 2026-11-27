"""App integration against an offline controller and trusted fixture executor.

This does not exercise or claim container isolation.
"""

import hashlib
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone

from django_ergo.analysis.controller import Controller
from django_ergo.analysis.models import AnalysisDataset
from django_ergo.analysis.models import AnalysisJob
from django_ergo.analysis.models import AnalysisWorkspace
from django_ergo.analysis.runner import RunnerUnavailableError
from django_ergo.conversation.models import ConversationAttachment
from django_ergo.conversation.models import ConversationSession
from django_ergo.plugins.analysis_service import AnalysisService

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(tmp_path, monkeypatch, settings):
    from django_ergo.analysis import executor
    from tests.test_analysis_runtime import Runtime

    monkeypatch.setattr(
        "django_ergo.plugins.analysis_service.closed_to", lambda bot, session: False
    )
    monkeypatch.setattr(
        "django_ergo.plugins.attachments.closed_to",
        lambda bot, session: session.bot_name == "private",
    )
    settings.MEDIA_ROOT = str(tmp_path / "attachments")
    user = get_user_model().objects.create(username="analysis-test")
    session = ConversationSession.objects.create(
        user=user, engine_type="openai", transport_type="http"
    )
    ctx = SimpleNamespace(user=user, session=session, bot=SimpleNamespace())

    class FixtureRuntime(Runtime):
        def __init__(self):
            super().__init__()
            self.results = {}

        def launch(self, job, workspace, request):
            self.launches += 1
            root = tmp_path / workspace
            root.mkdir(exist_ok=True)
            monkeypatch.setattr(executor, "ROOT", root)
            # Only known fixture requests, never supplied untrusted code.
            self.results[job] = {"ok": True, "result": executor.run(request)}
            self.pods[job] = "succeeded"

        def result(self, job):
            return self.results[job]

    runtime = FixtureRuntime()
    controller = Controller(str(tmp_path / "ledger"), runtime)
    client = SimpleNamespace(call=controller.dispatch)
    service = AnalysisService(ctx, client=client)
    return service, runtime, controller, ctx


def attachment(session, name, data):
    row = ConversationAttachment(
        session=session,
        filename=name,
        source="user",
        kind="document",
        media_type="application/octet-stream",
        size=len(data),
    )
    row.file.save(name, ContentFile(data), save=False)
    row.save()
    return row


def result(service, submitted):
    return service.status(submitted["job"])["response"]["result"]


def test_disabled_and_chat_creation_allocate_nothing(setup, settings):
    _, _, _, ctx = setup
    assert AnalysisWorkspace.objects.count() == 0
    settings.ERGO_ANALYSIS_ENABLED = False
    with pytest.raises(RunnerUnavailableError, match="disabled"):
        AnalysisService(ctx)
    assert AnalysisWorkspace.objects.count() == 0


def test_lazy_workspace_select_and_cursor_binding(setup):
    service, _, _, ctx = setup
    source = attachment(ctx.session, "test.csv", b"x,y\n3,c\n1,a\n2,b\n")
    imported = service.import_attachment(str(source.id))
    assert AnalysisWorkspace.objects.count() == 1
    dataset = result(
        service, service.submit({"op": "dataset", "file": imported["file"]})
    )
    assert dataset["rows"] == 3
    query = {"columns": ["x", "y"], "limit": 2}
    first = result(service, service.select(dataset["dataset"], query))
    assert first["rows"] == [[1, "a"], [2, "b"]]
    assert first["total_rows"] == 3
    assert first["cursor"]
    second = result(service, service.select(dataset["dataset"], query, first["cursor"]))
    assert second["rows"] == [[3, "c"]]
    assert second["cursor"] is None
    with pytest.raises(PermissionError, match="Cursor"):
        service.select(
            dataset["dataset"], {"columns": ["x"], "limit": 2}, first["cursor"]
        )
    with pytest.raises(signing.BadSignature):
        service.select(dataset["dataset"], query, first["cursor"] + "tamper")
    assert AnalysisDataset.objects.count() == 1


def test_every_handle_is_bound_to_session_even_same_user(setup):
    service, _, _, ctx = setup
    job = service.submit({"op": "list"})
    workspace = service.workspace()
    other = ConversationSession.objects.create(
        user=ctx.user, engine_type="openai", transport_type="http"
    )
    outsider = AnalysisService(
        SimpleNamespace(user=ctx.user, session=other, bot=ctx.bot),
        client=service.client,
    )
    with pytest.raises(PermissionError):
        outsider.owned_workspace(workspace.id)
    with pytest.raises(PermissionError):
        outsider.status(job["job"])
    ds = AnalysisDataset.objects.create(
        workspace=workspace,
        job=AnalysisJob.objects.get(pk=job["job"]),
        filename="dataset.parquet",
        version="a" * 64,
        schema=[],
        rows=0,
    )
    with pytest.raises(PermissionError):
        outsider.select(str(ds.id), {"columns": ["x"]})
    user2 = get_user_model().objects.create(username="other")
    with pytest.raises(PermissionError):
        AnalysisService(
            SimpleNamespace(user=user2, session=ctx.session, bot=ctx.bot),
            client=service.client,
        )


def test_attachment_acls_and_independent_streamed_exports(setup, monkeypatch):
    service, _, _, ctx = setup
    other = ConversationSession.objects.create(
        user=ctx.user, engine_type="openai", transport_type="http"
    )
    source = attachment(other, "source.csv", b"x\n1\n")
    imported = service.import_attachment(str(source.id))
    exported = service.export_file(imported["file"], "result.csv")
    output = ConversationAttachment.objects.get(pk=exported["attachment"])
    assert output.session_id == ctx.session.id
    assert output.sha256 == hashlib.sha256(b"x\n1\n").hexdigest()
    with output.file.open("rb") as stream:
        assert stream.read() == b"x\n1\n"
    service.workspace().delete()
    assert ConversationAttachment.objects.filter(pk=output.id).exists()
    other.bot_name = "private"
    other.save(update_fields=["bot_name"])
    with pytest.raises(ValueError, match="No file"):
        service.import_attachment(str(source.id))
    user2 = get_user_model().objects.create(username="other")
    foreign_session = ConversationSession.objects.create(
        user=user2, engine_type="openai", transport_type="http"
    )
    forbidden = attachment(foreign_session, "no.csv", b"x\n2\n")
    with pytest.raises(ValueError, match="No file"):
        service.import_attachment(str(forbidden.id))


def test_attachment_stream_actual_size_enforced(setup, monkeypatch):
    service, _, _, ctx = setup
    row = attachment(ctx.session, "lie.csv", b"123456789")
    row.size = 1
    row.save(update_fields=["size"])
    monkeypatch.setattr("django_ergo.plugins.analysis_service.MAX_FILE_BYTES", 8)
    with pytest.raises(ValueError, match="stream exceeds"):
        service.import_attachment(str(row.id))


def test_cleanup_preserves_export_and_retains_active_receipts(
    setup, monkeypatch, settings
):
    from datetime import timedelta

    service, runtime, controller, ctx = setup
    source = attachment(ctx.session, "a.csv", b"x\n1\n")
    imported = service.import_attachment(str(source.id))
    exported = service.export_file(imported["file"])
    workspace = service.workspace()
    monkeypatch.setattr(
        "django_ergo.management.commands.cleanup_analysis.configured_client",
        lambda: service.client,
    )
    settings.ERGO_ANALYSIS_ENABLED = True
    stale = timezone.now() - timedelta(days=31)
    AnalysisWorkspace.objects.filter(pk=workspace.id).update(touched_at=stale)
    call_command("cleanup_analysis")
    assert AnalysisJob.objects.filter(workspace=workspace).exists()
    with controller.db:
        controller.db.execute("UPDATE workspaces SET touched=0,lease_until=0")
    call_command("cleanup_analysis")
    workspace.refresh_from_db()
    assert workspace.retired is True
    assert not AnalysisJob.objects.filter(workspace=workspace).exists()
    assert ConversationAttachment.objects.filter(pk=exported["attachment"]).exists()


def test_tools_registered_without_eager_provisioning(setup):
    from django_ergo.bots.plugins import resolve_plugin_class

    service, _, _, ctx = setup
    plugin = resolve_plugin_class("analysis")(ctx.bot, {})
    toolkits = plugin.toolkits(ctx)
    assert len(toolkits) == 1
    assert len(plugin._tools()) == 7
    assert AnalysisWorkspace.objects.count() == 0


def test_import_honors_configured_attachment_cross_session_policy(setup):
    from django_ergo.plugins.attachments import AttachmentsPlugin

    service, _, _, ctx = setup
    configured = AttachmentsPlugin(ctx.bot, {"other_sessions": False})
    configured.on_load()
    ctx.bot.plugin = lambda name: configured if name == "attachments" else None
    other = ConversationSession.objects.create(
        user=ctx.user, engine_type="openai", transport_type="http"
    )
    row = attachment(other, "restricted.csv", b"x\n1\n")
    with pytest.raises(ValueError, match="own session"):
        service.import_attachment(str(row.id))


def test_ambiguous_app_submission_retries_same_identity(setup):
    service, runtime, _, _ = setup
    real_call = service.client.call
    lost = [True]

    def call(body):
        value = real_call(body)
        if lost[0]:
            lost[0] = False
            message = "fixture response lost after acceptance"
            raise RunnerUnavailableError(message)
        return value

    service.client = SimpleNamespace(call=call)
    submitted = service.submit({"op": "list"})
    assert submitted["state"] == "submitting"
    service.status(submitted["job"])
    completed = service.status(submitted["job"])
    assert completed["state"] == "succeeded"
    assert runtime.launches == 1


def test_library_skills_load_analysis_without_provisioning(tmp_path):
    from django_ergo.bots.runtime import Bot

    folder = tmp_path / "bot"
    folder.mkdir()
    (folder / "agents.md").write_text("You analyze data.")
    (folder / "bot.yaml").write_text(
        "name: analyst\nskills: {include: [data-analysis, calculations-and-scripting]}\n"
    )
    bot = Bot.load(folder)
    assert bot.plugin("analysis") is not None
    assert bot.plugin("attachments") is not None
    skills = {s.name: s for s in bot.skill_defs}
    assert skills["data-analysis"].requires == ["analysis", "attachments"]
    assert skills["calculations-and-scripting"].requires == ["analysis"]
    assert AnalysisWorkspace.objects.count() == 0


def test_import_checks_source_checksum_without_parsing_in_app(setup):
    service, _, _, ctx = setup
    source = attachment(ctx.session, "checksum.csv", b"x\n1\n")
    source.sha256 = "a" * 64
    source.save(update_fields=["sha256"])
    with pytest.raises(ValueError, match="checksum mismatch"):
        service.import_attachment(str(source.id))


def test_private_export_copies_cannot_be_laundered_through_workspace(setup):
    from django_ergo.conversation.models import PrivateExport
    from django_ergo.conversation.models import StructuredCall

    service, _, _, ctx = setup
    original = ConversationSession.objects.create(
        user=ctx.user, engine_type="openai", transport_type="http"
    )
    receipt = PrivateExport.objects.create(
        source=original,
        destination=ctx.session,
        call=StructuredCall.objects.create(kind="fixture", user=ctx.user),
    )
    copy = attachment(ctx.session, "protected.csv", b"x\n1\n")
    copy.private_export = receipt
    copy.save(update_fields=["private_export"])
    with pytest.raises(PermissionError, match="Private export copies"):
        service.import_attachment(str(copy.id))
    assert AnalysisWorkspace.objects.count() == 0


def test_source_protection_rechecked_without_timestamp_change(setup, monkeypatch):
    from django_ergo.conversation.models import PrivateExport
    from django_ergo.conversation.models import StructuredCall

    service, _, _, ctx = setup
    original = ConversationSession.objects.create(
        user=ctx.user, engine_type="openai", transport_type="http"
    )
    receipt = PrivateExport.objects.create(
        source=original,
        destination=ctx.session,
        call=StructuredCall.objects.create(kind="fixture", user=ctx.user),
    )
    source = attachment(ctx.session, "newly-protected.csv", b"x\n1\n")
    original_transfer = service.transfer

    from contextlib import contextmanager

    @contextmanager
    def protect_after_initial_lookup():
        # QuerySet.update deliberately bypasses auto_now, so version checks alone
        # cannot enforce a newly applied no-forward restriction.
        ConversationAttachment.objects.filter(pk=source.pk).update(
            private_export=receipt
        )
        with original_transfer() as acquired:
            yield acquired

    monkeypatch.setattr(service, "transfer", protect_after_initial_lookup)
    with pytest.raises(ValueError, match="changed during import"):
        service.import_attachment(str(source.id))
    assert AnalysisJob.objects.count() == 0


@pytest.mark.parametrize("operation", ["job", "dataset", "workspace"])
def test_noncanonical_ids_rejected_before_orm_lookup(setup, operation):
    from django_ergo.analysis.protocol import ProtocolError

    service, _, _, _ = setup
    callbacks = {
        "job": lambda: service.status("../../not-a-job"),
        "dataset": lambda: service.select("../../not-a-dataset", {}),
        "workspace": lambda: service.owned_workspace("../../not-a-workspace"),
    }
    with pytest.raises(ProtocolError, match="identifier"):
        callbacks[operation]()


def test_app_rejects_forged_dataset_before_persisting_handle(setup, monkeypatch):
    from django_ergo.analysis.protocol import ProtocolError

    service, runtime, _, ctx = setup
    source = attachment(ctx.session, "test.csv", b"x\n1\n")
    imported = service.import_attachment(str(source.id))
    submitted = service.submit({"op": "dataset", "file": imported["file"]})
    runtime.results[submitted["job"]]["result"]["schema"] = [
        {"name": "x", "type": {"untrusted": "object"}}
    ]
    with pytest.raises(ProtocolError, match="Malformed dataset"):
        service.status(submitted["job"])
    assert not AnalysisDataset.objects.exists()


def test_ambiguous_submission_can_cancel_without_launching(setup, monkeypatch):
    service, runtime, _, _ = setup
    real_call = service.client.call

    def uncertain(body):
        if body["action"] == "submit":
            message = "Fixture connection lost before submit"
            raise RunnerUnavailableError(message)
        return real_call(body)

    monkeypatch.setattr(service.client, "call", uncertain)
    submitted = service.submit({"op": "list"})
    assert submitted["state"] == "submitting"
    assert service.status(submitted["job"], cancel=True)["state"] == "cancelled"
    assert runtime.launches == 0


@pytest.mark.parametrize(
    "operation", ["submit", "status", "cancel", "select", "import", "export"]
)
def test_revoked_session_blocks_every_operation(setup, monkeypatch, operation):
    import uuid

    service, runtime, _, _ = setup
    monkeypatch.setattr(
        "django_ergo.plugins.analysis_service.closed_to", lambda bot, session: True
    )
    callbacks = {
        "submit": lambda: service.submit({"op": "list"}),
        "status": lambda: service.status(str(uuid.uuid4())),
        "cancel": lambda: service.status(str(uuid.uuid4()), cancel=True),
        "select": lambda: service.select(str(uuid.uuid4()), {}),
        "import": lambda: service.import_attachment(str(uuid.uuid4())),
        "export": lambda: service.export_file("file.csv"),
    }
    with pytest.raises(PermissionError, match="authorized analysis session"):
        callbacks[operation]()
    assert runtime.launches == 0
    assert not AnalysisWorkspace.objects.exists()
