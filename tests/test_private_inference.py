"""Private inference pools: only allowlisted bots reach private models.

Every negative case asserts zero HTTP requests reached the (mocked) Fireworks
transport. No test touches the network: the guarded engine's inner transport
is an ``httpx.MockTransport`` and real HTTP transports are disabled.
"""

from __future__ import annotations

import copy
import json
import logging
import pickle
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from django.contrib.auth import get_user_model
from pydantic import BaseModel

from django_ergo.bots import agents
from django_ergo.bots import routing
from django_ergo.bots.definition import BotDefinition
from django_ergo.bots.providers import Providers
from django_ergo.bots.providers import ProvidersError
from django_ergo.bots.providers import load_providers
from django_ergo.bots.providers import parse_providers
from django_ergo.bots.registry import BotRegistry
from django_ergo.bots.runtime import Bot
from django_ergo.conversation import inference_policy
from django_ergo.conversation.engines import fireworks_api
from django_ergo.conversation.engines.fireworks_api import FireworksAPIEngine
from django_ergo.conversation.engines.openai_api import OpenAIAPIEngine
from django_ergo.conversation.inference_policy import PolicyError
from django_ergo.conversation.inference_policy import PrivateGrant
from django_ergo.conversation.inference_policy import SpendCapError
from django_ergo.conversation.inference_policy import check_grant
from django_ergo.conversation.inference_policy import mint_grant
from django_ergo.conversation.models import ConversationSession
from django_ergo.conversation.models import InferenceRequest
from django_ergo.conversation.models import StructuredCall
from django_ergo.conversation.runtime import EngineSpec
from django_ergo.conversation.runtime import build_engine
from django_ergo.conversation.runtime import generate_once
from django_ergo.conversation.runtime import get_default_engine_spec
from django_ergo.conversation.runtime import run_workflow_task
from django_ergo.pricing import exact_price
from django_ergo.pricing import price_for

User = get_user_model()

FLASH = "accounts/fireworks/models/deepseek-v4p1-flash-1017"
GLM = "accounts/fireworks/models/glm-5p3"
KIMI = "accounts/fireworks/models/kimi-k3"
KEY = "fw-test-sentinel-key-7c1e9a"
ENDPOINT = "https://api.fireworks.ai/inference/v1/chat/completions"

PROVIDERS = f"""
default: auto/small
providers:
  openai:
    type: openai
    api_key_env: TEST_OPENAI_KEY
    models: [gpt-6-luna, gpt-6-sol]
  subscription:
    type: claude
    transport: cli
    models: [claude-haiku-5-5, claude-sonnet-5-5]
tiers:
  small: [openai/gpt-6-luna, openai/gpt-6-sol]
private_pools:
  personal:
    enabled: true
    provider: fireworks
    allowed_bots: [personal]
    default: flash
    models:
      flash: {FLASH}
      glm: {GLM}
      heavy: {KIMI}
"""


def providers_from(text: str = PROVIDERS) -> Providers:
    return parse_providers(text)


def bots(found: Providers, **engines) -> dict[str, Bot]:
    registry = BotRegistry()
    registry.providers = found
    out = {}
    for name, engine in engines.items():
        bot = Bot(BotDefinition.from_dict({"name": name, "engine": engine or {}}))
        registry.add(bot)
        out[name] = bot
    return out


def completion(model: str, *, usage: dict | None = None, tool=None) -> dict:
    message: dict = {"role": "assistant", "content": None if tool else "hello"}
    if tool:
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": tool[0], "arguments": json.dumps(tool[1])},
            }
        ]
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool else "stop",
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return body


USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 200,
    "total_tokens": 1200,
    "prompt_tokens_details": {"cached_tokens": 400},
    "completion_tokens_details": {"reasoning_tokens": 50},
}


class Fireworks:
    """A mocked Fireworks endpoint: records requests, answers with ``reply``."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.auth: list[str] = []
        self.replies: list = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.auth.append(request.headers.get("authorization", ""))
        body = json.loads(request.content)
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply or completion(body["model"], usage=USAGE))

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def refuse(self, request):
        msg = f"Real network call attempted: {request.url}"
        raise AssertionError(msg)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


@pytest.fixture
def fireworks(monkeypatch):
    mock = Fireworks()
    monkeypatch.setattr(
        fireworks_api, "_inner_transport", lambda: httpx.MockTransport(mock.handler)
    )
    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test-openai")
    return mock


@pytest.fixture
def found(monkeypatch) -> Providers:
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test-openai")
    return providers_from()


@pytest.fixture
def user(db):
    return User.objects.create_user(username="lee", password="x")


# -- providers.yaml -------------------------------------------------------------


def test_pool_is_parsed_and_kept_out_of_every_public_list(found):
    assert [m.id for m in found.models()] == [
        "openai/gpt-6-luna",
        "openai/gpt-6-sol",
    ] + [
        "subscription/claude-haiku-5-5",
        "subscription/claude-sonnet-5-5",
    ]
    assert found.find("personal/heavy") is None
    assert not found.knows("personal/heavy")
    for refs in found.routing.tiers.values():
        assert not any(found.is_private(r) for r in refs)
    for choices in found.routing.agents.values():
        assert not any(found.is_private(c.model) for c in choices)
    pool, alias, model_id = found.private("personal")
    assert (pool.name, alias, model_id) == ("personal", "flash", FLASH)
    assert found.private("personal/heavy")[2] == KIMI
    with pytest.raises(PolicyError, match="no member 'turbo'"):
        found.private("personal/turbo")
    with pytest.raises(PolicyError, match="isn't configured"):
        Providers.from_dict({}).private("personal")


POOL = {
    "provider": "fireworks",
    "enabled": True,
    "allowed_bots": ["personal"],
    "models": {"flash": FLASH},
}
PUBLIC = {"openai": {"type": "openai", "models": ["gpt-6-luna"]}}


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"private_pools": {"personal": {**POOL, "allow_all": True}}}, "unknown keys"),
        ({"private_pools": {"personal": {**POOL, "enabled": "yes"}}}, "true or false"),
        ({"private_pools": {"personal": {**POOL, "provider": "openai"}}}, "provider"),
        ({"private_pools": {"personal": {**POOL, "default": "heavy"}}}, "default"),
        (
            {"private_pools": {"personal": {**POOL, "allowed_bots": "personal"}}},
            "allowed_bots",
        ),
        ({"private_pools": {"personal": {**POOL, "models": {"a/b": FLASH}}}}, "alias"),
        (
            {
                "private_pools": {
                    "personal": {**POOL, "models": {"a": FLASH, "b": FLASH}}
                }
            },
            "already",
        ),
        ({"private_pools": {"personal": {**POOL, "daily_usd_cap": -1}}}, "more than 0"),
        ({"private_pools": {"personal": {**POOL, "daily_usd_cap": True}}}, "number"),
        (
            {
                "providers": PUBLIC,
                "private_pools": {"openai": POOL},
            },
            "clashes",
        ),
        ({"private_pool": {"personal": POOL}}, "Unknown key"),
        (
            {"providers": {"openai": {"type": "openai", "models": [FLASH]}}},
            "private model",
        ),
        (
            {
                "providers": {"fw": {"type": "openai", "models": ["kimi-k3"]}},
                "private_pools": {"personal": {**POOL, "models": {"k": "kimi-k3"}}},
            },
            "private model",
        ),
        (
            {"providers": {"personal": {"type": "openai", "models": ["gpt-6-luna"]}}},
            "reserved",
        ),
        (
            {"providers": {"fireworks": {"type": "openai", "models": ["gpt-6-luna"]}}},
            "reserved",
        ),
        (
            {
                "providers": {
                    "openai": {
                        "type": "openai",
                        "config": {"base_url": "https://api.fireworks.ai/inference/v1"},
                        "models": ["gpt-6-luna"],
                    }
                }
            },
            "private inference endpoint",
        ),
        (
            {
                "providers": {
                    "openai": {
                        "type": "openai",
                        "api_key_env": "FIREWORKS_API_KEY",
                        "models": ["gpt-6-luna"],
                    }
                }
            },
            "reserved",
        ),
        (
            {
                "providers": PUBLIC,
                "private_pools": {"personal": POOL},
                "default": "personal/flash",
            },
            "default",
        ),
        (
            {
                "providers": PUBLIC,
                "private_pools": {"personal": POOL},
                "tiers": {"small": ["personal/flash"]},
            },
            "tiers.small",
        ),
        (
            {
                "providers": {
                    **PUBLIC,
                    "sub": {
                        "type": "claude",
                        "transport": "cli",
                        "models": ["claude-haiku-5-5"],
                    },
                },
                "agents": {
                    "small": [{"agent": "omp", "provider": "sub", "model": KIMI}]
                },
            },
            "agents.small",
        ),
    ],
)
def test_private_config_is_validated_strictly(data, error):
    with pytest.raises(ProvidersError, match=error):
        Providers.from_dict(data)


def test_duplicate_aliases_in_yaml_are_rejected(tmp_path):
    (tmp_path / "providers.yaml").write_text(
        f"""
private_pools:
  personal:
    provider: fireworks
    models:
      flash: {FLASH}
      flash: {KIMI}
"""
    )
    with pytest.raises(ProvidersError, match="duplicate key 'flash'"):
        load_providers([tmp_path])


def test_generated_tiers_never_include_the_pool(found):
    assert all(
        not found.is_private(ref)
        for tier in ("small", "medium", "large", "xlarge")
        for ref in found.routing.tiers.get(tier, [])
    )


def test_a_disabled_pool_changes_nothing_public(monkeypatch):
    monkeypatch.setenv("TEST_OPENAI_KEY", "sk-test-openai")
    without = providers_from(PROVIDERS.split("private_pools:")[0])
    disabled = providers_from(PROVIDERS.replace("enabled: true", "enabled: false"))
    assert [m.id for m in disabled.models()] == [m.id for m in without.models()]
    assert disabled.routing.tiers == without.routing.tiers
    assert disabled.routing.agents == without.routing.agents
    assert disabled.default == without.default
    for name, engine in {
        "devbox": {},
        "kitchen": {"config": {"model": "openai/gpt-6-sol"}},
    }.items():
        assert (
            bots(disabled, **{name: engine})[name].engine_spec()
            == bots(without, **{name: engine})[name].engine_spec()
        )
    personal = bots(disabled, personal={"config": {"model": "personal"}})["personal"]
    with pytest.raises(PolicyError, match="disabled"):
        personal.make_engine()


# -- a bot that isn't allowlisted ------------------------------------------------


@pytest.mark.parametrize("selector", ["personal", "personal/heavy", "personal/flash"])
def test_unlisted_bot_naming_the_pool_fails_closed(found, fireworks, selector):
    devbox = bots(found, devbox={"config": {"model": selector}})["devbox"]
    assert devbox.model_ref() == selector  # never swapped for the public default
    with pytest.raises(
        PolicyError, match="isn't allowed to use private pool 'personal'"
    ):
        devbox.engine_spec()
    with pytest.raises(PolicyError):
        devbox.make_engine()
    assert fireworks.requests == []


def test_unconfigured_pool_fails_closed_instead_of_defaulting(fireworks):
    found = providers_from(PROVIDERS.split("private_pools:")[0])
    bot = bots(found, personal={"config": {"model": "personal"}})["personal"]
    with pytest.raises(PolicyError, match="isn't configured"):
        bot.make_engine()
    assert fireworks.requests == []


def test_raw_private_model_ids_and_fireworks_engine_types_are_refused(found, fireworks):
    by_id = bots(found, a={"config": {"model": KIMI}})["a"]
    with pytest.raises(PolicyError):
        by_id.make_engine()
    by_type = bots(found, b={"type": "fireworks", "config": {"model": "x"}})["b"]
    with pytest.raises(PolicyError):
        by_type.make_engine()
    assert fireworks.requests == []


@pytest.mark.django_db
def test_unlisted_bot_cannot_pick_or_keep_a_private_chat_model(found, fireworks, user):
    devbox = bots(found, devbox={})["devbox"]
    session = ConversationSession.objects.create(
        user=user, bot_name="devbox", engine_type="openai", transport_type="api"
    )
    with pytest.raises(PolicyError):
        devbox.pick_model(session, "personal/heavy")
    session.refresh_from_db()
    assert session.model == ""
    # A private selection stored some other way (an old config, a direct write) is
    # never forgotten into the public default: the chat fails.
    session.model = "personal/heavy"
    assert devbox.session_model(session) == "personal/heavy"
    with pytest.raises(PolicyError):
        devbox.engine_spec(session)
    with pytest.raises(PolicyError):
        devbox.make_engine(session, purpose="illustrate")
    assert fireworks.requests == []


@pytest.mark.django_db(transaction=True)
async def test_unlisted_bot_threads_and_delegated_threads_stay_public(found, fireworks):
    user = await User.objects.acreate(username="lee2")
    both = bots(found, personal={"config": {"model": "personal/heavy"}}, devbox={})
    personal, devbox = both["personal"], both["devbox"]
    parent = await personal.create_session(user)
    assert parent.engine_type == "fireworks"
    # No model: the delegate's own (public) default, nothing from its parent.
    child = await devbox.create_session(user, parent=parent)
    assert child.engine_type == "openai"
    engine = devbox.make_engine(child)
    assert type(engine) is OpenAIAPIEngine
    # The parent's private selection handed over explicitly is refused.
    with pytest.raises(PolicyError):
        await devbox.create_session(
            user, parent=parent, metadata={"model": "personal/heavy"}
        )
    assert not await ConversationSession.objects.filter(
        bot_name="devbox", model__startswith="personal"
    ).aexists()
    assert fireworks.requests == []


def test_title_and_router_engines_of_unlisted_bot_are_public(found, fireworks):
    devbox = bots(found, devbox={})["devbox"]
    assert type(devbox.make_engine(purpose="new_thread_metadata")) is OpenAIAPIEngine
    assert type(devbox.make_engine(model=devbox.resolve_ref("auto/small", None))) is (
        OpenAIAPIEngine
    )


# -- routing ----------------------------------------------------------------------


def test_auto_tiers_never_resolve_to_a_private_model(found, fireworks, monkeypatch):
    devbox = bots(found, devbox={"config": {"model": "auto/small"}})["devbox"]
    forged = SimpleNamespace(metadata={"routed_model": "personal/heavy"}, model="")
    assert devbox.resolve_ref("auto/small", forged) == "openai/gpt-6-luna"
    # Even a tier that somehow lists a private model never hands it out.
    found.routing.tiers["small"] = ["personal/heavy", "openai/gpt-6-luna"]
    monkeypatch.setattr(routing, "current_usage", dict)
    monkeypatch.setattr(routing, "active_rules", lambda r: routing.RoutingRules())
    assert routing.pick_model(found, "small") == "openai/gpt-6-luna"
    found.routing.tiers["small"] = ["personal/heavy"]
    with pytest.raises(PolicyError):
        devbox.resolve_ref("auto/small", None)
    with pytest.raises(ValueError, match="No provider"):
        routing.pick_model(found, "small")


def test_choose_all_over_limit_picks_public_headroom(found, monkeypatch):
    monkeypatch.setattr(routing, "current_usage", lambda: {"openai": {"five_hour": 99}})
    rules = routing.RoutingRules(
        limits=[routing.Limit(provider="openai", window="five_hour", max_used=50)]
    )
    monkeypatch.setattr(routing, "active_rules", lambda r: rules)
    found.routing.tiers["small"] = ["personal/heavy", "openai/gpt-6-sol"]
    assert routing.pick_model(found, "small") == "openai/gpt-6-sol"


def test_custom_router_cannot_return_a_model_outside_its_candidates(
    found, settings, monkeypatch
):
    monkeypatch.setattr(routing, "current_usage", dict)
    monkeypatch.setattr(routing, "active_rules", lambda r: routing.RoutingRules())
    settings.DJANGO_ERGO = {
        **getattr(settings, "DJANGO_ERGO", {}),
        "MODEL_ROUTER": lambda candidates, usage, rules, current: "personal/heavy",
    }
    with pytest.raises(ValueError, match="isn't one of its candidates"):
        routing.pick_model(found, "small")


def test_retry_and_reroute_never_offer_a_private_model(found, monkeypatch):
    monkeypatch.setattr(routing, "current_usage", dict)
    monkeypatch.setattr(routing, "active_rules", lambda r: routing.RoutingRules())
    found.routing.tiers["small"] = ["openai/gpt-6-luna", "personal/heavy"]
    assert (
        routing.retry_model(found, "small", "openai/gpt-6-luna", "429 rate limit") == ""
    )
    devbox = bots(found, devbox={"config": {"model": "auto/small"}})["devbox"]
    session = SimpleNamespace(metadata={}, model="")
    with pytest.raises(ValueError, match="isn't a model in this chat's tier"):
        devbox.reroute(session, "personal/heavy", "user asked")


def test_agent_tiers_skip_private_models(found, monkeypatch):
    monkeypatch.setattr(routing, "current_usage", dict)
    monkeypatch.setattr(routing, "active_rules", lambda r: routing.RoutingRules())
    found.routing.agents["small"] = [
        routing.AgentChoice(agent="omp", model=KIMI, provider="subscription"),
        routing.AgentChoice(
            agent="claude", model="claude-haiku-5-5", provider="subscription"
        ),
    ]
    assert routing.pick_agent(found, "small").model == "claude-haiku-5-5"


@pytest.mark.parametrize(
    "model",
    ["personal", "personal/heavy", KIMI, "fireworks/kimi-k3", "Fireworks/glm-5p3"],
)
def test_coding_agents_refuse_private_models_before_any_side_effect(
    found, model, monkeypatch
):
    devbox = bots(found, devbox={})["devbox"]
    calls = []
    monkeypatch.setattr(agents, "manager_for", lambda *a: calls.append(a))
    ctx = SimpleNamespace(bot=devbox, session=object())
    with pytest.raises(PolicyError, match="Coding agents can't use the private model"):
        agents.start(ctx, agents.AgentSpec("brief", "path:/x", model=model))
    assert calls == []


# -- low-level library paths --------------------------------------------------------


async def test_settings_default_engine_type_cannot_send_privately(settings, fireworks):
    settings.DJANGO_ERGO = {
        **getattr(settings, "DJANGO_ERGO", {}),
        "CONVERSATION_ENGINE_TYPE": "fireworks",
        "CONVERSATION_ENGINE_CONFIG": {"model": KIMI},
    }
    engine = build_engine(get_default_engine_spec())
    assert isinstance(engine, FireworksAPIEngine)
    with pytest.raises(PolicyError, match="No valid private-inference grant"):
        await engine.generate("hi")
    with pytest.raises(PolicyError):
        await generate_once("hi")
    assert fireworks.requests == []


def test_bot_on_settings_default_fireworks_engine_is_refused(settings, fireworks):
    settings.DJANGO_ERGO = {
        **getattr(settings, "DJANGO_ERGO", {}),
        "CONVERSATION_ENGINE_TYPE": "fireworks",
    }
    bot = bots(Providers(), plain={})["plain"]
    with pytest.raises(PolicyError, match="only reachable through a private pool"):
        bot.engine_spec()


@pytest.mark.django_db(transaction=True)
async def test_session_manager_and_workflow_tasks_never_get_a_grant(fireworks):
    from django_ergo.conversation.manager import SessionManager
    from django_ergo.conversation.window import WindowChat

    user = await User.objects.acreate(username="lee3")
    forged = await ConversationSession.objects.acreate(
        user=user,
        engine_type="fireworks",
        transport_type="api",
        metadata={"model": KIMI, "grant": {"bot": "personal"}},
    )
    with pytest.raises(PolicyError, match="grant can't be configured"):
        await SessionManager().get_engine(forged)
    session = await ConversationSession.objects.acreate(
        user=user,
        engine_type="fireworks",
        transport_type="api",
        metadata={"model": KIMI, "allow_private": True, "pool": "personal"},
    )
    engine = await SessionManager().get_engine(session)
    assert isinstance(engine, FireworksAPIEngine)
    assert engine.grant is None
    with pytest.raises(PolicyError):
        await engine.complete([{"role": "user", "content": "hi"}])
    spec = EngineSpec("fireworks", "api", {"model": KIMI})
    with pytest.raises(PolicyError):
        await run_workflow_task(
            user=user, workflow=None, message="hi", engine_spec=spec
        )
    with pytest.raises(PolicyError, match="exact model ID"):
        await WindowChat.create(user=user, engine_spec=spec)
    window = await WindowChat.create(
        user=user, engine_spec=spec, metadata={"model": KIMI}
    )
    with pytest.raises(PolicyError, match="No valid"):
        await window.engine.complete([{"role": "user", "content": "hi"}])
    assert fireworks.requests == []


@pytest.mark.django_db(transaction=True)
async def test_structured_call_on_ungranted_engine_fails_with_policy_category(
    fireworks,
):
    from django_ergo.conversation.structured import StructuredCallSpec
    from django_ergo.conversation.structured import run_structured_call

    engine = FireworksAPIEngine({"model": KIMI})
    spec = StructuredCallSpec(kind="revision", response_model=Out, max_turns=1)
    result = await run_structured_call(spec, "hi", engine=engine)
    assert result.call.error_category == "policy"
    assert fireworks.requests == []


class Out(BaseModel):
    result: str


# -- grants and the guarded engine ---------------------------------------------------


def pool(found, **changes):
    base = found.private_pools["personal"]
    return inference_policy.PrivatePool(
        **{
            "name": base.name,
            "provider": base.provider,
            "enabled": base.enabled,
            "allowed_bots": base.allowed_bots,
            "models": base.models,
            "default": base.default,
            "daily_usd_cap": base.daily_usd_cap,
            **changes,
        }
    )


def test_grants_cannot_be_forged_copied_or_serialized(found):
    the_pool = found.private_pools["personal"]
    with pytest.raises(PolicyError, match="minted by"):
        PrivateGrant(
            _mint=object(),
            pool=the_pool,
            bot="personal",
            alias="heavy",
            model_id=KIMI,
            selector="personal/heavy",
        )
    forged = object.__new__(PrivateGrant)
    for name, value in {
        "pool": the_pool,
        "bot": "personal",
        "model_id": KIMI,
        "expires_at": float("inf"),
    }.items():
        object.__setattr__(forged, name, value)
    with pytest.raises(PolicyError, match="No valid"):
        check_grant(forged, model_id=KIMI)
    with pytest.raises(PolicyError, match="No valid"):
        check_grant({"bot": "personal", "model_id": KIMI}, model_id=KIMI)
    grant = mint_grant(the_pool, bot="personal", selector="personal/heavy")
    with pytest.raises(TypeError):
        copy.copy(grant)
    with pytest.raises(TypeError):
        pickle.dumps(grant)
    with pytest.raises(PolicyError, match="is for"):
        check_grant(grant, model_id=FLASH)
    with pytest.raises(PolicyError, match="isn't allowed"):
        mint_grant(the_pool, bot="devbox", selector="personal/heavy")
    with pytest.raises(PolicyError, match="disabled"):
        mint_grant(pool(found, enabled=False), bot="personal", selector="personal")
    with pytest.raises(TypeError):
        grant.bot = "devbox"
    with pytest.raises(TypeError):
        grant.model_id = FLASH
    with pytest.raises(TypeError):
        the_pool.models["heavy"] = FLASH
    assert (grant.bot, grant.model_id) == ("personal", KIMI)


async def test_engine_refuses_forged_grant_and_changed_model_before_http(
    found, fireworks
):
    engine = FireworksAPIEngine({"model": KIMI})
    with pytest.raises(PolicyError):
        engine.authorize(object())
    engine._grant = object.__new__(PrivateGrant)
    with pytest.raises(PolicyError):
        await engine.complete([{"role": "user", "content": "hi"}])
    engine.authorize(
        mint_grant(
            found.private_pools["personal"], bot="personal", selector="personal/heavy"
        )
    )
    engine.model = FLASH  # mutated after authorization
    with pytest.raises(PolicyError, match="is for"):
        await engine.complete([{"role": "user", "content": "hi"}])
    assert fireworks.requests == []


async def test_cached_client_reuse_outside_a_guarded_send_is_refused(found, fireworks):
    engine = FireworksAPIEngine({"model": KIMI})
    engine.authorize(
        mint_grant(
            found.private_pools["personal"], bot="personal", selector="personal/heavy"
        )
    )
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the SDK wraps it
        await engine.client.chat.completions.create(
            model=KIMI, messages=[{"role": "user", "content": "hi"}]
        )
    cause = caught.value
    while cause is not None and not isinstance(cause, PolicyError):
        cause = cause.__cause__ or cause.__context__
    assert isinstance(cause, PolicyError)
    assert fireworks.requests == []


@pytest.mark.parametrize(
    "config",
    [
        {"model": KIMI, "base_url": "https://example.com/v1"},
        {"model": KIMI, "api_key": "sk-other"},
        {"model": KIMI, "api_key_env": "OTHER"},
        {"model": KIMI, "max_retries": 5},
        {"model": KIMI, "grant": "personal"},
        {},
    ],
)
def test_fireworks_engine_config_cannot_move_endpoint_key_or_model(config):
    with pytest.raises(PolicyError):
        FireworksAPIEngine(config)


def test_bot_config_cannot_override_private_endpoint_or_key(found):
    bad = bots(
        found,
        personal={
            "config": {"model": "personal", "base_url": "https://evil.example/v1"}
        },
    )["personal"]
    with pytest.raises(PolicyError, match="base_url"):
        bad.make_engine()
    keyed = bots(
        found, personal={"config": {"model": "personal"}, "api_key_env": "X_KEY"}
    )["personal"]
    with pytest.raises(PolicyError, match="api_key_env"):
        keyed.make_engine()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.fireworks.ai/inference/v1",
        "https://API.Fireworks.AI./inference/v1",
        "http://gateway.fireworks.ai:8443/v1",
    ],
)
async def test_public_openai_engine_refuses_fireworks_endpoints(base_url, monkeypatch):
    built = []
    import openai

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: built.append(kw))
    engine = OpenAIAPIEngine(
        {"model": "gpt-6-luna", "api_key": "k", "base_url": base_url}
    )
    with pytest.raises(PolicyError):
        await engine.complete([{"role": "user", "content": "hi"}])
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    with pytest.raises(PolicyError):
        await OpenAIAPIEngine({"model": "gpt-6-luna", "api_key": "k"}).generate("hi")
    assert built == []


async def test_public_openai_engine_refuses_a_client_redirected_after_construction():
    sent = []
    engine = OpenAIAPIEngine({"model": "gpt-6-luna", "api_key": "k"})

    async def create(**kwargs):
        sent.append(kwargs)

    engine._client = SimpleNamespace(
        base_url=httpx.URL("https://api.fireworks.ai/inference/v1/"),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    with pytest.raises(PolicyError):
        await engine.complete([{"role": "user", "content": "hi"}])
    assert sent == []


# -- the allowlisted bot ------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("selector", "model_id"), [("personal", FLASH), ("personal/heavy", KIMI)]
)
async def test_allowlisted_bot_sends_to_the_fixed_endpoint_and_records_cost(
    found, fireworks, selector, model_id
):
    user = await User.objects.acreate(username=f"lee-{selector.replace('/', '-')}")
    personal = bots(found, personal={"config": {"model": selector}})["personal"]
    session = await personal.create_session(user)
    assert (session.engine_type, session.transport_type) == ("fireworks", "api")
    engine = personal.make_engine(session)
    assert isinstance(engine, FireworksAPIEngine)
    assert engine.grant.bot == "personal"
    events = [e async for e in engine.send(session, "hello")]
    assert any(e.text == "hello" for e in events)

    assert [str(r.url) for r in fireworks.requests] == [ENDPOINT]
    assert fireworks.bodies[0]["model"] == model_id
    assert fireworks.auth == [f"Bearer {KEY}"]
    row = await InferenceRequest.objects.aget()
    price = exact_price("fireworks", model_id)
    expected = (600 * price.input + 400 * price.cached + 200 * price.output) / Decimal(
        1_000_000
    )
    assert row.cost_usd == expected
    assert row.cost_status == "priced"
    assert (
        row.prompt_tokens,
        row.cached_tokens,
        row.output_tokens,
        row.reasoning_tokens,
    ) == (
        1000,
        400,
        200,
        50,
    )
    assert (row.bot_name, row.pool, row.provider, row.model, row.selector) == (
        "personal",
        "personal",
        "fireworks",
        model_id,
        selector,
    )
    assert row.billing_mode == "api"
    assert row.session_id == session.pk
    assert row.user_id == user.pk
    assert row.outcome == "ok"
    assert row.rates["output"] == str(price.output)


def test_kimi_cost_is_exact():
    price = exact_price("fireworks", KIMI)
    assert price.cost(1000, 400, 200) == Decimal("0.00492")
    assert exact_price("fireworks", KIMI + "-turbo") is None
    assert price_for(KIMI + "-turbo") is None  # no prefix matching onto other models
    assert price_for(KIMI).cache_read == pytest.approx(0.30)


@pytest.mark.django_db(transaction=True)
async def test_title_call_is_linked_to_its_structured_call(found, fireworks):
    fireworks.replies.append(
        completion(
            FLASH, usage=USAGE, tool=("submit_output", {"title": "Dinner plans"})
        )
    )
    user = await User.objects.acreate(username="lee-title")
    personal = bots(found, personal={"config": {"model": "personal"}})["personal"]
    assert await personal.thread_metadata("What's for dinner?", user=user) == {
        "title": "Dinner plans"
    }
    row = await InferenceRequest.objects.select_related("structured_call").aget()
    assert row.structured_call.kind == "new_thread_metadata"
    assert row.purpose == "new_thread_metadata"
    call = await StructuredCall.objects.aget()
    assert float(call.cost_usd) == pytest.approx(
        float(exact_price("fireworks", FLASH).cost(1000, 400, 200)), abs=1e-8
    )


@pytest.mark.django_db(transaction=True)
async def test_disabled_pool_missing_key_and_cap_fail_without_http(
    found, fireworks, monkeypatch
):
    user = await User.objects.acreate(username="lee-fail")
    disabled = Providers(
        providers=found.providers,
        routing=found.routing,
        private_pools={"personal": pool(found, enabled=False)},
    )
    bot = bots(disabled, personal={"config": {"model": "personal"}})["personal"]
    with pytest.raises(PolicyError, match="disabled"):
        bot.make_engine()

    personal = bots(found, personal={"config": {"model": "personal/heavy"}})["personal"]
    engine = personal.make_engine()
    monkeypatch.delenv("FIREWORKS_API_KEY")
    with pytest.raises(PolicyError, match="FIREWORKS_API_KEY isn't set"):
        await engine.complete([{"role": "user", "content": "hi"}])
    assert not await InferenceRequest.objects.aexists()
    monkeypatch.setenv("FIREWORKS_API_KEY", KEY)

    capped = Providers(
        providers=found.providers,
        routing=found.routing,
        private_pools={"personal": pool(found, daily_usd_cap=Decimal("0.0001"))},
    )
    engine = bots(capped, personal={"config": {"model": "personal/heavy"}})[
        "personal"
    ].make_engine()
    with pytest.raises(SpendCapError, match="daily cap"):
        await engine.complete([{"role": "user", "content": "hi"}])

    unpriced = Providers(
        providers=found.providers,
        routing=found.routing,
        private_pools={
            "personal": pool(
                found,
                daily_usd_cap=Decimal(100),
                models={"new": "accounts/fireworks/models/unpriced-x"},
                default="new",
            )
        },
    )
    engine = bots(unpriced, personal={"config": {"model": "personal"}})[
        "personal"
    ].make_engine()
    with pytest.raises(SpendCapError, match="no exact price"):
        await engine.complete([{"role": "user", "content": "hi"}])
    assert fireworks.requests == []
    assert user.pk


@pytest.mark.django_db(transaction=True)
async def test_cap_counts_earlier_spend_and_reservations(found, fireworks):
    capped = Providers(
        providers=found.providers,
        routing=found.routing,
        private_pools={"personal": pool(found, daily_usd_cap=Decimal("0.10"))},
    )
    engine = bots(capped, personal={"config": {"model": "personal/flash"}})[
        "personal"
    ].make_engine()
    await engine.complete([{"role": "user", "content": "hi"}])
    assert len(fireworks.requests) == 1
    first = await InferenceRequest.objects.aget()
    assert first.reserved_usd > first.cost_usd > 0
    # An in-flight (or failed, maybe billed) attempt holds its reservation.
    await InferenceRequest.objects.filter(pk=first.pk).aupdate(
        cost_status="unknown", cost_usd=None, reserved_usd=Decimal("0.0999")
    )
    with pytest.raises(SpendCapError):
        await engine.complete([{"role": "user", "content": "hi"}])
    assert len(fireworks.requests) == 1


@pytest.mark.django_db(transaction=True)
async def test_missing_usage_is_unknown_not_zero(found, fireworks):
    fireworks.replies.append(completion(KIMI, usage=None))
    engine = bots(found, personal={"config": {"model": "personal/heavy"}})[
        "personal"
    ].make_engine()
    await engine.complete([{"role": "user", "content": "hi"}])
    row = await InferenceRequest.objects.aget()
    assert row.cost_usd is None
    assert row.cost_status == "unknown"


@pytest.mark.django_db(transaction=True)
async def test_transient_failure_retries_same_model_and_records_each_attempt(
    found, fireworks, monkeypatch
):
    from django_ergo.conversation import structured
    from django_ergo.conversation.structured import StructuredCallSpec
    from django_ergo.conversation.structured import run_structured_call

    monkeypatch.setattr(structured, "RETRY_DELAYS", (0, 0, 0))
    fireworks.replies += [
        httpx.Response(500, json={"error": {"message": "boom"}}),
        completion(KIMI, usage=USAGE, tool=("submit_output", {"result": "ok"})),
    ]
    engine = bots(found, personal={"config": {"model": "personal/heavy"}})[
        "personal"
    ].make_engine()
    spec = StructuredCallSpec(kind="test", response_model=Out, max_turns=2)
    await run_structured_call(spec, "hi", engine=engine)
    assert [b["model"] for b in fireworks.bodies] == [KIMI, KIMI]
    rows = [r async for r in InferenceRequest.objects.order_by("attempt")]
    assert [(r.attempt, r.outcome) for r in rows] == [(1, "error"), (2, "ok")]
    assert rows[0].cost_usd is None  # may have been billed: unknown, not 0
    assert rows[0].reserved_usd is not None


@pytest.mark.django_db(transaction=True)
async def test_transport_refusal_is_a_policy_error_not_a_retried_network_error(
    found, fireworks, monkeypatch
):
    from django_ergo.conversation import inference_ledger
    from django_ergo.conversation.structured import StructuredCallSpec
    from django_ergo.conversation.structured import run_structured_call

    real_open = inference_ledger.open_attempt

    async def open_then_lose_key(*args, **kwargs):
        entry = await real_open(*args, **kwargs)
        monkeypatch.delenv("FIREWORKS_API_KEY")
        return entry

    monkeypatch.setattr(inference_ledger, "open_attempt", open_then_lose_key)
    engine = bots(found, personal={"config": {"model": "personal/heavy"}})[
        "personal"
    ].make_engine()
    spec = StructuredCallSpec(kind="test", response_model=Out, max_turns=2)
    result = await run_structured_call(spec, "hi", engine=engine)
    assert result.call.error_category == "policy"
    assert fireworks.requests == []
    row = await InferenceRequest.objects.aget()
    assert (row.outcome, row.cost_usd, row.cost_status) == ("not_sent", 0, "not_sent")


# -- secret hygiene --------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_key_never_appears_in_reprs_config_ledger_errors_or_logs(
    found, fireworks, caplog
):
    caplog.set_level(logging.DEBUG)
    for name in ("openai", "httpx", "httpcore", "django_ergo"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    personal = bots(found, personal={"config": {"model": "personal/heavy"}})["personal"]
    spec = personal.engine_spec()
    assert KEY not in repr(spec)
    engine = personal.make_engine()
    await engine.complete([{"role": "user", "content": "hi"}])
    fireworks.replies.append(
        httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(Exception) as failed:  # noqa: PT011 - the SDK's AuthenticationError
        await engine.complete([{"role": "user", "content": "hi"}])
    texts = [
        repr(engine),
        repr(vars(engine)),
        repr(engine.config),
        repr(engine.grant),
        repr(engine.client),
        str(failed.value),
        repr(failed.value),
        caplog.text,
        repr(fireworks.requests[-1].headers.get("authorization")),
    ]
    texts += [
        repr({f.name: getattr(row, f.name) for f in row._meta.fields})
        async for row in InferenceRequest.objects.all()
    ]
    for text in texts:
        assert KEY not in text
    assert fireworks.auth == [f"Bearer {KEY}", f"Bearer {KEY}"]  # only on the wire
