import { defineConfig } from 'tsup'

// React stays external (peer). Everything else is bundled so the tarball has no runtime dependencies,
// and the ESM-only markdown stack works from the CJS build too.
export default defineConfig({
  entry: ['src/index.ts'],
  format: ['esm', 'cjs'],
  dts: true,
  target: 'es2019',
  platform: 'neutral',
  mainFields: ['module', 'main'],
  external: ['react', 'react-dom', 'react/jsx-runtime'],
  noExternal: [/^(?!react($|\/|-dom)).*/],
  clean: true,
  treeshake: true,
  minify: true,
  banner: { js: "'use client'" },
  sourcemap: false,
  outExtension: ({ format }) => ({ js: format === 'cjs' ? '.cjs' : '.js' }),
})
