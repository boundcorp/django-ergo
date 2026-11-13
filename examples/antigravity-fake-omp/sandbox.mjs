// Linux/x86-64, fake-only experiment; this is NOT an adapter or a production
// security boundary. No caller-supplied program, arguments, environment, mounts,
// or request are accepted. It is not connected to adapter.mjs.
//
// bubblewrap v0.8.0 documentation, pinned to its release commit:
// https://github.com/containers/bubblewrap/blob/4ab175fe6d3a4053444b04d805a6d686f53455b9/bwrap.xml
// https://github.com/containers/bubblewrap/blob/4ab175fe6d3a4053444b04d805a6d686f53455b9/README.md
// Explicit --unshare-user is important: --unshare-all alone permits userns to
// fail. --disable-userns also requires a non-setuid bwrap and kernel support.
// Failures, including unavailable namespaces, never fall back to a direct run.
//
// This is not proof of isolation against hostile code: there is no seccomp
// policy, process-count or memory limit, or independent security review. The
// read-only library directory exposes its contents, the kernel remains shared,
// and stdio remains a communication channel. Host binaries, libraries, the
// repository, OS, and namespace enforcement are trusted.
import { spawnSync } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync, existsSync, writeFileSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const FAKE = fileURLToPath(new URL('./fake-cli.cjs', import.meta.url));
const BWRAP = '/usr/bin/bwrap';

// Fixed, absolute paths and explicit required namespaces. No host home, project
// tree, network sockets, or host /proc are mounted. The fake and runtime are
// read-only; bwrap creates a fresh root and a private minimal /dev.
export const SANDBOX_ARGS = Object.freeze([
  '--unshare-user', '--unshare-ipc', '--unshare-pid', '--unshare-net', '--unshare-uts',
  '--disable-userns', '--cap-drop', 'ALL', '--die-with-parent', '--new-session', '--clearenv',
  '--ro-bind', '/usr/bin/node', '/usr/bin/node',
  '--ro-bind', '/lib/x86_64-linux-gnu', '/lib/x86_64-linux-gnu',
  '--ro-bind', '/lib64/ld-linux-x86-64.so.2', '/lib64/ld-linux-x86-64.so.2',
  '--ro-bind', FAKE, '/fake-cli.cjs',
  '--dev', '/dev', '--chdir', '/', '--', '/usr/bin/node', '/fake-cli.cjs', 'text',
]);

export class SandboxRefusedError extends Error {
  constructor(message) { super(message); this.name = 'SandboxRefusedError'; }
}

export function runSandboxedFake(options = {}) {
  if (!options || typeof options !== 'object' || Array.isArray(options) ||
      Object.keys(options).some(key => key !== 'command' && key !== 'scenario') ||
      (options.command !== undefined && options.command !== FAKE) ||
      (options.scenario !== undefined && options.scenario !== 'text')) {
    throw new SandboxRefusedError('Only the bundled fake and fixed text scenario are permitted');
  }
  if (process.platform !== 'linux' || process.arch !== 'x64' ||
      typeof process.getuid !== 'function' || process.getuid() === 0) {
    throw new SandboxRefusedError('Requires unprivileged Linux x86-64');
  }
  const result = spawnSync(BWRAP, SANDBOX_ARGS, {
    input: '{}', encoding: 'utf8', env: {}, cwd: '/',
    timeout: 3000, killSignal: 'SIGKILL', maxBuffer: 64 * 1024,
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0 || result.signal) {
    throw new SandboxRefusedError(`Sandbox launch or fake execution failed: ${result.error?.code ?? result.signal ?? `exit ${result.status}`}`);
  }
  return result.stdout;
}

// Fixed probe code only: callers cannot choose a program or sandbox settings.
// The paths identify a fabricated private host fixture, not the user's home.
const PROBE = `
const fs = require('node:fs');
const net = require('node:net');
const { spawnSync } = require('node:child_process');
const [canary, outside, port] = process.argv.slice(1);
function denied(fn) { try { fn(); return false; } catch { return true; } }
const results = {
  canaryHidden: denied(() => fs.readFileSync(canary)),
  outsideOverwriteDenied: denied(() => fs.writeFileSync(canary, 'changed')),
  outsideCreateDenied: denied(() => fs.writeFileSync(outside, 'created')),
  // bwrap sets PWD to --chdir even after --clearenv.
  environmentCleared: Object.keys(process.env).length === 1 && process.env.PWD === '/',
};
const child = spawnSync('/usr/bin/node', ['-e', \`
  const fs = require('node:fs');
  const net = require('node:net');
  function denied(fn) { try { fn(); return false; } catch { return true; } }
  const observation = {
    hidden: denied(() => fs.readFileSync(process.argv[1])),
    writeDenied: denied(() => fs.writeFileSync(process.argv[2], 'created')),
    environmentCleared: Object.keys(process.env).length === 1 && process.env.PWD === '/',
    hostNetworkDenied: false,
  };
  const socket = net.createConnection({ host: '127.0.0.1', port: Number(process.argv[3]) });
  socket.setTimeout(500);
  socket.once('connect', () => socket.destroy());
  socket.once('error', error => {
    observation.hostNetworkDenied = ['ECONNREFUSED', 'ENETUNREACH', 'EHOSTUNREACH'].includes(error.code);
  });
  socket.once('timeout', () => socket.destroy());
  socket.once('close', () => process.stdout.write(JSON.stringify(observation)));
\`, canary, outside, port], { encoding: 'utf8', timeout: 1500, killSignal: 'SIGKILL', maxBuffer: 4096 });
try {
  const observation = JSON.parse(child.stdout);
  results.subprocessIsolated = !child.error && child.status === 0 &&
    observation.hidden === true && observation.writeDenied === true &&
    observation.environmentCleared === true && observation.hostNetworkDenied === true;
} catch { results.subprocessIsolated = false; }
const socket = net.createConnection({ host: '127.0.0.1', port: Number(port) });
socket.setTimeout(500);
socket.once('connect', () => { results.hostNetworkDenied = false; socket.destroy(); });
socket.once('error', error => {
  results.hostNetworkDenied = ['ECONNREFUSED', 'ENETUNREACH', 'EHOSTUNREACH'].includes(error.code);
});
socket.once('timeout', () => { results.hostNetworkDenied = false; socket.destroy(); });
socket.once('close', () => { process.stdout.write(JSON.stringify(results)); });
`;

// Creates only a private, synthetic host fixture and loopback listener. A
// namespace failure or any failed/indeterminate probe is a refusal, not a
// successful isolation result.
export async function runSandboxIsolationProbes() {
  if (process.platform !== 'linux' || process.arch !== 'x64' ||
      typeof process.getuid !== 'function' || process.getuid() === 0) {
    throw new SandboxRefusedError('Requires unprivileged Linux x86-64');
  }
  let dir;
  const server = createServer(socket => socket.destroy());
  try {
    dir = mkdtempSync(join(tmpdir(), 'ergo-sandbox-'));
    const canary = join(dir, 'canary');
    const outside = join(dir, 'outside-write');
    const secret = randomBytes(32).toString('hex');
    writeFileSync(canary, secret, { mode: 0o600 });
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    const args = [
      ...SANDBOX_ARGS.slice(0, SANDBOX_ARGS.indexOf('--')),
      '--', '/usr/bin/node', '-e', PROBE, canary, outside, String(server.address().port),
    ];
    const result = spawnSync(BWRAP, args, {
      encoding: 'utf8', env: { HOME: dir, PWD: dir, SANDBOX_CANARY_SECRET: secret }, cwd: '/',
      timeout: 3000, killSignal: 'SIGKILL', maxBuffer: 64 * 1024,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    if (result.error || result.status !== 0 || result.signal) {
      throw new Error('probe launch or execution failed');
    }
    const observations = JSON.parse(result.stdout);
    const checks = [
      'canaryHidden', 'outsideOverwriteDenied', 'outsideCreateDenied',
      'environmentCleared', 'subprocessIsolated', 'hostNetworkDenied',
    ];
    const failed = checks.filter(key => observations[key] !== true);
    if (readFileSync(canary, 'utf8') !== secret) failed.push('hostCanaryChanged');
    if (existsSync(outside)) failed.push('hostOutsideWriteCreated');
    if (failed.length) {
      throw new Error(`isolation probe failed: ${failed.join(', ')}`);
    }
    return observations;
  } catch (error) {
    throw new SandboxRefusedError(`Sandbox isolation probes refused: ${error.message}`);
  } finally {
    server.close();
    if (dir) rmSync(dir, { recursive: true, force: true });
  }
}
