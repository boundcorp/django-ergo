// Installs React 18 (REACT18_VERSION, default 18.3.1 as the PDS frontend; 18.2.0 is the peer floor) into .react18/ (its own node_modules) so the same tests can run against it.
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const v = process.env.REACT18_VERSION || '18.3.1'
const dir = join(dirname(fileURLToPath(import.meta.url)), '..', '.react18')
const installed = join(dir, 'node_modules', 'react', 'package.json')
if (existsSync(installed) && JSON.parse(readFileSync(installed, 'utf8')).version !== v) rmSync(dir, { recursive: true })
if (!existsSync(join(dir, 'node_modules', 'react-dom'))) {
  mkdirSync(dir, { recursive: true })
  writeFileSync(
    join(dir, 'package.json'),
    JSON.stringify({ private: true, dependencies: { react: v, 'react-dom': v } }),
  )
  execFileSync('npm', ['install', '--no-audit', '--no-fund'], { cwd: dir, stdio: 'inherit' })
}
