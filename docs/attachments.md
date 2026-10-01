# Message attachments

User messages can carry images, audio and documents:

```python
from django_ergo.conversation.attachments import Attachment

await engine.send(session, "What's in the fridge?",
                  attachments=[Attachment.from_path("fridge.jpg")])

# Also on run_conversation_turn, run_workflow_task, run_structured_call,
# StructuredSession.start/send, and Engine.append_user_message.
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
