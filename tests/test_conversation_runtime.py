"""Tests for the reusable conversation runtime layer."""

from __future__ import annotations

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import override_settings
from django_ergo.conversation.engine import Engine
from django_ergo.conversation.engine import EngineResponse
from django_ergo.conversation.runtime import AssistantTaskResult
from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.runtime import build_engine
from django_ergo.conversation.runtime import generate_once
from django_ergo.conversation.runtime import get_default_engine_spec
from django_ergo.conversation.runtime import run_workflow_task
from django_ergo.models import Workflow
from django_ergo.tools import tool_registry

User = get_user_model()

pytestmark = pytest.mark.django_db


class FakeEngine(Engine):
    engine_type = "fake"

    def __init__(self):
        self.generated = None

    async def start_session(self, session) -> str:
        return "fake-session"

    async def resume_session(self, session) -> None:
        return

    async def send(self, session, message: str, additional_tools=None):
        yield EngineResponse(event_type="text", text="hello ")
        yield EngineResponse(
            event_type="tool_use",
            tool_use={"id": "tool_1", "name": "dangerous", "input": {"x": 1}},
        )

    async def submit_tool_result(  # noqa: PLR0913
        self,
        session,
        tool_use_id,
        result,
        is_error=False,
        additional_tools=None,
    ):
        yield EngineResponse(event_type="done", raw={})

    def get_tools_schema(self, workflow) -> list[dict]:
        return []

    def reconstruct_messages(self, session) -> list[dict]:
        return []

    async def close_session(self, session) -> None:
        return

    def get_tool_adapter(self):
        class Adapter:
            @staticmethod
            def parse_tool_call(raw: dict) -> tuple[str, dict]:
                return raw["name"], raw["input"]

        return Adapter()

    async def generate(
        self,
        prompt: str,
        workflow=None,
        system: str | None = None,
        response_model: type | None = None,
    ):
        self.generated = {
            "prompt": prompt,
            "workflow": workflow,
            "system": system,
            "response_model": response_model,
        }
        return EngineResponse(event_type="done", text="ok")


@pytest.fixture()
def user():
    return User.objects.create_user(username="runtime-user", password="testpass")


@pytest.fixture()
def workflow():
    return Workflow.objects.create(
        name="Runtime Test Workflow",
        description="Workflow for runtime tests",
        instructions="You are a runtime test assistant.",
        tools_config={"enabled_tools": [], "approved_tools": []},
    )


class TestEngineSpec:
    @override_settings(
        DJANGO_ERGO={
            "CONVERSATION_ENGINE_TYPE": "openai",
            "CONVERSATION_TRANSPORT_TYPE": "api",
            "CONVERSATION_ENGINE_CONFIG": {"model": "gpt-4o-mini"},
        }
    )
    def test_get_default_engine_spec_from_settings(self):
        spec = get_default_engine_spec()
        assert spec == EngineSpec(
            engine_type="openai",
            transport_type="api",
            config={"model": "gpt-4o-mini"},
        )

    def test_build_engine(self):
        engine = build_engine(EngineSpec(engine_type="openai", config={"api_key": "x"}))
        assert engine.engine_type == "openai"
        assert engine.api_key == "x"


class TestGenerateOnce:
    def test_generate_once_delegates_to_engine(self):
        engine = FakeEngine()

        result = async_to_sync(generate_once)(
            "hello",
            engine=engine,
            system="system prompt",
        )

        assert result.text == "ok"
        assert engine.generated["prompt"] == "hello"
        assert engine.generated["system"] == "system prompt"


class TestRunWorkflowTask:
    def test_run_workflow_task_collects_text_and_approvals(self, user, workflow):
        engine = FakeEngine()
        original_tool = tool_registry._tools.get("dangerous")
        original_func = tool_registry._tool_functions.get("dangerous")
        tool_registry._tools["dangerous"] = type(
            "ToolConfigStub",
            (),
            {"requires_approval": True},
        )()

        try:
            result = async_to_sync(run_workflow_task)(
                user=user,
                workflow=workflow,
                message="please do the thing",
                engine=engine,
            )
        finally:
            if original_tool is None:
                tool_registry._tools.pop("dangerous", None)
            else:
                tool_registry._tools["dangerous"] = original_tool
            if original_func is None:
                tool_registry._tool_functions.pop("dangerous", None)
            else:
                tool_registry._tool_functions["dangerous"] = original_func

        assert isinstance(result, AssistantTaskResult)
        assert result.text == "hello "
        assert len(result.approvals) == 1
        assert result.approvals[0].tool_name == "dangerous"
        assert result.session.status == "completed"
