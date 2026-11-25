// Installs React 18 into .react18/ (its own node_modules) so the same tests can run against it.
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const dir = join(dirname(fileURLToPath(import.meta.url)), '..', '.react18')
if (!existsSync(join(dir, 'node_modules', 'react-dom'))) {
  mkdirSync(dir, { recursive: true })
  writeFileSync(
    join(dir, 'package.json'),
    JSON.stringify({ private: true, dependencies: { react: '18.2.0', 'react-dom': '18.2.0' } }),
  )
  execFileSync('npm', ['install', '--no-audit', '--no-fund'], { cwd: dir, stdio: 'inherit' })
}
