// `@ergo/chat-ui` is a linked local package; its dist imports React from the package's own devDependency copy.
// Vite dedupes that (vite.config.ts `resolve.dedupe`); bun test does not, and two Reacts break hooks. Point the
// package's React imports at this project's copy.
import { plugin } from 'bun'
import { createRequire } from 'node:module'

const here = createRequire(import.meta.url)
plugin({
  name: 'dedupe-react',
  setup(build) {
    build.onLoad({ filter: /ergo-chat-ui\/dist\/.*\.(js|cjs)$/ }, async args => {
      const source = await Bun.file(args.path).text()
      const contents = source.replace(/(["'])(react(?:-dom)?(?:\/[a-z-]+)?)\1/g, (match, _quote, name) => {
        try {
          return JSON.stringify(here.resolve(name))
        } catch {
          return match
        }
      })
      return { contents, loader: 'js' }
    })
  },
})
