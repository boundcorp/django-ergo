# Message attachments

User messages can carry images, audio and documents:

```python
from django_ergo.conversation.attachments import Attachment

await engine.send(session, "What's in the fridge?",
                  attachments=[Attachment.from_path("fridge.jpg")])

# Also on run_conversation_turn, run_workflow_task, run_structured_call,
# revise_structured_call, and Engine.append_user_message.
Attachment(media_type="audio/ogg", data=voice_bytes, filename="note.ogg")
Attachment(media_type="image/jpeg", url="https://example.com/photo.jpg")
```

`kind` (`image`, `audio`, `document`) is inferred from the media type. Each
attachment is stored as a `ConversationAttachment` row, linked to its message
by sequence. The bytes go in a `FileField` on default storage (under
`ergo/attachments/`), so the host project needs `MEDIA_ROOT` or another
storage backend. The row also records size and sha256.

How engines send them:

| kind | Claude | OpenAI (Chat Completions) |
| --- | --- | --- |
| image | `image` block (base64 or URL), placed before the text | `image_url` part (data URL or URL) |
| document | `document` block (PDF, plain text) | `file` part |
| audio | transcript as text | `input_audio` (wav/mp3) when engine config has `"audio_input": True`, otherwise the transcript as text |

Audio transcripts come from `Attachment(transcript=...)`, or from
`DJANGO_ERGO["AUDIO_TRANSCRIBER"]` when that is set. It's an async callable
`(data, media_type, filename) -> str`, and
`django_ergo.conversation.attachments.openai_transcriber` (whisper-1) is
provided. A failed transcription is logged and leaves the transcript empty.

Renderers and compaction summaries show attachments as placeholders, so
transcripts don't carry base64 data.

## Images in tool results

A tool can return images with its text (`django_ergo.conversation.images`):

```python
from django_ergo.conversation.images import ToolImage, ToolResult

return ToolResult("Here is the chart", [ToolImage(png_bytes, name="chart.png")])
return ToolResult("The photo", [ToolImage.from_attachment(row)])  # a stored file
```

A bot tool may also return a bare `ToolImage`. Claude gets `image` blocks
inside the `tool_result`. OpenAI tool messages can't hold images, so the tool
message gets the text and a user message with the image parts follows the
tool messages.

In a session, image bytes are saved as a session file (source `bot`), and the
stored tool result keeps an `image_ref` item (`attachment_id`, `name`,
`media_type`), not base64: in `ClaudeContentBlock.tool_result_content` for
Claude, in `OpenAIMessage.images` for OpenAI. Standalone structured calls
keep a note instead of the bytes in their transcript.

## How many images a call carries

Images sent with user messages and images from tool results share one
window: each model call carries the latest `DJANGO_ERGO["IMAGES_IN_CONTEXT"]`
images (default 2). Older ones are replaced by `[image omitted: name
(id=...)]`. With Pillow installed (`django-ergo[images]`), images are downscaled to
`DJANGO_ERGO["IMAGE_MAX_SIDE"]` pixels (default 1024) on the long side
before sending; without it they are sent as they are when they're JPEG, PNG,
GIF or WebP under 5 MB, and left out otherwise.
