"""ChatReply: the structured response a bot gives to every chat message.

A chat reply is a structured call made against the chat session. The model
uses its tools as usual and finishes by calling ``send_reply`` with either:

- a **message**: what to tell the user, optionally with suggested
  follow-ups the user can tap, or
- a **question**: something the bot needs from the user, with suggested
  answers. Channels show the suggestions as buttons; the user can still
  type anything.

    spec = chat_reply_spec(toolkits=[...])
    result = await run_structured_call(spec, "Plan dinner", session=session)
    reply: ChatReply = result.parsed
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from django_ergo.conversation.structured import StructuredCallSpec

if TYPE_CHECKING:
    from django_ergo.conversation.toolkit import Toolkit

CHAT_REPLY_KIND = "chat_reply"
CHAT_REPLY_TOOL = "send_reply"
MAX_SUGGESTIONS = 6

CHAT_REPLY_INSTRUCTIONS = f"""\
Every reply to the user goes through the {CHAT_REPLY_TOOL} tool; never \
answer in plain text. Use your other tools first if you need them, then call \
{CHAT_REPLY_TOOL} exactly once.
- type "message": your answer or update. Add suggestions only when there \
are obvious next things the user might say.
- type "question": you need a decision or a detail from the user before \
you can go on. Ask one question and give 2 to 4 short suggested answers.
- status: one short line for the thread list, in plain words: what you need \
from the user ("Approve the migration run"), or what you are doing now \
("Running the full test suite"). Leave it empty when there's nothing to add.
- When you read a large tool result, note the detail you need; call the tool \
again only if you still need it."""


class ChatReply(BaseModel):
    """A bot's reply: a message, or a question with suggested answers."""

    type: Literal["message", "question"] = Field(
        default="message",
        description='"message" to tell the user something, "question" to ask them',
    )
    text: str = Field(description="What the user sees, in plain conversational text")
    suggestions: list[str] = Field(
        default_factory=list,
        max_length=MAX_SUGGESTIONS,
        description=(
            "Short replies the user can pick instead of typing: the answer "
            "options for a question, or likely follow-ups for a message"
        ),
    )
    status: str = Field(
        default="",
        description=(
            "One short line for the thread list: what you need from the user, "
            "or what you are doing now"
        ),
    )

    @property
    def is_question(self) -> bool:
        return self.type == "question"

    def as_message(self) -> str:
        """The reply as stored in chat history."""
        if not self.suggestions:
            return self.text
        options = " / ".join(self.suggestions)
        return f"{self.text}\n\nSuggested replies: {options}"


def chat_reply_spec(
    toolkits: list[Toolkit] | None = None,
    *,
    instructions: str = "",
    max_turns: int = 50,
    max_tokens: int | None = None,
) -> StructuredCallSpec:
    """The structured-call spec for one chat reply turn."""
    system = "\n\n".join(p for p in (instructions, CHAT_REPLY_INSTRUCTIONS) if p)
    return StructuredCallSpec(
        kind=CHAT_REPLY_KIND,
        system_prompt=system,
        response_model=ChatReply,
        toolkits=list(toolkits or []),
        max_turns=max_turns,
        wrap_up=True,
        max_tokens=max_tokens,
        output_tool_name=CHAT_REPLY_TOOL,
    )
