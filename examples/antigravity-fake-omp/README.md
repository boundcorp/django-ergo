# Fake-only OMP provider contract experiment

This is **not an Antigravity integration** and is not production code. It runs
only the bundled `fake-cli.cjs` through its fixed absolute path, with an empty
child environment and a fixed scenario allowlist. It never discovers or launches
`agy`, reads credentials, logs in, makes inference or quota requests, or registers
an engine/provider. The test peer emits a deliberately **invented** NDJSON
protocol, not a verified agy wire protocol. `command` exists only to exercise
the rejection of every value other than that exact bundled path; it is not a
production security boundary against a modified repository or runtime.

## Contract and experiment

On this host, `/usr/local/bin/omp` resolves to the CLI in
`@oh-my-pi/pi-coding-agent` **18.12.6**; its accompanying `@oh-my-pi/pi-ai` is
also **18.12.6**. The package repository is
[can1357/oh-my-pi](https://github.com/can1357/oh-my-pi), package directories
`packages/coding-agent` and `packages/ai`. Whether this is the particular
"OMP2" intended by any external request is unverified. Installed
`dist/types/extensibility/extensions/types.d.ts` exposes
`registerProvider(name, {api, models, streamSimple})`, where
`streamSimple(model, context, options)` returns an
`AssistantMessageEventStream`. This example intentionally does **not** call
`registerProvider` or write OMP configuration. Its factory accepts OMP's actual
`createAssistantMessageEventStream` so tests exercise that stream's async
iteration, terminal events, and `result()` behavior.

The adapter passes `model.id` and the OMP context, including prior assistant
calls and `toolResult` messages, to the fake process as JSON on stdin. It emits
OMP `start`, text lifecycle and `toolcall_start/delta/end` events, followed by
`done`, or a terminal `error` (`aborted` for cancellation). Successful fake
tool calls are held until the fake stream and process exit successfully; the
parent owns execution and approvals. The prototype reports zero usage/cost
because the fake has no usage data, not as a claim about real inference.
Timeout, abort, malformed data, fake fallback, foreign/native tool events,
nonzero exits, incomplete streams, and size limits terminate the fake process
group and fail the turn. Text already streamed cannot be recalled. A staged
tool call is emitted after text, so interleaved text/tool ordering is not
faithfully preserved.

## Offline checks

Requires Bun and the installed OMP package at the path imported in the test;
`fake-cli.cjs` has a `/usr/bin/node` shebang. No package install or network is
needed:

```sh
bun test examples/antigravity-fake-omp/adapter.test.mjs
```

The test deliberately imports the installed OMP stream implementation by its
host-specific absolute path. For another offline installation, update that test
import to its local `pi-ai/src/utils/event-stream.ts`; the adapter itself does
not hard-code an OMP installation path.

## Safety and future work

Fallback and foreign/built-in tool interception here is **post-event detection,
not a prevention boundary** for a real client. A real tool or hook may already
have executed before such an event is observed, and some actions may not be
reported. The upstream Hermes project documents silent fallback and user hooks;
no property of these fake fixtures proves isolation. A live bridge remains
unimplemented and blocked on independent sandbox design/review and policy review,
including the [Antigravity Additional Terms](https://antigravity.google/terms/).
Nothing here asserts production safety or legal permission.

A real integration would additionally need a verified CLI protocol and model
mapping, independently enforced pre-execution isolation of tools and hooks,
input framing and truncation limits, multimodal handling, faithful tool/text
ordering, real usage/error semantics, process/platform behavior, OMP extension
registration and integration tests, and separately authorized operational and
billing controls. None of those is implemented by this fake. No real-client
command should be added to this example as a shortcut.

No Hermes code was reused. The archived
[hermes-plugin-antigravity-agy](https://github.com/Realtyxxx/hermes-plugin-antigravity-agy)
is MIT-licensed, copyright 2026 Realtyxxx; its documentation was used only as
background. The predecessor's local artifacts are preserved at
`/tmp/antigravity-omp-bridge-investigation.md` and
`/tmp/google-capacity-audit.md` (host-local, not part of this checkout).

Future configuration handoff belongs to the owners of the separately managed
`/home/dev/p/boundcorp/ergo-bots` config repo: only after reviewed library and
Ergonaut integration, policy approval, and explicit authorization should they
assess `providers.yaml`. No valid config stanza is supplied or changed here.
