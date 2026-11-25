import type { AppProps } from 'next/app'
// Next 13.1 allows global CSS from node_modules in _app.
import '@ergo/chat-ui/styles.css'

export default function App({ Component, pageProps }: AppProps) {
  return <Component {...pageProps} />
}
