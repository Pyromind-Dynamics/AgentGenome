"""Artifact access belongs to the execution host, not the engine filesystem."""

from pathlib import Path
from typing import Any, Protocol

from .ledger import file_ref


class ArtifactPort(Protocol):
    def path(self, name: str) -> str: ...
    def describe(self, name: str) -> dict[str, Any]: ...
    def read_text(self, name: str) -> str: ...


class LocalArtifacts:
    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, name: str) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    def describe(self, name: str) -> dict[str, Any]:
        return file_ref(self.root, self.root / name)

    def read_text(self, name: str) -> str:
        return (self.root / name).read_text(encoding="utf-8")
