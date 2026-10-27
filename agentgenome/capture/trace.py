"""Stage A: normalize an exported Pi session into alignable steps.

The plugin exports raw entries; this module is the only place that knows how a
Pi tool call becomes a graph-worthy step. Everything here is mechanical: no
model calls, no filesystem access, so a capture can be replayed in tests.
"""
from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, replace
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
    "-o", "--output", "--out",
    "--output-file", "--dest", "--destination", "--save", "--save-to", "--target",
}
_READ_FLAGS = {
    "-i", "--input", "--in", "--input-dir", "--input_dir", "--outdir",
    "--output-dir", "--output_dir", "--source", "--src",
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

_REPORTED_WRITE = re.compile(
    r"(?i)\b(?:wrote|written|saved|output|outputted)\b|"
    r"(?:已写入|写入|输出到|产物(?:为|在|保存在|写到|[：:]))"
)
_REPORTED_ARROW = re.compile(
    r"(?:->|=>)[ \t]*(?P<path>[^\s\"'`<>，。；、]+)"
)
_REPORTED_DIRECT = re.compile(
    r"(?i)(?:"
    r"\b(?:written|saved|output|outputted)\b[ \t]*(?:to|into|:)"
    r"|(?:已写入|写入|输出到|产物(?:为|在|保存在|写到|[：:]))"
    r")[ \t:：]*(?P<path>[^\s\"'`<>，。；、]+)"
)

# A `{name}` token is how the agent declares a chain parameter instead of a literal
# path, so the same command can be replayed with another input.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
_COMMAND_SUBSTITUTION = "__agentgenome_command_substitution__"
_COMMAND_SUBSTITUTION_SHAPE = "<command-substitution>"


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
    # A step whose effect matters even though no later step consumes its artifact
    # (creating a Label Studio project, submitting a platform job) says so when it is
    # distilled: the reverse slice keeps it instead of dropping it as auxiliary.
    keep: bool = False
    # Successful tools sometimes report a product they created internally. This is
    # separate from write_paths because the command line cannot be replayed with a
    # different output until that output becomes an explicit argument.
    reported_writes: tuple[str, ...] = ()
    # Positional paths whose direction the command line does not settle. They are
    # reads by default; the report names them so a chain that loses a product says
    # why instead of failing silently.
    unresolved: tuple[str, ...] = ()
    agent_context: Mapping[str, Any] | None = None

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
            "keep": self.keep,
            "reported_writes": list(self.reported_writes),
            "unresolved": list(self.unresolved),
            "signature": [self.kind, self.command_shape, list(self.reads), list(self.writes)],
        }
        if self.arguments is not None:
            data["arguments"] = dict(self.arguments)
        if self.agent_context is not None:
            data["agent_context"] = dict(self.agent_context)
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
    export_dropped_steps: int = 0
    # Files observed before an in-place editor change are revised inputs, not
    # newly generated intermediates. Keep this evidence even when reads are pruned.
    revised_inputs: tuple[str, ...] = ()


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
    # Positional paths of an interpreter call (`python3 script.py in.csv out.csv`).
    # The command line says nothing about which of them the script writes, so these
    # are read by default and reported here; a stage that freezes the command asks
    # the caller to declare its output instead of guessing.
    unresolved: tuple[str, ...] = ()
    slice_paths: tuple[str, ...] = ()
    slice_sources: tuple[tuple[str, tuple[str, ...]], ...] = ()


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


def _command_substitution_end(text: str, start: int) -> int | None:
    """The index after a `$(...)` that starts at `start`, or None when unclosed."""
    depth = 1
    quote = ""
    index = start + 2
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(text):
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _shield_command_substitutions(text: str) -> str:
    """Replace `$()` bodies before path tokenization.

    A command substitution may contain an arbitrary shell program. Its arguments
    are not inputs to the outer command, and the closing `)` is not part of a path:
    tokenizing `$(dirname out/sft.jsonl)` used to invent `out/sft.jsonl)`.
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
            if quote == '"' and char == "$" and text[index + 1:index + 2] == "(":
                end = _command_substitution_end(text, index)
                if end is not None:
                    out.append(_COMMAND_SUBSTITUTION)
                    index = end
                    continue
            out.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < len(text):
            out.append(text[index:index + 2])
            index += 2
            continue
        if char in "'\"":
            quote = char
            out.append(char)
            index += 1
            continue
        if char == "$" and text[index + 1:index + 2] == "(":
            end = _command_substitution_end(text, index)
            if end is not None:
                out.append(_COMMAND_SUBSTITUTION)
                index = end
                continue
        out.append(char)
        index += 1
    return "".join(out)


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
    unresolved: list[str] = []
    direction: str | None = None
    pending = False
    pending_flag: str | None = None
    script_seen = name not in _INTERPRETERS
    index = 1
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == _COMMAND_SUBSTITUTION:
            shape.append(_COMMAND_SUBSTITUTION_SHAPE)
            literals.append((pending_flag or "", _COMMAND_SUBSTITUTION_SHAPE))
            direction = None
            pending = False
            pending_flag = None
            continue
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
            if name in _INTERPRETERS and flag in _INLINE_FLAGS and index < len(tokens):
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
                if direction is None and not written and name in _INTERPRETERS:
                    # The interpreter decides what its positional paths are, and the
                    # command line does not. Keep the common reading (an input) but
                    # report the ambiguity, so a stage that freezes the command can
                    # ask for the output instead of guessing wrong.
                    unresolved.append(token)
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
                   tuple(write_paths), name, tuple(literals), tuple(unresolved))


def _last_wins(paths: list[str]) -> list[str]:
    """Keep the final position of every path: the head command has the last word."""
    seen: dict[str, None] = {}
    for path in paths:
        seen.pop(path, None)
        seen[path] = None
    return list(seen)


def parse_command(command: str) -> Command:
    text, inline = _split_inline(command)
    tokens = _tokenize(_unfold_newlines(_shield_command_substitutions(text)))
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
    # A readback after a producer is not an external input to the whole call, but
    # only when the shell guarantees the producer ran to success before the reader.
    # `&&` chains carry that guarantee. `||` runs its right side only after the
    # left FAILED, `|` starts both sides at once (the data flows through stdout,
    # not through a shared file), and `;`/`&` order execution without promising
    # success — so writes on those connectors prove nothing about what the next
    # segment reads. A pipeline's status is its last member's, so `&&` after a
    # pipeline trusts only the tail's writes. Reads and writes within the SAME
    # simple command remain visible (in-place IO).
    # After `||`, keep reads for the rest of that conditional chain: an adjacent
    # `&&` does not prove its producer was reached (A || B && C can skip B).
    read_paths: list[str] = []
    guaranteed: set[str] = set()
    tail_writes: set[str] = set()
    branched = False
    slice_paths: list[str] = []
    slice_sources: list[tuple[str, tuple[str, ...]]] = []
    stream_reads: list[str] = []
    sliced_stream = False
    for index, part in enumerate(parsed):
        if index:
            separator = separators[index - 1]
            if separator == "||":
                branched = True
            elif separator in {";", "&"}:
                branched = False
            if separator == "&&" and not branched:
                guaranteed |= tail_writes
            elif separator != "|":
                guaranteed = set()
        if index and separators[index - 1] != "|":
            sliced_stream = False
            stream_reads = []
        stream_reads.extend(path for path in part.read_paths
                            if role(path, writing=False) != ROLE_SCRIPT)
        if part.name != "cd":
            read_paths.extend(path for path in part.read_paths
                              if str(PurePosixPath(path)) not in guaranteed)
        tail_writes = {str(PurePosixPath(path)) for path in part.write_paths}
        sliced_stream = sliced_stream or part.name in {"head", "tail", "shuf", "split"} or (
            part.name == "sed" and "-n" in segments[index])
        if sliced_stream:
            slice_paths.extend(part.write_paths)
            slice_sources.extend((path, tuple(dict.fromkeys(stream_reads))) for path in part.write_paths)
    read_paths = _last_wins(read_paths)
    write_paths = _last_wins([path for part in parsed for path in part.write_paths])
    return Command(" ".join(shape), _roles(read_paths, writing=False),
                   _roles(write_paths, writing=True), tuple(read_paths),
                   tuple(write_paths), parsed[0].name,
                   tuple(item for part in parsed for item in part.literals),
                   tuple(_last_wins([item for part in parsed for item in part.unresolved])),
                   tuple(_last_wins(slice_paths)), tuple(slice_sources))


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


def _declared_writes(entry: dict[str, Any]) -> tuple[str, ...]:
    """Products a distilled plan declared for a command the parser reads as inputs.

    A script that takes its product positionally (`script.py in out`) looks like a
    read to the parser, and one that writes its products itself names nothing at all.
    The plan's declaration is what keeps such a product on the step that made it.
    """
    value = entry.get("declared_writes")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _declared_paths(write_paths: tuple[str, ...], declared: tuple[str, ...],
                    command: str) -> tuple[str, ...]:
    """A step's products: what the command names, plus the declarations it carries."""
    written = [*write_paths]
    for path in declared:
        if path in command and path not in written:
            written.append(path)
    return tuple(written)


def _reported_path(token: str) -> bool:
    """Whether a tool result names a product rather than prose.

    Tool output quotes source and messages, so `写入…` inside a `grep` hit looks
    like a write. Prose reaches this check with sentence slashes but no file
    shape, so a value that is not rooted needs a real suffix to count.
    """
    if not _is_path(token):
        return False
    if token.startswith(("/", "./", "../", "~/")):
        return True
    return bool(_suffix(token))


def _reported_writes(text: str, *, success: bool) -> tuple[str, ...]:
    """Products a successful tool reports in its result text.

    Tool output is evidence, not a replayable interface. Keep only path-shaped,
    non-device values so plan extraction can see the producer without mistaking
    progress prose or URL output for a file. A successful script can print a large
    JSON payload before its final arrow, so parse each line instead of imposing a
    fixed text-window limit between the verb and the destination.
    """
    if not success:
        return ()
    found: list[str] = []
    for line in text.splitlines():
        keyword = _REPORTED_WRITE.search(line)
        if keyword is None:
            continue
        tail = line[keyword.start():]
        # Arrows are unambiguous, including after long `wrote [...] rows` output.
        matches = list(_REPORTED_ARROW.finditer(tail)) or list(
            _REPORTED_DIRECT.finditer(tail)
        )
        for match in matches:
            path = match.group("path").rstrip(").,;:")
            if not path or path in _DEVICE_PATHS or not _reported_path(path):
                continue
            if path.endswith("/"):
                continue
            if path not in found:
                found.append(path)
    return tuple(found)


def _tool_step(entry: dict[str, Any], trace_id: str, seq: int) -> tuple[Step, str | None] | None:
    tool = str(entry.get("tool") or "")
    args = entry.get("args") if isinstance(entry.get("args"), dict) else {}
    result = entry.get("result") if isinstance(entry.get("result"), dict) else None
    text = str(result.get("text") or "") if result else ""
    success = result is not None and result.get("is_error") is not True
    rejected = bool(result) and result.get("is_error") is True and bool(_REFUSAL.search(text))
    raw_ref = str(entry.get("entry_id") or "")
    declared = _declared_writes(entry)
    reported = _reported_writes(text, success=success)

    def build(kind: str, shape: str, reads: tuple[str, ...] = (), writes: tuple[str, ...] = (),
              read_paths: tuple[str, ...] = (), write_paths: tuple[str, ...] = (),
              reported_writes: tuple[str, ...] = (),
              unresolved: tuple[str, ...] = ()) -> Step:
        # Session tool calls need their arguments to become replayable tool nodes.
        arguments = dict(args) if kind == KIND_TOOL else None
        return Step(seq, trace_id, kind, tool, str(args.get("command") or ""), shape, reads,
                    writes, read_paths, write_paths, success, rejected, raw_ref,
                    verify=str(entry.get("verify") or ""),
                    arguments=arguments, reported_writes=reported_writes,
                    unresolved=unresolved,
                    agent_context=entry.get("agent_context")
                    if isinstance(entry.get("agent_context"), dict) else None)

    if tool in _SHELL_TOOLS:
        text_command = str(args.get("command") or "")
        command = parse_command(text_command)
        if probe_command(command):
            reported = ()
        written = _declared_paths(command.write_paths, declared, text_command)
        reported = tuple(path for path in reported if path not in written)
        # Only the products the parser missed get reclassified; a path the command
        # already reads and writes stays on both sides so emit can reject it.
        reclassified = tuple(
            path
            for path in (*written, *reported)
            if path not in command.write_paths
        )
        read_paths = tuple(path for path in command.read_paths if path not in reclassified)
        # A path that ended up declared as a product is settled, so it is not reported
        # as ambiguous; what remains is genuinely undecided by the command line.
        unresolved = tuple(token for token in command.unresolved
                           if token not in reclassified)
        step = build(KIND_SHELL, command.shape, _roles(read_paths, writing=False),
                     _roles((*written, *reported), writing=True), read_paths,
                     written, reported_writes=reported, unresolved=unresolved)
        return step, _shell_noise(step, command)
    if tool in _WRITE_TOOLS:
        target = str(args.get("path") or args.get("file_path") or "")
        if not target:
            return build(KIND_OTHER, f"<tool:{tool}>"), None
        return build(KIND_WRITE, f"{tool} <path>", (), _roles([target], writing=True),
                     (), (target,), reported_writes=()), None
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


def agent_write_inputs(step: Step) -> tuple[str, ...]:
    """Observed inputs for an agent task materialized through write, never edit.

    Old traces without task context remain ambiguous. Writer bytes alone are not
    evidence of a replayable semantic task.
    """
    context = step.agent_context or {}
    if (step.kind != KIND_WRITE or step.tool_name != "write"
            or context.get("complete") is not True
            or not isinstance(context.get("instruction"), str)
            or not context["instruction"].strip()
            or not isinstance(context.get("intent"), str)
            or not context["intent"].strip()):
        return ()
    inputs = context.get("inputs")
    if not isinstance(inputs, list):
        return ()
    return tuple(path for path in inputs if isinstance(path, str) and _is_path(path)
                 and role(path, writing=False) != ROLE_SCRIPT
                 and path not in step.write_paths)


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
    export_dropped = document.get("dropped_steps", 0)
    if (not isinstance(export_dropped, int) or isinstance(export_dropped, bool)
            or export_dropped < 0):
        raise ValueError("trace dropped_steps must be a non-negative integer")
    steps: list[Step] = []
    dropped: list[Dropped] = []
    observed: set[str] = set()
    written: set[str] = set()
    revised: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        clipped_fields = entry.get("clipped_fields", [])
        if not isinstance(clipped_fields, list) or any(
                not isinstance(field, str) for field in clipped_fields):
            raise ValueError(f"trace {trace_id} has invalid clipped_fields")
        if any(field == "args" or field.startswith("args.") for field in clipped_fields):
            raise ValueError(
                f"轨迹 {trace_id} 第 {entry.get('seq', '?')} 步的命令/工具参数被裁剪"
                "（clipped_fields）；请重新导出完整会话")
        # An export numbers its own steps; keeping that number is what makes the
        # `<trace_id>:<seq>` an agent reads in the file the same one this module
        # reports back, even after the stages below drop probes from the list.
        declared = entry.get("seq")
        seq = declared if isinstance(declared, int) and not isinstance(declared, bool) else len(steps)
        candidate = _candidate(entry, trace_id, seq)
        if candidate is None:
            continue
        step, reason = candidate
        if step.success and not step.rejected:
            if step.kind == KIND_READ:
                observed.update(str(PurePosixPath(path)) for path in step.write_paths)
            elif step.kind == KIND_SHELL and probe_command(parse_command(step.command)):
                observed.update(str(PurePosixPath(path)) for path in step.read_paths)
            elif step.kind != KIND_HUMAN:
                products = {str(PurePosixPath(path))
                            for path in (*step.write_paths, *step.reported_writes)}
                editing = step.kind == KIND_WRITE and step.tool_name in {"edit", "multiedit"}
                for path in products:
                    if editing and path in observed and path not in written:
                        revised.add(path)
                    elif not editing:
                        revised.discard(path)
                written.update(products)
        if entry.get("keep") is True:
            step = replace(step, keep=True)
        if reason is not None:
            dropped.append(Dropped(step.seq, step.kind, step.tool_name, reason, step.raw_ref))
            continue
        steps.append(step)
    session_id = document.get("session_id")
    session_name = document.get("session_name")
    return Trace(trace_id, None if session_id is None else str(session_id),
                 None if session_name is None else str(session_name), source,
                 tuple(steps), tuple(dropped), export_dropped, tuple(sorted(revised)))


def steps_payload(trace: Trace) -> dict[str, Any]:
    return {
        "trace_id": trace.trace_id,
        "session_id": trace.session_id,
        "session_name": trace.session_name,
        "source": trace.source,
        "step_count": len(trace.steps),
        "dropped_count": len(trace.dropped),
        "export_dropped_steps": trace.export_dropped_steps,
        "revised_inputs": list(trace.revised_inputs),
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
    lines.extend([f"- 步骤：{len(trace.steps)} 条（规范化过滤 {len(trace.dropped)} 条，"
                  f"导出截断 {trace.export_dropped_steps} 条）", "",
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
    ambiguous = [step for step in trace.steps if step.unresolved]
    if ambiguous:
        # The parser cannot tell an input from a product here; naming them keeps a
        # chain that loses a product diagnosable instead of silently short.
        lines.extend(["", "方向不明（按输入处理，产出要用 output 显式声明）：" + "；".join(
            f"`{step.seq}` " + "、".join(f"`{token}`" for token in step.unresolved)
            for step in ambiguous)])
    return "\n".join(lines) + "\n"
