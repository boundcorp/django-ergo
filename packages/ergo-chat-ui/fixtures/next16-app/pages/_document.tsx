import createEmotionServer from '@emotion/server/create-instance'
import Document, { Head, Html, Main, NextScript } from 'next/document'
import React from 'react'
import createEmotionCache from '../lib/createEmotionCache'

// Same Emotion SSR extraction as the PDS frontend's _document.
export default class MyDocument extends Document {
  static async getInitialProps(ctx: any) {
    const emotionCache = createEmotionCache()
    const { extractCriticalToChunks } = createEmotionServer(emotionCache)
    const originalRenderPage = ctx.renderPage
    ctx.renderPage = () =>
      originalRenderPage({
        enhanceApp: (App: any) => (props: any) => <App emotionCache={emotionCache} {...props} />,
      })
    const initialProps = await Document.getInitialProps(ctx)
    const emotionStyles = extractCriticalToChunks(initialProps.html)
    const emotionStyleTags = emotionStyles.styles.map(style => (
      <style
        data-emotion={`${style.key} ${style.ids.join(' ')}`}
        key={style.key}
        dangerouslySetInnerHTML={{ __html: style.css }}
      />
    ))
    return { ...initialProps, styles: [...emotionStyleTags, ...React.Children.toArray(initialProps.styles)] }
  }

  render() {
    return (
      <Html lang="en">
        <Head>
          <link rel="icon" href="data:," />
        </Head>
        <body>
          <Main />
          <NextScript />
        </body>
      </Html>
    )
  }
}
