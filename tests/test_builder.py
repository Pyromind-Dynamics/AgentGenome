from __future__ import annotations

from pathlib import Path

import pytest

from builder import CompileError, check, compile, package_pin
from builder.compiler import _Compiler


def write_graph(tmp_path: Path, body: str) -> Path:
    package = tmp_path / "pkg"
    package.mkdir(parents=True)
    (package / "graph.yaml").write_text(body, encoding="utf-8")
    return package


VALID = """\
version: "gt/1.0"
node:
  id: root
  mode: sequence
  output:
    result: {type: json, compose: [work.output.result]}
  verify:
    - metric: "result.rows > 0"
  children:
    - id: work
      do: {run: "echo '{\\"rows\\": 1}' > {artifact}"}
      output:
        result: {type: json}
      verify:
        - run: "test -s {artifact}"
"""


def test_three_documented_legal_packages_compile(tmp_path: Path) -> None:
    assert check(Path("templates/data-cleaning")) == []
    graph = compile(Path("templates/data-cleaning"))
    assert graph.pin.startswith("sha256:")
    assert graph.node.children[0].input["data_file"].source.kind == "ancestor_input"
    assert graph.node.verify[1].expression is not None

    package = write_graph(tmp_path, VALID)
    assert check(package) == []
    assert compile(package).node.path == "root"

    skeleton = """\
version: "gt/1.0"
node:
  id: hello
  mode: sequence
  output: {greeting: {type: path, compose: [greet.output.greeting]}}
  verify: [{run: "test -s {output.greeting}"}]
  children:
    - id: greet
      do: {run: "echo hello > {artifact}"}
      output: {greeting: {type: path}}
      verify: [{run: "test -s {artifact}"}]
"""
    assert check(write_graph(tmp_path / "skeleton", skeleton)) == []


LEAF_DO = '      do: {run: "echo \'{\\"rows\\": 1}\' > {artifact}"}\n'


def test_artifact_extension_is_declared_per_leaf(tmp_path: Path) -> None:
    body = VALID.replace(LEAF_DO, "      artifact: {ext: jsonl}\n" + LEAF_DO)
    package = write_graph(tmp_path, body)
    assert check(package) == []
    assert compile(package).node.children[0].artifact_ext == ".jsonl"


@pytest.mark.parametrize(
    "declaration",
    ["artifact: {ext: 'json.l'}", "artifact: {suffix: jsonl}", "artifact: jsonl"],
)
def test_artifact_declaration_must_be_a_short_extension(
    tmp_path: Path, declaration: str
) -> None:
    body = VALID.replace(LEAF_DO, f"      {declaration}\n" + LEAF_DO)
    diagnostics = check(write_graph(tmp_path, body))
    assert [item.code for item in diagnostics] == ["E_ARTIFACT"]


def test_artifact_declaration_is_rejected_on_a_group(tmp_path: Path) -> None:
    body = VALID.replace(
        "  verify:\n    - metric:", "  artifact: {ext: jsonl}\n  verify:\n    - metric:"
    )
    diagnostics = check(write_graph(tmp_path, body))
    assert [item.code for item in diagnostics] == ["E_ARTIFACT"]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("      mode: try\n", "E_NODE_KIND"),
        ("  verify: []\n", "E_VERIFY_REQUIRED"),
        ("      on_fail: {retry: 0}\n", "E_RETRY_N"),
        ("      do: {run: 'echo {input.nope}'}\n", "E_SLOT"),
        ("  output:\n    result: {type: json}\n", "E_COMPOSE"),
        ("version: \"gt/9.0\"\n", "E_VERSION"),
    ],
)
def test_invalid_graphs_produce_machine_diagnostics(tmp_path: Path, mutation: str, code: str) -> None:
    body = VALID
    if mutation.startswith("version"):
        body = body.replace('version: "gt/1.0"\n', mutation)
    elif mutation.startswith("  verify"):
        body = body.replace('  verify:\n    - metric: "result.rows > 0"\n', mutation)
    elif mutation.startswith("  output"):
        start = body.index("  output:")
        end = body.index("  verify:", start)
        body = body[:start] + mutation + body[end:]
    elif mutation.startswith("      do"):
        start = body.index("      do:")
        end = body.index("      output:", start)
        body = body[:start] + mutation + body[end:]
    else:
        body = body.replace("      verify:\n        - run: \"test -s {artifact}\"\n",
                            "      verify:\n        - run: \"test -s {artifact}\"\n" + mutation)
    diagnostics = check(write_graph(tmp_path, body))
    assert any(item.code == code for item in diagnostics)
    with pytest.raises(CompileError):
        compile(tmp_path / "pkg")


def test_pin_is_deterministic_and_covers_attached_files(tmp_path: Path) -> None:
    package = write_graph(tmp_path, VALID)
    (package / "script.sh").write_text("echo one\n", encoding="utf-8")
    first = package_pin(package)
    assert first == package_pin(package)
    (package / "script.sh").write_text("echo two\n", encoding="utf-8")
    assert first != package_pin(package)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("version: [\n", "E_YAML_PARSE"),
        ('version: "gt/1.0"\n', "E_TOPLEVEL"),
        (VALID.replace("id: root", "id: Bad"), "E_NODE_ID"),
        (VALID.replace('do: {run: "echo \'{\\"rows\\": 1}\' > {artifact}"}',
                       'input: {x: {type: path, from: nowhere.output.x}}\n      do: {run: "echo ok > {artifact}"}'),
         "E_INPUT_FROM"),
        (VALID.replace('metric: "result.rows > 0"', 'metric: "sum(result.rows) > 0"'), "E_METRIC_EXPR"),
    ],
)
def test_remaining_diagnostic_codes(tmp_path: Path, body: str, expected: str) -> None:
    assert expected in {diagnostic.code for diagnostic in check(write_graph(tmp_path, body))}


def test_package_symlink_is_rejected(tmp_path: Path) -> None:
    package = write_graph(tmp_path, VALID)
    (package / "linked").symlink_to(package / "graph.yaml")
    assert "E_PACKAGE_SYMLINK" in {diagnostic.code for diagnostic in check(package)}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"do": {"run": "echo ok"}}, {"shell"}),
        ({"do": {"llm": "do work"}}, {"llm"}),
        ({"verify": [{"run": "true"}]}, {"shell"}),
        ({"verify": [{"metric": "true"}]}, set()),
        ({"verify": [{"llm": "judge"}]}, {"llm"}),
        ({"verify": [{"human": "approve"}]}, {"human"}),
        ({"danger": True}, {"human"}),
        ({"on_fail": "ask"}, {"human"}),
        ({"on_fail": "abort"}, set()),
        ({"mode": "try", "route": "llm"}, {"llm"}),
        ({"mode": "try", "route": "static"}, set()),
        ({"mode": "loop"}, set()),
    ],
)
def test_needs_inference_matches_documented_mapping(
    tmp_path: Path, raw: dict[str, object], expected: set[str]
) -> None:
    compiler = _Compiler(tmp_path)
    raw["needs"] = sorted(expected)
    assert compiler.needs(raw, "root") == expected
    assert compiler.diagnostics == []


def test_needs_are_normalized_and_cannot_omit_inferred_requirements(tmp_path: Path) -> None:
    missing = VALID.replace(
        '      do: {run: "echo \'{\\"rows\\": 1}\' > {artifact}"}\n',
        '      needs: []\n      do: {run: "echo \'{\\"rows\\": 1}\' > {artifact}"}\n',
    )
    assert "E_NEEDS" in {item.code for item in check(write_graph(tmp_path, missing))}

    extra = VALID.replace(
        '      do: {run: "echo \'{\\"rows\\": 1}\' > {artifact}"}\n',
        '      needs: [shell, human]\n      do: {run: "echo \'{\\"rows\\": 1}\' > {artifact}"}\n',
    )
    graph = compile(write_graph(tmp_path / "extra", extra))
    assert graph.node.children[0].needs == frozenset({"shell", "human"})

    invalid = VALID.replace(
        '      do: {run: "echo \'{\\"rows\\": 1}\' > {artifact}"}\n',
        '      needs: [database]\n      do: {run: "echo \'{\\"rows\\": 1}\' > {artifact}"}\n',
    )
    assert "E_NEEDS" in {item.code for item in check(write_graph(tmp_path / "invalid", invalid))}


TOOL_PACKAGE = """\
version: "gt/1.0"
node:
  id: submit-sop
  mode: sequence
  input:
    dataset: {type: path, from: params.dataset}
  output:
    submission: {type: json, compose: [submit.output.submission]}
  verify:
    - metric: "submission.task_id"
  children:
    - id: submit
      input:
        dataset: {type: path, from: submit-sop.input.dataset}
      do:
        tool:
          name: df_submit_pipeline
          args:
            dataset: "{input.dataset}"
            task_name: pretag
            retries: 2
      output:
        submission: {type: json}
      verify:
        - metric: "submission.task_id"
"""


def test_tool_leaf_compiles_and_infers_needs(tmp_path: Path) -> None:
    package = write_graph(tmp_path, TOOL_PACKAGE)
    assert check(package) == []
    node = compile(package).node.children[0]
    assert node.needs == frozenset({"tool"})
    assert node.do is None
    assert node.do_kind == "tool"
    assert node.tool is not None
    assert node.tool.name == "df_submit_pipeline"
    assert dict(node.tool.args) == {"dataset": "{input.dataset}", "task_name": "pretag",
                                    "retries": 2}


def test_tool_declaration_is_checked(tmp_path: Path) -> None:
    bad_name = TOOL_PACKAGE.replace("name: df_submit_pipeline", "name: DF Submit")
    codes = {item.code for item in check(write_graph(tmp_path / "name", bad_name))}
    assert "E_TOOL_CALL" in codes

    bad_args = TOOL_PACKAGE.replace(
        '          args:\n            dataset: "{input.dataset}"\n'
        '            task_name: pretag\n            retries: 2\n',
        "          args: [dataset]\n")
    codes = {item.code for item in check(write_graph(tmp_path / "args", bad_args))}
    assert "E_TOOL_CALL" in codes

    bad_slot = TOOL_PACKAGE.replace("task_name: pretag", 'task_name: "{input.nope}"')
    codes = {item.code for item in check(write_graph(tmp_path / "slot", bad_slot))}
    assert "E_SLOT" in codes


def test_tool_leaf_needs_declaration_may_not_omit_the_port(tmp_path: Path) -> None:
    missing = TOOL_PACKAGE.replace("    - id: submit\n", "    - id: submit\n      needs: []\n")
    codes = {item.code for item in check(write_graph(tmp_path, missing))}
    assert "E_NEEDS" in codes


def test_tried_slots_require_static_prior_candidate(tmp_path: Path) -> None:
    compiler = _Compiler(tmp_path)
    compiler.tried_slots(
        [{"id": "first"}, {"id": "second", "do": "echo {tried.first.reason}"}],
        "root", "static",
    )
    assert compiler.diagnostics == []
    compiler.tried_slots(
        [{"id": "first", "do": "echo {tried.second.reason}"}],
        "root", "static",
    )
    assert compiler.diagnostics[-1].code == "E_SLOT"
