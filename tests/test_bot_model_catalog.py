from __future__ import annotations

import json
import sqlite3
import traceback
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from django_ergo.bots import model_catalog as catalog
from django_ergo.bots.providers import Providers
from django_ergo.conversation.models import ModelCatalogSnapshot

pytestmark = pytest.mark.django_db


@pytest.fixture
def discovery(monkeypatch, tmp_path):
    """Mock all sources and seed an empty baseline for catalog-delta tests."""
    monkeypatch.setattr(catalog, "_local_configuration", lambda: set())
    monkeypatch.setattr(catalog, "_official_models", lambda source: None)
    rows = {"openai-codex": []}

    def local(provider):
        models = rows.get(provider)
        return models, {
            "source": "omp_cache",
            "status": "ok" if models is not None else "unhandled",
            "count": len(models or []),
        }

    monkeypatch.setattr(catalog, "_local_catalog", local)
    providers = Providers.from_dict(
        {
            "providers": {
                "chatgpt": {
                    "type": "openai",
                    "transport": "cli",
                    "models": ["coding-one"],
                }
            },
            "tiers": {"medium": ["chatgpt/coding-one"]},
        }
    )
    context = {
        "bot": SimpleNamespace(name="catalog-test", path=tmp_path, providers=providers),
        "user": SimpleNamespace(pk=1),
        "run": SimpleNamespace(schedule="weekly"),
    }
    catalog.check_available(context)
    return context, rows


def model(name="coding-one", **extra):
    return {
        "id": name,
        "cost": {"input": 2, "output": 8, "cacheRead": 0.2},
        "contextWindow": 200000,
        "kind": "chat",
        "input": ["text"],
        "maxTokens": 32000,
        "reasoning": True,
        "thinking": {"mode": "effort", "efforts": ["low", "high"]},
        **extra,
    }


@pytest.mark.parametrize("model_count", [0, 2])
def test_first_baseline_seeds_once_without_a_catalog_dump(discovery, model_count):
    from django_ergo.bots.schedules import Action
    from django_ergo.bots.schedules import _run_python

    context, rows = discovery
    ModelCatalogSnapshot.objects.all().delete()
    rows["openai-codex"] = [
        model(f"initial-{index}", kind="tts", cost={}) for index in range(model_count)
    ]
    action = Action("python", function="ergo.models.check_available")
    first = _run_python(context["bot"], action, context)
    assert first["baseline_seeded"] is True
    assert first["model_count"] == model_count
    assert first["summary"] == f"Baseline seeded, {model_count} models"
    assert first["notify"] is True
    assert first["new"] == first["removed"] == first["changed"] == []
    assert first["flagged"] == []
    assert len(ModelCatalogSnapshot.objects.get().models) == model_count
    second = _run_python(context["bot"], action, context)
    assert second["baseline_seeded"] is False
    assert second["model_count"] == model_count
    assert second["notify"] is False
    assert second["new"] == second["removed"] == second["changed"] == []
    assert second["flagged"] == []


def test_new_model_is_discovered_and_next_run_is_quiet(discovery):
    context, rows = discovery
    rows["openai-codex"] = [model()]
    first = catalog.check_available(context)
    assert first["notify"] is True
    assert first["new"][0]["selector"] == "chatgpt/coding-one"
    assert first["new"][0]["routable"] is True
    assert first["new"][0]["pricing_source"] == "omp_cache"
    assert first["new"][0]["pricing_authority"] == "catalog"
    assert first["new"][0]["capabilities"]["maxTokens"] == 32000
    second = catalog.check_available(context)
    assert second["notify"] is False
    assert (
        second["new"]
        == second["removed"]
        == second["changed"]
        == second["flagged"]
        == []
    )
    assert ModelCatalogSnapshot.objects.count() == 1


def test_recreated_schedule_context_reuses_persisted_baseline(discovery):
    from django_ergo.bots.schedules import Action
    from django_ergo.bots.schedules import _run_python

    context, rows = discovery
    ModelCatalogSnapshot.objects.all().delete()
    rows["openai-codex"] = [model()]
    root = context["bot"].path
    context["bot"].definition = SimpleNamespace(root_dir=root)
    context["run"] = SimpleNamespace(pk=101, schedule="weekly", minute="2026-11-08")
    action = Action("python", function="ergo.models.check_available")
    first = _run_python(context["bot"], action, context)
    baseline = ModelCatalogSnapshot.objects.get()
    assert first["baseline_seeded"] is True
    assert first["model_count"] == 1
    assert first["summary"] == "Baseline seeded, 1 models"
    assert first["notify"] is True
    assert first["new"] == first["removed"] == first["changed"] == []
    assert first["flagged"] == []
    assert "chatgpt/coding-one" in baseline.models

    # A later scheduled task reconstructs every object; its run ID/minute and
    # fallback path are transient, but its definition root/name/user/schedule
    # identify the same persisted baseline.
    later = {
        "bot": SimpleNamespace(
            name=context["bot"].name,
            path=root / "another-worker",
            definition=SimpleNamespace(root_dir=root),
            providers=deepcopy(context["bot"].providers),
        ),
        "user": SimpleNamespace(pk=context["user"].pk),
        "run": SimpleNamespace(pk=202, schedule="weekly", minute="2026-11-15"),
    }
    second = _run_python(later["bot"], action, later)
    assert second["new"] == second["removed"] == second["changed"] == []
    assert second["flagged"] == []
    assert second["notify"] is False
    assert second["baseline_seeded"] is False
    assert ModelCatalogSnapshot.objects.get().pk == baseline.pk

    rows["openai-codex"] = [model(), model("later-model")]
    third = _run_python(later["bot"], action, later)
    assert third["baseline_seeded"] is False
    assert third["model_count"] == 2
    assert third["notify"] is True
    assert [record["id"] for record in third["new"]] == ["later-model"]
    assert third["removed"] == third["changed"] == []
    assert third["summary"] == "1 new, 0 removed, 0 changed models"


@pytest.mark.parametrize(
    "kind",
    ["tts", "stt", "embedding", "image", "realtime", "moderation", "audio", "video"],
)
def test_non_chat_models_are_not_coding_candidates(discovery, kind):
    context, rows = discovery
    # Even positive tool/text metadata cannot override a non-chat kind.
    rows["openai-codex"] = [model(kind, kind=kind, supportsTools=True)]
    result = catalog.check_available(context)
    record = result["new"][0]
    assert record["priced"] is True
    assert record["allowed_for_coding"] is False
    assert record["routable"] is False
    assert {flag["reason"] for flag in result["flagged"]} == {
        "coding_capability_not_supported"
    }
    assert catalog.check_available(context)["notify"] is False


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"kind": "unrecognized"},
        {"kind": "text"},
        {"input": ["text"]},
        {"input": ["image"], "maxTokens": 1000},
        {"input": ["text"], "maxTokens": 0},
        {"input": ["text"], "maxTokens": True},
        {"input": ["text"], "maxTokens": 1000, "supportsTools": False},
        {"kind": "chat", "supportsTools": False},
    ],
)
def test_missing_or_incompatible_capabilities_fail_closed(discovery, metadata):
    context, rows = discovery
    rows["openai-codex"] = [
        {"id": "not-established-chat", "cost": {"input": 2, "output": 8}, **metadata}
    ]
    record = catalog.check_available(context)["new"][0]
    assert record["priced"] is True
    assert record["allowed_for_coding"] is False
    assert record["routable"] is False


@pytest.mark.parametrize(
    "model_id",
    [
        "gpt-realtime-2.1",
        "tts-1",
        "speech.stt",
        "gpt-transcribe",
        "speech-transcription",
        "text-embedding-3-small",
        "text-embeddings-v2",
        "omni-moderation-latest",
        "gpt-image-2",
        "video-gen",
        "gpt-audio",
        "voice/TTS",
    ],
)
def test_non_chat_name_tokens_override_chat_metadata(discovery, model_id):
    context, rows = discovery
    rows["openai-codex"] = [model(model_id, supportsTools=True)]
    result = catalog.check_available(context)
    record = result["new"][0]
    assert record["capabilities"]["kind"] == "chat"
    assert record["priced"] is True
    assert record["allowed_for_coding"] is False
    assert record["routable"] is False
    assert {flag["reason"] for flag in result["flagged"]} == {
        "coding_capability_not_supported"
    }


def test_realtime_without_kind_is_not_inferred_to_be_chat(discovery):
    context, rows = discovery
    rows["openai-codex"] = [
        {
            "id": "gpt-realtime-2.1",
            "cost": {"input": 4, "output": 16},
            "input": ["text", "audio"],
            "maxTokens": 32000,
            "contextWindow": 128000,
            "supportsTools": True,
        }
    ]
    record = catalog.check_available(context)["new"][0]
    assert record["priced"] is True
    assert record["allowed_for_coding"] is False
    assert record["routable"] is False


def test_new_priced_unpriced_and_zero_placeholder_models(discovery):
    context, rows = discovery
    catalog.check_available(context)
    rows["openai-codex"] = [
        model(),
        model("unknown", cost={}),
        model("zero-placeholder", cost={"input": 0, "output": 0}),
        model("quarantined", quarantined=True),
    ]
    result = catalog.check_available(context)
    discovered = {m["id"]: m for m in result["new"]}
    assert discovered["coding-one"]["priced"] is True
    assert discovered["unknown"]["priced"] is False
    assert discovered["unknown"]["routable"] is False
    assert discovered["zero-placeholder"]["priced"] is False
    assert discovered["quarantined"]["routable"] is False
    assert {f["reason"] for f in result["flagged"]} == {"unpriced", "quarantined"}
    assert catalog.check_available(context)["notify"] is False


def test_metadata_and_price_changes_are_audited(discovery):
    context, rows = discovery
    rows["openai-codex"] = [model()]
    catalog.check_available(context)
    rows["openai-codex"] = [model(cost={"input": 1, "output": 4}, contextWindow=400000)]
    result = catalog.check_available(context)
    assert result["new"] == result["removed"] == []
    assert result["changed"][0]["before"]["pricing"]["input"] == 2
    assert result["changed"][0]["after"]["pricing"]["input"] == 1
    assert result["changed"][0]["after"]["capabilities"]["contextWindow"] == 400000
    assert catalog.check_available(context)["notify"] is False


def test_private_fireworks_never_becomes_shared_coding_route(discovery):
    context, rows = discovery
    context["bot"].providers = Providers.from_dict(
        {
            "private_pools": {
                "personal": {
                    "provider": "fireworks",
                    "enabled": True,
                    "allowed_bots": ["catalog-test"],
                    "default": "one",
                    "models": {"one": "accounts/fireworks/models/private-one"},
                }
            },
        }
    )
    rows["fireworks"] = [
        model("accounts/fireworks/models/private-one"),
        model("public-model", public=True),
    ]
    result = catalog.check_available(context)
    assert len(result["new"]) == 2
    assert all(
        m["private"] and not m["routable"] and not m["allowed_for_coding"]
        for m in result["new"]
    )
    assert all(m["configured_provider"] == "personal" for m in result["new"])
    assert context["bot"].providers.models() == []
    assert not any(context["bot"].providers.routing.tiers.values())


def test_removed_routed_model_is_prominently_flagged(discovery):
    context, rows = discovery
    rows["openai-codex"] = [model()]
    catalog.check_available(context)
    rows["openai-codex"] = []
    result = catalog.check_available(context)
    assert result["removed"][0]["id"] == "coding-one"
    assert result["flagged"][0]["reason"] == "removed_in_route"
    assert result["flagged"][0]["severity"] == "critical"
    assert catalog.check_available(context)["notify"] is False


def test_failed_fetch_does_not_advance_snapshot_and_error_is_secret_safe(
    discovery, monkeypatch
):
    context, rows = discovery
    rows["openai-codex"] = [model()]
    catalog.check_available(context)
    previous = ModelCatalogSnapshot.objects.get().models
    rows["openai-codex"] = [model("later")]

    def failed(source):
        try:
            msg = "upstream echoed super-secret-api-key"
            raise RuntimeError(msg)  # noqa: TRY301 — simulate an upstream credential echo
        except RuntimeError:
            raise catalog.ModelCatalogError(source.provider, "official_list") from None

    monkeypatch.setattr(catalog, "_official_models", failed)
    with pytest.raises(catalog.ModelCatalogError) as raised:
        catalog.check_available(context)
    assert "super-secret-api-key" not in "".join(
        traceback.format_exception(raised.value)
    )
    assert ModelCatalogSnapshot.objects.get().models == previous


def test_unhandled_source_preserves_baseline_and_reports_coverage(discovery):
    context, rows = discovery
    rows["openai-codex"] = [model()]
    catalog.check_available(context)
    rows.pop("openai-codex")
    result = catalog.check_available(context)
    assert result["notify"] is False
    assert result["removed"] == []
    assert all(
        c["status"] == "unhandled"
        for c in result["coverage"]
        if c["provider"] == "openai-codex"
    )
    assert any(
        c["provider"] == "google" and c["status"] == "not_configured"
        for c in result["coverage"]
    )
    assert ModelCatalogSnapshot.objects.get().models


def test_local_catalog_queries_only_configured_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "_agent_dir", lambda: tmp_path)
    with sqlite3.connect(tmp_path / "models.db") as db:
        db.execute(
            "CREATE TABLE model_cache (provider_id TEXT PRIMARY KEY, models TEXT, authoritative INTEGER)"
        )
        db.execute(
            "INSERT INTO model_cache VALUES (?, ?, ?)",
            ("xai-oauth", json.dumps([model("grok-build")]), 1),
        )
        db.execute(
            "INSERT INTO model_cache VALUES (?, ?, ?)", ("google", "not valid json", 1)
        )
    models, coverage = catalog._local_catalog("xai-oauth")
    assert models[0]["id"] == "grok-build"
    assert coverage["authoritative_membership"] is True
    assert catalog._local_catalog("unconfigured")[0] is None


def test_local_configuration_reads_provider_names_not_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(catalog, "_agent_dir", lambda: tmp_path)
    monkeypatch.setattr(catalog.os, "environ", {})
    (tmp_path / "config.yml").write_text(
        "providers:\n  google: {}\n  disabled: {enabled: false}\n"
    )
    with sqlite3.connect(tmp_path / "agent.db") as db:
        # No data column: this enforces that credential payloads are never queried.
        db.execute("CREATE TABLE auth_credentials (provider TEXT, disabled_cause TEXT)")
        db.executemany(
            "INSERT INTO auth_credentials VALUES (?, ?)",
            [("xai-oauth", None), ("fireworks", "disabled")],
        )
    assert catalog._local_configuration() == {"google", "xai-oauth"}


def test_cli_aliases_and_host_configured_providers_are_covered(discovery, monkeypatch):
    context, rows = discovery
    context["bot"].providers = Providers.from_dict(
        {
            "providers": {
                "claude": {"type": "claude", "transport": "cli", "models": []},
                "chatgpt": {"type": "openai", "transport": "cli", "models": []},
                "api": {"type": "openai", "models": []},
            }
        }
    )
    monkeypatch.setattr(
        catalog,
        "_local_configuration",
        lambda: {"xai", "xai-oauth", "google", "fireworks", "unsupported"},
    )
    for provider in ("anthropic", "openai", "xai", "xai-oauth", "google", "fireworks"):
        rows[provider] = [model(provider + "-one")]
    result = catalog.check_available(context)
    covered = {c["provider"] for c in result["coverage"]}
    assert covered == {
        "anthropic",
        "openai",
        "openai-codex",
        "xai",
        "xai-oauth",
        "google",
        "fireworks",
        "unsupported",
    }
    records = {m["provider"]: m for m in result["new"]}
    assert records["xai"]["allowed_for_coding"] is False
    assert records["google"]["routable"] is False
    assert records["xai-oauth"]["allowed_for_coding"] is True
    assert records["fireworks"]["private"] is True


def install_http(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        catalog.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_official_anthropic_pagination_uses_only_get(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        if not request.url.params:
            return httpx.Response(
                200, json={"data": [{"id": "one"}], "has_more": True, "last_id": "one"}
            )
        assert request.url.params["after_id"] == "one"
        return httpx.Response(200, json={"data": [{"id": "two"}], "has_more": False})

    install_http(monkeypatch, handle)
    assert [
        m["id"] for m in catalog._official_models(catalog.Source("claude", "anthropic"))
    ] == ["one", "two"]
    assert len(requests) == 2


def test_official_http_failure_never_echoes_key_or_body(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-not-for-storage")
    install_http(
        monkeypatch,
        lambda request: httpx.Response(401, json={"error": "secret-not-for-storage"}),
    )
    with pytest.raises(catalog.ModelCatalogError) as raised:
        catalog._official_models(catalog.Source("api", "openai"))
    rendered = "".join(traceback.format_exception(raised.value))
    assert "secret-not-for-storage" not in rendered
    assert "401" not in rendered


def test_incomplete_official_pagination_is_a_failure(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    install_http(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": [], "has_more": True}),
    )
    with pytest.raises(catalog.ModelCatalogError):
        catalog._official_models(catalog.Source("api", "openai"))


def test_xai_official_price_units_and_google_capabilities():
    providers = Providers()
    xai = catalog._normalize(
        catalog.Source("xai", "xai"),
        {
            "id": "grok",
            "prompt_text_token_price": 20000,
            "completion_text_token_price": 100000,
            "context_length": 200000,
        },
        "official_list",
        providers,
        (),
    )
    assert xai["pricing"] == {"input": 2, "output": 10}
    assert xai["priced"] is True
    assert xai["routable"] is False
    google = catalog._normalize(
        catalog.Source("google", "google"),
        {
            "name": "models/gemini",
            "inputTokenLimit": 1000000,
            "outputTokenLimit": 64000,
            "supportedGenerationMethods": ["generateContent"],
        },
        "official_list",
        providers,
        (),
    )
    assert google["id"] == "gemini"
    assert google["capabilities"]["inputTokenLimit"] == 1000000
    assert google["priced"] is False


def test_secret_fields_and_values_are_not_persisted(discovery, monkeypatch):
    context, rows = discovery
    monkeypatch.setenv("CATALOG_TEST_API_KEY", "secret-not-for-storage")
    rows["openai-codex"] = [
        model(
            thinking={
                "mode": "secret-not-for-storage",
                "api_key": "also-secret",
                "access_token": "another-secret",
            }
        )
    ]
    result = catalog.check_available(context)
    serialized = json.dumps(result) + json.dumps(
        ModelCatalogSnapshot.objects.get().models
    )
    assert "secret-not-for-storage" not in serialized
    assert "also-secret" not in serialized
    assert "another-secret" not in serialized
    assert "[redacted]" in serialized


@pytest.mark.parametrize("provider", ["google", "fireworks"])
def test_google_and_fireworks_pagination(monkeypatch, provider):
    monkeypatch.setenv(catalog.KEY_ENVS[provider], "test-key")
    monkeypatch.setenv("FIREWORKS_ACCOUNT_ID", "test-account")
    pages = []

    def handle(request):
        assert request.method == "GET"
        assert "test-key" not in str(request.url)
        pages.append(request.url)
        if provider == "fireworks":
            assert request.url.path == "/v1/accounts/test-account/models"
        if not request.url.params:
            return httpx.Response(
                200, json={"models": [{"name": "one"}], "nextPageToken": "next"}
            )
        assert request.url.params["pageToken"] == "next"
        return httpx.Response(200, json={"models": [{"name": "two"}]})

    install_http(monkeypatch, handle)
    assert len(catalog._official_models(catalog.Source(provider, provider))) == 2
    assert len(pages) == 2


def test_authoritative_membership_enriches_exact_ids_without_importing_catalog_only_models(
    discovery, monkeypatch
):
    context, rows = discovery
    rows["openai-codex"] = [model(), model("catalog-only")]
    monkeypatch.setattr(
        catalog,
        "_official_models",
        lambda source: [{"id": "coding-one"}, {"id": "new-unpriced"}],
    )
    result = catalog.check_available(context)
    records = {m["id"]: m for m in result["new"]}
    assert set(records) == {"coding-one", "new-unpriced"}
    assert records["coding-one"]["priced"] is True
    assert records["coding-one"]["pricing_source"] == "omp_cache"
    assert records["coding-one"]["pricing_authority"] == "catalog"
    assert records["new-unpriced"]["priced"] is False


def test_lifecycle_change_is_preserved_and_retirement_is_not_routable():
    record = catalog._normalize(
        catalog.Source("claude", "anthropic"),
        model(
            "claude-one",
            lifecycle="retired",
            retires_at="2026-11-01",
            max_input_tokens=200000,
        ),
        "official_list",
        Providers(),
        (),
    )
    assert record["capabilities"]["lifecycle"] == "retired"
    assert record["capabilities"]["max_input_tokens"] == 200000
    assert record["routable"] is False


@pytest.mark.parametrize("route", ["agent", "pinned"])
def test_removed_canonical_agent_or_pinned_model_is_flagged(discovery, route):
    from django_ergo.bots.routing import AgentChoice

    context, rows = discovery
    providers = context["bot"].providers
    providers.routing.tiers = {}
    if route == "agent":
        providers.routing.agents = {
            "medium": [
                AgentChoice(
                    agent="codex", provider="chatgpt", model="openai-codex/coding-one"
                )
            ]
        }
    else:
        context["bot"].model_ref = lambda: "chatgpt/coding-one"
    rows["openai-codex"] = [model()]
    catalog.check_available(context)
    rows["openai-codex"] = []
    result = catalog.check_available(context)
    assert result["flagged"][0]["reason"] == "removed_in_route"


def test_short_environment_marker_does_not_corrupt_catalog_metadata(
    discovery, monkeypatch
):
    context, rows = discovery
    monkeypatch.setenv("UNRELATED_KEY", "x")
    rows["openai-codex"] = [model(input=["text"])]
    result = catalog.check_available(context)
    assert result["new"][0]["capabilities"]["input"] == ["text"]


def test_api_key_does_not_replace_subscription_membership(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    def unexpected(request):
        msg = "Subscription membership must not call the API account list"
        raise AssertionError(msg)

    install_http(monkeypatch, unexpected)
    assert (
        catalog._official_models(
            catalog.Source("subscription", "anthropic", subscription=True)
        )
        is None
    )


def test_catalog_error_restricts_audit_values_to_public_identifiers():
    error = catalog.ModelCatalogError("secret-provider-value", "secret-source-value")
    assert error.provider == "other"
    assert error.source == "catalog"
    assert "secret" not in str(error)


def test_large_private_catalog_survives_the_actual_workflow_wrapper(
    discovery, monkeypatch
):
    from django_ergo.bots.schedules import Action
    from django_ergo.bots.schedules import _run_python

    context, rows = discovery
    monkeypatch.setattr(catalog, "_local_configuration", lambda: {"fireworks"})
    expected = {f"private-{index}" for index in range(100)}
    rows["fireworks"] = [model(name, cost={}) for name in sorted(expected)]
    action = Action("python", function="ergo.models.check_available")
    result = _run_python(context["bot"], action, context)
    assert {record["id"] for record in result["new"]} == expected
    assert all(record["private"] and not record["routable"] for record in result["new"])
    assert all(record["capabilities"]["maxTokens"] == 32000 for record in result["new"])
    flagged = {(flag["selector"], flag["reason"]) for flag in result["flagged"]}
    assert flagged == {
        (f"fireworks/{name}", reason)
        for name in expected
        for reason in ("private", "unpriced", "coding_provider_not_allowed")
    }
    assert _run_python(context["bot"], action, context)["notify"] is False


@pytest.mark.parametrize("populated_baseline", [False, True])
@pytest.mark.parametrize("overflow", ["items", "bytes"])
def test_report_validation_failure_never_consumes_a_diff(
    discovery, populated_baseline, overflow
):
    from django_ergo.bots.schedule_workflow import WorkflowError
    from django_ergo.bots.schedules import Action
    from django_ergo.bots.schedules import _run_python

    context, rows = discovery
    action = Action("python", function="ergo.models.check_available")
    rows["openai-codex"] = [model()]
    if populated_baseline:
        _run_python(context["bot"], action, context)
    previous = list(ModelCatalogSnapshot.objects.values("models", "coverage"))
    excessive = [0] * 10001 if overflow == "items" else "x" * 1_000_001
    rows["openai-codex"] = [model("later", thinking={"details": excessive})]
    with pytest.raises(WorkflowError):
        _run_python(context["bot"], action, context)
    assert list(ModelCatalogSnapshot.objects.values("models", "coverage")) == previous
    rows["openai-codex"] = [model("later")]
    recovered = _run_python(context["bot"], action, context)
    assert [record["id"] for record in recovered["new"]] == ["later"]
    assert [record["id"] for record in recovered["removed"]] == (
        ["coding-one"] if populated_baseline else []
    )
