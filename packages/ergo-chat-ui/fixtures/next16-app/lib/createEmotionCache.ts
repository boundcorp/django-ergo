import createCache from '@emotion/cache'

// Same as PDS: MUI styles are prepended so host overrides keep precedence.
export default function createEmotionCache() {
  return createCache({ key: 'mui', prepend: true })
}
