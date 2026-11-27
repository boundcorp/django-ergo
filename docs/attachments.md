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
| `ergo_attachments_look(attachment_id, question)` | see an image, PDF or video: an image comes back in the tool result so the bot looks itself; a video goes to Gemini (below); other files go to the bot's model in a separate call that answers the question |
| `ergo_attachments_frames(attachment_id, count or times)` | still frames from a video (up to 8, evenly spaced or at given seconds), which the bot sees itself and which are saved in the chat |
| `ergo_attachments_create`, `ergo_attachments_update` | write or replace a text file in this chat |
| `ergo_attachments_edit(attachment_id, old_text, new_text, replace_all, expected_sha256 or expected_version)` | replace exact text in a text file of this chat, only if the file is unchanged since the bot's read (below) |
| `ergo_attachments_append(attachment_id, content, separator, idempotency_key, expected_sha256)` | add text to the end of a text file of this chat; a retry with the same key adds it once (below) |
| `ergo_attachments_archive`, `ergo_attachments_unarchive` | clear old files out of the working set (below) |

### Guarded edits and appends

`ergo_attachments_edit` and `ergo_attachments_append` work on text files in the
bot's own session, up to `max_bytes`. Edit needs `expected_sha256` (12 or more
hex characters, from a list or read) or `expected_version`; append accepts them
optionally. The row is locked (`SELECT ... FOR UPDATE`) and the precondition is
compared with the bytes read inside the lock, so two writers cannot both pass.
Failures change nothing and come back as structured errors: `stale_version`
(with `current_sha256`), `not_found`, `ambiguous_match` (count and first line
numbers; edit only), `conflict` (reused append key with different content),
`limit_exceeded` and `invalid_argument`. Receipts hold `version`, `sha256`,
`previous_sha256`, `changed_lines`, `total_lines` and up to 20 preview lines,
never the whole file.

Append keys are scoped to the file, stored in its metadata, and the last 100 are
kept. A retry with the same key and content returns the first receipt with
`replayed: true` before any precondition check, so a timed-out call can be
retried with its old `expected_sha256`. The separator is added only when the file
is non-empty and does not already end with it.

Limits: a file larger than `max_bytes` (an upload can be) can't be edited or
appended to, and a result over `max_bytes` is refused; nothing is truncated. The
lock covers `ergo_attachments_edit` and `_append`. `ergo_attachments_update` and
the Files panel do not take it; they are unchanged, so a whole-file write that
races a locked edit can still win after the edit commits. Page validation for
`.jhtml` files is not run on edit.

Text reads accept optional 1-based inclusive `start_line` and `end_line`.
Ranged or large reads return numbered lines, a content hash prefix and version,
and a continuation line when more lines fit in the selected range. Attachment
list and write receipts expose the full SHA-256 and version. Text output
is limited to 50,000 characters; a single longer line is cut explicitly.
Ranged reads are available for files up to `max_bytes`; larger uploads retain
the legacy bounded preview. Short, un-ranged text keeps its existing format.

While the skill is loaded, each turn's context lists the chat's files; while
it isn't, the skill list shows how many there are, so the bot knows to load
it when someone uploads something. The bot writes only into its own chat
and reads other chats only when they belong to the same person.

### Private export snapshots

Private source files cannot be shared by ordinary sends, forwards, or old
share metadata. The opt-in `private_exports` plugin requires `private: true`
and normal outward routing permission (for example `permissions.call_bots`;
parent/starter routing remains implicit). This grants no inbound access. See
[Approved private exports](bots.md#approved-private-exports) for configuration,
migration prerequisites, and the approval workflow.

Call `ergo_private_export_prepare` with an exact existing destination session
UUID, the exact brief text, and optional source-session-owned file references.
It returns the exact `destination`, `message`, and `artifacts` arguments for
`ergo_private_export`; each artifact includes `id`, `sha256`, `filename`, and
`media_type`. Inspect the source files before requesting approval: the real
authenticated user reviews the exact text and artifact IDs/digests, not a
hidden transcript. No source history is copied.

Destination/content changes, source mutation, expired (15-minute turn lifetime),
replayed, or stale approvals fail closed. A successful export creates immutable,
destination-owned snapshots. Only the exact destination session may read
them: there is no source-ID or sibling-session grant, and snapshots cannot
be forwarded or shared onward. Ordinary public sharing remains unchanged.
Private bots can still work within the same bot and receive legitimate
incoming child replies, but cannot send or forward to another bot outside
the approved export path.

Bash and custom code are not a hardened execution boundary. Bot instructions
**MUST forbid shell, client, database, or direct-file fallbacks around export
approval**, including when an export is rejected or unavailable.

### Images and the context window

Images are expensive, so each model call carries only the latest two
(`IMAGES_IN_CONTEXT`), whether they came from people or tools, plus the images
the newest round of tool calls just returned (up to 8); older ones
become `[image omitted: name (id=...)]` and the bot can look again by id.
Install the `images` extra (Pillow) so images are downscaled to 1024px
first. Details are in [How many images a call carries](#how-many-images-a-call-carries).

### Video

Neither Claude nor OpenAI takes video, so the plugin gives a bot two ways
to watch one:

- **`ergo_attachments_look`** uploads the video to the Gemini Files API
  (streamed from storage, never read into memory whole), asks Gemini the
  bot's question with the picture and the soundtrack, and deletes the
  upload. `start_seconds` and `end_seconds` limit it to part of the video,
  so a long video needn't be cut first. The answer gives timestamps and
  quotes on-screen text and speech. It needs `GEMINI_API_KEY` (a per-user
  `GEMINI_API_KEY__<USERNAME>` wins). Gemini samples one frame a second, so
  a 30-second ad is roughly 8k input tokens, about a cent on Flash.
- **`ergo_attachments_frames`** takes JPEG stills with ffmpeg: `count`
  evenly spaced frames, or frames at `times` (seconds). Each frame is a
  separate ffmpeg run that seeks straight to its timestamp and decodes one
  frame on one thread, scaled to `width` (default 768), so memory stays in
  the tens of MB however long the video is. ffmpeg runs with the highest
  OOM score, so if a pod runs out of memory anyway the kernel kills it, not
  the worker. The frames are saved in the chat as bot files, ready to attach
  to a brief or look at again.

```yaml
plugins:
  - name: attachments
    video_model: gemini-flash-latest  # any Gemini model that takes video
    gemini_api_key_env: GEMINI_API_KEY
    max_video_bytes: 2000000000       # the Files API limit
```

A video an agent job renders reaches these tools through
`local_shell_attach`. The library calls are in
`django_ergo.conversation.video` (`probe`, `extract_frames`, `ask_gemini`).

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
| other documents (video, zip...) | text naming the file and its id | the same |

The model can't read a video or a zip, so the stand-in tells it the file
arrived and gives its id and filename, which a tool that takes a chat file
(an upload tool, say) can use.

Audio transcripts come from `Attachment(transcript=...)`, or from
`DJANGO_ERGO["AUDIO_TRANSCRIBER"]` when that is set. It's an async callable
`(data, media_type, filename) -> str`, and
`django_ergo.conversation.attachments.openai_transcriber` (`gpt-transcribe`) is
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
`media_type`) in `MessageBlock.tool_result_content`, not base64. Standalone
structured calls keep a note instead of the bytes in their transcript.

## How many images a call carries

Images sent with user messages and images from tool results share one
window: each model call carries the latest `DJANGO_ERGO["IMAGES_IN_CONTEXT"]`
images (default 2), plus the images in the newest round of tool results (up to
8, `ROUND_IMAGES_MAX`), so a bot that looks at four files at once sees all four.
Older ones are replaced by `[image omitted: name
(id=...)]`. With Pillow installed (`django-ergo[images]`), images are downscaled to
`DJANGO_ERGO["IMAGE_MAX_SIDE"]` pixels (default 1024) on the long side
before sending; without it they are sent as they are when they're JPEG, PNG,
GIF or WebP under 5 MB, and left out otherwise.

## Files in this chat

The attachments plugin puts a "Files in this chat" section in each turn's
context: the newest 20 unarchived files (`CONTEXT_LISTED`), one line each
with name, media type, size and id, then a count of the older and the
archived ones. `ergo_attachments_list` lists them all.

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

For ordinary public sharing, Ergonaut's Files panel lists the chat's own
files and, under "Shared with this chat", files another chat shared with it
(`ergo_thread_send` attachments, forwards and events; a chat passes a shared
file on the same way, by id). Those stay in the sender's chat, so they open
and download from here but are pinned or deleted there. Private export
snapshots instead belong to the exact destination and cannot be reshared.
Images and videos show a preview
(`/api/attachments/<id>/thumbnail`: Pillow downscales images, ffmpeg grabs a
video frame); the panel filters
by type (images as a grid, docs, media) and searches by name once a chat has
a few files.

A tool that replaces its own earlier files (say, a fresh render of the same
board) should archive the old ones rather than delete them. Tool results in
history point at an image by file id (`image_ref`), so a deleted file leaves a
broken image in the transcript and an `[image unavailable: ...]` placeholder
for the model.
