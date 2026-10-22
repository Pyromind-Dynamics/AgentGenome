"""Build the agentgenome wheel without setuptools.

setuptools' bdist_wheel cleans its own staging tree, and this environment blocks
bulk deletes, so the wheel is assembled directly: a wheel is a zip with a
dist-info directory, and RECORD carries the digests.

The distribution ships four packages plus one top-level module, mirroring
`[tool.setuptools.packages.find] include` and `py-modules` in pyproject.toml.
"""
from __future__ import annotations

import base64
import hashlib
import zipfile
from pathlib import Path

ROOT = Path("/Users/zhangpeng/PycharmProjects/AgentGenome")
VERSION = (ROOT / "pyproject.toml").read_text(encoding="utf-8").split('version = "', 1)[1].split('"', 1)[0]
DIST_INFO = f"agentgenome-{VERSION}.dist-info"
TARGET = ROOT / "dist" / f"agentgenome-{VERSION}-py3-none-any.whl"

PACKAGES = ("agentgenome", "builder", "core", "adapters")
MODULES = ("cli.py",)

METADATA = f"""Metadata-Version: 2.1
Name: agentgenome
Version: {VERSION}
Summary: Verification-pinned executable task graphs for LLM agents
Requires-Python: >=3.11
Requires-Dist: PyYAML
"""

WHEEL = """Wheel-Version: 1.0
Generator: agentgenome-manual
Root-Is-Purelib: true
Tag: py3-none-any
"""

ENTRY_POINTS = """[console_scripts]
agentgenome = cli:main
agentgenome-assets = agentgenome.cli:main
"""


def digest(data: bytes) -> str:
    raw = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
    return f"sha256={raw.decode()}"


def sources() -> list[Path]:
    found: list[Path] = []
    for name in PACKAGES:
        found.extend(
            path for path in (ROOT / name).rglob("*.py")
            if "__pycache__" not in path.parts
        )
    found.extend(ROOT / name for name in MODULES if (ROOT / name).is_file())
    return sorted(found)


def main() -> None:
    files = sources()
    if not files:
        raise SystemExit("no sources found")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    records: list[str] = []
    with zipfile.ZipFile(TARGET, "w", zipfile.ZIP_DEFLATED) as wheel:
        for path in files:
            arc = path.relative_to(ROOT).as_posix()
            data = path.read_bytes()
            wheel.writestr(arc, data)
            records.append(f"{arc},{digest(data)},{len(data)}")
        extras = (
            ("METADATA", METADATA),
            ("WHEEL", WHEEL),
            ("top_level.txt", "\n".join(PACKAGES) + "\n"),
            ("entry_points.txt", ENTRY_POINTS),
        )
        for name, text in extras:
            arc = f"{DIST_INFO}/{name}"
            data = text.encode()
            wheel.writestr(arc, data)
            records.append(f"{arc},{digest(data)},{len(data)}")
        records.append(f"{DIST_INFO}/RECORD,,")
        wheel.writestr(f"{DIST_INFO}/RECORD", "\n".join(records) + "\n")
    print(f"built {TARGET} ({len(files)} modules, {TARGET.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
