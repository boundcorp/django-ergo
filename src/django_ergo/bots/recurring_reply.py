"""Structured outcomes and notification policy for recurring checks."""

from typing import Literal

from pydantic import Field
from pydantic import model_validator

from django_ergo.conversation.chat_reply import ChatReply


class RecurringReply(ChatReply):
    """Record the outcome of every recurring check, including check-ins."""

    wake_outcome: Literal["unchanged", "wait_ended", "progress"] | None = Field(
        default=None,
        description=(
            "unchanged when the condition or work is still waiting without "
            "meaningful progress; wait_ended when the condition being waited for "
            "has ended; progress only for a concrete advance toward the goal."
        ),
    )


class QuietRecurringReply(RecurringReply):
    """Keep the structured audit result without notifying about unchanged state."""

    unchanged: bool = Field(
        default=False,
        description=(
            "True only when this recurring check found no meaningful change and "
            "needs no user action. False for changes, questions, or attachments."
        ),
    )

    @model_validator(mode="after")
    def quiet_unchanged(self):
        if self.unchanged and not self.is_question and not self.attachments:
            self.text = ""
            self.suggestions = []
            self.status = ""
        return self


def apply_notification_policy(spec, metadata: dict):
    """Apply the recurring outcome schema and optional quiet notification policy."""
    if metadata.get("recurring_wake"):
        spec.response_model = RecurringReply
        spec.system_prompt += (
            "\nThis is a recurring check. Report wake_outcome as unchanged if "
            "the wait continues without meaningful progress, wait_ended if the "
            "condition being waited for has ended, or progress only for a "
            "concrete advance toward the goal. A progress claim does not reset "
            "the platform's 12-check safety cap."
        )
        if not metadata.get("periodic_checkins"):
            spec.response_model = QuietRecurringReply
            spec.system_prompt += (
                "\nThis is a quiet recurring check. Set unchanged=true only after "
                "checking and finding no meaningful change; do not send routine "
                "all-clear messages. Changes, questions, and attachments must remain "
                "visible. Never use unchanged to hide a failure or approval request."
            )
    return spec
