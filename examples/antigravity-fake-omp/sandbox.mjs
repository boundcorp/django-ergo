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
// This only tests a configuration with a trusted bundled fake. It does not
// prove isolation against hostile code: no seccomp policy, resource controls,
// hostile-process probes, or independent security review are provided. The
// read-only library directory exposes its contents, the kernel remains shared,
// and inherited stdio remains a communication channel. Trust in host binaries,
// libraries, repository, OS, and namespace enforcement is assumed.
import { spawnSync } from 'node:child_process';
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
