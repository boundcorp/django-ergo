"""Engine implementations."""

ENGINE_REGISTRY = {
    ("claude", "api"): "django_ergo.conversation.engines.claude_api.ClaudeAPIEngine",
    ("claude", "cli"): "django_ergo.conversation.engines.claude_code.ClaudeCodeEngine",
    ("openai", "api"): "django_ergo.conversation.engines.openai_api.OpenAIAPIEngine",
    ("openai", "cli"): "django_ergo.conversation.engines.codex_cli.CodexCLIEngine",
}
