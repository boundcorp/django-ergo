"""Engine implementations."""

ENGINE_REGISTRY = {
    ("claude", "cli"): "django_ergo.conversation.engines.claude_cli.ClaudeCLIEngine",
    ("claude", "api"): "django_ergo.conversation.engines.claude_api.ClaudeAPIEngine",
    ("openai", "api"): "django_ergo.conversation.engines.openai_api.OpenAIAPIEngine",
}
