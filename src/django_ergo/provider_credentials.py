"""Credential *names* withheld from ordinary child processes.

Keep a conservative union across reloads, and reread registered config files at
launch so a stale registry cannot expose a newly configured alias. No credential
values are cached here. Unreadable existing sources block the launch; removed
sources keep their previous names blocked, and are checked again if recreated.
This is environment hygiene, not isolation from same-UID code or credential files.
"""

from collections.abc import Mapping
from pathlib import Path
from threading import RLock

_LOCK = RLock()
_NAMES: set[str] = set()
_SOURCES: set[Path] = set()
SDK_CREDENTIALS = frozenset(
    [
        "ABLITERATION_API_KEY",
        "AI_GATEWAY_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_SEARCH_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AZURE_API_KEY",
        "AZURE_CLIENT_SECRET",
        "AZURE_OPENAI_AD_TOKEN",
        "AZURE_OPENAI_API_KEY",
        "BOUNDCORP_ORG_OPENAI_KEY",
        "BRAVE_API_KEY",
        "CEREBRAS_API_KEY",
        "CHARM_HYPER_API_KEY",
        "CLINE_API_KEY",
        "CLOUDFLARE_AI_GATEWAY_API_KEY",
        "CLOUDSDK_AUTH_ACCESS_TOKEN",
        "CODEX_API_KEY",
        "COHERE_API_KEY",
        "COMMAND_CODE_API_KEY",
        "CO_API_KEY",
        "DEEPSEEK_API_KEY",
        "EXA_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIREWORKS_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_ACCESS_TOKEN",
        "GOOGLE_CLOUD_API_KEY",
        "GROQ_API_KEY",
        "HELMCODE_API_KEY",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "JINA_API_KEY",
        "KAGI_API_KEY",
        "KILO_API_KEY",
        "KIMI_SEARCH_API_KEY",
        "MINIMAX_API_KEY",
        "MISTRAL_API_KEY",
        "MNEMOPI_EMBEDDING_API_KEY",
        "MNEMOPI_LLM_API_KEY",
        "MOONSHOT_API_KEY",
        "MOONSHOT_SEARCH_API_KEY",
        "OLLAMA_CLOUD_API_KEY",
        "OPENAI_API_KEY",
        "OPENCODE_API_KEY",
        "OPENROUTER_API_KEY",
        "PARALLEL_API_KEY",
        "PERPLEXITY_API_KEY",
        "SINGULARITYAPI_DEV_API_KEY",
        "SINGULARITYAPI_TECH_API_KEY",
        "SMITHERY_API_KEY",
        "STENCIL_API_KEY",
        "STEPFUN_API_KEY",
        "SYNTHETIC_API_KEY",
        "TAVILY_API_KEY",
        "TINYFISH_API_KEY",
        "TOGETHERAI_API_KEY",
        "TOGETHER_API_KEY",
        "TYPESAFE_API_KEY",
        "UMANS_AI_CODING_PLAN_API_KEY",
        "WAFER_SERVERLESS_API_KEY",
        "XAI_API_KEY",
        "YOLO_AUTO_API_KEY",
        "ZAI_API_KEY",
    ]
)


def register_credential_config(config) -> None:
    """Collect arbitrary api_key_env/key_env selectors, including plugin aliases."""
    if isinstance(config, Mapping):
        for key, value in config.items():
            if (
                isinstance(value, str)
                and value
                and (str(key).lower().endswith("api_key_env") or key == "key_env")
            ):
                with _LOCK:
                    _NAMES.add(value.upper())
            else:
                register_credential_config(value)
    elif isinstance(config, list | tuple):
        for value in config:
            register_credential_config(value)


def register_credential_source(path) -> None:
    with _LOCK:
        _SOURCES.add(Path(path).absolute())


def provider_credential_names() -> frozenset[str]:
    from django.conf import settings

    if settings.configured:
        register_credential_config(getattr(settings, "DJANGO_ERGO", {}))
        register_credential_config(
            {
                "VOICE_OPENAI_API_KEY_ENV": getattr(
                    settings, "VOICE_OPENAI_API_KEY_ENV", ""
                )
            }
        )
    with _LOCK:
        sources = tuple(_SOURCES)
    if sources:
        import yaml

        for path in sources:
            try:
                content = path.read_text()
            except FileNotFoundError:
                # A removed bot/provider cannot introduce new names. Retain its
                # old names and source path, including across later recreation.
                continue
            register_credential_config(yaml.safe_load(content))
    with _LOCK:
        return SDK_CREDENTIALS | _NAMES
