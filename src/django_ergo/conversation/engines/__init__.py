"""Engine implementations."""

ENGINE_REGISTRY = {
    ("claude", "api"): "django_ergo.conversation.engines.claude_api.ClaudeAPIEngine",
    ("openai", "api"): "django_ergo.conversation.engines.openai_api.OpenAIAPIEngine",
}
