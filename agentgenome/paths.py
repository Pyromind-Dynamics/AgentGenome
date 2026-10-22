"""One canonical spelling per file.

A capture meets four vocabularies for the same file:

``host``      the server's own disk          ``/Users/…/conversations/<id>/…``
``mount``     the sandbox mount of that disk ``/target-workspace/.pyromind-agent/<id>/…``
``workspace`` a path relative to it           ``public_data/…``, ``.agents/…``, ``scripts/…``
``storage``   the user's Storage             ``storage/datasets/…``, ``datasets/…``

The mount spelling is a *transport* detail: it is what a sandbox prints, never
what a plan should remember. Comparing two spellings by suffix ("does one end
with the other") guesses at identity and quietly pairs the wrong files, so every
comparison goes through :func:`canonical` instead: strip the mount once, decide
the namespace, then compare exactly.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

WORKSPACE = "workspace"
STORAGE = "storage"
HOST = "host"

# Mounts a sandbox prints for the same directory the host calls `conversations/<id>`.
# The conversation's own directory is `<mount>/.pyromind-agent/<id>`, and that whole
# prefix is transport: what remains is the path a plan should have written.
_MOUNT = re.compile(
    r"^(?:/)?(?:target-workspace|workspace)/(?:\.pyromind-agent/[^/]+/)?"
)

# A Storage path is spelled two ways: the sandbox alias `storage/datasets/…` and
# the object path `datasets/…`. Both name the same object.
_STORAGE_ALIAS = "storage"
_OBJECT_ROOTS = ("datasets",)

# Relative roots that live inside the workspace.
_WORKSPACE_ROOTS = ("public_data", ".agents", "knowledge", "scripts", "out", "data")


def _strip_mount(value: str) -> tuple[str, bool]:
    match = _MOUNT.match(value)
    if match is None:
        return value, False
    return value[match.end():], True


def canonical(value: str) -> tuple[str, str]:
    """The (namespace, path) a spelling names.

    The mount is dropped before anything else, so a sandbox path and the
    workspace-relative path of the same file compare equal.
    """
    text = str(value).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text, _ = _strip_mount(text)
    if text.startswith("/"):
        text = text[1:]
    if not text:
        return WORKSPACE, ""
    head, _, rest = text.partition("/")
    if head == _STORAGE_ALIAS:
        return STORAGE, rest or head
    if head in _OBJECT_ROOTS:
        return STORAGE, text
    if head in _WORKSPACE_ROOTS or not rest:
        return WORKSPACE, text
    # Anything else that came in absolute was a host path we cannot re-root.
    return (WORKSPACE if not value.startswith("/") else HOST), text


def same(left: str, right: str) -> bool:
    return canonical(left) == canonical(right)


def names(value: str, namespace: str) -> bool:
    return canonical(value)[0] == namespace


def mount_spelling(value: str) -> bool:
    """Whether a value is a sandbox mount path.

    A plan may not remember one: the next replay runs in another mount, and the
    platform never sees it.
    """
    text = str(value).strip().replace("\\", "/")
    return _MOUNT.match(text) is not None


def storage_spelling(value: str) -> bool:
    """Whether a value uses the sandbox alias `/storage/…` the platform rejects."""
    return str(value).strip().replace("\\", "/").startswith("/storage/")


def display(value: str) -> str:
    """The canonical spelling to show a user: relative, no mount, no leading slash."""
    namespace, path = canonical(value)
    if namespace == STORAGE:
        return f"{_STORAGE_ALIAS}/{path}" if not path.startswith(_STORAGE_ALIAS) else path
    return path


def suffix(path: str) -> str:
    return PurePosixPath(path).suffix
