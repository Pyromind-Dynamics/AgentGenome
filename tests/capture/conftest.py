import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentgenome.local_host import LocalExecutionHost
from agentgenome.service import GenomeService
from agentgenome.capture.trace import capture_trace

PREL_LABEL = '''import csv, json, sys

raw, rules = sys.argv[1], sys.argv[2]
prefix = json.load(open(rules)).get("prefix", "")
with open(raw, newline="") as stream:
    for row in csv.DictReader(stream):
        print(json.dumps({"text": prefix + row["text"], "label": row.get("label", "")}))
'''

CLEAN = '''import csv, json, sys

with open(sys.argv[1]) as stream:
    rows = [json.loads(line) for line in stream if line.strip()]
writer = csv.DictWriter(sys.stdout, fieldnames=["text", "label"])
writer.writeheader()
for row in rows:
    writer.writerow({"text": row["text"], "label": row["label"]})
'''

TRAIN = '''import argparse, csv

parser = argparse.ArgumentParser()
parser.add_argument("data")
parser.add_argument("--epochs", type=int, default=1)
parser.add_argument("--output", required=True)
args = parser.parse_args()
with open(args.data, newline="") as stream:
    rows = list(csv.DictReader(stream))
with open(args.output, "w") as stream:
    stream.write(f"rows={len(rows)} epochs={args.epochs}\\n")
'''

EVALUATE = '''import csv, json, sys

model, data = sys.argv[1], sys.argv[2]
settings = dict(item.split("=") for item in open(model).read().split())
with open(data, newline="") as stream:
    rows = list(csv.DictReader(stream))
print(json.dumps({"rows": len(rows), "epochs": int(settings["epochs"])}))
'''


@dataclass(frozen=True)
class PipelineWorkspace:
    root: Path

    def params(self, *, raw="data/raw.csv", review="out/review.jsonl", rules="rules.json"):
        return {"raw": raw, "review": review, "rules": rules}

    def write(self, relative, text):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


@pytest.fixture
def pipeline_workspace(tmp_path):
    """A workspace whose scripts actually run, so replays are real executions."""
    workspace = PipelineWorkspace(tmp_path / "workspace")
    for name, source in (("prelabel", PREL_LABEL), ("clean", CLEAN),
                         ("train", TRAIN), ("evaluate", EVALUATE)):
        workspace.write(f"scripts/{name}.py", source)
    workspace.write("rules.json", json.dumps({"prefix": "p-"}))
    workspace.write("data/raw.csv", "text,label\none,a\ntwo,b\n")
    workspace.write("data/raw2.csv", "text,label\nthree,c\n")
    workspace.write("out/review.jsonl",
                    json.dumps({"text": "p-one", "label": "a"}) + "\n"
                    + json.dumps({"text": "p-two", "label": "b"}) + "\n")
    workspace.write("out/review2.jsonl",
                    json.dumps({"text": "p-three", "label": "c"}) + "\n")
    return workspace


@pytest.fixture
def service(tmp_path):
    return GenomeService(tmp_path / "registry")


@pytest.fixture
def host(service, pipeline_workspace):
    return LocalExecutionHost(service.root, pipeline_workspace.root)


@pytest.fixture
def user_entry():
    def build(entry_id, text):
        return {"kind": "user", "entry_id": entry_id,
                "timestamp": "2026-01-01T00:00:00Z", "text": text}

    return build


@pytest.fixture
def tool_entry():
    def build(entry_id, command, *, text="ok", is_error=False, tool="bash", args=None):
        return {
            "kind": "tool", "entry_id": entry_id, "timestamp": "2026-01-01T00:00:01Z",
            "tool": tool, "call_id": entry_id,
            "args": {"command": command} if args is None else args,
            "result": {"is_error": is_error, "text": text},
        }

    return build


@pytest.fixture
def trace_factory():
    def build(steps, trace_id="session-1", session_name="预打标到评测"):
        document = {"trace_version": 1, "trace_id": trace_id, "session_id": trace_id,
                    "session_name": session_name, "step_count": len(steps), "steps": steps}
        return capture_trace(json.dumps(document))

    return build


@pytest.fixture
def pipeline_document(tool_entry, user_entry):
    """预打标 → 人工复核（会话外）→ 清洗 → 训练 → 评测。"""

    def build(trace_id, *, prefix="a", raw="data/raw.csv", reviewed="out/review.jsonl",
              epochs=3, model="models/model.pt", metrics="reports/metrics.json"):
        steps = [
            user_entry("u1", "先给数据预打标"),
            tool_entry(f"{prefix}1",
                       f"python3 scripts/prelabel.py {raw} rules.json > out/prelabeled.jsonl"),
            user_entry("u2", f"我去 Label Studio 复核完了，导出的结果在 {reviewed}，继续清洗"),
            tool_entry(f"{prefix}2",
                       f"python3 scripts/clean.py {reviewed} > out/clean.csv"),
            tool_entry(f"{prefix}3",
                       f"python3 scripts/train.py out/clean.csv --epochs {epochs} "
                       f"--output {model}"),
            tool_entry(f"{prefix}4",
                       f"python3 scripts/evaluate.py {model} out/clean.csv > {metrics}"),
        ]
        return {"trace_version": 1, "trace_id": trace_id, "session_id": trace_id,
                "session_name": "预打标到评测", "step_count": len(steps), "steps": steps}

    return build


@pytest.fixture
def pipeline_trace(pipeline_document):
    def build(trace_id, **kwargs):
        return capture_trace(json.dumps(pipeline_document(trace_id, **kwargs)))

    return build


@pytest.fixture
def trace_refs(pipeline_workspace, pipeline_document):
    """Write traces where the extension exports them, then return genome_capture refs."""

    def build(*traces):
        refs = []
        for trace in traces:
            trace = dict(trace)
            trace_id = str(trace.pop("trace_id"))
            relative = f"public_data/agentgenome-traces/{trace_id}.json"
            pipeline_workspace.write(relative, json.dumps(pipeline_document(trace_id, **trace)))
            refs.append({"trace_id": trace_id, "path": relative})
        return refs

    return build
