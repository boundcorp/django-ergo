import type { AppProps } from 'next/app'
import { CacheProvider, type EmotionCache } from '@emotion/react'
import { CssBaseline, ThemeProvider } from '@mui/material'
// Global CSS from node_modules is allowed in _app (Pages Router).
import '@ergo/chat-ui/styles.css'
import createEmotionCache from '../lib/createEmotionCache'
import { theme } from '../lib/theme'

const clientSideEmotionCache = createEmotionCache()

export default function App({
  Component,
  emotionCache = clientSideEmotionCache,
  pageProps,
}: AppProps & { emotionCache?: EmotionCache }) {
  return (
    <CacheProvider value={emotionCache}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <Component {...pageProps} />
      </ThemeProvider>
    </CacheProvider>
  )
}
