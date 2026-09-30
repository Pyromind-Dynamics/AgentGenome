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
        process = subprocess.Popen(
            [sys.executable, "-m", "agentgenome.local_service", "--home", str(home), "--idle-seconds", "2"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={**os.environ, "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"})
        processes.append(process)
        return process

    def connect(scope="one", version=VERSION):
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

        request("hello", {"scope": scope, "cwd": str(tmp_path), "protocol": PROTOCOL, "version": version})
        return request

    process = launch()
    yield home, process, launch, connect
    for client in reversed(sockets):
        client.close()
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        process.stderr.close()


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
