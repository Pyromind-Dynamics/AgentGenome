# Distillation provenance and draft repair

> Updated 2026-10-24 after independent review. The fixes below replace automatic
> writer-origin guessing with explicit classification in the reviewed plan. See
> [acceptance review](distillation-acceptance-review.md) for the original counterexamples.

These rules apply to any task; there are no special dataset, script or output names.

## Natural requests and Python helpers (2026-10-26)

Brief and detailed requests use the same tools. The `genome_distill` schema separates
queries (`plan_id`) from new requests (`label` and nonempty `source_traces`), so
allowing read-only queries does not remove validation of creation arguments.

When a completed task hides its I/O inside a script, mechanical extraction can return
`needs_normalization` with script paths and source-step evidence. This creates no plan,
run or asset. Pi should inspect the named scripts, parameterize and classify the task,
then resubmit; users need not supply graph nodes or repair tool arguments themselves.

## Reusable output directories (2026-10-26)

The trace parser treats `--output-dir`, `--output_dir`, and `--outdir` as path inputs.
This lets distillation expose the requested destination as a graph root parameter instead
of misclassifying it as a produced file. Pi guidance asks the plan to bind that parameter
to the user-selected directory and to verify the actual deliverables there. The behavior
has unit coverage and real Pi graph replays with two different directories. A node still
declares one primary tracked artifact; if the script also copies that artifact into the
delivery directory, the engine may materialize its tracked copy separately. Inspect both
locations in review rather than claiming that every runtime file is contained in the
requested directory.

Packaging follows ordinary `import helper` / `from helper import ...` references to
existing same-directory Python files transitively, in addition to named runtime files.
Python syntax is checked without executing scripts. This is not a full dependency
resolver: external packages, dynamic imports and package-directory layouts still need
the declared runtime and actual replay validation. `planning_preflight` names the checks
it performs and explicitly excludes runtime and business-correctness claims.

A comparison trial that ends by removing all of its own outputs is a side branch,
even if it reads a planned artifact for `diff`/`cmp`. If it retains an audit file or
is an ordinary downstream task, the scope check still applies. No directory name such
as `smoke` is used to waive that check.

Secondary outputs cannot retain unresolved `{name}` tokens in frozen commands. Such
a package is blocked during planning, with a request to split or consolidate outputs;
the token must not become a literal filename. Literal scratch outputs remain supported.

Pi is instructed to review the generated implementation and verifiers before showing
a ready plan, including permitted empty collections and unresolved-item reporting.
This semantic review is an agent responsibility, not a formal proof by the compiler.

## Native human handoff and recovery (2026-10-26)

An explicit `genome_resume` artifact is a source file for the waiting human node.
The native host resolves it relative to the run's workspace and copies it to that
node's declared artifact before approval. Omitting the source retains the existing
prewritten-target behavior. Approval alone does not fabricate an output file.

Requeueing a failed replay registers the new run in the native service, binds the
Pi stage executor, and defers its start until the current agent turn ends, just like
other native submissions. The failed run remains available as evidence. Requeue is
also prohibited inside an active LLM stage, where starting a nested graph would
violate the stage protocol.

For complete files that fit within the bounded read, input analysis counts all
records before reducing the stored preview to 20 records. Byte-truncated inputs
continue to report a sampled count, never an exact total. Preview size is not an
execution limit.

## Replay parameter and artifact checks (2026-10-25)

Verification commands are part of the frozen task: run-node verification is retained
when the plan is exported as a trace, and scripts used only by verification are copied
and remapped just like execution scripts. A missing verifier blocks package building.
The review separates `effective_steps` from `excluded_steps` and marks whether each
declared step will enter the graph. Omitted steps trigger a warning; final-artifact
selection can legitimately prune a branch, so a required acceptance step must consume
the preceding artifact and be included in the final dependency chain, or be a node's
`verify`. A ready plan still needs real execution validation.

Mechanical candidate extraction requires completed-review/export wording for a human
handoff; a request mentioning review and a path is not a completed human action.
Reported products require an output statement, rather than a bare occurrence of the
word for artifact inside status prose. Neither rule recovers arbitrary implicit
script I/O or proves the meaning of all shell output.

Explicit `validation_params` are overrides of graph root inputs. An unknown key blocks
planning and confirmation instead of silently falling back to the original input.
Partial overrides still retain all other bound inputs. If no overrides were supplied,
the existing default replay uses the compiled plan's bound inputs, including slicing;
unused or output bindings from `params` are not new replay inputs.

Each graph leaf currently materializes one primary artifact. If a later node consumes
another file written by that leaf, emission now rejects the plan with the producer,
consumer and file path. Split the command into single-artifact steps, or consolidate
the needed data into one file. Multiple independently addressable artifacts per leaf
are not implemented by this fix. They must never silently become external root inputs.

The local execution copy is also a working directory. Newly generated scratch files
survive human pause/resume and do not count as edits to the frozen package. On resume,
every original package file and directory is still checked against the source; edits,
deletions and symlink replacements of those resources are rejected. This does not
publish runtime files or alter the source package pin.

YAML/YML inputs are identified as `yaml` and expose top-level fields for contract
checks. Earlier samples or declarations that treated YAML as plain `text` remain
compatible. Reuse of a valid YAML contract no longer needs `allow_mismatch`.

An original directory name such as a batch-specific output directory is not by itself
proof of an invalid workflow. The demonstrated dangling human path came from an
unlinked secondary product; the guard above blocks that graph. No directory-name
blacklist or task-specific path rewrite is added.

## Command and input provenance

- A successful producer followed by `&& cat/head` does not introduce a self dependency.
  Conditional or concurrent writes do not establish an input. After `||`, the remaining
  conditional chain conservatively keeps its reads, including on a following `&&`.
  A single command reading and writing the same file still exposes both directions.
- Slice provenance follows the command and pipeline that write the slice. A producer
  whose stdout is piped to `head` does not turn its independent file output into a slice.
  Root slice inputs still trigger review; paths produced by declared nodes are not roots.
- An input observed through a successful file read before its first successful in-place
  edit is recorded as a `revised_input`. A file created in the session and later edited
  remains a generated intermediate. Failed reads are not input evidence.
- Scope previews distinguish connected omitted steps from unrelated branches. An ordinary
  task instruction mentioning an input is not evidence of a human handback. Explicit
  handbacks retain named materials and their preceding producers using canonical paths;
  later overwrites do not count as ancestors. Unnamed materials require scope review.

## Agent tasks materialized by a writer

New Pi exports attach `agent_context` to writer calls: the user instruction, visible
assistant intent and successfully read input paths in the current task. Thinking blocks
and generated output bytes are not copied into that context. Truncated context cannot
justify promotion. The original writer arguments remain in the source trace as before.

A writer candidate is provisional: its content does not prove who made the decisions.
Pi classifies it using the original user instruction and visible execution intent.
User decisions become `human`; model judgment becomes `llm` with `origin_reason`, a
nonempty explanation shown in the plan and bound into its digest. Mixed work must be
split; uncertain classification needs clarification. No word-cooccurrence counts are
used, including counts retained in older traces. Missing `origin_reason` blocks writer
promotion under both policies, for single-value and structured outputs alike.

An LLM writer still needs a source link, matching output, all observed input paths and
an explicit verification contract. The reason is an auditable agent declaration for
user review, not a machine proof of semantic provenance. Its default existence check
is a minimum check, not a claim of semantic correctness.

Arbitrary editor operations, script generation, fixed tool calls and human actions do
not become LLM tasks. Old writer traces are subject to the same explicit classification;
no historical model decision is fabricated. `adaptive-first` retains its existing
explicit trust policy for other node forms.

Pending plans with writer-derived LLM nodes need `origin_reason` and a new reviewed
digest. Published graphs and their execution interfaces are unchanged.

When freezing nodes, verification commands receive the same path substitution as task
commands, including their runtime artifact path. Checks do not run against stale files
left in the original workspace.

## Query and revise a pending plan

`genome_list` includes this conversation's recent `distill_plans` summaries.

Query an existing plan without building or executing anything:

```json
{"plan_id":"<id>"}
```

Pass this object to `genome_distill`. To revise, call the same tool with `plan_id`, its
current `digest` and the complete corrected distillation arguments (label, steps,
source traces, bindings, contracts and validation scope). The plan ID is retained;
`revision` increments and the new content receives a new digest. Querying does not
refresh preflight. Revising recomputes it and does not create a capture or run.

Only `planned` plans can be revised. Cross-conversation revisions, stale digests and
request-ID reuse with different contents fail. Every create and revise request is
registered with a durable receipt, including creations made before receipts existed;
one request ID names exactly one operation, and identical retries replay the recorded
receipt instead of applying the edit twice. `genome_confirm_distill` requires the plan's
current `digest`: a missing, stale or superseded digest fails before any capture or run
is created or the confirmation state changes, and a correct digest on an already
confirmed plan replays the stored result. Re-show the revised plan before confirming it.

`genome_prepare_revision` remains a separate operation for an already executed run.

The structural review and temporary planning build are distinct. `planning_preflight`
reports the temporary build result; it may read scripts but executes no workflow and
publishes nothing. Confirmation remains necessary for the actual replay.

## Registry continuity

After the first successful local service request, Pi saves an `agentgenome.registry`
entry in its session. Resume uses that home even if `AGENTGENOME_HOME` changed in the
new terminal. List and distill responses include registry home, scope and client version;
`/genome registry` displays them. Failed connections do not pin a new registry.

Existing sessions without a registry entry cannot reveal a previous environment variable.
Launch them with the intended `AGENTGENOME_HOME` once. Existing registries are not merged
and plans or assets are not moved automatically. SDK-managed storage is unchanged.

## Validation boundaries

Regression tests use multiple extensions, command layouts and task shapes, including
positive and negative provenance cases. An integration test drives the real compiler,
LLM artifact handoff, human pause/resume, downstream scripts and publication using
deterministic model/human ports in an isolated registry. It does not call a live provider.

An execution scope description is not a row limiter for arbitrary programs. Supply
an actual subset input or a task-supported limit parameter; never treat a sentence
such as “first ten rows” as proof that a replay is bounded. Dataset-specific cleaning
and label-quality evaluation remain business workflow concerns.

When an LLM stage's `genome_step_result` receipt has been accepted by the service,
and the same Pi round then ends with a provider error, the native extension preserves
the receipt only if it was the round's final tool call. The run then performs its
ordinary frozen artifact verification. A missing receipt, an aborted round, or any
tool call after the receipt still fails the stage.
