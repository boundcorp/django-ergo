"""Explicit unchanged results for quiet recurring checks, not ordinary replies."""

from pydantic import Field
from pydantic import model_validator

from django_ergo.conversation.chat_reply import ChatReply


class QuietRecurringReply(ChatReply):
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
    """Only recurring checks opt into unchanged suppression; gates stay on spec."""
    if metadata.get("recurring_wake") and not metadata.get("periodic_checkins"):
        spec.response_model = QuietRecurringReply
        spec.system_prompt += (
            "\nThis is a quiet recurring check. Set unchanged=true only after "
            "checking and finding no meaningful change; do not send routine "
            "all-clear messages. Changes, questions, and attachments must remain "
            "visible. Never use unchanged to hide a failure or approval request."
        )
    return spec
