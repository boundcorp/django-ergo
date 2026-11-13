#!/usr/bin/node
// Deliberately inert fake protocol peer; no CLI discovery, network, or credentials.
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const request = JSON.parse(input);
  const scenario = process.argv[2];
  const send = event => process.stdout.write(JSON.stringify(event) + '\n');
  const ready = () => send({ type: 'agent', name: 'inventory-only' });
  const tool = () => send({ type: 'tool', source: 'inventory', id: 'call-1', name: 'lookup', arguments: { query: 'example' } });
  if (scenario === 'fallback') { send({ type: 'agent', name: 'default' }); return; }
  ready();
  switch (scenario) {
    case 'text': send({ type: 'text', text: 'Hello ' }); send({ type: 'text', text: 'world ☃' }); break;
    case 'context': send({ type: 'text', text: JSON.stringify(request) }); break;
    case 'tool': send({ type: 'text', text: 'Looking up' }); tool(); break;
    case 'foreign-tool': tool(); send({ type: 'tool', source: 'inventory', id: 'call-2', name: 'other', arguments: {} }); break;
    case 'native-tool': tool(); send({ type: 'tool', source: 'native', id: 'call-2', name: 'lookup', arguments: {} }); break;
    case 'malformed': process.stdout.write('{invalid\n'); return;
    case 'missing-done': return;
    case 'nonzero': process.exitCode = 7; return;
    case 'fake-error': send({ type: 'error', message: 'simulated failure' }); return;
    case 'oversized': send({ type: 'text', text: 'x'.repeat(300000) }); return;
    case 'hang': send({ type: 'text', text: String(process.pid) }); setInterval(() => {}, 1000); return;
    case 'slow': setTimeout(() => { send({ type: 'text', text: 'late' }); send({ type: 'done' }); }, 10000); return;
    default: process.exitCode = 2; return;
  }
  send({ type: 'done' });
});
