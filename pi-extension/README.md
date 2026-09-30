# AgentGenome for Pi

Reuse historical experience as a versioned graph and scripts. The plugin provides
`genome_list`, `genome_get`, `genome_run`, `genome_status`, and `genome_cancel`.
No Pi changes, SDK, separate Pi worker, or manually started server are required.

## Install

After publication:

```sh
pi install npm:@agentgenome/pi-extension
```

Requires macOS/Linux, Pi **0.84.2** (the tested version), and `uv` for initial setup.
In Pi, run `/genome setup` once. It installs the bundled Python wheel and pinned
dependencies into a private, versioned environment. It never installs into system
Python. Initial setup needs network access for Python/dependencies; normal startup
uses the installed environment. Python used by package scripts comes from this
environment; packages requiring additional Python libraries need an explicitly
prepared environment through `AGENTGENOME_PYTHON`.

The first tool call automatically connects to or starts the local service.
Multiple Pi windows share assets; each session has its own runs and notifications.
Default registry: `~/.agentgenome`; override with `AGENTGENOME_HOME`.
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

## Development / packaging

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
