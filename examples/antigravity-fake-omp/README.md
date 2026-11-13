# Offline agy protocol and OMP contract experiments

The source-shaped replay is **not a live Antigravity integration** and is not
production code. The original contract experiment runs
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

## Source-backed offline replay

`agy-protocol.mjs` parses a deliberately conservative subset of documented
stream-json; `agy-omp.mjs` connects synthetic byte chunks to OMP's event
stream, with no child process or provider registration. The fixtures are
**synthetic**, not recordings of real sessions. The parser refuses tool
steps, subagents, unsupported events and inconsistent results rather than
pretending an observed event can prevent actions that may already have run.
It buffers replay output until successful completion. OMP compatibility is
limited to the installed stream API; Ergonaut integration is not implemented.

The separate pure `recognizeHermesToolCall` recognizes the source-described
ACTIVE `call_mcp_tool` shape only for an explicit Hermes tool inventory; it
does not execute, authorize, or emit OMP tool calls. The OMP replay rejects all
tool steps. Terminal token counters are copied into OMP's corresponding
fields; unknown usage and costs remain zero for this synthetic replay and are
not a billing statement.

The official [headless documentation](https://antigravity.google/docs/cli/headless.md)
(retrieved 2026-11-13; no immutable documentation revision published) describes
newline-delimited `init`, `step_update`, and `result`, `agent_response`
`text_delta`, token counters, errors, and `user` messages with string or text
block content. It documents tool steps as execution reports, including
`tool_info.parameters` and output, not a safe interception mechanism. Streaming
input does not represent native assistant, system or tool-result messages.
The offline OMP bridge therefore accepts only one text-only user message with
no system prompt or tools; it rejects all other contexts rather than flattening
or silently losing them. No truncation-safe message limit is established.

The official documentation lists example CLI slugs such as
`gemini-4-carbon-high`, `gemini-4-argon-medium`, `gemini-4-hydrogen-xhigh`,
`gemini-3.8-flash-medium`, and `gemini-3.1-pro-high`; these are not evidence
of access, actual current availability, or equivalent Gemini API model IDs.
The archived Hermes source at commit
[`ec07f86e6d918803ac5e328a4780d0ec0f48d0b8`](https://github.com/Realtyxxx/hermes-plugin-antigravity-agy/tree/ec07f86e6d918803ac5e328a4780d0ec0f48d0b8)
describes its testing against agy 1.2.10–1.2.11, tool-event fields and silent
agent fallback in [design §§3.2–3.4](https://github.com/Realtyxxx/hermes-plugin-antigravity-agy/blob/ec07f86e6d918803ac5e328a4780d0ec0f48d0b8/docs/design.md#32-agy-silently-falls-back-to-its-default-agent).
Its [README](https://github.com/Realtyxxx/hermes-plugin-antigravity-agy/blob/ec07f86e6d918803ac5e328a4780d0ec0f48d0b8/README.md)
reports silently truncated flattened messages around 65–69k tokens. These are
upstream claims, not measurements in this checkout. Do not rely on `init.agent`
as proof of the actual loaded agent; upstream reports it can name the requested
agent even after fallback.

## Independent sandbox experiment

`sandbox.mjs` is a separate Linux x86-64 bubblewrap experiment restricted to
the bundled harmless fake. It requires user, network, PID, IPC and UTS
namespaces, a new root with only read-only runtime/fake mounts, an empty
environment and no host home; setup failure refuses execution without direct
fallback. The original fake adapter is **not** run through this sandbox.
The experiment has no live CLI path and no allowed inference channel.
[Bubblewrap v0.8.0 documentation](https://github.com/containers/bubblewrap/blob/4ab175fe6d3a4053444b04d805a6d686f53455b9/bwrap.xml)
describes the mechanisms; their specification alone is not proof that this
host enforces them. This remains a shared-kernel prototype with no seccomp,
resource limits, adversarial isolation tests, or independent review. Its
read-only library mount and stdio are visible to the fake. A future live design
must prove containment before any CLI, agent, tool or hook runs, and separately
design narrowly authorized authentication and network access; it must not
trust flags or post-event termination alone.

On the development host, the bundled fake completed under this bubblewrap
configuration during the offline smoke check. This proves only that this
particular harmless process ran; no adversarial containment proof was performed.

## Offline checks

Requires Bun and the installed OMP package at the path imported in the test;
`fake-cli.cjs` has a `/usr/bin/node` shebang. No package install or network is
needed:

```sh
bun test examples/antigravity-fake-omp/*.test.mjs
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
no property of these synthetic fixtures proves isolation. A live bridge remains
unimplemented and blocked on independently verified sandboxing, a credential and
restricted inference-channel design, and policy review, including
[Antigravity Additional Terms §6](https://antigravity.google/terms/), which
prohibits third-party software, tools or services from accessing the Service.
Offline work does not resolve permission for live use.

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
