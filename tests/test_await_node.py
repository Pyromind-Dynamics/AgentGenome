"""An await node parks on an external task instead of polling in a shell.

The chain under test is the shape a platform submit produces: a tool that only
returns a receipt, then a node that turns that receipt into the real product.
"""
import json
from pathlib import Path

from builder import compile as compile_graph
from core.api import RunStatus, resume, run
from core.artifacts import LocalArtifacts
from core.ports import AwaitResult, Ports, ShellResult, ToolResult
from agentgenome.service import GenomeService

GRAPH = '''version: "gt/1.0"
node:
  id: chain
  mode: sequence
  children:
    - id: submit
      do:
        tool:
          name: df_submit_pipeline
          args: {input_path: "storage/input.jsonl"}
      output: {handle: {type: json}}
      artifact: {ext: json}
      verify:
        - run: "test -s {output.handle}"
    - id: wait
      input:
        handle: {type: json, from: submit.output.handle}
      do:
        await: {receipt: "{input.handle}", timeout: 600}
      output: {product: {type: text}}
      artifact: {ext: jsonl}
      verify:
        - run: "test -s {output.product}"
  output:
    product: {type: text, compose: [wait.output.product]}
  verify:
    - run: "test -s {output.product}"
'''


class FakeShell:
    def __init__(self):
        self.commands = []

    def execute(self, command, cwd):
        self.commands.append(command)
        return ShellResult(command, 0, "", "", 0)


class FakeTool:
    def __init__(self, receipt):
        self.receipt = receipt
        self.calls = 0

    def call(self, name, arguments, *, execution_id):
        self.calls += 1
        return ToolResult(False, dict(self.receipt), "submitted")

    def cancel(self, execution_id):
        raise AssertionError("no cancellation expected")


class FakeAwait:
    """Pending until ``done`` is set, like a real platform task."""

    def __init__(self, product: Path):
        self.product = product
        self.probes = 0
        self.done = False

    def probe(self, receipt_path):
        self.probes += 1
        assert Path(receipt_path).is_file(), "await must be handed a real receipt file"
        if not self.done:
            return AwaitResult("pending")
        return AwaitResult("completed", product=str(self.product))


def package(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(dict(
        id="chain", version="1.0.0", name="chain", description="chain", parameters={},
    )))
    (source / "graph.yaml").write_text(GRAPH)
    return source


def ports_for(tmp_path, product, receipt):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return Ports(
        shell=FakeShell(),
        artifacts=LocalArtifacts(work),
        tool=FakeTool(receipt),
        await_port=FakeAwait(product),
    )


def test_await_parks_without_running_a_shell_and_resumes_to_the_product(tmp_path):
    graph = compile_graph(package(tmp_path))
    product = tmp_path / "processed.jsonl"
    product.write_text('{"id": "1"}\n', encoding="utf-8")
    ports = ports_for(tmp_path, product, {"task_id": "t1", "output_dir": "/store/run"})
    directory = tmp_path / "run"

    parked = run(graph, {}, ports, directory)
    assert parked.status is RunStatus.WAITING
    assert parked.waiting["kind"] == "external"
    assert parked.waiting["node"] == "chain.wait"
    # The wait is a state, not a command: nothing was polled through the shell.
    assert not [c for c in ports.shell.commands
                if "wait_pipeline" in c or "poll_task" in c]
    assert ports.await_port.probes == 1

    ports.await_port.done = True
    finished = resume(graph, ports, directory)
    assert finished.status is RunStatus.SUCCEEDED
    assert not [c for c in ports.shell.commands
                if "wait_pipeline" in c or "poll_task" in c]
    assert ports.tool.calls == 1
    artifact = Path(ports.artifacts.root) / "artifacts" / "chain--wait.jsonl"
    assert artifact.read_text(encoding="utf-8") == '{"id": "1"}\n'


def test_await_deadline_fails_the_node_instead_of_parking_forever(tmp_path):
    graph = compile_graph(package(tmp_path))
    product = tmp_path / "processed.jsonl"
    product.write_text("x\n", encoding="utf-8")
    ports = ports_for(tmp_path, product, {"task_id": "t1"})
    directory = tmp_path / "run"
    assert run(graph, {}, ports, directory).status is RunStatus.WAITING

    # Age the checkpoint past the declared timeout, then wake the run up.
    checkpoint = json.loads((directory / "checkpoint.json").read_text())
    checkpoint["nodes"]["chain.wait"]["await"]["awaited_at"] -= 601
    (directory / "checkpoint.json").write_text(json.dumps(checkpoint))

    ports.await_port.done = True
    finished = resume(graph, ports, directory)
    assert finished.status is RunStatus.FAILED
    assert "超过" in finished.failure["summary"]


class Host:
    """The smallest host that can prepare one attempt."""

    def __init__(self, ports):
        self.ports = ports

    def prepare(self, run_id, package_dir, params, path_parameters, cancel, emit,
                **options):
        return self.ports, dict(params)

    def read(self, path):
        return Path(path).read_bytes()


def test_resuming_a_parked_await_never_resubmits_the_tool(tmp_path):
    service = GenomeService(tmp_path / "catalog")
    service.import_asset(package(tmp_path))
    state = service.submit("scope", "request", "chain", "1.0.0", {}, allow_draft=True)
    product = tmp_path / "processed.jsonl"
    product.write_text('{"id": "1"}\n', encoding="utf-8")
    ports = ports_for(tmp_path, product, {"task_id": "t1"})
    host = Host(ports)
    emit = lambda event: None  # noqa: E731 - the service injects its own sink

    parked = service.execute("scope", state["id"], host, emit)
    assert parked["status"] == "waiting"
    assert parked["result"]["waiting"]["kind"] == "external"
    assert ports.tool.calls == 1

    assert service.resume("scope", state["id"])["status"] == "queued"
    assert ports.tool.calls == 1, "the submit node must not run twice"

    ports.await_port.done = True
    finished = service.execute("scope", state["id"], host, emit)
    assert finished["status"] == "succeeded"
    assert ports.tool.calls == 1


def test_replay_requeue_resumes_an_interrupted_await_from_its_checkpoint(tmp_path):
    service = GenomeService(tmp_path / "catalog")
    service.import_asset(package(tmp_path))
    state = service.submit("scope", "request", "chain", "1.0.0", {}, allow_draft=True)
    ports = ports_for(tmp_path, tmp_path / "processed.jsonl", {"task_id": "t1"})
    host = Host(ports)

    assert service.execute("scope", state["id"], host, lambda event: None)["status"] == "waiting"
    assert ports.tool.calls == 1

    # A host restart marks a live run interrupted; the parked checkpoint survives.
    with service._db() as db:
        db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (state["id"],))

    requeued = service.replay_requeue("scope", state["id"])
    assert requeued["id"] == state["id"], "an interrupted await resumes, never resubmits"
    assert requeued["status"] == "queued"
    assert ports.tool.calls == 1


def test_cancelling_a_parked_await_releases_the_run(tmp_path):
    service = GenomeService(tmp_path / "catalog")
    service.import_asset(package(tmp_path))
    state = service.submit("scope", "request", "chain", "1.0.0", {}, allow_draft=True)
    ports = ports_for(tmp_path, tmp_path / "processed.jsonl", {"task_id": "t1"})

    assert service.execute("scope", state["id"], Host(ports), lambda event: None)["status"] == "waiting"
    assert service.cancel("scope", state["id"])["status"] == "cancelled"
