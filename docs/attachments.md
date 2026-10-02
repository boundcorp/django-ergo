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

## Archiving session files

A long chat collects files the bot no longer needs. The attachments plugin's
`ergo_attachments_archive` tool archives them, so they stop crowding the
bot's working set:

```python
plugin.archive(ctx, ["<id>", "<id>"])                   # these files
plugin.archive(ctx, all_files=True)                     # every file in the chat
plugin.archive(ctx, all_files=True, older_than_days=7)  # not updated for a week
plugin.archive(ctx, all_files=True, keep_latest=3)      # all but the newest three
plugin.unarchive(ctx, ["<id>"])                         # bring one back
```

`older_than_days` and `keep_latest` also narrow a list of ids. Archiving sets
`ConversationAttachment.archived_at`; the file stays stored and readable by id
(`ergo_attachments_read`, `ergo_attachments_look`). Archived files are left
out of `ergo_attachments_list` (unless `include_archived`), of the "Files in
this chat" context section (which notes how many are archived) and of the
skill hint's count. A file sent with a message stays in the message history,
where the image window above already limits what's sent. The bot archives
only files in its own session. Archiving is reversible, so neither tool asks
for approval. Ergonaut's Files panel hides archived files behind a "Show N
archived" toggle.
