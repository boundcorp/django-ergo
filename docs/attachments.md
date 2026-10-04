# Attachments

Files in conversations: images, audio, PDFs and text. The first part covers
how files work in a bot's chats; the rest is the library underneath.

## Files in a bot's chats

A chat's files come from three places, recorded on each file as its
`source`:

- **Sent with a message.** In Ergonaut, the 📎 button or a pasted image;
  in Telegram, photos (an album arrives as one turn), voice notes, audio
  and documents. These reach the model natively with the message (see the
  table below).
- **Uploaded to the chat** from Ergonaut's Files panel, without a message.
- **Written by the bot**: text files from the `attachments` plugin, pages
  from the `pages` plugin, and images that tools return.

Every file is a `ConversationAttachment` row on the chat's session, stored
on Django's default storage (Garage or S3 in Ergonaut, local files without
it).

### The attachments plugin

Add it to let the bot work with files beyond what came in with a message:

```yaml
plugins:
  - name: attachments
    max_bytes: 5000000     # largest file the bot may write
    other_sessions: true   # may read files from the same person's other chats
```

Its `attachments` skill gives:

| Tool | Does |
| --- | --- |
| `ergo_attachments_list` | files in this chat, or another of the person's chats |
| `ergo_attachments_read` | a file's text (or a short description of a non-text file) |
| `ergo_attachments_look(attachment_id, question)` | see an image or PDF: an image comes back in the tool result so the bot looks itself; other files go to the bot's model in a separate call that answers the question |
| `ergo_attachments_create`, `ergo_attachments_update` | write or replace a text file in this chat |
| `ergo_attachments_archive`, `ergo_attachments_unarchive` | clear old files out of the working set (below) |

While the skill is loaded, each turn's context lists the chat's files; while
it isn't, the skill list shows how many there are, so the bot knows to load
it when someone uploads something. The bot writes only into its own chat
and reads other chats only when they belong to the same person.

### Images and the context window

Images are expensive, so each model call carries only the latest two
(`IMAGES_IN_CONTEXT`), whether they came from people or tools; older ones
become `[image omitted: name (id=...)]` and the bot can look again by id.
Install the `images` extra (Pillow) so images are downscaled to 1024px
first. Details are in [How many images a call carries](#how-many-images-a-call-carries).

### Audio

Voice notes reach the model as a transcript (OpenAI can take raw audio
with `audio_input`, below; Claude can't hear it at all). Set
`DJANGO_ERGO["AUDIO_TRANSCRIBER"] =
"django_ergo.conversation.attachments.openai_transcriber"` (with
`OPENAI_API_KEY`) to transcribe on arrival. Without a transcriber the bot
sees only that an audio file arrived.

### Pinning and pages

Any chat file can be pinned (Files panel, or the `pages` plugin's
`ergo_page_pin`); pinned files show as tabs above the transcript. HTML and
`.jhtml` files the bot writes are served sandboxed (`Content-Security-Policy:
sandbox`), so their scripts run without the app's cookies. See
[Pages and pins](bots.md#pages-and-pins).

### From tool code

Tools can return images with `ToolResult` (below), and read a chat's files
through the session: `ctx.session.attachments.all()`, with
`django_ergo.conversation.attachments.read_text(row)` for text.

## Pull requests

A pull request a bot links in its reply, or a worker links in its result, is
recorded as a chat file with no stored bytes (`django_ergo.conversation.links`):
`url` is the PR, and `metadata` has `link: github_pr`, `repo`, `number`,
`title`, `state` (`open`, `draft`, `merged`, `closed`) and `checks`
(`passing`, `failing`, `pending`). `record_pull_requests(session, text)` adds
the new ones; `refresh_pull_request(row)` reads the live state with the GitHub
CLI, which apps run on a schedule (Ergonaut: every minute, for PRs not merged
or closed).

## Library: attachments on messages

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
