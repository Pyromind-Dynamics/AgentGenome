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

    def connect(scope="one", version=VERSION, cwd=None, stage_protocol=1, judge_protocol=0):
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
                          "version": version, "stage_protocol": stage_protocol, "judge_protocol": judge_protocol})
        request.close = lambda: (stream.close(), client.close())
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


REVISED_CLEAN = '''import csv
import sys


def main() -> int:
    if len(sys.argv) != 3 or not sys.argv[2]:
        print("clean: delimiter must be a non-empty string", file=sys.stderr)
        return 1
    delimiter = sys.argv[2]
    with open(sys.argv[1], newline="") as f:
        text = f.read()
    if len(delimiter) == 1:
        rows = list(csv.reader(text.splitlines(), delimiter=delimiter))
    else:
        rows = [line.split(delimiter) for line in text.splitlines() if line.strip()]
    if not rows:
        print("clean: empty input", file=sys.stderr)
        return 1
    header, data = rows[0], rows[1:]
    kept = []
    seen = set()
    for row in data:
        if any(cell.strip() == "" for cell in row):
            continue
        key = tuple(row)
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    out = csv.writer(sys.stdout)
    out.writerow(header)
    out.writerows(kept)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def delimiter_case(case_id, delimiter):
    header = delimiter.join(["name", "age"])
    rows = [delimiter.join(["Ada", "36"]), delimiter.join(["Ada", "36"]),
            delimiter.join(["Bob", ""]), delimiter.join(["Cy", "12"])]
    return {"id": case_id,
            "files": {"input.csv": "\n".join([header, *rows]) + "\n"},
            "params": {"data_file": "fixture:input.csv", "delimiter": delimiter},
            "assertions": [
                {"node": "clean-sop", "output": "report", "kind": "json",
                 "expected": {"rows": 2, "columns": ["name", "age"]}},
                {"node": "clean-sop.clean", "output": "cleaned", "kind": "csv",
                 "expected": [["name", "age"], ["Ada", "36"], ["Cy", "12"]]}]}


def failed_revision_run(client, tmp_path):
    client("import", {"source": str(ROOT / "templates/data-cleaning-revision")})
    data = tmp_path / "input.csv"
    data.write_text("name;;age\nAda;;36\nAda;;36\nBob;;\nCy;;12\n")
    run = client("validate", {"asset_id": "data-cleaning", "version": "1.1.0",
                              "params": {"data_file": str(data), "delimiter": ";;"}})
    client("start", {"run_id": run["id"]})
    assert wait_run(client, run["id"])["status"] == "failed"
    return run, data


def submit_arguments(draft, data):
    return {"revision_id": draft["revision_id"],
            "params": {"data_file": str(data), "delimiter": ";;"},
            "change_summary": "support multi-character delimiters",
            "baseline_cases": [delimiter_case("comma", ",")],
            "regression_cases": [delimiter_case("double-semicolon", ";;")]}


def test_native_revision_loop_publishes_and_keeps_verification_frozen(server, tmp_path):
    home, _, _, connect = server
    client = connect()
    run, data = failed_revision_run(client, tmp_path)

    recovery = client("status", {"run_id": run["id"]})["recovery"]
    assert recovery["revision"]["available"] is True
    assert recovery["revision"]["reasons"] == []

    draft = client("prepare_revision", {"run_id": run["id"]}, "draft")
    directory = Path(draft["editable_dir"])
    assert directory == home / "revision-drafts" / draft["revision_id"]
    assert draft["verification_protection"] == "prompt"
    assert draft["allowed_scripts"] == ["scripts/clean.py", "scripts/summarize.py"]
    # Prompt-level protection: the copy carries the whole package as context.
    assert (directory / "scripts/verify_report.py").is_file()
    (directory / "scripts/clean.py").write_text(REVISED_CLEAN)
    (directory / "scripts/verify_report.py").write_text("raise SystemExit(1)\n")

    child = client("run", submit_arguments(draft, data), "submit")
    assert child["status"] == "queued"
    assert child["candidate_version"] == "1.1.1"
    assert not directory.exists()
    assert client("run", submit_arguments(draft, data), "submit")["id"] == child["id"]

    client("start", {"run_id": child["id"]})
    done = wait_run(client, child["id"])
    assert done["status"] == "succeeded", done
    assert done["result"]["publication"] == {"status": "published", "version": "1.1.1"}
    assert [a["version"] for a in client("list")["assets"] if a["id"] == "data-cleaning"] == ["1.1.1"]
    original = (ROOT / "templates/data-cleaning-revision/scripts/verify_report.py").read_bytes()
    assert (home / "assets/data-cleaning/1.1.1/scripts/verify_report.py").read_bytes() == original
    report = json.loads(Path(done["result"]["outputs"]["report"]["execution_path"]).read_text())
    assert report == {"rows": 2, "columns": ["name", "age"]}


def test_native_revision_draft_survives_restart_while_orphans_are_reclaimed(server, tmp_path):
    home, process, launch, connect = server
    client = connect()
    run, data = failed_revision_run(client, tmp_path)
    draft = client("prepare_revision", {"run_id": run["id"]}, "draft")
    directory = Path(draft["editable_dir"])
    (home / "revision-drafts/never-registered").mkdir(parents=True)

    process.terminate()
    process.wait(timeout=5)
    launch()
    client = connect()
    assert not (home / "revision-drafts/never-registered").exists()
    assert directory.is_dir()

    (directory / "scripts/clean.py").write_text(REVISED_CLEAN)
    child = client("run", submit_arguments(draft, data), "submit")
    assert child["status"] == "queued"
    assert not directory.exists()


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


def native_stage(client):
    client("import", {"source": str(ROOT / "templates/native-agent-task-check")})
    run = client("validate", {"asset_id": "native-agent-task-check", "version": "1.0.0", "params": {}})
    client("start", {"run_id": run["id"]})
    for _ in range(200):
        stages = client("snapshot")["stages"]
        if stages:
            return run, stages[0]["request"]
        time.sleep(.02)
    raise AssertionError("stage was not handed off")


def test_legacy_native_client_rejects_llm_before_submission(server):
    home, _, _, connect = server
    client = connect(stage_protocol=None)
    client("import", {"source": str(ROOT / "templates/native-agent-task-check")})
    with pytest.raises(ValueError, match="agent_task"):
        client("validate", {"asset_id": "native-agent-task-check", "version": "1.0.0", "params": {}})
    assert client("snapshot")["runs"] == []


@pytest.mark.parametrize("mode,expected", [
    ("ok", "succeeded"), ("missing_receipt", "failed"),
    ("missing_file", "failed"), ("bad_report", "failed"), ("aborted", "failed"),
])
def test_native_llm_stage_waits_for_round_and_verifies_outputs(server, tmp_path, mode, expected):
    home, _, _, connect = server
    client = connect()
    run, request = native_stage(client)
    stage_id = request["request_id"]
    assert request["node_id"] == "agent-check.explain"
    assert request["resource_dir"].endswith("work/package")
    peer = connect()
    claim = client("stage_claim", {"request_id": stage_id})
    assert peer("stage_claim", {"request_id": stage_id}) == {"claimed": False}
    args = {"request_id": stage_id, "execution_id": claim["execution_id"]}
    receipt = {**args, "outcome": "completed", "summary": "Report written"}
    with pytest.raises(ValueError, match="not started"):
        client("step_result", receipt)
    with pytest.raises(ValueError, match="another connection"):
        peer("stage_begin", args)
    with pytest.raises(ValueError, match="execution round"):
        client("stage_begin", {**args, "execution_id": "wrong"})
    other = connect("other")
    assert other("snapshot")["stages"] == []
    with pytest.raises(ValueError, match="not active"):
        other("stage_claim", {"request_id": stage_id})
    client("stage_begin", args)
    if mode != "missing_file":
        Path(request["expected_outputs"][0]).write_text(json.dumps({
            "rows": 99 if mode == "bad_report" else 2,
            "names": ["Ada", "Cy"], "summary": "两条记录。",
        }))
    if mode != "missing_receipt":
        assert client("step_result", receipt)["awaiting_verification"]
        assert client("step_result", receipt)["accepted"]
    assert client("status", {"run_id": run["id"]})["status"] == "running"
    assert not (home / "runs" / run["id"] / "work/artifacts/agent-check--verify.out").exists()
    client("stage_finish", {**args, "status": "aborted" if mode == "aborted" else "completed"})
    finished = wait_run(client, run["id"])
    assert finished["status"] == expected, finished
    assert client("snapshot")["stages"] == []
    with pytest.raises(ValueError, match="not active"):
        client("step_result", receipt)
    if mode == "ok":
        report = Path(finished["result"]["outputs"]["report"]["execution_path"])
        assert json.loads(report.read_text()) == {"passed": True, "rows": 2, "names": ["Ada", "Cy"]}


@pytest.mark.parametrize("mode", ["cancel", "cancel_disconnect", "disconnect", "disconnect_unclaimed", "restart"])
def test_native_stage_interruption_never_redelivers_old_work(server, tmp_path, mode):
    home, process, launch, connect = server
    client = connect()
    observer = connect()
    run, request = native_stage(client)
    if mode != "disconnect_unclaimed":
        claim = client("stage_claim", {"request_id": request["request_id"]})
        args = {"request_id": request["request_id"], "execution_id": claim["execution_id"]}
        client("stage_begin", args)
    if mode in {"cancel", "cancel_disconnect"}:
        observer("cancel", {"run_id": run["id"]})
        if mode == "cancel_disconnect":
            client.close()
        expected = "cancelled"
    elif mode == "disconnect":
        client.close()
        expected = "failed"
    elif mode == "disconnect_unclaimed":
        connect("unrelated-session")
        observer.close()
        client.close()
        time.sleep(.15)
        observer = connect()
        expected = "failed"
    else:
        process.terminate()
        process.wait(timeout=5)
        launch()
        observer = connect()
        expected = "interrupted"
    assert wait_run(observer, run["id"])["status"] == expected
    assert observer("snapshot")["stages"] == []
    assert not Path(request["expected_outputs"][0]).exists()
    with pytest.raises(ValueError, match="not active"):
        observer("stage_claim", {"request_id": request["request_id"]})


def gated_stage_package(tmp_path):
    """The pre-stage script blocks until a release file exists, so the test decides
    exactly when the handoff is attempted relative to session exit."""
    source = tmp_path / "gated-stage"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps({
        "id": "native-gated-stage-check", "version": "1.0.0", "name": "会话退出阶段验证",
        "description": "前置脚本等待放行文件后再进入 llm 节点", "parameters": {},
        "requires": ["agent_task"], "outputs": {"report": "Agent 报告"},
    }))
    gate = tmp_path / "release"
    (source / "graph.yaml").write_text(f'''version: "gt/1.0"
node:
  id: agent-check
  mode: sequence
  output:
    report: {{type: json, compose: [explain.output.report]}}
  verify:
    - metric: "report.rows == 2"
  children:
    - id: prepare
      do:
        run: "until [ -f {gate} ]; do sleep 0.05; done; printf 'name,age' > {{artifact}}"
      output:
        source: {{type: path}}
      verify:
        - run: "test -s {{artifact}}"
    - id: explain
      input:
        source: {{type: path, from: prepare.output.source}}
      do:
        llm: "读取 CSV {{input.source}}，将 JSON 报告写入 {{artifact}}，然后调用 genome_step_result 并结束轮次。"
      output:
        report: {{type: json}}
      verify:
        - run: "test -s {{artifact}}"
''')
    return source


@pytest.mark.parametrize("reopen_before_handoff", [False, True])
def test_native_stage_interrupted_when_session_exits_before_handoff(server, tmp_path, reopen_before_handoff):
    home, _, _, connect = server
    source = gated_stage_package(tmp_path)
    client = connect()
    stranger = connect("other")
    client("import", {"source": str(source)})
    run = client("validate", {"asset_id": "native-gated-stage-check", "version": "1.0.0", "params": {}})
    client("start", {"run_id": run["id"]})
    client.close()
    reopened = None
    if reopen_before_handoff:
        # Reopening the same session must not revive its old connection's run,
        # even when another client keeps the registry service alive.
        time.sleep(.15)
        reopened = connect()
    # Release the pre-stage script only after the owning session has no client left;
    # the still-connected stranger must not keep the handoff alive for 30 minutes.
    (tmp_path / "release").write_text("go")
    artifact = home / "runs" / run["id"] / "work/artifacts/agent-check--prepare.out"
    for _ in range(300):
        if artifact.exists():
            break
        time.sleep(.02)
    else:
        raise AssertionError("pre-stage script did not finish")
    if reopened is None:
        reopened = connect()
    finished = wait_run(reopened, run["id"])
    assert finished["status"] == "failed", finished
    assert reopened("snapshot")["stages"] == []


def test_native_stage_loses_start_connection_even_when_peer_claimed_it(server):
    _, _, _, connect = server
    client = connect()
    run, request = native_stage(client)
    peer = connect()
    claim = peer("stage_claim", {"request_id": request["request_id"]})
    assert claim["claimed"]
    args = {"request_id": request["request_id"], "execution_id": claim["execution_id"]}
    peer("stage_begin", args)
    client.close()
    assert wait_run(peer, run["id"])["status"] == "failed"
    assert peer("snapshot")["stages"] == []
    with pytest.raises(ValueError, match="not active"):
        peer("step_result", {**args, "outcome": "completed"})


def test_native_stage_timeout_releases_pending_handoff(tmp_path):
    import asyncio
    from agentgenome.native_stages import NativeStages
    from agentgenome.service import GenomeService

    async def check():
        stages = NativeStages(GenomeService(tmp_path / "catalog"), timeout=.01)
        result = await stages.wait("one", {"request_id": "expired", "run_id": "run"})
        assert result["status"] == "failed"
        assert stages.snapshot("one") == []
    asyncio.run(check())


@pytest.mark.parametrize("already_waiting", [False, True])
def test_native_stage_cancellation_takes_precedence_over_disconnection(tmp_path, already_waiting):
    import asyncio
    from threading import Event
    from agentgenome.native_stages import NativeStages
    from agentgenome.service import GenomeService

    async def check():
        service = GenomeService(tmp_path / "catalog")
        signal = service._signals["run"] = Event()
        connected = True
        stages = NativeStages(service)
        if already_waiting:
            task = asyncio.create_task(stages.wait("one", {"request_id": "stage", "run_id": "run"},
                                                   deliverable=lambda: connected))
            await asyncio.sleep(0)
            assert "stage" in stages.pending
        signal.set()
        connected = False
        if already_waiting:
            stages.disconnect("owner", abandoned_scope="one")
            result = await task
        else:
            result = await stages.wait("one", {"request_id": "stage", "run_id": "run"},
                                       deliverable=lambda: connected)
        assert result["status"] == "cancelled"
        assert stages.pending == {}
    asyncio.run(check())


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


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_native_resume_lands_an_external_human_artifact(server, tmp_path, relative, restart):
    home, process, launch, connect = server
    client = connect()
    client("import", {"source": str(human_package(tmp_path))})
    run = client("validate", {"asset_id": "native-human-check", "version": "1.0.0", "params": {}})
    client("start", {"run_id": run["id"]})
    waiting = wait_run(client, run["id"])
    assert waiting["status"] == "waiting"
    target = Path(waiting["result"]["waiting"]["artifact"])
    source = tmp_path / "exports" / "review result.csv"
    source.parent.mkdir()
    source.write_text("approved\n")
    if restart:
        process.terminate()
        process.wait(timeout=5)
        launch()
        client = connect()
    argument = str(source.relative_to(tmp_path)) if relative else str(source)
    client("resume", {"run_id": run["id"], "artifact": argument})
    client("start", {"run_id": run["id"]})
    finished = wait_run(client, run["id"])
    assert finished["status"] == "succeeded", finished
    assert target.read_bytes() == source.read_bytes()


def test_native_requeue_registers_the_new_run_and_keeps_the_failure(server, tmp_path):
    home, _, _, connect = server
    client = connect()
    client("import", {"source": str(TEMPLATE)})
    failed = validate(client)
    client("start", {"run_id": failed["id"]})
    assert wait_run(client, failed["id"])["status"] == "failed"
    (tmp_path / "input.csv").write_text("name,age\nAda,36\n")
    retry = client("replay_requeue", {"run_id": failed["id"]})
    assert retry["id"] != failed["id"]
    assert any(row["id"] == retry["id"] for row in client("snapshot")["runs"])
    client("start", {"run_id": retry["id"]})
    assert wait_run(client, retry["id"])["status"] == "succeeded"
    assert client("status", {"run_id": failed["id"]})["status"] == "failed"


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
      do: {run: "mkdir -p scratch && printf 'side file\\n' > scratch/notes.txt && printf 'once\\n' >> {artifact}"}
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
    scratch = home / "runs" / run["id"] / "work/package/scratch/notes.txt"
    assert scratch.read_text() == "side file" + chr(10)
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
    prepared = second("run", {
        "asset_id": "data-cleaning",
        "version": "1.0.0",
        "params": {"data_file": "second.csv"},
        "execution_scope": {"mode": "full", "description": "验证一条新输入"},
    })
    assert prepared["run_id"] is None
    assert prepared["requires_confirmation"] is True
    confirmed = second("confirm_run", {
        "plan_id": prepared["plan_id"],
        "digest": prepared["digest"],
    })
    run = confirmed["run"]
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
    with pytest.raises(ValueError, match="package changed"):
        host.prepare("test", TEMPLATE, {"data_file": "a.csv"}, ["data_file"],
                     threading.Event(), lambda _: None)


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
    assert plan["status"] == "planned"
    assert plan["requires_confirmation"] is True
    assert plan["plan_id"]
    assert "capture_id" not in plan
    assert "run_id" not in plan
    assert not (home / "captures").exists()
    assert plan["review"]["plan_type"] == "structural"
    assert [step["kind"] for step in plan["review"]["steps"]] == ["run", "run"]
    assert plan["review"]["preflight"]["builds_package"] is False

    minimal_plan = client("distill", {
        **arguments,
        "execution_scope": {"mode": "sample", "description": "定向验证小样"},
        "validation_params": {
            "raw": "public_data/data/raw.csv",
            "rules": "public_data/rules.json",
        },
    }, "distill-minimal")
    assert "run_id" not in minimal_plan
    queued = client("confirm_distill", {
        "plan_id": minimal_plan["plan_id"],
        "digest": minimal_plan["digest"],
    }, "confirm-minimal")
    assert queued["preflight"]["trace_id"].startswith("distilled-")
    assert [node["id"] for node in queued["preflight"]["nodes"]] == ["prelabel", "clean"]
    assert queued["run_id"]
    client("start", {"run_id": queued["run_id"]})
    state = wait_run(client, queued["run_id"])
    assert state["status"] == "succeeded"
    assert state["result"]["capture"]["stage"] == "published"
    assert (
        client("capture_status", {"capture_id": queued["capture_id"]})["stage"]
        == "published"
    )
    assert (home / "captures" / queued["capture_id"] / "report.md").is_file()

    again_plan = client("distill", {
        **arguments,
        "execution_scope": {"mode": "sample", "description": "定向验证小样"},
        "validation_params": {
            "raw": "public_data/data/raw.csv",
            "rules": "public_data/rules.json",
        },
    }, "distill-2")
    assert "run_id" not in again_plan
    again = client("confirm_distill", {
        "plan_id": again_plan["plan_id"],
        "digest": again_plan["digest"],
    }, "confirm-again")
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


JUDGE_TEMPLATE = ROOT / "templates/native-model-judge-check"
JUDGE_GOOD = "订单总数12笔，已发货9笔，待发货3笔。"


def submit_judge(client, summary=JUDGE_GOOD, key="judge-run"):
    client("import", {"source": str(JUDGE_TEMPLATE)})
    run = client("validate", {"asset_id": "native-model-judge-check", "version": "1.0.0",
                              "params": {"summary": summary}}, key)
    client("start", {"run_id": run["id"]})
    return run


def pending_judge(client):
    for _ in range(200):
        pending = client("snapshot")["judges"]
        if pending:
            return pending[0]
        time.sleep(.02)
    raise AssertionError("judge request did not arrive")


def test_native_judge_requires_a_capable_client(server):
    _, _, _, connect = server
    with pytest.raises(ValueError, match="lacks capabilities.*model_judge"):
        submit_judge(connect(stage_protocol=0))


@pytest.mark.parametrize("response,exit_code", [
    ({"verdict": {"passed": True, "reason": "Matches all facts"}}, 0),
    ({"verdict": {"passed": False, "reason": "Missing pending orders"}}, 1),
    ({"error": "provider unavailable"}, 2),
    ({"verdict": {"passed": "true", "reason": "invalid bool"}}, 2),
])
def test_native_model_judge_verdicts_are_not_stage_receipts(server, response, exit_code):
    home, _, _, connect = server
    client = connect(stage_protocol=0, judge_protocol=1)
    run = submit_judge(client)
    request = pending_judge(client)
    assert request["evidence"] == JUDGE_GOOD + "\n"
    assert request["criterion"] == (JUDGE_TEMPLATE / "prompts/criterion.txt").read_text()
    assert client("snapshot")["stages"] == []
    peer = connect(judge_protocol=1)
    assert peer("snapshot")["judges"] == []
    with pytest.raises(ValueError, match="not active"):
        peer("judge_claim", {"request_id": request["request_id"]})
    claim = client("judge_claim", {"request_id": request["request_id"]})
    assert claim["claimed"] is True
    assert client("judge_claim", {"request_id": request["request_id"]})["claimed"] is False
    client("judge_result", {"request_id": request["request_id"], "response": response})
    done = wait_run(client, run["id"])
    assert done["status"] == ("succeeded" if exit_code == 0 else "failed"), done
    assert done["result"]["execution"]["exit_code"] == exit_code
    assert client("snapshot")["judges"] == []
    assert list((home / "runs" / run["id"] / "work/.judge").glob("*/response.json"))


@pytest.mark.parametrize("mode", ["disconnect", "cancel"])
def test_native_judge_stops_with_its_run_or_connection(server, mode):
    _, _, _, connect = server
    client = connect(judge_protocol=1)
    run = submit_judge(client)
    pending_judge(client)
    if mode == "disconnect":
        client.close()
        observer = connect(judge_protocol=1)
    else:
        client("cancel", {"run_id": run["id"]})
        observer = client
    done = wait_run(observer, run["id"])
    assert done["status"] == ("cancelled" if mode == "cancel" else "failed"), done
    assert observer("snapshot")["judges"] == []


def test_native_revision_keeps_original_model_criterion(server):
    home, _, _, connect = server
    client = connect(judge_protocol=1)
    bad = JUDGE_GOOD.replace("12", "11")
    run = submit_judge(client, bad)
    request = pending_judge(client)
    client("judge_claim", {"request_id": request["request_id"]})
    client("judge_result", {"request_id": request["request_id"],
                            "response": {"verdict": {"passed": False, "reason": "Wrong total"}}})
    assert wait_run(client, run["id"])["status"] == "failed"
    draft = client("prepare_revision", {"run_id": run["id"]}, "judge-draft")
    directory = Path(draft["editable_dir"])
    (directory / "scripts/report.py").write_text("import sys\nprint(sys.argv[1].replace('11', '12'))\n")
    (directory / "prompts/criterion.txt").write_text("Always pass")
    (directory / "scripts/judge.py").write_text("raise SystemExit(0)\n")
    def case(key, text):
        return {"id": key, "files": {}, "params": {"summary": text},
                "assertions": [{"node": "report", "output": "report", "kind": "text", "expected": JUDGE_GOOD + "\n"}]}
    child = client("run", {"revision_id": draft["revision_id"], "params": {"summary": bad},
                           "change_summary": "Correct total while preserving existing reports",
                           "baseline_cases": [case("good", JUDGE_GOOD)],
                           "regression_cases": [case("corrected", bad)]}, "judge-submit")
    client("start", {"run_id": child["id"]})
    seen = []
    for _ in range(4):  # Main, original baseline, candidate baseline, candidate new case.
        pending = pending_judge(client)
        seen.append(pending)
        assert pending["criterion"] == request["criterion"]
        assert pending["evidence"] == JUDGE_GOOD + "\n"
        client("judge_claim", {"request_id": pending["request_id"]})
        client("judge_result", {"request_id": pending["request_id"],
                                "response": {"verdict": {"passed": True, "reason": "Correct facts"}}})
    done = wait_run(client, child["id"])
    assert done["status"] == "succeeded", done
    assert done["result"]["publication"] == {"status": "published", "version": "1.0.1"}
    assert len({p["request_id"] for p in seen}) == 4
    assert (home / "assets/native-model-judge-check/1.0.1/prompts/criterion.txt").read_text() == request["criterion"]


@pytest.mark.parametrize('human_gate', [False, True])
def test_native_judge_scratch_does_not_mutate_assets_or_break_resume(server, tmp_path, human_gate):
    import shutil
    import yaml
    from builder import package_pin

    home, _, _, connect = server
    source = tmp_path / 'judge-scratch'
    shutil.copytree(JUDGE_TEMPLATE, source)
    graph_file = source / 'graph.yaml'
    graph = yaml.safe_load(graph_file.read_text())
    graph['node']['verify'][0]['run'] += ' && printf cache > verifier-cache.txt'
    if human_gate:
        graph['node'] = {
            'id': 'flow', 'mode': 'sequence',
            'output': {'report': {'type': 'path', 'compose': ['report.output.report']}},
            'verify': [{'run': 'test -s {output.report}'}],
            'children': [graph['node'], {
                'id': 'review', 'do': {'human': 'Confirm report'},
                'output': {'answer': {'type': 'path'}}, 'verify': [{'run': 'test -s {artifact}'}],
            }],
        }
    graph_file.write_text(yaml.safe_dump(graph, allow_unicode=True, sort_keys=False))
    client = connect(judge_protocol=1)
    client('import', {'source': str(source)})
    asset = home / 'assets/native-model-judge-check/1.0.0'
    original_pin = package_pin(asset)
    original_bytes = {p.relative_to(asset): p.read_bytes() for p in asset.rglob('*') if p.is_file()}

    def start(key):
        run = client('validate', {'asset_id': 'native-model-judge-check', 'version': '1.0.0',
                                  'params': {'summary': JUDGE_GOOD}}, key)
        client('start', {'run_id': run['id']})
        request = pending_judge(client)
        client('judge_claim', {'request_id': request['request_id']})
        client('judge_result', {'request_id': request['request_id'],
                                'response': {'verdict': {'passed': True, 'reason': 'All facts correct'}}})
        return run, wait_run(client, run['id'])

    run, done = start('first-scratch')
    verification = home / 'runs' / run['id'] / 'work/verification'
    assert (verification / 'verifier-cache.txt').read_text() == 'cache'
    assert package_pin(asset) == original_pin
    # This public status call used to fail in recovery.context after cache pollution.
    assert client('status', {'run_id': run['id']})['status'] == ('waiting' if human_gate else 'succeeded')
    if human_gate:
        saved_output = Path(done['result']['completed'][0]['outputs']['report']['execution_path'])
        saved_bytes = saved_output.read_bytes()
        Path(done['result']['waiting']['artifact']).write_text('approved\n')
        client('resume', {'run_id': run['id']})
        client('start', {'run_id': run['id']})
        done = wait_run(client, run['id'])
        assert done['status'] == 'succeeded', done
        assert not (verification / 'verifier-cache.txt').exists()
        assert saved_output.read_bytes() == saved_bytes
    else:
        assert done['status'] == 'succeeded', done
        second, again = start('second-scratch')
        assert again['status'] == 'succeeded', again
        assert second['id'] != run['id']
    assert package_pin(asset) == original_pin
    assert {p.relative_to(asset): p.read_bytes() for p in asset.rglob('*') if p.is_file()} == original_bytes


def test_native_revision_scratch_keeps_original_and_published_assets_usable(server, tmp_path):
    import shutil
    from builder import package_pin

    home, _, _, connect = server
    source = tmp_path / 'revision-scratch'
    shutil.copytree(ROOT / 'templates/data-cleaning-revision', source)
    verifier = source / 'scripts/verify_report.py'
    # Normal verifier scratch I/O, without model_judge capability.
    verifier.write_text('from pathlib import Path\nPath("verifier-cache.txt").write_text("cache")\n' + verifier.read_text())
    client = connect()
    client('import', {'source': str(source)})
    original = home / 'assets/data-cleaning/1.1.0'
    original_pin = package_pin(original)
    data = tmp_path / 'double.csv'
    data.write_text('name;;age\nAda;;36\nAda;;36\nBob;;\nCy;;12\n')
    parent = client('validate', {'asset_id': 'data-cleaning', 'version': '1.1.0',
                                 'params': {'data_file': str(data), 'delimiter': ';;'}}, 'scratch-parent')
    client('start', {'run_id': parent['id']})
    assert wait_run(client, parent['id'])['status'] == 'failed'
    draft = client('prepare_revision', {'run_id': parent['id']}, 'scratch-draft')
    Path(draft['editable_dir'], 'scripts/clean.py').write_text(REVISED_CLEAN)
    child = client('run', submit_arguments(draft, data), 'scratch-submit')
    client('start', {'run_id': child['id']})
    done = wait_run(client, child['id'])
    assert done['status'] == 'succeeded', done
    assert done['result']['publication'] == {'status': 'published', 'version': '1.1.1'}
    assert package_pin(original) == original_pin
    assert not (original / 'verifier-cache.txt').exists()
    candidate = home / 'assets/data-cleaning/1.1.1'
    assert not (candidate / 'verifier-cache.txt').exists()
    frozen = client('get', {'asset_id': 'data-cleaning', 'version': '1.1.1'})['pin']
    assert package_pin(candidate) == frozen
    assert client('status', {'run_id': child['id']})['status'] == 'succeeded'
    repeat = client('run', {'asset_id': 'data-cleaning', 'version': '1.1.1',
                            'params': {'data_file': str(data), 'delimiter': ';;'},
                            'execution_scope': {'mode': 'full', 'description': 'Reuse the published revision on the complete test input'}}, 'scratch-repeat')
    assert repeat['status'] == 'planned'
    repeat = client('confirm_run', {'plan_id': repeat['plan_id'], 'digest': repeat['digest']})['run']
    assert repeat['status'] == 'queued'
    client('start', {'run_id': repeat['id']})
    assert wait_run(client, repeat['id'])['status'] == 'succeeded'
    assert package_pin(candidate) == frozen
    scratch = list((home / 'runs').glob('*/work/verification/verifier-cache.txt'))
    assert len(scratch) == 3  # Revision main + two candidate cases; baseline uses its business copy.


TOOL_TEMPLATE = ROOT / 'templates/native-tool-check'


def tool_run(client, tmp_path):
    client('tool_bind', {'protocol': 1})
    client('import', {'source': str(TOOL_TEMPLATE)})
    target = tmp_path / 'actual-tool-output.txt'
    run = client('validate', {'asset_id': 'native-tool-check', 'version': '1.0.0',
                              'params': {'target': str(target)}}, 'tool-run')
    client('start', {'run_id': run['id']})
    for _ in range(200):
        pending = client('snapshot')['tools']
        if pending:
            return run, pending[0], target
        time.sleep(.02)
    raise AssertionError('tool was not handed off')


def test_native_tool_without_context_is_rejected(server, tmp_path):
    _, _, _, connect = server
    client = connect()
    client('import', {'source': str(TOOL_TEMPLATE)})
    with pytest.raises(ValueError, match='lacks capabilities.*tool.*Pi 1.1.0'):
        client('validate', {'asset_id': 'native-tool-check', 'version': '1.0.0',
                             'params': {'target': str(tmp_path / 'output')}})


def test_native_tool_result_materializes_once_and_is_connection_owned(server, tmp_path):
    home, _, _, connect = server
    client = connect()
    run, pending, target = tool_run(client, tmp_path)
    peer = connect()
    peer('tool_bind', {'protocol': 1})
    assert peer('snapshot')['tools'] == []
    args = {'execution_id': pending['execution_id']}
    with pytest.raises(ValueError, match='not active'):
        peer('tool_claim', args)
    assert client('tool_claim', args)['claimed']
    assert client('tool_claim', args) == {'claimed': False}
    # Transport double only: real Pi filesystem execution is covered by the TUI test.
    target.write_text(pending['arguments']['content'])
    response = {'is_error': False, 'result': {'path': str(target)}, 'text': 'Written'}
    client('tool_result', {**args, 'response': response})
    with pytest.raises(ValueError, match='not active'):
        client('tool_result', {**args, 'response': response})
    done = wait_run(client, run['id'])
    assert done['status'] == 'succeeded', done
    output = Path(done['result']['outputs']['receipt']['execution_path'])
    assert json.loads(output.read_text()) == {'path': str(target)}
    import sqlite3
    with sqlite3.connect(home / 'state.sqlite3') as db:
        assert db.execute('SELECT state FROM tool_receipts').fetchall() == [('completed',)]


@pytest.mark.parametrize('mode', ['cancel_before_claim', 'cancel_stopped', 'cancel_completed', 'failure', 'disconnect'])
def test_native_tool_failures_and_cancellation_keep_truthful_receipts(server, tmp_path, mode):
    home, _, _, connect = server
    client = connect()
    run, pending, target = tool_run(client, tmp_path)
    args = {'execution_id': pending['execution_id']}
    if mode != 'cancel_before_claim':
        client('tool_claim', args)
    observer = client
    if mode.startswith('cancel'):
        client('cancel', {'run_id': run['id']})
        if mode != 'cancel_before_claim':
            if mode == 'cancel_completed':
                target.write_text(pending['arguments']['content'])
            client('tool_result', {**args, 'response': {'is_error': mode != 'cancel_completed',
                'result': {'stopped': True}, 'text': 'Finished after cancellation request'}})
    elif mode == 'failure':
        client('tool_result', {**args, 'response': {'is_error': True, 'result': {}, 'text': 'Permission denied'}})
    else:
        client.close()
        observer = connect()
        observer('tool_bind', {'protocol': 1})
    done = wait_run(observer, run['id'])
    assert done['status'] == ('cancelled' if mode.startswith('cancel') else 'failed'), done
    assert observer('snapshot')['tools'] == []
    import sqlite3
    with sqlite3.connect(home / 'state.sqlite3') as db:
        state = db.execute('SELECT state FROM tool_receipts').fetchone()[0]
    expected = 'completed' if mode == 'cancel_completed' else 'unknown' if mode == 'disconnect' else 'failed'
    assert state == expected
    assert target.exists() == (mode == 'cancel_completed')
    if mode == 'disconnect':
        assert observer('status', {'run_id': run['id']})['recovery']['revision']['available'] is False
        with pytest.raises(ValueError, match='tool.*unconfirmed'):
            observer('prepare_revision', {'run_id': run['id']})
        with pytest.raises(ValueError, match='tool|confirmed|unknown'):
            observer('replay_requeue', {'run_id': run['id']})


def test_native_tool_unknown_result_does_not_retry_or_allow_requeue(server, tmp_path):
    import shutil
    import sqlite3
    import yaml

    home, _, _, connect = server
    source = tmp_path / 'retry-tool'
    shutil.copytree(TOOL_TEMPLATE, source)
    graph_path = source / 'graph.yaml'
    graph = yaml.safe_load(graph_path.read_text())
    graph['node']['on_fail'] = {'retry': 2}
    graph_path.write_text(yaml.safe_dump(graph))
    client = connect()
    client('tool_bind', {'protocol': 1})
    client('import', {'source': str(source)})
    target = tmp_path / 'side-effect.txt'
    run = client('validate', {'asset_id': 'native-tool-check', 'version': '1.0.0',
                              'params': {'target': str(target)}}, 'unknown-tool')
    client('start', {'run_id': run['id']})
    for _ in range(200):
        pending = client('snapshot')['tools']
        if pending:
            break
        time.sleep(.02)
    assert len(pending) == 1
    execution_id = pending[0]['execution_id']
    client('tool_claim', {'execution_id': execution_id})
    target.write_text('side effect happened once')
    # The JS adapter sends this error envelope when Pi reports an error after
    # execution. It must become an unknown receipt, not a retryable ToolResult.
    client('tool_result', {'execution_id': execution_id,
                           'response': {'error': 'Pi tool ran but returned an error; outcome unknown'}})
    done = wait_run(client, run['id'])
    assert done['status'] == 'failed', done
    assert client('snapshot')['tools'] == []
    assert target.read_text() == 'side effect happened once'
    with sqlite3.connect(home / 'state.sqlite3') as db:
        assert db.execute('SELECT state FROM tool_receipts').fetchall() == [('unknown',)]
    events = [json.loads(line) for line in (home / 'runs' / run['id'] / 'ledger.jsonl').read_text().splitlines()]
    assert not any(event['event'] == 'node_retry' for event in events)
    assert client('status', {'run_id': run['id']})['recovery']['revision']['available'] is False
    with pytest.raises(ValueError, match='tool.*unconfirmed'):
        client('prepare_revision', {'run_id': run['id']})
    with pytest.raises(ValueError, match='tool|confirmed|unknown'):
        client('replay_requeue', {'run_id': run['id']})


def test_native_tool_unconfirmed_stop_is_interrupted_and_not_replayed(server, tmp_path):
    home, _, _, connect = server
    client = connect()
    run, pending, _ = tool_run(client, tmp_path)
    client('tool_claim', {'execution_id': pending['execution_id']})
    client('cancel', {'run_id': run['id']})
    # No terminal response: a cancellation request is not proof that the tool stopped.
    done = wait_run(client, run['id'])
    assert done['status'] == 'interrupted', done
    assert done['result']['cancellation_unconfirmed']
    assert client('status', {'run_id': run['id']})['recovery']['revision']['available'] is False
    with pytest.raises(ValueError, match='tool.*unconfirmed'):
        client('prepare_revision', {'run_id': run['id']})
    assert client('snapshot')['tools'] == []
    with pytest.raises(ValueError, match='tool|confirmed|unknown'):
        client('replay_requeue', {'run_id': run['id']})


def native_await_run(client, tmp_path, timeout=10, tool=False):
    import yaml
    source = tmp_path / 'await-source'
    source.mkdir()
    (source / 'scripts').mkdir()
    output = tmp_path / 'external-output'
    output.mkdir()
    receipt = {'task_id': 'native-task', 'output_dir': str(output)}
    count = tmp_path / 'submit-count.txt'
    (source / 'scripts' / 'submit.py').write_text(
        'import json,sys\nfrom pathlib import Path\n'
        f'p=Path({str(count)!r});p.write_text(p.read_text()+"x" if p.exists() else "x")\n'
        f'Path(sys.argv[1]).write_text(json.dumps({receipt!r}))\n')
    submit = {'tool': {'name': 'async_submit', 'args': {}}} if tool else {'run': 'python3 scripts/submit.py {artifact}'}
    graph = {'version': 'gt/1.0', 'node': {'id': 'flow', 'mode': 'sequence', 'children': [
        {'id': 'submit', 'do': submit, 'output': {'receipt': {'type': 'json'}},
         'verify': [{'run': 'test -s {artifact}'}]},
        {'id': 'wait', 'input': {'receipt': {'type': 'json', 'from': 'submit.output.receipt'}},
         'do': {'await': {'receipt': '{input.receipt}', 'timeout': timeout}},
         'output': {'product': {'type': 'text'}}, 'verify': [{'run': 'test -s {artifact}'}]}],
        'output': {'product': {'type': 'text', 'compose': ['wait.output.product']}},
        'verify': [{'run': 'test -s {output.product}'}]}}
    if tool:
        graph['node']['children'].append({'id': 'finish',
            'do': {'tool': {'name': 'after_await', 'args': {}}},
            'output': {'receipt': {'type': 'json'}}, 'verify': [{'run': 'test -s {artifact}'}]})
    (source / 'manifest.json').write_text(json.dumps(dict(
        id='native-await-check', version='1.0.0', name='Await', description='Await', parameters={})))
    (source / 'graph.yaml').write_text(yaml.safe_dump(graph))
    if tool:
        client('tool_bind', {'protocol': 1})
    client('import', {'source': str(source)})
    run = client('validate', {'asset_id': 'native-await-check', 'version': '1.0.0', 'params': {}})
    client('start', {'run_id': run['id']})
    if tool:
        for _ in range(200):
            tasks = client('snapshot')['tools']
            if tasks:
                task = tasks[0]
                client('tool_claim', {'execution_id': task['execution_id']})
                client('tool_result', {'execution_id': task['execution_id'],
                                      'response': {'is_error': False, 'result': receipt, 'text': 'accepted'}})
                break
            time.sleep(.02)
        else:
            raise AssertionError('no submit request')
    parked = wait_run(client, run['id'])
    assert parked['status'] == 'waiting', parked
    assert parked['phase'] == 'waiting_external'
    return run['id'], output, count, parked['waiting']['deadline']


def wait_native_terminal(client, run_id):
    for _ in range(400):
        state = client('status', {'run_id': run_id})
        if state['status'] in {'succeeded', 'failed', 'cancelled', 'interrupted'}:
            return state
        time.sleep(.02)
    raise AssertionError('await did not wake automatically')


@pytest.mark.parametrize('outcome', ['completed', 'failed', 'missing', 'timeout'])
def test_native_await_automatically_resolves_real_files(server, tmp_path, outcome):
    _, _, _, connect = server
    client = connect()
    run_id, output, count, _ = native_await_run(client, tmp_path, timeout=2 if outcome in {'missing', 'timeout'} else 10)
    if outcome == 'completed':
        # An actual separate process produces the result; no FakeAwait or resume.
        subprocess.run([sys.executable, '-c', 'from pathlib import Path;import json;'
            f'p=Path({str(output)!r});(p/"processed.jsonl").write_text("real product\\n");'
            '(p/"report.json").write_text(json.dumps({"status":"succeeded"}))'], check=True)
    elif outcome != 'timeout':
        (output / 'report.json').write_text(json.dumps({'status': 'failed' if outcome == 'failed' else 'succeeded'}))
    result = wait_native_terminal(client, run_id)
    assert result['status'] == ('succeeded' if outcome == 'completed' else 'failed'), result
    assert count.read_text() == 'x', 'upstream submission must never run twice'
    if outcome == 'completed':
        assert Path(result['result']['outputs']['product']['execution_path']).read_text() == 'real product\n'
    elif outcome in {'missing', 'timeout'}:
        assert '超过' in result['result']['failure']['summary']
        assert not result['result'].get('outputs')


def test_native_await_restart_rebuilds_monitor_and_keeps_original_deadline(server, tmp_path):
    _, process, launch, connect = server
    client = connect()
    run_id, output, count, deadline = native_await_run(client, tmp_path)
    client.close()
    process.terminate(); process.wait(timeout=5)
    launch()
    client = connect()
    assert client('status', {'run_id': run_id})['waiting']['deadline'] == deadline
    (output / 'processed.jsonl').write_text('after restart\n')
    (output / 'report.json').write_text('{"status":"succeeded"}')
    assert wait_native_terminal(client, run_id)['status'] == 'succeeded'
    assert count.read_text() == 'x'


def test_native_await_cancel_ignores_late_completion(server, tmp_path):
    _, _, _, connect = server
    client = connect()
    run_id, output, count, _ = native_await_run(client, tmp_path)
    assert client('cancel', {'run_id': run_id})['status'] == 'cancelled'
    (output / 'processed.jsonl').write_text('late result\n')
    (output / 'report.json').write_text('{"status":"succeeded"}')
    time.sleep(1.2)  # Allow a full service monitor tick after the late event.
    assert client('status', {'run_id': run_id})['status'] == 'cancelled'
    assert count.read_text() == 'x'


def test_native_await_tool_submission_can_reconnect_without_resubmission(server, tmp_path):
    home, _, _, connect = server
    client = connect()
    run_id, output, _, deadline = native_await_run(client, tmp_path, tool=True)
    client.close()
    client = connect()
    client('snapshot')  # A read-only peer cannot acquire the tool-dependent continuation.
    (output / 'processed.jsonl').write_text('tool product\n')
    (output / 'report.json').write_text('{"status":"succeeded"}')
    time.sleep(1.2)
    assert client('status', {'run_id': run_id})['status'] == 'waiting'
    client('tool_bind', {'protocol': 1})
    client('snapshot')
    assert client('status', {'run_id': run_id})['waiting']['deadline'] == deadline
    for _ in range(200):
        tasks = client('snapshot')['tools']
        if tasks:
            task = tasks[0]
            assert task['name'] == 'after_await', 'must never re-submit the original task'
            client('tool_claim', {'execution_id': task['execution_id']})
            client('tool_result', {'execution_id': task['execution_id'],
                                  'response': {'is_error': False, 'result': {'done': True}, 'text': 'done'}})
            break
        time.sleep(.02)
    else:
        raise AssertionError('new connection did not receive the downstream tool')
    assert wait_native_terminal(client, run_id)['status'] == 'succeeded'
    assert client('snapshot')['tools'] == []
    import sqlite3
    with sqlite3.connect(home / 'state.sqlite3') as db:
        assert db.execute('SELECT state FROM tool_receipts').fetchall() == [('completed',), ('completed',)]


def test_native_await_keeps_idle_service_alive_without_reexecuting_pending_graph(server, tmp_path):
    import sqlite3
    home, process, _, connect = server
    client = connect()
    run_id, output, count, _ = native_await_run(client, tmp_path)
    checkpoint = home / 'runs' / run_id / 'checkpoint.json'
    saved_checkpoint = checkpoint.read_bytes()
    ledger = home / 'runs' / run_id / 'ledger.jsonl'
    saved_ledger = ledger.read_bytes()
    client.close()
    time.sleep(2.3)  # Fixture service idle limit is two seconds.
    assert process.poll() is None
    assert checkpoint.read_bytes() == saved_checkpoint
    assert ledger.read_bytes() == saved_ledger, 'pending probes must not execute the graph'
    (output / 'processed.jsonl').write_text('offline result\n')
    (output / 'report.json').write_text('{"status":"succeeded"}')
    for _ in range(200):
        with sqlite3.connect(home / 'state.sqlite3') as db:
            status = db.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()[0]
        if status == 'succeeded':
            break
        time.sleep(.02)
    assert status == 'succeeded'
    assert count.read_text() == 'x'
