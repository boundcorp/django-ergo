export type ChatSendSource = 'composer' | 'suggestion'

export type ChatSendPlan<Attachment extends { id: string }> = {
  attachmentIds: string[]
  composer: 'clear' | 'preserve'
  draft: string
  message: string
  outgoing: Attachment[]
  sentFiles: Attachment[]
}

/**
 * Suggestions send their own text without consuming the text or attachments
 * currently being composed for a later message.
 */
export function prepareChatSend<Attachment extends { id: string }>(
  source: ChatSendSource,
  message: string,
  draft: string,
  outgoing: Attachment[],
): ChatSendPlan<Attachment> {
  if (source === 'suggestion') {
    return { attachmentIds: [], composer: 'preserve', draft, message, outgoing, sentFiles: [] }
  }
  return {
    attachmentIds: outgoing.map(file => file.id),
    composer: 'clear',
    draft: '',
    message,
    outgoing: [],
    sentFiles: outgoing,
  }
}
