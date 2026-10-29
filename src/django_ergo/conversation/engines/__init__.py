"""Engine implementations."""

ENGINE_REGISTRY = {
    ("claude", "api"): "django_ergo.conversation.engines.claude_api.ClaudeAPIEngine",
    ("claude", "cli"): "django_ergo.conversation.engines.claude_code.ClaudeCodeEngine",
    ("openai", "api"): "django_ergo.conversation.engines.openai_api.OpenAIAPIEngine",
    ("openai", "cli"): "django_ergo.conversation.engines.codex_cli.CodexCLIEngine",
    # Private pools only (bots.providers private_pools); it refuses to send
    # without a grant (conversation.inference_policy).
    (
        "fireworks",
        "api",
    ): "django_ergo.conversation.engines.fireworks_api.FireworksAPIEngine",
}
