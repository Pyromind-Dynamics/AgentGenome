import json
from pathlib import Path

import pytest

from adapters import LocalShell
from agentgenome.cli import LocalHost
from agentgenome.service import GenomeService
from core.artifacts import LocalArtifacts
from core.ports import Ports

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "data-cleaning"


@pytest.fixture
def catalog(tmp_path):
    service = GenomeService(tmp_path / "catalog")
    service.import_asset(TEMPLATE)
    return service


def submit(service, data, *, scope="validation", request="first", draft=True):
    return service.submit(scope, request, "data-cleaning", "1.0.0", {"data_file": str(data)}, allow_draft=draft)


def test_asset_validation_publication_and_two_batches(catalog, tmp_path):
    first = tmp_path / "first.csv"
    first.write_text("name,age\nAda,36\nAda,36\nBob,\nCy,12\n")
    assert catalog.list() == []
    with pytest.raises(ValueError, match="successful validation"):
        catalog.publish("data-cleaning", "1.0.0")
    state = submit(catalog, first)
    assert submit(catalog, first)["id"] == state["id"]
    result = catalog.execute("validation", state["id"], LocalHost(catalog.root), lambda _: None)
    assert result["status"] == "succeeded"
    report = catalog.root / "runs" / state["id"] / result["result"]["outputs"]["report"]["path"]
    assert json.loads(report.read_text()) == {"rows": 2, "columns": ["name", "age"]}
    catalog.publish("data-cleaning", "1.0.0")
    assert catalog.list()[0]["id"] == "data-cleaning"
    second = tmp_path / "second.csv"
    second.write_text("name,age\nX,1\n")
    state2 = submit(catalog, second, scope="another", draft=False)
    result2 = catalog.execute("another", state2["id"], LocalHost(catalog.root), lambda _: None)
    assert result2["status"] == "succeeded"
    assert result2["pin"] == result["pin"]
    with pytest.raises(ValueError, match="different parameters"):
        submit(catalog, second)
    with pytest.raises(ValueError, match="conversation"):
        catalog.status("someone-else", state["id"])


def test_parameters_busy_cancel_recovery_and_freeze(catalog, tmp_path):
    with pytest.raises(ValueError, match="parameters"):
        catalog.submit("a", "missing", "data-cleaning", "1.0.0", {}, allow_draft=True)
    state = submit(catalog, tmp_path / "data.csv")
    with pytest.raises(ValueError, match="active graph"):
        submit(catalog, tmp_path / "other.csv", request="other")
    assert catalog.cancel("validation", state["id"])["status"] == "cancelled"
    pending = submit(catalog, tmp_path / "data.csv", request="new")
    catalog.recover()
    assert catalog.status("validation", pending["id"])["status"] == "interrupted"
    package = catalog.root / "assets/data-cleaning/1.0.0"
    (package / "scripts/clean.py").write_text("print('changed')")
    with pytest.raises(ValueError, match="changed"):
        submit(catalog, tmp_path / "x.csv", request="changed")


def test_failure_does_not_run_downstream_and_remote_artifacts(catalog, tmp_path):
    state = submit(catalog, tmp_path / "absent.csv")
    result = catalog.execute("validation", state["id"], LocalHost(catalog.root), lambda _: None)
    assert result["status"] == "failed"
    assert not (catalog.root / "runs" / state["id"] / "artifacts/clean-sop--summarize.out").exists()
    data = tmp_path / "data.csv"
    data.write_text("name,age\nA,1\n")
    execution_root = tmp_path / "separate-execution-host"

    class Host:
        def prepare(self, run_id, package, params, paths, cancel, emit):
            return Ports(shell=LocalShell(), artifacts=LocalArtifacts(execution_root)), params

    state = submit(catalog, data, request="remote")
    result = catalog.execute("validation", state["id"], Host(), lambda _: None)
    assert result["status"] == "succeeded"
    assert not (catalog.root / "runs" / state["id"] / "artifacts").exists()
    assert json.loads((execution_root / result["result"]["outputs"]["report"]["path"]).read_text())["rows"] == 1


def test_unsupported_graph_is_rejected(catalog, tmp_path):
    import shutil
    source = tmp_path / "unsupported"
    shutil.copytree(TEMPLATE, source)
    graph = source / "graph.yaml"
    graph.write_text(graph.read_text().replace("mode: sequence", "mode: sequence\n  needs: [shell, llm]"))
    with pytest.raises(ValueError, match="fixed script"):
        catalog.import_asset(source)


def test_registry_has_one_execution_owner(catalog):
    other = GenomeService(catalog.root)
    catalog.claim_execution_owner()
    try:
        with pytest.raises(RuntimeError, match="already has"):
            other.claim_execution_owner()
    finally:
        catalog.release_execution_owner()
    other.claim_execution_owner()
    other.release_execution_owner()
