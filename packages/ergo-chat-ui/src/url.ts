/** Schemes a link in message text may use. */
const LINK_SCHEMES = /^(https?|mailto|tel):/i

function hasControl(url: string): boolean {
  // eslint-disable-next-line no-control-regex
  return /[\u0000- \u007f\\]/.test(url)
}

/**
 * Link URLs from untrusted text: http(s), mailto, tel, same-page `#x` and same-origin paths.
 * Anything else (javascript:, data:, vbscript:, protocol-relative `//host`, control characters) is rejected.
 */
export function safeLinkUrl(url: string | undefined | null): string | undefined {
  if (!url) return undefined
  const value = url.trim()
  if (!value || hasControl(value) || value.startsWith('//')) return undefined
  if (LINK_SCHEMES.test(value)) return value
  // No scheme at all: a relative path or fragment.
  if (/^[a-z][a-z\d+.-]*:/i.test(value)) return undefined
  return value
}

/** Attachment URLs: as links, plus `blob:` (a just-selected file) and raster `data:image/` URLs. */
export function safeAttachmentUrl(url: string | undefined | null): string | undefined {
  if (!url) return undefined
  const value = url.trim()
  if (/^blob:/i.test(value) && !hasControl(value)) return value
  if (/^data:image\/(png|jpe?g|gif|webp|avif);base64,[a-z\d+/=]+$/i.test(value)) return value
  return safeLinkUrl(value)
}
