"""Synthetic offline timing evidence, never microphone/provider latency.

Run from repository root (parent agent only during integration):
PYTHONPATH="$PWD/ergonaut:$PWD/src" /tmp/ergo-voice-test/bin/pytest \
    -c ergonaut/pyproject.toml -q -s --disable-warnings \
    artifacts/voice-interpreter-20261114/test_native_timing.py

Every input, backend function argument and worker reply below is scripted. perf_counter
measures local replay, durable database/coordinator work and the scripted turn runner.
It says nothing about acoustic endpointing, network RTT, provider intent quality or
real task duration. The original unsafe fixture remains untouched in voice-causal-20261114.
"""

# ruff: noqa: F811, INP001, SLF001, T201 -- fixture imports/private native SDK/stdout evidence

import json
import os
from pathlib import Path
from queue import Empty
from time import perf_counter
from unittest.mock import PropertyMock
from unittest.mock import patch
from uuid import uuid4

import pytest
from asgiref.sync import async_to_sync
from asgiref.sync import sync_to_async
from celery import Celery

from django_ergo.bots import messaging
from django_ergo.bots import webhooks
from ergonaut import realtime_voice_bridge as bridge
from ergonaut.api import realtime_voice
from ergonaut.api.tests.test_realtime_voice_interpreter import arguments
from ergonaut.api.tests.test_realtime_voice_interpreter import bind
from ergonaut.api.tests.test_realtime_voice_interpreter import bot_folder  # noqa: F401
from ergonaut.api.tests.test_realtime_voice_interpreter import cook  # noqa: F401
from ergonaut.api.tests.test_realtime_voice_interpreter import drain
from ergonaut.api.tests.test_realtime_voice_interpreter import (  # noqa: F401
    held_messages,
)
from ergonaut.api.tests.test_realtime_voice_interpreter import (  # noqa: F401
    interpreter_voice,
)
from ergonaut.api.tests.test_realtime_voice_interpreter import native_events
from ergonaut.api.tests.test_realtime_voice_interpreter import native_sdk
from ergonaut.api.tests.test_realtime_voice_interpreter import (  # noqa: F401
    normal_voice,
)
from ergonaut.api.tests.test_realtime_voice_interpreter import offline_key  # noqa: F401
from ergonaut.api.tests.test_realtime_voice_interpreter import settle
from ergonaut.api.tests.test_realtime_voice_interpreter import tasks
from ergonaut.api.tests.test_realtime_voice_interpreter import use_bots  # noqa: F401
from ergonaut.api.tests.test_realtime_voice_interpreter import wire
from ergonaut.apps.bots.tasks import deliver_thread_message
from ergonaut.apps.bots.tests.fakes import say


@pytest.mark.django_db(transaction=True)
def test_synthetic_native_ingress_acceptance_and_scripted_result_timings(
    interpreter_voice, held_messages, use_bots
):
    _, _, voice = interpreter_voice
    scripted = use_bots(say("Synthetic verified outcome: cobalt-spoon."))
    marks = {}
    trace = []

    async def scenario():
        assistant = bridge.Assistant(
            str(voice.pk),
            await sync_to_async(realtime_voice.bridge_state)(voice.pk),
            None,
        )
        sdk = native_sdk(assistant)
        sdk.on(
            "function_call",
            lambda call: trace.append({"sdk_function_call": call.call_id}),
        )
        with patch.object(
            bridge.Assistant,
            "duplex_session",
            new_callable=PropertyMock,
            return_value=sdk,
        ):
            await bind(assistant, sdk)
            await sdk._update_tools(assistant.tools)
            config = sdk._session_start_event().model_dump(exclude_none=True)
            assert config["session"]["delegation"]["type"] == "responses"
            native_tools = config["session"]["delegation"]["responses"]["tools"]
            assert {tool["name"] for tool in native_tools} == {"ergo_voice_intent"}
            Path(__file__).with_name("sdk-session-config.json").write_text(
                json.dumps(config, indent=2, sort_keys=True, default=str) + "\n"
            )
            marks["synthetic_ingress"] = perf_counter()
            for event in [
                {
                    "type": "session.input_transcript.delta",
                    "delta": "STALE EARLIER TASK. ",
                    "start_ms": 1200,
                    "end_ms": 1300,
                },
                {
                    "type": "session.input_transcript.delta",
                    "delta": "Please start distinct offline task one.",
                    "start_ms": 2500,
                    "end_ms": 3000,
                },
            ]:
                wire(assistant, sdk, event)
                sdk._end_speech("user")
            events = native_events(
                arguments(text="Please start distinct offline task one.")
            )
            for event in events[:-1]:
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            marks["staged_before_completion"] = perf_counter()
            assert held_messages == []
            marks["response_completed_ingress"] = perf_counter()
            wire(assistant, sdk, events[-1])
            await assistant.flush_native_events()
            marks["durable_acceptance"] = perf_counter()
            assert len(held_messages) == 1
            queued = await sync_to_async(realtime_voice.session_out)(voice)
            Path(__file__).with_name("browser-queued-session.json").write_text(
                json.dumps(queued, indent=2, sort_keys=True, default=str) + "\n"
            )
            marks["scripted_task_start"] = perf_counter()
            await sync_to_async(messaging.deliver)(
                held_messages[0], registry=webhooks.get_registry()
            )
            marks["scripted_task_complete"] = perf_counter()
            await assistant.deliver_results(
                await sync_to_async(realtime_voice.bridge_state)(voice.pk)
            )
            await settle(assistant)
            marks["result_context_queued"] = perf_counter()
            assert any(
                "cobalt-spoon" in event.get("content", "") for event in drain(sdk)
            )

    async_to_sync(scenario)()
    [task] = tasks(voice)
    assert task.text == "Please start distinct offline task one."
    assert task.message.status == "answered"
    assert len(scripted.calls) == 1
    # Actual backend output from this disposable test DB, not a handwritten UI fixture.
    completed = realtime_voice.session_out(voice)
    Path(__file__).with_name("browser-session.json").write_text(
        json.dumps(completed, indent=2, sort_keys=True, default=str) + "\n"
    )
    origin = marks["synthetic_ingress"]
    print(
        json.dumps(
            {
                "fixture": "native-interpreter-synthetic-offline",
                "timing_source": "time.perf_counter",
                "synthetic": True,
                "provider_requests": 0,
                "audio_devices": 0,
                "milestones_seconds": {
                    key: stamp - origin for key, stamp in marks.items()
                },
                "response_completion_to_durable_acceptance_seconds": marks[
                    "durable_acceptance"
                ]
                - marks["response_completed_ingress"],
                "durable_acceptance_to_scripted_task_completion_seconds": marks[
                    "scripted_task_complete"
                ]
                - marks["durable_acceptance"],
                "scripted_task_runner_seconds": marks["scripted_task_complete"]
                - marks["scripted_task_start"],
                "queued_request_text": task.text,
                "actual_sdk_events": trace,
                "queued_tasks": len(held_messages),
                "verified_result": task.message.reply_text,
                "limits": "Scripted wire replay and local DB/runner only; not provider, hardware, or human latency.",
            },
            sort_keys=True,
        )
    )


@pytest.mark.django_db(transaction=True)
def test_native_request_crosses_real_disposable_broker_and_normal_task_handler(
    interpreter_voice, use_bots, settings
):
    broker = os.environ.get("VOICE_TEST_BROKER_URL", "")
    if not broker:
        pytest.skip("Set VOICE_TEST_BROKER_URL for the disposable Redis scenario.")
    assert broker.startswith("redis://127.0.0.1:")
    _, _, voice = interpreter_voice
    use_bots(say("Broker-backed synthetic normal result: cobalt-spoon."))
    app = Celery("voice-native-disposable", broker=broker, set_as_current=False)
    queue_name = "voice-native-" + uuid4().hex

    def publish(message_id):
        app.send_task(
            "ergonaut.deliver_thread_message",
            args=[str(message_id)],
            queue=queue_name,
            retry=False,
        )

    settings.DJANGO_ERGO = {**settings.DJANGO_ERGO, "THREAD_MESSAGE_RUNNER": publish}

    async def scenario():
        assistant = bridge.Assistant(
            str(voice.pk),
            await sync_to_async(realtime_voice.bridge_state)(voice.pk),
            None,
        )
        sdk = native_sdk(assistant)
        with patch.object(
            bridge.Assistant,
            "duplex_session",
            new_callable=PropertyMock,
            return_value=sdk,
        ):
            await bind(assistant, sdk)
            for event in native_events(
                arguments(text="Run only this broker-backed synthetic task.")
            ):
                wire(assistant, sdk, event)
            await assistant.flush_native_events()
            await settle(assistant)

    try:
        async_to_sync(scenario)()
        [task] = tasks(voice)
        with app.connection_for_read() as connection:
            queue = connection.SimpleQueue(queue_name)
            try:
                received = queue.get(block=False)
                message_id = received.payload[0][0]
                assert message_id == str(task.message_id)
                assert received.headers["task"] == "ergonaut.deliver_thread_message"
                # Run the production Celery task handler, retaining its normal thread lock.
                deliver_thread_message.run(message_id)
                received.ack()
                with pytest.raises(Empty):
                    queue.get(block=False)
            finally:
                queue.close()
                connection.default_channel.queue_delete(queue=queue_name)
        task.message.refresh_from_db()
        assert task.message.status == "answered"
        assert "cobalt-spoon" in task.message.reply_text
        print(
            json.dumps(
                {
                    "fixture": "native-real-disposable-redis-normal-handler",
                    "synthetic": True,
                    "native_parser": "GPTLiveSession._handle_event",
                    "broker": "loopback Redis, unique disposable queue",
                    "normal_handler": "ergonaut.deliver_thread_message",
                    "message_id": message_id,
                    "status": task.message.status,
                    "provider_requests": 0,
                },
                sort_keys=True,
            )
        )
    finally:
        app.close()
