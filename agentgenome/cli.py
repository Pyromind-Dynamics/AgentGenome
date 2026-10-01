"""Explicit local asset import, validation and publication."""
import argparse
import json
from pathlib import Path
from uuid import uuid4

from adapters import LocalShell
from core.ports import Ports
from .service import GenomeService


class LocalHost:
    def __init__(self, root: Path):
        self.root = root

    def prepare(self, run_id, package, params, path_parameters, cancel, emit):
        return Ports(shell=LocalShell()), {
            key: str(Path(value).resolve()) if key in path_parameters else value
            for key, value in params.items()
        }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import")
    imp.add_argument("source")
    for name in ("validate", "publish"):
        command = commands.add_parser(name)
        command.add_argument("asset_id")
        command.add_argument("version")
        if name == "validate":
            command.add_argument("--params", required=True, help="JSON object")
    args = parser.parse_args()
    service = GenomeService(args.home)
    if args.command == "import":
        result = service.import_asset(args.source)
    elif args.command == "publish":
        result = service.publish(args.asset_id, args.version)
    else:
        service.claim_execution_owner()
        try:
            service.recover()
            service.check_capabilities(args.asset_id, args.version, set())
            state = service.submit("local-validation", uuid4().hex, args.asset_id, args.version,
                                   json.loads(args.params), allow_draft=True)
            result = service.execute("local-validation", state["id"], LocalHost(service.root), lambda event: None)
        finally:
            service.release_execution_owner()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.command == "validate" and result["status"] != "succeeded":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
