# AgentGenome for Pi

Reuse historical experience as a versioned graph and scripts. The plugin provides
`genome_list`, `genome_get`, `genome_run`, `genome_status`, and `genome_cancel`.
No Pi changes, SDK, separate Pi worker, or manually started server are required.

## Install

After publication:

```sh
pi install npm:@agentgenome/pi-extension
```

Requires macOS/Linux, Pi **0.84.2, 0.87.0, or 1.1.0**, and `uv` for initial setup.
Extension loading is verified on all three listed versions; available capabilities
depend on the host APIs. Native `tool` graph nodes are verified on Pi **1.1.0**.
In Pi, run `/genome setup` once. It installs the bundled Python wheel and pinned
dependencies into a private, versioned environment. It never installs into system
Python. Initial setup needs network access for Python/dependencies; normal startup
uses the installed environment. Python used by package scripts comes from this
environment; packages requiring additional Python libraries need an explicitly
prepared environment through `AGENTGENOME_PYTHON`.

The first tool call automatically connects to or starts the local service.
Multiple Pi windows share assets; each session has its own runs and notifications.
Default registry: `~/.agentgenome`; override with `AGENTGENOME_HOME`.
After the first successful request, native Pi sessions remember their registry across
terminal restarts. `/genome registry` shows the current home, scope and client version.
Existing sessions without a registry record must be launched once with the intended
`AGENTGENOME_HOME`; separate existing registries are never merged automatically.

`genome_distill({plan_id})` reads a pending plan. To repair it, pass the same `plan_id`,
its current `digest`, and the complete corrected arguments to `genome_distill`.
The ID stays stable, `revision` increments, and confirming an older digest is rejected.
`genome_confirm_distill` itself requires the plan's current `digest`: confirmation is
bound to the exact version the user reviewed, including on idempotent repeats, and a
missing or stale digest fails before any capture or run is created.
Each request id stands for exactly one create or revise operation; reusing one for a
different operation is rejected, and identical retries replay the recorded receipt.
This is distinct from `genome_prepare_revision`, which adapts an already executed run.
See [provenance and draft repair](../docs/distillation-reliability.md) for the full contract.
Native execution runs trusted scripts locally with your account's permissions;
it is not the SDK sandbox. Execution copies and artifacts live under
`<home>/runs/<id>/work/`. Commands have a 30-minute timeout.

## Try data-cleaning

Create `input.csv` in Pi's working directory:

```csv
name,age
Ada,36
Ada,36
Bob,
Cy,12
```

Then use these commands in Pi (replace the template path):

```text
/genome import /absolute/path/to/AgentGenome/templates/data-cleaning
/genome validate data-cleaning 1.0.0 {"data_file":"input.csv"}
```

After successful validation:

```text
/genome publish data-cleaning 1.0.0
```

Ask Pi to use the published data-cleaning experience to clean another CSV. It
returns a run ID, displays progress, and follows up after completion. The same
asset is reused without generating new scripts. `validate` also shows its result
and triggers a follow-up. The template is copied into the registry, never executed
from an editable template checkout.

Progress uses a Pi widget, refreshed from bounded service snapshots once a second;
full command output stays in the run's execution log. Only completion enters the
model conversation. Cancellation stops the command process group and does not
trigger an agent reply. Missing inputs, command/verification failures are reported
without automatically retrying the whole graph (declared graph-node retries remain).

Session switches/reloads do not kill active runs. Results are delivered when the
original session returns. Delivery is deduplicated using Pi's actual custom message
history; already queued follow-ups keep their delivery lease while the client is
connected. Restarts do not replay scripts. Unconfirmed runs become `interrupted`.
Closing all clients lets the service exit after 60 idle seconds, once active jobs
finish. Queued but not started work is never automatically resumed after reload.

The first release targets interactive Pi sessions. One-shot `pi -p` may exit before
an asynchronous completion is delivered; use an interactive session for automatic
follow-up. Same-session simultaneous windows should be avoided for editing Pi's
own session log; different sessions can run concurrently.

## SDK integration

```ts
import { provideGenomeHost } from "@agentgenome/pi-extension";
import { createEventBus, DefaultResourceLoader } from "@earendil-works/pi-coding-agent";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const eventBus = createEventBus();
const binding = provideGenomeHost(eventBus,
  (method, payload, signal) => peer.request(method, payload, signal));
const loader = new DefaultResourceLoader({
  eventBus,
  additionalExtensionPaths: [dirname(fileURLToPath(import.meta.resolve("@agentgenome/pi-extension")))],
});
await loader.reload();
if (loader.getExtensions().errors.length || !binding.discovered) {
  throw new Error("AgentGenome did not connect to the execution host");
}
```

Both environments load the **same default extension** through Pi's standard package
loader. The plugin discovers an optional host synchronously through `pi.events`
(`agentgenome:host:v1`); register the provider before loading extensions. Event buses
are session-scoped and providers remain available across extension reloads.
The SDK no longer calls `createGenomeExtension` or defines/registers genome tools.
The earlier factory exports remain available for API compatibility only.

When a host is discovered, its transport, execution environment, progress display
and completion delivery are used. A failed host request never falls back to local
execution. SDK startup checks the handshake before creating the Pi session, so a
missing/incompatible provider cannot silently start the native environment.
The host routes to `agentgenome.dispatch.invoke` and supplies
the existing Python `Host.prepare` execution port. The SDK remains responsible for
permissions, sandbox, Storage URLs, and Product Runtime notifications.

Do not point native Pi at a registry currently owned by an SDK execution host.
Both respect the same execution lock, and a conflict is reported rather than
recovered destructively. An incompatible running service is not replaced; close
clients after its jobs finish, wait for idle exit, then use the upgraded plugin.

Native clients also compare Python source fingerprints, so different builds with
the same version cannot silently reuse an older service. A mismatch reports an
error without replacing the service or replaying any runs. An explicitly selected
`AGENTGENOME_PYTHON` must support this handshake as well.

Human nodes park without holding a worker or reading the background service's
stdin. Pi shows the work order and artifact path; after the user writes the
artifact, `genome_resume` queues the same run from its checkpoint. Only that gate
is approved, declared verification still runs, and subsequent gates wait again.
Already delivered work orders are not repeated when reopening the same session.
After resume is queued, end the agent turn and wait for the completion message.

Native Pi also supports `do.llm` stages (`requires: [agent_task]`) in the current
interactive session. The plugin delivers one stage when the session is idle;
write its declared output, call `genome_step_result`, then end the turn. The
session must stay connected while an llm stage is being handled. A one-shot
`pi -p` process may exit before the handoff; use an interactive or persistent
host for llm stages. A missing receipt, mismatched execution ID, or unsuccessful
model round can also fail the stage. Human nodes instead park at a durable
checkpoint and can resume after reconnecting. A receipt
alone does not finish the stage: the matching model round must end successfully
before scripts verify the files and the graph continues. Do not poll or start
another graph from a stage. Stage handoffs expire after 30 minutes; cancellation,
owner disconnection or service restart never re-delivers an old stage. Switching
sessions closes the old handoff and fails it rather than sending it to the new
session. A handoff is only created or kept while the connection that started the
execution remains connected and stage-capable. Reopening the same session file
does not revive that connection's old execution. A delivered stage message whose round is
already dead is intercepted and its turn aborted before the model acts on it.
The extension does not filter or rewrite Pi conversation history for model requests.
Later runs need an explicit new submission. If a terminal run aborts its active
stage round, completion delivery waits for Pi to become idle before claiming a
notification; acknowledgement still requires the message to appear in session history.

Use `templates/native-agent-task-check` to test script → Agent → script, and
`templates/data-cleaning-revision` to test the native revision loop (draft
preparation in the registry, prompt-level protection, regression and
publication). Use `templates/native-model-judge-check` to test independent model
verification: it requires only `model_judge`. The judge uses the current Pi model
in a separate request containing only the frozen criterion and evidence, without
chat history or tools. It never creates an agent turn or uses stage receipts.
Verdicts must contain a boolean `passed` and nonempty `reason`; the judge script
exits 0 (pass), 1 (reject), or 2 (call/protocol error). Errors fail closed.
Calls are bounded to 90 seconds and aborted on cancellation or session closure;
requests cannot be transferred to a reopened connection. Credentials stay in Pi.
Native model judging requires Pi's `modelRegistry.streamSimple` API (tested with
Pi 1.1.0); clients without it do not advertise this capability. SDK judging still
uses its own host adapter.
Revision verification continues to use the original criterion and judge script.
Verification runs in a pin-checked per-run copy (`work/verification/`), so normal
relative scratch/cache writes cannot change the stored asset. Resuming rebuilds
that copy and keeps business artifacts. This is not filesystem access control.
Use one judge call per `verify.run` command; use separate rules for multiple judges.
Read-only verification protection remains undeclared; `data-cleaning-v2` still
requires that additional capability.
Capture's separate
new-parameter probe is not covered by this stage handoff; do not infer generalized
reuse from a successful main run alone.

## Development / packaging

Native tool nodes use Pi's public `ExtensionToolContext.executeTool` pipeline
(tested on Pi 1.1.0), including schema validation, hooks and permission checks.
Pi 0.84.2 and 0.87.0 load the extension, but do
not expose `executeTool`; tool graphs explain that the Pi host lacks the
required tool context. Pi 1.1.0 is the verified version for native tool nodes.
Names match the session's callable tools exactly; `genome_*` control tools are
excluded. Normal `genome_prepare_run` / `genome_confirm_run` and capture tool
calls bind the executor automatically. A fresh `/genome validate` command has
no executor context: first have the Agent call `genome_list` in that session.
Without a compatible bound context, tool graphs are rejected rather than echoed.
Use `templates/native-tool-check` to verify an actual write and its artifact.
Each execution ID is dispatched once; cancellation waits for a terminal tool
outcome. Unconfirmed stop/result or disconnect remains unknown and is not
automatically replayed. Cancellation cannot undo already completed side effects.

From the AgentGenome repository:

```sh
cd pi-extension
npm test
npm pack
pi install /absolute/path/to/AgentGenome/pi-extension
```

`npm pack` builds and bundles the Python wheel using `uv build`. The published npm
artifact contains the runtime, not references to this checkout. Python and npm
versions must match; changing bundled bytes creates a different isolated runtime.
For offline development with an existing build environment, `uv build` accepts
`UV_NO_BUILD_ISOLATION=1` and `UV_PYTHON=/path/to/python`.

`AGENTGENOME_PYTHON=/absolute/path/to/python` optionally uses an existing environment
with the **same** AgentGenome version installed. This skips setup only, not service
protocol/version validation. It is useful for development and air-gapped installs.

Local transport is versioned JSONL on a mode-0600 Unix socket in a private 0700
directory. Requests have IDs; startup and completion are deduplicated separately.
Socket paths are derived from the canonical registry path, including symlinks.

### Native asynchronous await

The local service probes parked external tasks about once per second without a
model turn or shell polling loop. Its current receipt protocol is
`{task_id?, output_dir}` with `report.json` and `processed.jsonl` in that local
directory. Relative directories use the original workspace. A successful report
without its product remains pending until the original deadline; status metadata
never substitutes for the requested product. Invalid receipts fail explicitly.

Completion, failure and timeout resume the same run from its checkpoint; upstream
submission is not repeated. External waits have a separate widget, not a human
work order. The monitor survives service restart via stored checkpoints and keeps
an otherwise idle service alive while external waits exist. Script-only graphs
may continue without Pi. Graphs requiring Pi tools/stages/judges conservatively
wait for a capable connection before continuation, including deadline failure
processing; reopening the original session and calling genome_list rebinds tools.
The original deadline is retained, and old connection-owned calls are not replayed.
Cancellation stops the graph's wait, not the independently submitted task.
This is a local file adapter, not a generic remote callback implementation.
See `templates/native-await-check` for a real background-process test tool.
