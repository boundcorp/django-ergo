import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

// REACT_MAJOR=18 runs the same tests against React 18 (installed by scripts/react18.mjs into .react18/).
const here = dirname(fileURLToPath(import.meta.url))
const r18 = join(here, '.react18', 'node_modules')
const alias =
  process.env.REACT_MAJOR === '18'
    ? [
        { find: /^react$/, replacement: join(r18, 'react') },
        { find: /^react\/(.*)$/, replacement: join(r18, 'react', '$1') },
        { find: /^react-dom$/, replacement: join(r18, 'react-dom') },
        { find: /^react-dom\/(.*)$/, replacement: join(r18, 'react-dom', '$1') },
      ]
    : []

export default defineConfig({
  resolve: { alias },
  test: { environment: 'happy-dom', include: ['test/**/*.test.{ts,tsx}'], server: { deps: { inline: [/react/] } } },
})
