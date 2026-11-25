/**
 * The seam between Ergonaut's session API and `@ergo/chat-ui`: Ergonaut sessions/messages/calls in, the package's
 * `Message`/`Part`/`Attachment` model out, plus the few rules the chat screen applies at that boundary.
 * Pure functions; the React side (cards, slots) lives in components/Transcript.tsx and pages/Chat.tsx.
 *
 * What the package renders and what stays here is listed in packages/ergo-chat-ui/README.md ("Ergonaut mapping").
 */
import type { Attachment, Message as ChatMessage, Part, Role } from '@ergo/chat-ui'
import type { Approval, Block, Message } from './api'
import { api } from './api'

/** Carried on every mapped message (`ChatMessage.meta`) so slots can read the Ergonaut record back. */
export type AdapterMeta = {
  source: Message
  /** `blocks[i]` is the Ergonaut block behind `parts[i]` (tool results are folded into their call, so indices differ from `source.blocks`). */
  blocks: Block[]
}

export function adapterMeta(message: ChatMessage): AdapterMeta {
  return message.meta as AdapterMeta
}

function roleOf(role: string): Role {
  return role === 'user' || role === 'assistant' ? role : 'system'
}

/**
 * One package message per Ergonaut message (id = line, the stable key SSE merges by).
 *
 * - text → text; attachment → attachment; thinking/context → custom parts (the host renders them);
 * - tool_use + the tool_result with the same id (anywhere later in the list) → one `tool` part:
 *   status `error` if `is_error`, `done` if a result exists, else `running`;
 * - tool_result blocks are consumed by their call and produce no part of their own.
 */
export function toChatMessages(messages: Message[]): ChatMessage[] {
  const results = new Map<string, Extract<Block, { type: 'tool_result' }>>()
  for (const message of messages)
    for (const block of message.blocks) if (block.type === 'tool_result') results.set(block.tool_use_id, block)
  return messages.map(message => {
    const parts: Part[] = []
    const blocks: Block[] = []
    message.blocks.forEach((block, i) => {
      const add = (part: Part) => {
        parts.push(part)
        blocks.push(block)
      }
      switch (block.type) {
        case 'text':
          return add({ type: 'text', text: block.text })
        case 'thinking':
        case 'context':
          return add({ type: 'custom', kind: block.type, id: `${message.line}:${i}`, data: block.text })
        case 'attachment':
          return add({
            type: 'attachment',
            attachment: { id: block.id ?? `${message.line}:${i}`, name: block.label, mediaType: block.media_type },
          })
        case 'tool_use': {
          const result = results.get(block.id)
          return add({
            type: 'tool',
            id: block.id,
            name: block.name,
            status: result?.is_error ? 'error' : result ? 'done' : 'running',
            input: block.input,
            result: result?.content,
          })
        }
        default:
          return // tool_result
      }
    })
    return {
      id: String(message.line),
      role: roleOf(message.role),
      parts,
      createdAt: message.timestamp ?? undefined,
      meta: { source: message, blocks } satisfies AdapterMeta,
    }
  })
}

// --- Composer attachments -------------------------------------------------------------------------------------------

/** A file picked or pasted for the next message, already uploaded to the session. */
export type Outgoing = { id: string; filename: string; image: boolean; audio: boolean }

/** Round-trips exactly through `fromAttachment`: the kind rides on `mediaType`, the preview on `url`. */
export function toAttachment(file: Outgoing): Attachment {
  return {
    id: file.id,
    name: file.filename,
    mediaType: file.image ? 'image/*' : file.audio ? 'audio/*' : undefined,
    url: api.viewUrl(file.id),
  }
}

export function fromAttachment(attachment: Attachment): Outgoing {
  return {
    id: attachment.id,
    filename: attachment.name,
    image: !!attachment.mediaType?.startsWith('image/'),
    audio: !!attachment.mediaType?.startsWith('audio/'),
  }
}

// --- Sends ----------------------------------------------------------------------------------------------------------

/** The request left the browser but no answer came back: it may or may not have been accepted. */
export function ambiguousSend(message: string): string {
  return `${message} Send may have succeeded; check the conversation before retrying.`
}

// --- Approvals ------------------------------------------------------------------------------------------------------

/** The answers collected so far for one set of waiting calls. */
export type ApprovalChoices = { batch: string; choices: Record<string, boolean> }

/** Identity of a waiting batch: the call and exactly what it waits on. A change (new call, edited input) is a different batch. */
export function approvalBatch(callId: string | undefined, waiting: Approval[]): string {
  return JSON.stringify([callId, waiting])
}

/**
 * Record one answer. Answers belong to the batch they were given for: if the waiting calls changed since (stale),
 * earlier answers are dropped, so a decision is never sent for something the reader did not see. Once every waiting
 * call has an answer the whole set is returned in `submit` (sent together, with the `waiting` it was given for, which
 * the server checks) and the collected answers reset.
 */
export function chooseApproval(
  state: ApprovalChoices,
  callId: string | undefined,
  waiting: Approval[],
  approvalId: string,
  approve: boolean,
): { state: ApprovalChoices; submit: Record<string, boolean> | null } {
  const batch = approvalBatch(callId, waiting)
  const next = { ...(state.batch === batch ? state.choices : {}), [approvalId]: approve }
  if (waiting.every(a => a.id in next))
    return {
      state: { batch, choices: {} },
      submit: Object.fromEntries(waiting.map(a => [a.id, next[a.id]])),
    }
  return { state: { batch, choices: next }, submit: null }
}
