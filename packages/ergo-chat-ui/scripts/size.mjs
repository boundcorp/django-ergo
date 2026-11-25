import { readFileSync } from 'node:fs'
import { gzipSync } from 'node:zlib'
for (const f of ['dist/index.js', 'dist/index.cjs', 'dist/styles.css']) {
  const b = readFileSync(f)
  console.log(`${f.padEnd(18)} ${String(b.length).padStart(8)} B   gzip ${String(gzipSync(b).length).padStart(7)} B`)
}
