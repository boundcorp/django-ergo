import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from django_ergo.conversation import lifecycle
from django_ergo.conversation import structured
from django_ergo.conversation.models import MessageBlock
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.structured import StructuredCallSpec
from django_ergo.models import LifecycleEvent
from tests.test_conversation_structured import VALID_PLAN
from tests.test_conversation_structured import ApprovalToolkit
from tests.test_conversation_structured import Plan
from tests.test_conversation_structured import _chat_session
from tests.test_conversation_structured import claude_engine
from tests.test_conversation_structured import claude_tool

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="approval-durability")


async def _paused_call(user, in_session):
    toolkit = ApprovalToolkit()
    engine = claude_engine(
        claude_tool("delete_all", {"scope": "tmp"}, tool_id="d1"),
        claude_tool("submit_output", VALID_PLAN, tool_id="s1"),
    )
    spec = StructuredCallSpec(kind="planner", response_model=Plan, toolkits=[toolkit])
    session = await _chat_session(user) if in_session else None
    paused = await structured.run_structured_call(
        spec,
        "Clean up",
        user=user,
        session=session,
        engine=engine,
        allow_approvals=True,
    )
    assert [approval.tool_use_id for approval in paused.approvals] == ["d1"]
    return spec, engine, toolkit, paused.call, session


def _approval_stages():
    return list(
        LifecycleEvent.objects.filter(category="approval")
        .order_by("observed_at")
        .values_list("stage", flat=True)
    )


def _stored_result(call_id, session):
    call = StructuredCall.objects.get(pk=call_id)
    if session is not None:
        block = MessageBlock.objects.filter(
            message__session_id=session.pk,
            block_type="tool_result",
            tool_result_for="d1",
        ).first()
        result = None if block is None else (block.tool_result_content, block.is_error)
    else:
        blocks = [
            block
            for message in call.transcript
            for block in message.get("content", [])
            if block.get("type") == "tool_result" and block.get("tool_use_id") == "d1"
        ]
        result = None if not blocks else (blocks[0]["content"], blocks[0]["is_error"])
    return call.metadata, result


@pytest.mark.parametrize("in_session", [False, True], ids=["standalone", "session"])
@pytest.mark.parametrize("approved", [True, False], ids=["approved", "declined"])
async def test_resolution_events_follow_durable_results(
    user, settings, monkeypatch, in_session, approved
):
    settings.DJANGO_ERGO = {"LIFECYCLE_EVENTS_ENABLED": True}
    spec, engine, toolkit, call, session = await _paused_call(user, in_session)
    snapshots = []
    record_event = lifecycle.record_event

    def record_with_snapshot(stage, **kwargs):
        if stage in {"resumed", "blocker_resolved"}:
            snapshots.append((stage, _stored_result(call.pk, session)))
        return record_event(stage, **kwargs)

    monkeypatch.setattr(lifecycle, "record_event", record_with_snapshot)
    done = await structured.resume_structured_call(
        spec, call, {"d1": approved}, engine=engine
    )

    assert done.ok
    assert toolkit.ran == ([{"scope": "tmp"}] if approved else [])
    expected = (
        ("deleted", False) if approved else ("The user declined this tool call.", True)
    )
    assert [stage for stage, _ in snapshots] == ["resumed", "blocker_resolved"]
    for _, (metadata, result) in snapshots:
        assert "pending_approvals" not in metadata
        assert result == expected
    assert await sync_to_async(_approval_stages)() == [
        "blocker_created",
        "answer_received",
        "resumed",
        "blocker_resolved",
    ]


@pytest.mark.parametrize("in_session", [False, True], ids=["standalone", "session"])
async def test_failed_resume_save_does_not_resolve_blocker(
    user, settings, monkeypatch, in_session
):
    settings.DJANGO_ERGO = {"LIFECYCLE_EVENTS_ENABLED": True}
    spec, engine, toolkit, call, session = await _paused_call(user, in_session)
    original_save = StructuredCall.asave

    async def fail_resume_save(self, *args, **kwargs):
        fields = kwargs.get("update_fields")
        if "pending_approvals" not in self.metadata and (
            fields is None or "metadata" in fields
        ):
            msg = "resume persistence failed"
            raise RuntimeError(msg)
        return await original_save(self, *args, **kwargs)

    monkeypatch.setattr(StructuredCall, "asave", fail_resume_save)
    with pytest.raises(RuntimeError, match="resume persistence failed"):
        await structured.resume_structured_call(spec, call, {"d1": True}, engine=engine)

    assert toolkit.ran == [{"scope": "tmp"}]
    assert await sync_to_async(_approval_stages)() == [
        "blocker_created",
        "answer_received",
    ]
    metadata, result = await sync_to_async(_stored_result)(call.pk, session)
    assert metadata["pending_approvals"][0]["id"] == "d1"
    assert result == (("deleted", False) if in_session else None)


async def test_failed_session_result_write_does_not_resolve_blocker(
    user, settings, monkeypatch
):
    settings.DJANGO_ERGO = {"LIFECYCLE_EVENTS_ENABLED": True}
    spec, engine, _, call, session = await _paused_call(user, True)

    async def fail_results(*args, **kwargs):
        msg = "result persistence failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(engine, "append_tool_results", fail_results)
    with pytest.raises(RuntimeError, match="result persistence failed"):
        await structured.resume_structured_call(spec, call, {"d1": True}, engine=engine)

    assert await sync_to_async(_approval_stages)() == [
        "blocker_created",
        "answer_received",
    ]
    metadata, result = await sync_to_async(_stored_result)(call.pk, session)
    assert metadata["pending_approvals"][0]["id"] == "d1"
    assert result is None


@pytest.mark.parametrize("in_session", [False, True], ids=["standalone", "session"])
async def test_lifecycle_failure_does_not_block_resume(
    user, settings, monkeypatch, in_session
):
    settings.DJANGO_ERGO = {"LIFECYCLE_EVENTS_ENABLED": True}
    spec, engine, toolkit, call, _ = await _paused_call(user, in_session)

    def fail_event(*args, **kwargs):
        msg = "lifecycle unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(lifecycle, "record_event", fail_event)
    done = await structured.resume_structured_call(
        spec, call, {"d1": True}, engine=engine
    )
    assert done.ok
    assert toolkit.ran == [{"scope": "tmp"}]
