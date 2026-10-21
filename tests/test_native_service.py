import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentgenome.local_service import PROTOCOL, VERSION, socket_path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates/data-cleaning"


@pytest.fixture
def server(tmp_path):
    processes, sockets = [], []
    home = tmp_path / "catalog"

    def launch():
        # A child that fails to start prints a traceback longer than a small pipe
        # buffer, so an undrained stderr pipe can block it before it exits.
        process = subprocess.Popen(
            [sys.executable, "-m", "agentgenome.local_service", "--home", str(home), "--idle-seconds", "2"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"})
        processes.append(process)
        return process

    def connect(scope="one", version=VERSION, cwd=None):
        for _ in range(200):
            client = socket.socket(socket.AF_UNIX)
            client.settimeout(5)
            try:
                client.connect(str(socket_path(home)))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                client.close()
                time.sleep(0.02)
        else:
            raise AssertionError("service did not start")
        sockets.append(client)
        stream = client.makefile("rwb", buffering=0)
        sockets.append(stream)

        def request(action, arguments=None, request_id="request"):
            payload = {"id": request_id, "action": action, "arguments": arguments or {}}
            stream.write((json.dumps(payload) + "\n").encode())
            reply = json.loads(stream.readline())
            if "error" in reply:
                raise ValueError(reply["error"])
            return reply["result"]

        request("hello", {"scope": scope, "cwd": str(cwd or tmp_path), "protocol": PROTOCOL,
                          "version": version})
        return request

    process = launch()
    yield home, process, launch, connect
    for client in reversed(sockets):
        client.close()
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def wait_run(client, run_id):
    for _ in range(300):
        state = client("status", {"run_id": run_id})
        if state["status"] not in {"queued", "running", "cancelling"}:
            return state
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def validate(client, data="input.csv", request="first"):
    return client("validate", {"asset_id": "data-cleaning", "version": "1.0.0",
                               "params": {"data_file": data}}, request)


def human_package(tmp_path):
    source = tmp_path / "human"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({
        "id": "native-human-check", "version": "1.0.0", "name": "人工节点验证",
        "description": "等待用户写入人工产物", "parameters": {},
    }))
    (source / "graph.yaml").write_text('''version: "gt/1.0"
node:
  id: review
  do: {human: "请用户写入 approved"}
  output:
    answer: {type: path}
  verify:
    - run: "test -s {artifact}"
''')
    return source


def test_native_human_node_parks_without_a_terminal_or_fabricated_artifact(server, tmp_path):
    home, _, _, connect = server
    source = human_package(tmp_path)
    client = connect()
    client("import", {"source": str(source)})
    run = client("validate", {"asset_id": "native-human-check", "version": "1.0.0", "params": {}})
    client("start", {"run_id": run["id"]})
    state = wait_run(client, run["id"])
    assert state["status"] == "waiting", state
    waiting = state["result"]["waiting"]
    assert waiting["node"] == "review"
    assert waiting["question"] == "请用户写入 approved"
    assert Path(waiting["artifact"]) == home / "runs" / run["id"] / "work/artifacts/review.out"
    assert not Path(waiting["artifact"]).exists()
    assert client("snapshot")["runs"][0]["result"]["waiting"] == waiting
    other = connect("other")
    assert other("snapshot")["runs"] == []
    with pytest.raises(ValueError, match="not found"):
        other("status", {"run_id": run["id"]})
    # Waiting has released the execution thread; status and cancel remain usable.
    assert client("cancel", {"run_id": run["id"]})["status"] == "cancelled"


def test_noninteractive_host_keeps_specific_handshake_diagnostic(tmp_path):
    import io
    from adapters.terminal_human import TerminalHuman
    from agentgenome.local_host import LocalExecutionHost
    from agentgenome.service import GenomeService

    service = GenomeService(tmp_path / "catalog")
    service.import_asset(human_package(tmp_path))
    run = service.submit("one", "validate", "native-human-check", "1.0.0", {}, allow_draft=True)
    host = LocalExecutionHost(service.root, tmp_path, human=TerminalHuman(io.StringIO()))
    state = service.execute("one", run["id"], host, lambda _: None)
    assert state["status"] == "interrupted"
    assert "E_NEEDS_NONINTERACTIVE [review]" in state["result"]["error"]
    assert "interactive: true" in state["result"]["error"]


@pytest.mark.parametrize("content,expected", [(None, "failed"), ("approved\n", "succeeded")])
@pytest.mark.parametrize("restart", [False, True])
def test_native_human_resume_verifies_existing_artifact(server, tmp_path, content, expected, restart):
    home, process, launch, connect = server
    client = connect()
    client("import", {"source": str(human_package(tmp_path))})
    run = client("validate", {"asset_id": "native-human-check", "version": "1.0.0", "params": {}})
    client("start", {"run_id": run["id"]})
    state = wait_run(client, run["id"])
    assert state["status"] == "waiting"
    artifact = Path(state["result"]["waiting"]["artifact"])
    if content is not None:
        artifact.write_text(content)
    if restart:
        process.terminate()
        process.wait(timeout=5)
        launch()
        client = connect()
        assert client("status", {"run_id": run["id"]})["status"] == "waiting"
    queued = client("resume", {"run_id": run["id"]})
    assert queued["status"] == "queued"
    assert "waiting" not in queued["result"]
    assert "Resume queued" in queued["result"]["instruction"]
    assert "waiting" not in client("snapshot")["runs"][0]["result"]
    assert client("resume", {"run_id": run["id"]})["status"] == "queued"
    client("start", {"run_id": run["id"]})
    finished = wait_run(client, run["id"])
    assert finished["id"] == run["id"]
    assert finished["status"] == expected, finished
    assert artifact.read_text() == content if content is not None else not artifact.exists()
    events = [json.loads(line)["event"] for line in (home / "runs" / run["id"] / "ledger.jsonl").read_text().splitlines()]
    assert events.count("run_start") == 1
    assert events.count("run_resumed") == 1
    assert client("resume", {"run_id": run["id"]})["status"] == expected


def test_same_version_service_rejects_different_build_without_affecting_runs(server, tmp_path):
    from agentgenome.local_service import runtime_fingerprint

    home, process, _, connect = server
    client = connect()
    client("import", {"source": str(TEMPLATE)})
    run = validate(client)
    with pytest.raises(ValueError, match="build mismatch"):
        client("hello", {"protocol": PROTOCOL, "version": VERSION,
                         "scope": "other", "cwd": str(tmp_path), "runtime_fingerprint": "0" * 64})
    assert process.poll() is None
    assert client("status", {"run_id": run["id"]})["status"] == "queued"
    hello = connect("fresh")
    # A compatible client still uses the same registry after the rejected request.
    assert hello("list") == {"assets": []}
    assert len(runtime_fingerprint()) == 64


def test_native_resume_skips_completed_scripts_and_approves_only_current_gate(server, tmp_path):
    home, _, _, connect = server
    source = human_package(tmp_path)
    (source / "graph.yaml").write_text('''version: "gt/1.0"
node:
  id: flow
  mode: sequence
  output:
    answer: {type: path, compose: [second.output.answer]}
  verify:
    - run: "test -s {output.answer}"
  children:
    - id: prepare
      do: {run: "printf 'once\\n' >> {artifact}"}
      output:
        marker: {type: path}
      verify:
        - run: "test -s {artifact}"
    - id: first
      do: {human: "First approval"}
      output:
        answer: {type: path}
      verify:
        - run: "test -s {artifact}"
    - id: second
      do: {human: "Second approval"}
      output:
        answer: {type: path}
      verify:
        - run: "test -s {artifact}"
''')
    client = connect()
    client("import", {"source": str(source)})
    run = client("validate", {"asset_id": "native-human-check", "version": "1.0.0", "params": {}})
    client("start", {"run_id": run["id"]})
    for node in ("flow.first", "flow.second"):
        state = wait_run(client, run["id"])
        assert state["status"] == "waiting", state
        assert state["result"]["waiting"]["node"] == node
        artifact = Path(state["result"]["waiting"]["artifact"])
        assert not artifact.exists()
        artifact.write_text("approved\n")
        client("resume", {"run_id": run["id"]})
        client("start", {"run_id": run["id"]})
    assert wait_run(client, run["id"])["status"] == "succeeded"
    marker = home / "runs" / run["id"] / "work/artifacts/flow--prepare.out"
    assert marker.read_text() == "once\n"
    events = [json.loads(line) for line in (home / "runs" / run["id"] / "ledger.jsonl").read_text().splitlines()]
    assert sum(e["event"] == "run_start" for e in events) == 1
    assert sum(e["event"] == "run_resumed" for e in events) == 2
    assert sum(e["event"] == "call_shell" and e.get("node") == "flow.prepare" for e in events) == 1


def test_shared_service_two_batches_progress_receipts_and_isolation(server, tmp_path):
    home, process, _, connect = server
    first, second = connect(), connect("two")
    first("import", {"source": str(TEMPLATE)})
    (tmp_path / "input.csv").write_text("name,age\nAda,36\nAda,36\nBob,\nCy,12\n")
    draft = validate(first)
    assert validate(first)["id"] == draft["id"]
    assert first("status", {"run_id": draft["id"]})["status"] == "queued"
    with pytest.raises(ValueError, match="not found"):
        second("status", {"run_id": draft["id"]})
    first("start", {"run_id": draft["id"]})
    first("start", {"run_id": draft["id"]})
    assert wait_run(first, draft["id"])["status"] == "succeeded"
    report = home / "runs" / draft["id"] / "work/artifacts/clean-sop--summarize.out"
    assert json.loads(report.read_text())["rows"] == 2
    records = first("snapshot")["runs"]
    assert records[0]["progress"]["node"] == "clean-sop"
    token = first("claim", {"run_id": draft["id"]})["token"]
    assert token
    same_session = connect()
    assert same_session("claim", {"run_id": draft["id"]})["token"] is None
    first("renew", {"run_id": draft["id"], "token": token})
    first("ack", {"run_id": draft["id"], "token": token})
    assert first("snapshot")["runs"] == []
    first("publish", {"asset_id": "data-cleaning", "version": "1.0.0"})
    assert len(second("list")["assets"]) == 1
    (tmp_path / "second.csv").write_text("name,age\nDee,20\n")
    run = second("run", {"asset_id": "data-cleaning", "version": "1.0.0",
                         "params": {"data_file": "second.csv"}})
    second("start", {"run_id": run["id"]})
    assert wait_run(second, run["id"])["status"] == "succeeded"
    assert draft["pin"] == run["pin"]
    assert process.poll() is None
    with pytest.raises(ValueError, match="mismatch"):
        connect("three", "0.0.0")
    assert process.poll() is None


def test_owner_race_recovery_and_queued_cancel(server, tmp_path):
    home, process, launch, connect = server
    client = connect()
    client("import", {"source": str(TEMPLATE)})
    queued = validate(client)
    competitor = launch()
    assert competitor.wait(timeout=5) != 0
    assert client("status", {"run_id": queued["id"]})["status"] == "queued"
    process.kill()
    process.wait(timeout=5)
    replacement = launch()
    client = connect()
    assert client("status", {"run_id": queued["id"]})["status"] == "interrupted"
    assert not (home / "runs" / queued["id"] / "work").exists()
    client("ack", {"run_id": queued["id"], "receipt": True})
    assert client("snapshot")["runs"] == []
    other = validate(client, request="other")
    assert client("cancel", {"run_id": other["id"]})["status"] == "cancelled"
    assert client("start", {"run_id": other["id"]})["status"] == "cancelled"
    assert replacement.poll() is None


def test_running_cancel_stops_process_group_and_failure_stops_downstream(server, tmp_path):
    import shutil

    home, _, _, connect = server
    source = tmp_path / "slow"
    shutil.copytree(TEMPLATE, source)
    script = source / "scripts/clean.py"
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(30)\n")
    client = connect()
    client("import", {"source": str(source)})
    run = validate(client)
    client("start", {"run_id": run["id"]})
    for _ in range(200):
        records = client("snapshot")["runs"]
        if records[0]["progress"].get("command"):
            break
        time.sleep(0.02)
    assert client("cancel", {"run_id": run["id"]})["status"] in {"cancelling", "cancelled"}
    assert wait_run(client, run["id"])["status"] == "cancelled"
    assert not (home / "runs" / run["id"] / "work/artifacts/clean-sop--summarize.out").exists()
    # A separate, unchanged template with missing input fails without running summarize.
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["version"] = "2.0.0"
    (source / "manifest.json").write_text(json.dumps(manifest))
    shutil.copyfile(TEMPLATE / "scripts/clean.py", script)
    client("import", {"source": str(source)})
    run = client("validate", {"asset_id": "data-cleaning", "version": "2.0.0",
                              "params": {"data_file": "missing.csv"}}, "missing")
    client("start", {"run_id": run["id"]})
    assert wait_run(client, run["id"])["status"] == "failed"
    assert not (home / "runs" / run["id"] / "work/artifacts/clean-sop--summarize.out").exists()


def test_native_host_does_not_change_public_asset(tmp_path):
    import threading
    from agentgenome.local_host import LocalExecutionHost
    from builder import package_pin

    original = package_pin(TEMPLATE)
    host = LocalExecutionHost(tmp_path, tmp_path)
    ports, params = host.prepare("test", TEMPLATE, {"data_file": "a.csv"}, ["data_file"],
                                threading.Event(), lambda _: None)
    (ports.cwd / "scripts/clean.py").write_text("changed")
    assert package_pin(TEMPLATE) == original
    assert params["data_file"] == str(tmp_path / "a.csv")


def test_two_sessions_can_execute_concurrently_and_node_change_clears_progress(server, tmp_path):
    import shutil

    home, _, _, connect = server
    source = tmp_path / "parallel"
    shutil.copytree(TEMPLATE, source)
    (source / "scripts/clean.py").write_text(
        "import time\ntime.sleep(1)\nprint('name,age\\nAda,36')\n")
    first, second = connect("first"), connect("second")
    first("import", {"source": str(source)})
    one, two = validate(first), validate(second)
    first("start", {"run_id": one["id"]})
    second("start", {"run_id": two["id"]})
    for _ in range(100):
        statuses = [client("status", {"run_id": run["id"]})["status"]
                    for client, run in [(first, one), (second, two)]]
        if statuses == ["running", "running"]:
            break
        time.sleep(0.01)
    assert statuses == ["running", "running"]
    for client, run in [(first, one), (second, two)]:
        assert wait_run(client, run["id"])["status"] == "succeeded"
        assert (home / "runs" / run["id"] / "work/artifacts/clean-sop--clean.out").exists()
        progress = client("snapshot")["runs"][0]["progress"]
        assert "clean.py" not in progress.get("command", "")


PRELABEL = '''import csv, json, sys

raw, rules = sys.argv[1], sys.argv[2]
prefix = json.load(open(rules)).get("prefix", "")
for row in csv.DictReader(open(raw)):
    print(json.dumps({"text": prefix + row["text"], "label": row["label"]}))
'''

CLEAN = '''import csv, json, sys

rows = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
writer = csv.DictWriter(sys.stdout, fieldnames=["text", "label"])
writer.writeheader()
for row in rows:
    writer.writerow(row)
'''


def captured_workspace(root):
    """A workspace whose exported traces point at scripts that actually run."""
    for name, source in (("prelabel", PRELABEL), ("clean", CLEAN)):
        path = root / "public_data" / "scripts" / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    (root / "public_data" / "rules.json").write_text('{"prefix": "p-"}', encoding="utf-8")
    (root / "public_data" / "data").mkdir(parents=True, exist_ok=True)
    (root / "public_data" / "data" / "raw.csv").write_text("text,label\none,a\n", encoding="utf-8")
    (root / "public_data" / "out").mkdir(parents=True, exist_ok=True)
    (root / "public_data" / "out" / "review.jsonl").write_text(
        '{"text": "p-one", "label": "a"}\n', encoding="utf-8")
    commands = [
        "python3 public_data/scripts/prelabel.py public_data/data/raw.csv "
        "public_data/rules.json > public_data/out/prelabeled.jsonl",
        "python3 public_data/scripts/clean.py public_data/out/review.jsonl "
        "> public_data/out/clean.csv",
    ]
    refs = []
    for trace_id in ("s1", "s2"):
        steps = [{"kind": "tool", "entry_id": f"{trace_id}-{index}", "tool": "bash",
                  "call_id": f"{trace_id}-{index}", "timestamp": "2026-01-01T00:00:01Z",
                  "args": {"command": command},
                  "result": {"is_error": False, "text": "ok"}}
                 for index, command in enumerate(commands, start=1)]
        document = {"trace_version": 1, "trace_id": trace_id, "session_id": trace_id,
                    "step_count": len(steps), "steps": steps}
        relative = f"public_data/agentgenome-traces/{trace_id}.json"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")
        refs.append({"trace_id": trace_id, "path": relative})
    return refs


def test_distill_over_the_socket_publishes_and_reuses_a_graph(server, tmp_path):
    home, _, _, connect = server
    workspace = tmp_path / "distill-workspace"
    captured_workspace(workspace)
    client = connect("distill", cwd=str(workspace))
    arguments = {
        "label": "预打标到清洗", "asset_id": "prelabel-clean-distilled",
        "final_artifact": "public_data/out/clean.csv",
        "steps": [
            {"run": "python3 public_data/scripts/prelabel.py public_data/data/raw.csv "
                    "public_data/rules.json > public_data/out/prelabeled.jsonl",
             "from": "s1:1"},
            {"run": "python3 public_data/scripts/clean.py "
                    "public_data/out/prelabeled.jsonl > public_data/out/clean.csv",
             "from": "s1:2"},
        ],
    }

    plan = client("distill", arguments, "distill-1")
    assert plan["reused"] is False
    assert plan["run_id"]
    assert plan["distilled"]["trace_id"].startswith("distilled-")
    assert [node["id"] for node in plan["distilled"]["nodes"]] == ["prelabel", "clean"]

    client("start", {"run_id": plan["run_id"]})
    state = wait_run(client, plan["run_id"])
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert client("capture_status", {"capture_id": plan["capture_id"]})["stage"] == "published"
    assert (home / "captures" / plan["capture_id"] / "report.md").is_file()

    again = client("distill", arguments, "distill-2")
    assert again["reused"] is True
    assert again["run_id"] is None


def test_capture_over_the_socket_publishes_and_reuses_a_graph(server, tmp_path):
    home, _, _, connect = server
    workspace = tmp_path / "capture-workspace"
    refs = captured_workspace(workspace)
    client = connect("capture", cwd=str(workspace))

    plan = client("capture", {"trace_refs": refs, "label": "预打标到清洗",
                              "asset_id": "prelabel-clean"}, "capture-1")
    assert plan["reused"] is False
    assert plan["generalized"] is False
    assert plan["run_id"]

    client("start", {"run_id": plan["run_id"]})
    state = wait_run(client, plan["run_id"])
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert [item["id"] for item in client("list")["assets"]] == ["prelabel-clean"]
    assert client("capture_status", {"capture_id": plan["capture_id"]})["stage"] == "published"
    assert client("snapshot")["runs"][0]["asset_id"] == "prelabel-clean"
    assert (home / "captures" / plan["capture_id"] / "report.md").is_file()

    again = client("capture", {"trace_refs": refs, "label": "预打标到清洗",
                               "asset_id": "prelabel-clean"}, "capture-2")
    assert again["reused"] is True
    assert again["run_id"] is None
    assert len([item for item in client("snapshot")["runs"]]) == 1
