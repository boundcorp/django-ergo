import { describe, expect, test } from 'bun:test';
import { SandboxRefusedError, runSandboxedFake } from './sandbox.mjs';

describe('fake-only prelaunch sandbox experiment', () => {
  test('rejects alternative programs, scenarios, and configuration before launch', () => {
    for (const options of [
      { command: 'agy' }, { command: '/usr/bin/agy' }, { command: process.execPath },
      { command: './fake-cli.cjs' }, { scenario: 'hang' }, { scenario: 'tool' },
      { args: [] }, { env: {} }, { mounts: [] }, { timeout: 1 }, null,
    ]) {
      expect(() => runSandboxedFake(options)).toThrow(SandboxRefusedError);
    }
  });


  test('runs only the harmless fake when available, otherwise explicitly refuses', () => {
    let output;
    try {
      output = runSandboxedFake();
    } catch (error) {
      // bwrap, its dependencies, or required kernel features may be unavailable.
      // This path demonstrates refusal, not successful namespace enforcement.
      expect(error).toBeInstanceOf(SandboxRefusedError);
      expect(error.message).toMatch(/Requires unprivileged|Sandbox launch or fake execution failed/);
      return;
    }
    const events = output.trim().split('\n').map(line => JSON.parse(line));
    expect(events).toEqual([
      { type: 'agent', name: 'inventory-only' },
      { type: 'text', text: 'Hello ' },
      { type: 'text', text: 'world ☃' },
      { type: 'done' },
    ]);
  });
});
