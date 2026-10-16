"""Stage A: normalize an exported Pi session into alignable steps.

The plugin exports raw entries; this module is the only place that knows how a
Pi tool call becomes a graph-worthy step. Everything here is mechanical: no
model calls, no filesystem access, so a capture can be replayed in tests.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

TRACE_VERSION = 1
INLINE_MARK = "<inline>"

KIND_HUMAN = "human"
KIND_SHELL = "shell"
KIND_WRITE = "write"
KIND_READ = "read"
KIND_LLM = "llm"
KIND_TOOL = "tool"
KIND_OTHER = "other"

ROLE_SOURCE = "source"
ROLE_RULES = "rules"
ROLE_SCRIPT = "script"
ROLE_MODEL = "model"
ROLE_REPORT = "report"
ROLE_OUTPUT = "output"

_SHELL_TOOLS = {"bash", "terminal", "shell"}
_WRITE_TOOLS = {"write", "edit", "multiedit", "patch"}
_PROBE_TOOLS = {"read", "grep", "ls", "glob", "find", "search"}
_AGENT_TOOLS = {"task", "subagent", "agent"}
_IGNORED_TOOLS = {"todo", "todowrite", "todoread", "update_plan"}

_INTERPRETERS = {
    "python", "python3", "python3.10", "python3.11", "python3.12",
    "node", "bash", "sh", "zsh", "ruby", "perl", "rscript", "uv",
    "pytest", "make", "docker", "cargo", "go",
}
_SCRIPT_EXTS = {".py", ".sh", ".js", ".ts", ".r", ".rb", ".sql"}
_MODEL_EXTS = {".pt", ".pth", ".bin", ".safetensors", ".ckpt", ".onnx", ".gguf", ".joblib", ".pkl"}
_RULE_EXTS = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf"}
_REPORT_EXTS = {".md", ".html", ".htm", ".pdf", ".rst"}

_INLINE_FLAGS = {"-c", "-e", "--eval"}
_MODULE_FLAGS = {"-m", "--module"}
_VALUE_SHORT_FLAGS = {"-o", "-i", "-m", "-c", "-e", "-d", "-t", "-s", "-w",
                      "-k", "-p", "-n", "-g", "-j", "-x"}
_BOOLEAN_FLAGS = {"--verbose", "--quiet", "--force", "--recursive", "--all", "--dry-run",
                  "--debug", "--help", "--version", "--no-cache", "--yes", "--check"}
_WRITE_FLAGS = {
    "-o", "--output", "--out", "--outdir", "--output-dir", "--output_dir",
    "--output-file", "--dest", "--destination", "--save", "--save-to", "--target",
}
_READ_FLAGS = {
    "-i", "--input", "--in", "--input-dir", "--input_dir", "--source", "--src",
    "--data", "--data-file", "--dataset", "--config", "--rules", "--model",
    "--weights", "--checkpoint", "--from",
}

_WRITE_OPERATORS = {">", ">>", "1>", "2>", "&>"}
_READ_OPERATORS = {"<", "<<<"}
_OPERATORS = {"|", "||", "&&", ";", "&"}

# Shell redirections to a device are noise suppression, not a product: counting them
# as writes turns every `2>/dev/null` probe into a node with an unusable output.
_DEVICE_PATHS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin"}

# Positional paths belong to the script, so they read by default; only commands that
# mutate the filesystem make their positional paths outputs.
_WRITE_COMMANDS = {"rm", "rmdir", "mkdir", "touch", "tee", "truncate", "dd", "chmod",
                   "chown", "unzip", "gunzip", "gzip"}
_MOVE_COMMANDS = {"cp", "mv", "ln", "rsync", "install"}

_PROBE_COMMANDS = {
    "cat", "head", "tail", "less", "more", "ls", "find", "grep", "rg", "wc", "stat",
    "file", "du", "df", "pwd", "which", "whoami", "env", "echo", "printf", "sed",
    "awk", "sort", "uniq", "tree", "jq", "diff", "comm", "nl", "cut", "tr", "xxd",
    "md5sum", "sha256sum", "basename", "dirname", "realpath", "date", "uname",
}
_READONLY_GIT = {"status", "diff", "log", "show", "branch", "remote", "rev-parse",
                 "ls-files", "describe", "blame", "config"}

# Pi has no structural marker for a denied tool call, so the refusal is read from
# the error text. A miss only costs a garbage filter, never a wrong step.
_REFUSAL = re.compile(
    r"user denied|denied by user|was rejected|rejected by|blocked by|not allowed by",
    re.IGNORECASE,
)

# A `{name}` token is how the agent declares a chain parameter instead of a literal
# path, so the same command can be replayed with another input.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")


@dataclass(frozen=True)
class Step:
    seq: int
    trace_id: str
    kind: str
    tool_name: str
    command: str
    command_shape: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    success: bool
    rejected: bool
    raw_ref: str
    # A distilled llm step carries its acceptance command here; session steps leave it
    # empty because a captured command's verify is derived from its declared output.
    verify: str = ""
    # A distilled tool step carries the host tool's arguments; the artifact it declares
    # holds the call's JSON result. Session steps record no args, so they cannot be
    # emitted as tool nodes.
    arguments: Mapping[str, Any] | None = None

    @property
    def signature(self) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
        return (self.kind, self.command_shape, self.reads, self.writes)

    @property
    def inline_code(self) -> bool:
        return INLINE_MARK in self.command_shape

    def payload(self) -> dict[str, Any]:
        data = {
            "seq": self.seq,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "command": self.command,
            "command_shape": self.command_shape,
            "reads": list(self.reads),
            "writes": list(self.writes),
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "success": self.success,
            "rejected": self.rejected,
            "inline_code": self.inline_code,
            "verify": self.verify,
            "raw_ref": self.raw_ref,
            "signature": [self.kind, self.command_shape, list(self.reads), list(self.writes)],
        }
        if self.arguments is not None:
            data["arguments"] = dict(self.arguments)
        return data


@dataclass(frozen=True)
class Dropped:
    seq: int
    kind: str
    tool_name: str
    reason: str
    raw_ref: str

    def payload(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "tool_name": self.tool_name,
                "reason": self.reason, "raw_ref": self.raw_ref}


@dataclass(frozen=True)
class Trace:
    trace_id: str
    session_id: str | None
    session_name: str | None
    source: str
    steps: tuple[Step, ...]
    dropped: tuple[Dropped, ...]


@dataclass(frozen=True)
class Command:
    shape: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    name: str
    # Flag values the parser could not classify: `--images-root storage` keeps its
    # word, but nothing in the command says whether `storage` is a path. The pair is
    # (flag, value) so a later stage can ask the workspace which one it is.
    literals: tuple[tuple[str, str], ...] = ()


def _basename(token: str) -> str:
    name = PurePosixPath(token).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def _is_path(token: str) -> bool:
    if placeholder_key(token) is not None:
        return True
    if token.startswith(("/", "./", "../", "~/")):
        return True
    if "/" in token:
        return True
    return bool(_suffix(token)) and not token.startswith("-")


def placeholder_key(token: str) -> str | None:
    """The parameter name of a `{name}` token, or None for a literal path."""
    match = _PLACEHOLDER.fullmatch(token)
    return match.group(1) if match else None


def canonical_path(token: str, bindings: Mapping[str, str]) -> str:
    """The file a path token names: a `{param}` stands for the path bound to it.

    A plan may write `{filtered_jsonl}` in one step and read the path that
    placeholder binds to in the next; both spellings name one file, so every stage
    that pairs writes with reads has to compare them through this.
    """
    key = placeholder_key(token)
    return bindings.get(key, token) if key is not None else token


def _is_number(token: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", token))


def _flag_direction(flag: str) -> str | None:
    if flag in _WRITE_FLAGS:
        return "write"
    if flag in _READ_FLAGS:
        return "read"
    return None


def _takes_value(flag: str) -> bool:
    if flag.startswith("--"):
        return flag not in _BOOLEAN_FLAGS
    return flag in _VALUE_SHORT_FLAGS


def role(path: str, *, writing: bool) -> str:
    name = PurePosixPath(path).name.lower()
    suffix = _suffix(path)
    if suffix in _SCRIPT_EXTS:
        return ROLE_SCRIPT
    if suffix in _MODEL_EXTS:
        return ROLE_MODEL
    if writing:
        if suffix in _REPORT_EXTS or "report" in name or "metric" in name:
            return ROLE_REPORT
        return ROLE_OUTPUT
    if suffix in _RULE_EXTS or any(word in name for word in ("rule", "config", "schema")):
        return ROLE_RULES
    return ROLE_SOURCE


def _roles(paths: list[str], *, writing: bool) -> tuple[str, ...]:
    return tuple(sorted({role(path, writing=writing) for path in paths}))


def _split_inline(command: str) -> tuple[str, bool]:
    marker = re.search(r"<<-?\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?", command)
    if marker is None:
        return command, False
    return command[: marker.start()], True


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _unfold_newlines(text: str) -> str:
    """Turn an unquoted newline into `;` so every line is its own command.

    `shlex` treats a newline as plain whitespace, which silently welds a multi-line
    script into one command whose head is the first token: `rm -f a\\npython3 run.py`
    used to read as an `rm` whose positional paths include `run.py`. A
    backslash-newline is a line continuation and is dropped, so the same command
    reflowed at other columns keeps one shape.
    """
    out: list[str] = []
    quote = ""
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(text):
                out.append(text[index:index + 2])
                index += 2
                continue
            if char == quote:
                quote = ""
            out.append(char)
            index += 1
            continue
        if char == "\\":
            if text[index + 1:index + 2] == "\n":
                index += 2
                continue
            out.append(text[index:index + 2])
            index += 2
            continue
        if char in "'\"":
            quote = char
        elif char == "\n":
            out.append(";")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _simple_command(tokens: list[str]) -> Command:
    """Parse one simple command; its own head decides how positional paths read."""
    if not tokens:
        return Command("", (), (), (), (), "")
    name = _basename(tokens[0])
    head_is_script = _is_path(tokens[0]) and name not in _INTERPRETERS
    shape = [f"<script:{PurePosixPath(tokens[0]).stem}>" if head_is_script else name]
    read_paths: list[str] = []
    write_paths: list[str] = []
    moved: list[str] = []
    literals: list[tuple[str, str]] = []
    direction: str | None = None
    pending = False
    pending_flag: str | None = None
    script_seen = name not in _INTERPRETERS
    index = 1
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token in _WRITE_OPERATORS or token in _READ_OPERATORS:
            shape.append(token)
            direction = "write" if token in _WRITE_OPERATORS else "read"
            pending = False
            pending_flag = None
            continue
        if token.startswith("-") and token != "-":
            flag, sep, value = token.partition("=")
            if sep:
                shape.append(f"{flag}=<value>")
                if _flag_direction(flag) is not None:
                    (write_paths if _flag_direction(flag) == "write" else read_paths).append(value)
                pending = False
                pending_flag = None
                continue
            shape.append(flag)
            if flag in _INLINE_FLAGS and index < len(tokens):
                shape.append(INLINE_MARK)
                index += 1
                pending = False
                pending_flag = None
            elif flag in _MODULE_FLAGS and name in _INTERPRETERS and index < len(tokens):
                shape.append(tokens[index])
                index += 1
                pending = False
                pending_flag = None
            else:
                direction = _flag_direction(flag)
                pending = _takes_value(flag)
                pending_flag = flag if pending else None
            continue
        if not script_seen and _suffix(token) in _SCRIPT_EXTS:
            shape.append(f"<script:{PurePosixPath(token).stem}>")
            read_paths.append(token)
            script_seen = True
            direction = None
            pending = False
            pending_flag = None
            continue
        if _is_path(token):
            shape.append("<path>")
            if direction is None and name in _MOVE_COMMANDS:
                read_paths.append(token)
                moved.append(token)
            else:
                written = direction == "write" or (direction is None and name in _WRITE_COMMANDS)
                (write_paths if written else read_paths).append(token)
            direction = None
            pending = False
            pending_flag = None
            continue
        if _is_number(token):
            shape.append("<num>")
            direction = None
            pending = False
            pending_flag = None
            continue
        shape.append("<str>" if pending else token)
        literals.append((pending_flag or "", token))
        direction = None
        pending = False
        pending_flag = None
    if moved:
        read_paths.remove(moved[-1])
        write_paths.append(moved[-1])
    read_paths = [path for path in read_paths if path not in _DEVICE_PATHS]
    write_paths = [path for path in write_paths if path not in _DEVICE_PATHS]
    return Command(" ".join(shape), _roles(read_paths, writing=False),
                   _roles(write_paths, writing=True), tuple(read_paths),
                   tuple(write_paths), name, tuple(literals))


def _last_wins(paths: list[str]) -> list[str]:
    """Keep the final position of every path: the head command has the last word."""
    seen: dict[str, None] = {}
    for path in paths:
        seen.pop(path, None)
        seen[path] = None
    return list(seen)


def parse_command(command: str) -> Command:
    text, inline = _split_inline(command)
    tokens = _tokenize(_unfold_newlines(text))
    if not tokens:
        return Command(INLINE_MARK if inline else "", (), (), (), (), "")
    segments: list[list[str]] = [[]]
    separators: list[str] = []
    for token in tokens:
        if token in _OPERATORS:
            separators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)
    parsed = [_simple_command(segment) for segment in segments]
    shape: list[str] = []
    for index, part in enumerate(parsed):
        if index:
            shape.append(separators[index - 1])
        if part.shape:
            shape.append(part.shape)
    if inline:
        shape.extend(["<<", INLINE_MARK])
    read_paths = _last_wins([path for part in parsed for path in part.read_paths])
    write_paths = _last_wins([path for part in parsed for path in part.write_paths])
    return Command(" ".join(shape), _roles(read_paths, writing=False),
                   _roles(write_paths, writing=True), tuple(read_paths),
                   tuple(write_paths), parsed[0].name,
                   tuple(item for part in parsed for item in part.literals))


def probe_command(command: Command) -> bool:
    """A read-only inspection command: it never carries a product into the graph."""
    if command.name == "git":
        words = command.shape.split()
        return len(words) > 1 and words[1] in _READONLY_GIT and not command.write_paths
    return command.name in _PROBE_COMMANDS and not command.write_paths


def _shell_noise(step: Step, command: Command) -> str | None:
    if probe_command(command) and not step.writes:
        return "probe"
    if step.inline_code and not step.writes and not step.reads:
        return "probe"
    return None


def _tool_step(entry: dict[str, Any], trace_id: str, seq: int) -> tuple[Step, str | None] | None:
    tool = str(entry.get("tool") or "")
    args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
    result = entry.get("result") if isinstance(entry.get("result"), dict) else None
    text = str(result.get("text") or "") if result else ""
    success = result is not None and result.get("is_error") is not True
    rejected = bool(result) and result.get("is_error") is True and bool(_REFUSAL.search(text))
    raw_ref = str(entry.get("entry_id") or "")

    def build(kind: str, shape: str, reads: tuple[str, ...] = (), writes: tuple[str, ...] = (),
              read_paths: tuple[str, ...] = (), write_paths: tuple[str, ...] = ()) -> Step:
        return Step(seq, trace_id, kind, tool, str(args.get("command") or ""), shape, reads,
                    writes, read_paths, write_paths, success, rejected, raw_ref)

    if tool in _SHELL_TOOLS:
        command = parse_command(str(args.get("command") or ""))
        step = build(KIND_SHELL, command.shape, command.reads, command.writes,
                     command.read_paths, command.write_paths)
        return step, _shell_noise(step, command)
    if tool in _WRITE_TOOLS:
        target = str(args.get("path") or args.get("file_path") or "")
        if not target:
            return build(KIND_OTHER, f"<tool:{tool}>"), None
        return build(KIND_WRITE, f"{tool} <path>", (), _roles([target], writing=True),
                     (), (target,)), None
    if tool in _PROBE_TOOLS:
        target = str(args.get("path") or args.get("file_path") or "")
        paths = (target,) if target else ()
        return build(KIND_READ, f"{tool} <path>" if target else tool, (), (), (), paths), "probe"
    if tool in _IGNORED_TOOLS:
        return build(KIND_OTHER, f"<tool:{tool}>"), "plan"
    if tool in _AGENT_TOOLS:
        return build(KIND_LLM, f"<tool:{tool}>"), None
    # Any other tool call is a fixed invocation the graph could replay once the plan
    # declares the artifact its result lands in; without that declaration it stays out.
    # Its args still name the paths it consumes, and those paths are what put the call
    # on the session's dataflow instead of leaving it an opaque no-op.
    paths = tuple(dict.fromkeys(arg_paths(args)))
    return build(KIND_TOOL, f"<tool:{tool}>", _roles(list(paths), writing=False), (),
                 paths, ()), None


def arg_paths(value: Any) -> list[str]:
    """The path-like string values of a tool call's args, in declaration order.

    A tool call has no command line to tokenize, so the args are the only place a
    declared input path can appear; scanning them keeps a tool step's reads as
    discoverable as a shell step's.
    """
    if isinstance(value, str):
        return [value] if _is_path(value) else []
    if isinstance(value, dict):
        return [path for item in value.values() for path in arg_paths(item)]
    if isinstance(value, list):
        return [path for item in value for path in arg_paths(item)]
    return []


def _tool_call_step(entry: dict[str, Any], trace_id: str, seq: int) -> tuple[Step, str | None]:
    """A distilled fixed tool call: name, args, and the artifact its result lands in.

    Unlike a session's opaque tool call (a KIND_TOOL step with no declared artifact),
    a distilled tool step declares where the JSON result goes, so emit can turn it
    into a `do: {tool}` leaf the engine runs with no model in the loop.
    """
    name = str(entry.get("tool") or "")
    args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
    output = str(entry.get("output") or "")
    read_paths = tuple(dict.fromkeys(arg_paths(args)))
    write_paths = (output,) if output else ()
    return Step(seq, trace_id, KIND_TOOL, name, "", f"<tool:{name}>",
                _roles(list(read_paths), writing=False),
                _roles(list(write_paths), writing=True), read_paths, write_paths,
                True, False, str(entry.get("entry_id") or ""),
                arguments=dict(args)), None


def _llm_step(entry: dict[str, Any], trace_id: str, seq: int) -> tuple[Step, str | None]:
    """A distilled free-form agent step: prompt in, one declared artifact out.

    A session's `task`/`subagent` call is an opaque KIND_LLM with no declared artifact;
    a distilled llm step declares the exact inputs it consumes and the artifact it must
    materialise, so emit can turn it into a `do: {llm}` leaf node.
    """
    args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
    reads = tuple(str(path) for path in (args.get("reads") or ()))
    writes = tuple(str(path) for path in (args.get("writes") or ()))
    return Step(seq, trace_id, KIND_LLM, "llm", str(args.get("prompt") or ""), "<llm>",
                _roles(list(reads), writing=False), _roles(list(writes), writing=True),
                reads, writes, True, False, str(entry.get("entry_id") or ""),
                str(args.get("verify") or "")), None


def _human_step(entry: dict[str, Any], trace_id: str, seq: int) -> tuple[Step, str | None]:
    """A distilled human gate: a question in, one declared artifact out.

    Unlike a session's `user` message (a KIND_HUMAN step with nothing declared), this
    step says exactly what the person is asked and where their answer has to land, so
    emit can turn it into a `do: {human}` leaf the run parks on.
    """
    args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
    reads = tuple(str(path) for path in (args.get("reads") or ()))
    writes = tuple(str(path) for path in (args.get("writes") or ()))
    return Step(seq, trace_id, KIND_HUMAN, "human", str(args.get("question") or ""),
                "<human>", _roles(list(reads), writing=False),
                _roles(list(writes), writing=True), reads, writes, True, False,
                str(entry.get("entry_id") or "")), None


def _candidate(entry: dict[str, Any], trace_id: str,
               seq: int) -> tuple[Step, str | None] | None:
    if entry.get("kind") == "user":
        # A user turn carries no work and no artifact, but its text is what makes a
        # mid-chain handover visible ("我已经人工复核完导出了 <path>"): the plan may
        # not start at an artifact the person says they produced by hand.
        step = Step(seq, trace_id, KIND_HUMAN, "", str(entry.get("text") or ""), "",
                    (), (), (), (), True, False, str(entry.get("entry_id") or ""))
        return step, None
    if entry.get("kind") == "human":
        return _human_step(entry, trace_id, seq)
    if entry.get("kind") == "llm":
        return _llm_step(entry, trace_id, seq)
    if entry.get("kind") == "tool_call":
        return _tool_call_step(entry, trace_id, seq)
    if entry.get("kind") == "tool":
        return _tool_step(entry, trace_id, seq)
    return None


def capture_trace(text: str, *, source: str = "") -> Trace:
    document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError("trace must be a JSON object")
    if document.get("trace_version") != TRACE_VERSION:
        raise ValueError(f"unsupported trace version: {document.get('trace_version')!r}")
    entries = document.get("steps")
    if not isinstance(entries, list):
        raise ValueError("trace has no steps")
    trace_id = str(document.get("trace_id") or "trace")
    steps: list[Step] = []
    dropped: list[Dropped] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # An export numbers its own steps; keeping that number is what makes the
        # `<trace_id>:<seq>` an agent reads in the file the same one this module
        # reports back, even after the stages below drop probes from the list.
        declared = entry.get("seq")
        seq = declared if isinstance(declared, int) and not isinstance(declared, bool) else len(steps)
        candidate = _candidate(entry, trace_id, seq)
        if candidate is None:
            continue
        step, reason = candidate
        if reason is not None:
            dropped.append(Dropped(step.seq, step.kind, step.tool_name, reason, step.raw_ref))
            continue
        steps.append(step)
    session_id = document.get("session_id")
    session_name = document.get("session_name")
    return Trace(trace_id, None if session_id is None else str(session_id),
                 None if session_name is None else str(session_name), source,
                 tuple(steps), tuple(dropped))


def steps_payload(trace: Trace) -> dict[str, Any]:
    return {
        "trace_id": trace.trace_id,
        "session_id": trace.session_id,
        "session_name": trace.session_name,
        "source": trace.source,
        "step_count": len(trace.steps),
        "dropped_count": len(trace.dropped),
        "steps": [step.payload() for step in trace.steps],
        "dropped": [item.payload() for item in trace.dropped],
    }


def write_steps(directory: Path, trace: Trace) -> Path:
    target = directory / "steps.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(steps_payload(trace), indent=2, ensure_ascii=False) + "\n")
    return target


def render_report(trace: Trace) -> str:
    lines = [f"### trace `{trace.trace_id}`", ""]
    if trace.source:
        lines.extend([f"- 来源：`{trace.source}`", ""])
    if trace.session_name:
        lines.extend([f"- 会话：{trace.session_name}", ""])
    lines.extend([f"- 步骤：{len(trace.steps)} 条（丢弃 {len(trace.dropped)} 条）", "",
                  "| seq | kind | tool | 形状 | 读 | 写 | 结果 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"])
    for step in trace.steps:
        verdict = "rejected" if step.rejected else ("ok" if step.success else "failed")
        shape = step.command_shape.replace("|", "\\|") or "-"
        lines.append(f"| {step.seq} | {step.kind} | {step.tool_name or '-'} | `{shape}` | "
                     f"{','.join(step.reads) or '-'} | {','.join(step.writes) or '-'} | {verdict} |")
    if trace.dropped:
        lines.extend(["", "丢弃：" + "；".join(
            f"`{item.raw_ref or item.seq}` {item.tool_name or item.kind} ({item.reason})"
            for item in trace.dropped)])
    return "\n".join(lines) + "\n"
