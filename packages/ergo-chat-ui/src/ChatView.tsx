import { Composer } from './Composer'
import { RootProvider } from './root'
import { Transcript } from './Transcript'
import type { ChatViewProps } from './types'

/** Transcript plus composer in one column. Use `Transcript` and `Composer` directly for another layout. */
export function ChatView({
  messages,
  renderPart,
  empty,
  footer,
  allowMarkdownImages,
  theme = 'light',
  status = 'idle',
  error,
  composer,
  className = '',
  onSend,
  onStop,
  onAttach,
  onRemoveAttachment,
  placeholder,
  accept,
  draft,
  onDraftChange,
}: ChatViewProps) {
  const busy = status === 'sending' || status === 'streaming'
  return (
    <RootProvider value>
      <div className={`ergo-chat ergo-chat__view ${className}`.trim()} data-theme={theme}>
        <Transcript
          messages={messages}
          renderPart={renderPart}
          empty={empty}
          footer={footer}
          allowMarkdownImages={allowMarkdownImages}
          className="ergo-chat__view-transcript"
        />
        {status === 'error' && error ? (
          <div className="ergo-chat__alert" role="alert">
            {error}
          </div>
        ) : null}
        {composer ?? (
          <Composer
            onSend={onSend}
            onStop={onStop}
            onAttach={onAttach}
            onRemoveAttachment={onRemoveAttachment}
            busy={busy}
            placeholder={placeholder}
            accept={accept}
            draft={draft}
            onDraftChange={onDraftChange}
          />
        )}
      </div>
    </RootProvider>
  )
}
