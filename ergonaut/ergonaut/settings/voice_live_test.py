"""Settings with the priced voice delegation budget (docs/realtime-voice.md, "Conditional cost estimate").

The project settings plus the delegation budget that section prices. Only the voice bridge needs it
(``DJANGO_SETTINGS_MODULE=ergonaut.settings.voice_live_test``; the hosted ``voice-bridge`` Deployment
uses it): the bridge builds each delegation's budget and the worker enforces the one the message
carries. The default settings leave background delegation off. Change a number here and the estimate
there no longer holds: recompute it first.
"""

from .project import *  # noqa: F403

REALTIME_VOICE_DELEGATION_BUDGET = {
    "max_turns": 4,
    "max_input_tokens": 40_000,
    "max_output_tokens": 1_500,
    "deadline_seconds": 45,
    "start_within_seconds": 30,
    # The engine must be on exactly this model, so the price in the docs is the one that applies.
    "model": "gpt-6-luna",
    # Read-only, and still intersected with the selected chat's already offered tools.
    "allowed_tools": [
        "ergo_chat_history_read",
        "ergo_chat_history_search",
        "ergo_bot_list",
        "ergo_thread_list",
        "ergo_worker_list",
    ],
}
