"""Static, honest coverage manifest for the passive cost ledger.

This is a declaration reviewed with the code, not a measurement. It never says a
source is complete: nothing here is wired to automatic ingestion, and an empty
ledger means "nothing recorded", not "nothing spent".
"""

from dataclasses import asdict
from dataclasses import dataclass

# Statuses, strongest first. None of them means live or complete coverage.
ADAPTER_CALLABLE = (
    "adapter_callable_not_wired"  # explicit function exists; nobody calls it
)
AGGREGATE_ONLY = "aggregate_only"  # visible only as existing token/list-price estimates
NOT_INTEGRATED = "not_integrated"  # needs a change in another repository/owner
EXTERNAL_UNKNOWN = "external_unknown"  # billing is not ours to observe
UNSUPPORTED = "unsupported"  # no reviewed contract


@dataclass(frozen=True)
class CoverageEntry:
    source: str
    status: str
    granularity: str
    uncertainty: str
    duplicate_rule: str


_ENTRIES = (
    CoverageEntry(
        "llm_inference_request",
        ADAPTER_CALLABLE,
        "per request",
        "cost_usd is a list-price estimate, never a provider bill; reservation is a ceiling",
        "operation per InferenceRequest id; StructuredCall/AgentUsage aggregates must not also be ingested",
    ),
    CoverageEntry(
        "structured_call_and_agent_usage",
        AGGREGATE_ONLY,
        "aggregate",
        "token totals priced at list; not per-request, not billed",
        "constituent requests are the only ledger source; aggregates stay in /costs estimate",
    ),
    CoverageEntry(
        "native_cli_agents",
        AGGREGATE_ONLY,
        "cumulative per worker/model",
        "subscription or unknown funding; API-equivalent value is not money spent",
        "no transcript or cumulative replay into the ledger",
    ),
    CoverageEntry(
        "runpod_native_compute",
        ADAPTER_CALLABLE,
        "per rental resource period",
        "estimate = verified rate x bounded period; terminated is not reconciled; "
        "settled only by an explicit provider charge",
        "resource/component/exact period; overlapping periods rejected, charge bound to one operation",
    ),
    CoverageEntry(
        "runpod_native_storage",
        ADAPTER_CALLABLE,
        "per rental resource period",
        "storage liability continues after stop and is separate from compute; unknown until evidenced",
        "its own operation key, never folded into compute",
    ),
    CoverageEntry(
        "runpod_rest_api_quota",
        UNSUPPORTED,
        "request count",
        "API rate budget is a request limit, not money",
        "never mapped to cost",
    ),
    CoverageEntry(
        "runpod_serverless",
        UNSUPPORTED,
        "provider charge id",
        "no reviewed adapter; historical charges are not imported",
        "provider charge id only",
    ),
    CoverageEntry(
        "ads_mediagen_central",
        NOT_INTEGRATED,
        "per provider operation",
        "Storytime central budget and creativekit remain the authority; MediaSpend is reporting only",
        "preserve provider operation id; summaries must link, not re-bill",
    ),
    CoverageEntry(
        "gemini_developer_api_images",
        NOT_INTEGRATED,
        "per operation",
        "billed amount unknown until provider evidence; not a Google AI subscription allowance",
        "exact model/operation identity",
    ),
    CoverageEntry(
        "antigravity_cli",
        UNSUPPORTED,
        "subscription",
        "subscription funding, separate from metered Gemini API",
        "never summed with API spend",
    ),
    CoverageEntry(
        "minimax_sume_heygen_grok",
        NOT_INTEGRATED,
        "fractional credits",
        "separate caps; no pooled ceilings; Grok is subscription-only",
        "per provider caps",
    ),
    CoverageEntry(
        "ryan_minimax_proxy",
        EXTERNAL_UNKNOWN,
        "none",
        "external_shared funding, billing_unknown",
        "no ledger rows without provider evidence",
    ),
    CoverageEntry(
        "meta_ads_delivery",
        UNSUPPORTED,
        "campaign/adset/ad levels overlap",
        "levels nest; only one nonoverlapping level may ever be billed",
        "never sum levels",
    ),
    CoverageEntry(
        "direct_scripts_bypassing_mediagen",
        NOT_INTEGRATED,
        "none",
        "explicit gap",
        "n/a",
    ),
)


def coverage_manifest() -> dict:
    return {
        "live_ingestion": False,
        "complete": False,
        "note": "Declared coverage; an empty or small ledger is not zero spend.",
        "sources": [asdict(entry) for entry in _ENTRIES],
    }
