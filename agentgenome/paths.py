"""One canonical spelling per file.

A capture meets four vocabularies for the same file:

``host``      the server's own disk          ``/Users/…/conversations/<id>/…``
``mount``     the sandbox mount of that disk ``/target-workspace/.pyromind-agent/<id>/…``
``workspace`` a path relative to it           ``public_data/…``, ``.agents/…``, ``scripts/…``
``storage``   the user's Storage             ``/datasets/…`` (object path)

The mount spelling is a *transport* detail: it is what a sandbox prints, never
what a plan should remember. Comparing two spellings by suffix ("does one end
with the other") guesses at identity and quietly pairs the wrong files, so every
comparison goes through :func:`canonical` instead: strip the mount once, decide
the namespace, then compare exactly.

A plan names Storage by its **object path** (`/datasets/…`): the platform reads
it directly, and the sandbox reaches it by prepending its mount. The workspace
symlink `storage/…` that older captures used was removed from the SDK, so
:func:`canonical` still reads it for those captures while nothing writes it.
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

# Storage used to be reachable through a workspace symlink spelled `storage/…`.
# The SDK removed it, so this name only survives here to read old captures: an
# absolute `/storage/…` is a directory that really sits at the Storage root.
_LEGACY_STORAGE_ALIAS = "storage"
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
    absolute = text.startswith("/")
    if absolute:
        text = text[1:]
    if not text:
        return WORKSPACE, ""
    head, _, rest = text.partition("/")
    if head == _LEGACY_STORAGE_ALIAS:
        # Relative `storage/x` was the removed alias for Storage `x`; absolute
        # `/storage/x` names a directory that really sits at the Storage root.
        return (STORAGE, text) if absolute else (STORAGE, rest or head)
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

    A plan may not remember one: the platform addresses Storage by its object
    path, and a mount baked into a plan is a deployment detail leaking through.
    """
    text = str(value).strip().replace("\\", "/")
    return _MOUNT.match(text) is not None


def display(value: str) -> str:
    """The spelling a plan remembers: workspace-relative, or a Storage object path.

    Storage is shown as `/datasets/…` because that is what the platform reads and
    what the sandbox reaches by prepending its mount; the removed `storage/…`
    alias is normalised away.
    """
    namespace, path = canonical(value)
    if namespace == STORAGE:
        return f"/{path}"
    return path


def portable(value: str) -> str:
    """The same file without a sandbox mount prefix, when one is present.

    A trace records what the sandbox printed; a plan must remember the spelling
    that outlives that sandbox. The mount root itself is left alone — rewriting it
    to an empty string would turn `cd <mount>` into `cd`.
    """
    text = str(value).strip().replace("\\", "/")
    stripped, mounted = _strip_mount(text)
    if not mounted or not stripped:
        return value
    return display(stripped)


def suffix(path: str) -> str:
    return PurePosixPath(path).suffix
