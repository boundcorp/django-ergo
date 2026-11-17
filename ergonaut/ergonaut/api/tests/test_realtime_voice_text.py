"""Offline alternate protocol: real DB, normal handler/tools/approvals and installed SDK."""

# ruff: noqa: F811 -- shared existing fixtures

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import close_old_connections
from django_ergo.bots import orchestrator
from django_ergo.conversation.models import StructuredCall, ThreadMessage
from ninja.errors import HttpError

from ergonaut.api import realtime_voice
from ergonaut.api.tests.test_realtime_voice_budget import bot_folder, cook, held_messages, use_bots  # noqa: F401
from ergonaut.api.tests.test_realtime_voice_execution import deliver, normal_voice, post  # noqa: F401
from ergonaut.apps.bots.models import RealtimeVoiceSession
from ergonaut.apps.bots.tests.fakes import say, tool_call
from ergonaut.realtime_voice_text import availability, begin_utterance, claim_output, fail_utterance, finish_utterance


@pytest.fixture
def text_voice(normal_voice, settings, monkeypatch):
    chat, target, _ = normal_voice
    monkeypatch.setenv("OPENAI_API_KEY", "offline-never-contact-provider")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    settings.VOICE_TRANSCRIPTION_ENABLED = True
    settings.VOICE_TRANSCRIPTION_MODEL = "gpt-transcribe"
    settings.REALTIME_VOICE_TEXT_AGENT_ENABLED = True
    settings.REALTIME_VOICE_TEXT_AGENT_TTS_MODEL = "tts-1"
    settings.REALTIME_VOICE_TEXT_AGENT_TTS_VOICE = "alloy"
    settings.REALTIME_VOICE_THREAD_EXECUTION_AUTHORIZATIONS[str(chat.pk)].update(
        unlimited_intentional_requests=True,
        followup_costs_approved=True,
        followup_approval_reference="offline-normal-followups",
    )
    settings.REALTIME_VOICE_TEXT_AGENT_MEDIA_AUTHORIZATIONS = {
        str(chat.pk): {
            "owner": chat.user.get_username(),
            "bot": chat.bot_name,
            "stt_model": "gpt-transcribe",
            "tts_model": "tts-1",
            "tts_voice": "alloy",
            "stt_tts_costs_approved": True,
            "approval_reference": "offline-separate-media-consent",
        }
    }
    result = realtime_voice.create_session(
        SimpleNamespace(auth=chat.user), realtime_voice.CreateIn(chat_id=str(chat.pk), mode="text_agent")
    )
    voice = RealtimeVoiceSession.objects.get(pk=result["id"])
    assert realtime_voice.claim_bridge(voice.pk, "text-claim")
    return chat, target, voice


def submit(voice, identity, text="Check the disposable task code.", claim="text-claim"):
    begin_utterance(voice.pk, claim, identity)
    return finish_utterance(voice.pk, claim, identity, text)


@pytest.mark.django_db(transaction=True)
def test_media_grant_is_separate_and_native_remains_default(text_voice, settings):
    chat, _, voice = text_voice
    settings.REALTIME_VOICE_TEXT_AGENT_MEDIA_AUTHORIZATIONS = {}
    assert not availability(chat, chat.user)["enabled"]
    request = SimpleNamespace(auth=chat.user)
    with pytest.raises(HttpError, match="separate reviewed STT"):
        realtime_voice.create_session(request, realtime_voice.CreateIn(chat_id=str(chat.pk), mode="text_agent"))
    native = realtime_voice.create_session(request, realtime_voice.CreateIn(chat_id=str(chat.pk)))
    assert native["mode"] == "native"
    assert native["state"]["normal_thread_authorization"]["approval_reference"] == "disposable-offline-policy-only"
    assert native["state"]["normal_thread_authorization"]["unlimited_intentional_requests"] is True
    with pytest.raises(ValueError, match="separate reviewed STT"):
        begin_utterance(voice.pk, "text-claim", "revoked")
    assert not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "field,value", [("stt_model", "different"), ("tts_voice", "different"), ("owner", "other"), ("bot", "other")]
)
def test_media_scope_and_models_fail_closed(text_voice, settings, field, value):
    chat, _, voice = text_voice
    settings.REALTIME_VOICE_TEXT_AGENT_MEDIA_AUTHORIZATIONS[str(chat.pk)][field] = value
    assert not realtime_voice.config(SimpleNamespace(auth=chat.user), str(chat.pk))["text_agent"]["enabled"]
    with pytest.raises(ValueError):
        submit(voice, "wrong-scope")
    assert not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_final_identity_not_captions_creates_one_exact_normal_request(text_voice, use_bots, held_messages):
    chat, _, voice = text_voice
    scripted = use_bots(say("The disposable code is cobalt-spoon."))
    realtime_voice.record_event(voice.pk, "native-caption", "user", "Do NOT execute this caption.")
    assert held_messages == []
    root = submit(voice, "utterance-one", "What is the current task code?")
    assert submit(voice, "utterance-one", "What is the current task code?").pk == root.pk
    with pytest.raises(ValueError, match="different text"):
        submit(voice, "utterance-one", "Conflicting replay must never execute.")
    assert held_messages == [str(root.pk)]
    assert root.text == "What is the current task code?"
    assert root.recipient_session_id == chat.pk and root.sender_session_id is None
    assert root.metadata["message_author"]["ref"] == str(chat.user_id)
    assert "turn_budget" not in root.metadata
    assert not chat.messages.filter(provenance__voice_session_id=str(voice.pk)).exists()
    deliver(root)
    assert root.status == "answered" and "cobalt-spoon" in root.reply_text
    assert "Current task code is cobalt-spoon" in str(scripted.calls[0])
    assert "Do NOT execute this caption" not in str(scripted.calls[0])


@pytest.mark.django_db(transaction=True)
def test_concurrent_replay_and_distinct_turns_have_no_lifetime_cap(text_voice, held_messages):
    _, _, voice = text_voice
    begin_utterance(voice.pk, "text-claim", "concurrent")

    def finalize():
        close_old_connections()
        try:
            return finish_utterance(voice.pk, "text-claim", "concurrent", "One identified request").pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(lambda _: finalize(), range(2))
    assert first == second
    for index in range(6):
        submit(voice, f"later-{index}", f"Distinct finalized turn {index}")
    assert len(held_messages) == voice.delegations.count() == 7
    assert len(set(held_messages)) == 7


@pytest.mark.django_db(transaction=True)
def test_end_preserves_accepted_work_but_late_stt_and_stale_claims_cannot_admit(text_voice, use_bots):
    _, _, voice = text_voice
    use_bots(say("The accepted task survived hangup."))
    root = submit(voice, "accepted")
    begin_utterance(voice.pk, "text-claim", "late")
    with pytest.raises(ValueError, match="no longer active"):
        begin_utterance(voice.pk, "replaced-claim", "stale")
    realtime_voice.end_voice_session(voice.pk)
    with pytest.raises(ValueError, match="no longer active"):
        finish_utterance(voice.pk, "text-claim", "late", "Must not execute after End")
    fail_utterance(voice.pk, "text-claim", "late")
    deliver(root)
    restored = realtime_voice.session_out(voice)
    assert restored["status"] == "ended" and voice.delegations.count() == 1
    assert any("survived hangup" in event["text"] for event in restored["events"] if event["kind"] == "result")
    assert finish_utterance(voice.pk, "text-claim", "accepted", "Check the disposable task code.").pk == root.pk
    assert not voice.native_ledger.filter(namespace="text_turn", pending=True).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("approve", [False, True])
def test_alternate_uses_real_normal_approval_and_child_identity(text_voice, use_bots, monkeypatch, client, approve):
    chat, target, voice = text_voice
    from ergonaut.realtime_voice_outbox import thread_update_event
    from ergonaut.realtime_voice_text import pending_outputs

    monkeypatch.setattr(orchestrator.ergo_thread_send.__bot_tool__, "requires_approval", True)
    use_bots(
        tool_call("ergo_thread_send", {"thread": str(target.pk), "message": "Approved disposable child"}),
        say("Applied the normal approval decision."),
        say("The approved child completed."),
    )
    root = submit(voice, "approval", "Ask that existing thread, subject to normal approval.")
    deliver(root)
    assert root.status == "waiting" and not ThreadMessage.objects.filter(sender_session=chat).exists()
    [waiting] = pending_outputs(voice.pk)
    assert waiting["message_id"] == str(root.pk) and waiting["status"] == "waiting"
    assert claim_output(voice.pk, "text-claim", thread_update_event(voice.pk, waiting), waiting)
    assert pending_outputs(voice.pk) == []
    call = chat.structured_calls.get()
    ids = [item["id"] for item in call.metadata["pending_approvals"]]
    endpoint = f"/api/sessions/{chat.pk}/approvals"
    assert post(client, endpoint, {"approve": True, "approval_ids": ["stale"]}).status_code == 409
    assert post(client, endpoint, {"approve": approve, "approval_ids": ids}).status_code == 200
    children = ThreadMessage.objects.filter(sender_session=chat, recipient_session=target)
    assert children.count() == int(approve)
    if approve:
        assert children.get().metadata["voice_event_id"] == root.metadata["voice_event_id"]
        assert children.get().metadata["parent_thread_message"] == str(root.pk)
        child = children.get()
        deliver(child)
        child_outcome = next(row for row in pending_outputs(voice.pk) if row["message_id"] == str(child.pk))
        assert child_outcome["update_id"] == f"thread:{child.pk}:answered"
        assert child_outcome["event_id"] == root.metadata["voice_event_id"]
    root_outcome = next(row for row in pending_outputs(voice.pk) if row["message_id"] == str(root.pk))
    assert root_outcome["status"] == "answered" and "update_id" not in root_outcome
    assert post(client, endpoint, {"approve": True, "approval_ids": ids}).status_code == 409


@pytest.mark.django_db(transaction=True)
def test_native_and_text_admission_cannot_mix(text_voice, normal_voice):
    from ergonaut.realtime_voice_interpreter import _live_error

    _, _, voice = text_voice
    assert "cannot admit work" in _live_error(voice, "text-claim")
    with pytest.raises(ValueError, match="Native delegation"):
        realtime_voice.delegate(voice.pk, "native", "Must not execute")
    _, _, native = normal_voice
    assert realtime_voice.claim_bridge(native.pk, "native-claim")
    with pytest.raises(ValueError, match="no longer active"):
        begin_utterance(native.pk, "native-claim", "text-in-native")
    assert not voice.delegations.exists() and not native.delegations.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ending", ["success", "provider_error", "late_after_end"])
def test_installed_sdk_batch_stt_uses_existing_helper_and_never_submits_failed_or_late_audio(
    text_voice, monkeypatch, ending
):
    pytest.importorskip("livekit.agents")
    from livekit import rtc
    from livekit.agents import APIConnectOptions, APIError

    from ergonaut import realtime_voice_text_bridge as bridge

    _, _, voice = text_voice
    response = SimpleNamespace(text="SDK-finalized disposable speech", usage=None)

    async def transcribe(*args, **kwargs):
        assert args[0][:4] == b"RIFF" and kwargs["model"] == "gpt-transcribe"
        if ending == "provider_error":
            raise RuntimeError("secret provider detail must not be relayed")
        if ending == "late_after_end":
            await sync_to_async(realtime_voice.end_voice_session)(voice.pk)
        return response

    monkeypatch.setattr(bridge, "openai_transcription", transcribe)

    async def scenario():
        recognizer = bridge.OriginatingThreadSTT(str(voice.pk), "text-claim")
        frame = rtc.AudioFrame(data=bytes(4800), sample_rate=24000, num_channels=1, samples_per_channel=2400)
        if ending == "success":
            result = await recognizer.recognize(buffer=frame, conn_options=APIConnectOptions(max_retry=0))
            assert result.alternatives[0].text == response.text
        else:
            with pytest.raises(APIError, match="Speech could not be submitted"):
                await recognizer.recognize(buffer=frame, conn_options=APIConnectOptions(max_retry=0))
        await recognizer.aclose()

    async_to_sync(scenario)()
    assert voice.delegations.count() == int(ending == "success")
    assert not voice.native_ledger.filter(namespace="text_turn", pending=True).exists()
    assert StructuredCall.objects.filter(kind="voice_transcription").count() == int(ending != "provider_error")


@pytest.mark.django_db(transaction=True)
def test_verified_result_is_silent_until_gap_correlated_once_and_not_replayed(text_voice, use_bots):
    pytest.importorskip("livekit.agents")
    from ergonaut.realtime_voice_text_bridge import TextAssistant

    _, _, voice = text_voice
    use_bots(say("Verified result: cobalt-spoon."))
    root = submit(voice, "result")
    deliver(root)

    async def scenario():
        recognizer = SimpleNamespace(pending=0)
        assistant = TextAssistant(str(voice.pk), "text-claim", None, None)
        assistant.recognizer = recognizer
        handle = SimpleNamespace(interrupted=False, exception=lambda: None)
        said = []

        class Output:
            def say(self, text, **_):
                said.append(text)
                return handle

        async def played(*_):
            return None

        assistant.observe_output = played
        with patch.object(TextAssistant, "session", new_callable=PropertyMock, return_value=Output()):
            assistant.user_speaking = True
            await assistant.deliver_results()
            assert said == []
            assistant.user_speaking = False
            await assistant.deliver_results()
            await assistant.deliver_results()
            assert said == ["Verified result: cobalt-spoon."]

    async_to_sync(scenario)()
    [binding] = voice.native_ledger.filter(namespace="text_output")
    assert binding.data["root_event_id"] == root.metadata["voice_event_id"]
    assert binding.data["task_identity"] == f"root:{root.metadata['voice_event_id']}"
    event = voice.events.get(event_id=binding.data["result_event_id"])
    with pytest.raises(ValueError, match="no longer active"):
        claim_output(voice.pk, "stale-bridge", event, {"event_id": root.metadata["voice_event_id"]})


@pytest.mark.django_db(transaction=True)
def test_barge_in_interrupts_audio_not_accepted_work_and_keeps_new_turn(text_voice, use_bots):
    pytest.importorskip("livekit.agents")
    from ergonaut.realtime_voice_text_bridge import TextAssistant

    _, _, voice = text_voice
    use_bots(say("The first request has a verified answer."))
    answered = submit(voice, "answered")
    deliver(answered)
    pending = submit(voice, "pending", "Continue accepted work in the background.")

    async def scenario():
        assistant = TextAssistant(str(voice.pk), "text-claim", None, None)
        assistant.recognizer = SimpleNamespace(pending=0)
        begun = asyncio.Event()
        stopped = asyncio.Event()

        class Playback:
            interrupted = False

            def __await__(self):
                return stopped.wait().__await__()

            def exception(self):
                return None

        playback = Playback()

        class Output:
            def say(self, *_args, **_kwargs):
                begun.set()
                return playback

            def interrupt(self, *, force):
                assert force
                playback.interrupted = True
                stopped.set()

        with patch.object(TextAssistant, "session", new_callable=PropertyMock, return_value=Output()):
            delivery = asyncio.create_task(assistant.deliver_results())
            await asyncio.wait_for(begun.wait(), timeout=3)
            assistant.user_state_changed(SimpleNamespace(new_state="speaking"))
            await asyncio.wait_for(delivery, timeout=3)
            await sync_to_async(submit)(voice, "barge-in", "New spoken request after interruption.")

    async_to_sync(scenario)()
    pending.refresh_from_db()
    assert pending.status == "queued"
    assert voice.events.filter(kind="assistant", delivery="interrupted").count() == 1
    assert voice.delegations.count() == 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "model,input_tokens,output_tokens,expected",
    [
        ("tts-1", 0, 0, "0.00150000"),
        ("tts-1-hd", 0, 0, "0.00300000"),
        ("gpt-4o-mini-tts", 0, 0, None),
        ("gpt-4o-mini-tts", 100, 200, "0.00246000"),
    ],
)
def test_speech_cost_uses_known_billing_basis_and_missing_usage_is_unknown(
    text_voice, model, input_tokens, output_tokens, expected
):
    from decimal import Decimal

    from ergonaut.realtime_voice_text import record_synthesis

    chat, _, voice = text_voice
    record_synthesis(
        voice.pk,
        model,
        SimpleNamespace(
            characters_count=100,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            request_id="offline-synthesis",
            speech_id="identified-output",
            audio_duration=0.5,
            cancelled=True,
        ),
    )
    row = StructuredCall.objects.get(kind="voice_synthesis")
    assert row.session_id == chat.pk and row.user_id == chat.user_id
    assert row.cost_usd == (Decimal(expected) if expected else None)
    assert row.metadata["synthesis"]["unpriced"] is (expected is None)
    assert row.metadata["synthesis"]["cancelled"] is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("model", ["tts-1", "tts-1-hd"])
def test_model_specific_unsupported_voice_cannot_enable_spending(text_voice, settings, model):
    chat, _, voice = text_voice
    settings.REALTIME_VOICE_TEXT_AGENT_TTS_MODEL = model
    settings.REALTIME_VOICE_TEXT_AGENT_TTS_VOICE = "ballad"
    settings.REALTIME_VOICE_TEXT_AGENT_MEDIA_AUTHORIZATIONS[str(chat.pk)].update(tts_model=model, tts_voice="ballad")
    assert not availability(chat, chat.user)["enabled"]
    with pytest.raises(HttpError):
        realtime_voice.create_session(
            SimpleNamespace(auth=chat.user), realtime_voice.CreateIn(chat_id=str(chat.pk), mode="text_agent")
        )
    assert not voice.delegations.exists()


@pytest.mark.django_db(transaction=True)
def test_oldest_unannounced_answer_is_spoken_after_more_than_restore_window_requests(text_voice, use_bots):
    pytest.importorskip("livekit.agents")
    from ergonaut.apps.bots.models import RealtimeVoiceDelegation
    from ergonaut.realtime_voice_outbox import thread_update_event
    from ergonaut.realtime_voice_text_bridge import TextAssistant

    chat, _, voice = text_voice
    # Completed/announced history precedes an outstanding root, followed by a
    # full restore window of newer requests. Neither bounded projection contains it.
    prior = ThreadMessage.objects.bulk_create(
        [
            ThreadMessage(
                recipient_session=chat,
                text=f"Prior request {index}",
                status="answered",
                reply_text="Already announced.",
                metadata={
                    "voice_session_id": str(voice.pk),
                    "voice_event_id": f"prior-{index}",
                    "voice_execution_mode": "normal_thread",
                },
            )
            for index in range(100)
        ]
    )
    RealtimeVoiceDelegation.objects.bulk_create(
        [
            RealtimeVoiceDelegation(session=voice, event_id=row.metadata["voice_event_id"], text=row.text, message=row)
            for row in prior
        ]
    )
    for row in prior:
        task = {
            "message_id": str(row.pk),
            "event_id": row.metadata["voice_event_id"],
            "status": "answered",
            "result": row.reply_text,
        }
        assert claim_output(voice.pk, "text-claim", thread_update_event(voice.pk, task), task)
    use_bots(say("The oldest accepted request finally completed."))
    oldest = submit(voice, "oldest")
    for index in range(100):
        submit(voice, f"newer-{index}", f"Later distinct request {index}")
    deliver(oldest)
    # The public restore projection must not relabel older roots as child tasks.
    snapshot = realtime_voice.bridge_state(voice.pk, include_briefing=False)
    assert snapshot["thread_updates"] == []

    async def scenario():
        assistant = TextAssistant(str(voice.pk), "text-claim", None, None)
        assistant.recognizer = SimpleNamespace(pending=0)
        said = []

        class Output:
            def say(self, text, **_):
                said.append(text)
                return None

        async def played(*_):
            return None

        assistant.observe_output = played
        with patch.object(TextAssistant, "session", new_callable=PropertyMock, return_value=Output()):
            await assistant.deliver_results()
        assert said == ["The oldest accepted request finally completed."]

    async_to_sync(scenario)()


@pytest.mark.django_db(transaction=True)
def test_end_between_output_claim_and_authoritative_event_write_never_starts_tts(text_voice, use_bots, monkeypatch):
    pytest.importorskip("livekit.agents")
    from ergonaut import realtime_voice_text_bridge as bridge

    _, _, voice = text_voice
    use_bots(say("Verified result retained after End."))
    root = submit(voice, "ending")
    deliver(root)
    original_claim = bridge.claim_output

    def claim_then_end(*args):
        claimed = original_claim(*args)
        if claimed:
            realtime_voice.end_voice_session(voice.pk)
        return claimed

    monkeypatch.setattr(bridge, "claim_output", claim_then_end)

    async def scenario():
        assistant = bridge.TextAssistant(str(voice.pk), "text-claim", None, None)
        assistant.recognizer = SimpleNamespace(pending=0)
        said = []

        class Output:
            def say(self, text, **_):
                said.append(text)
                return None

        async def played(*_):
            return None

        assistant.observe_output = played
        with patch.object(bridge.TextAssistant, "session", new_callable=PropertyMock, return_value=Output()):
            await assistant.deliver_results()
        assert said == []

    async_to_sync(scenario)()
