"""Signals for hosts that want to observe the library (Ergonaut's Prometheus metrics).

Receivers must be cheap and must not raise; senders call ``send_robust`` so a
broken receiver never affects a turn or a tool call. No message text, tool
arguments or results are sent: only names, counts, durations and outcomes.
"""

from django.dispatch import Signal

# One run of a StructuredCall's loop ended (a turn, or a resumed segment of one
# after an approval). Keyword arguments:
#   call: the StructuredCall (``status``, ``kind``, ``engine_type``,
#     ``model_name``, ``error_category``)
#   bot_name: the session's bot, or "" for a standalone call
#   duration: seconds this segment ran
#   tokens: this segment's token deltas, a dict with ``input``, ``output``,
#     ``cache_write``, ``cache_read`` and ``reasoning`` (part of output)
#   cost_usd: this segment's cost as a float, or None when unpriced
#   requests: model responses this segment received
structured_call_finished = Signal()

# One structured call segment started. Keyword arguments: ``call``, ``bot_name``.
structured_call_started = Signal()

# One tool call was dispatched. Keyword arguments:
#   tool: the tool's name, or "unknown" when no toolkit has it
#   is_error: whether the result is an error the model sees
#   duration: seconds the tool ran
tool_call_finished = Signal()
