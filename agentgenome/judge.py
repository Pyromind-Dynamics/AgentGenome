"""Standalone verification client: copy into a task package, no SDK dependency."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from uuid import uuid4

MAX_BYTES = 256 * 1024


def read_small(path: Path):
    with path.open('rb') as file:
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('judge input exceeds 256 KiB')
    return data.decode('utf-8')


def evaluate(prompt: Path, evidence: Path, directory: Path, timeout: float = 120):
    request_id = uuid4().hex
    body = json.dumps({'request_id': request_id, 'criterion': read_small(prompt),
                       'evidence': read_small(evidence)}, ensure_ascii=False)
    if len(body.encode()) > MAX_BYTES:
        raise ValueError('judge request exceeds 256 KiB')
    directory.mkdir(parents=True, exist_ok=True)
    pending = directory / 'request.tmp'
    pending.write_text(body, encoding='utf-8')
    pending.replace(directory / 'request.json')
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (directory / 'response.ready').exists():
            response = json.loads(read_small(directory / 'response.json'))
            if response.get('request_id') != request_id:
                raise ValueError('judge response does not match request')
            if response.get('error'):
                raise ValueError(response['error'])
            verdict = response.get('verdict')
            if (not isinstance(verdict, dict) or type(verdict.get('passed')) is not bool
                    or not isinstance(verdict.get('reason'), str) or not verdict['reason'].strip()):
                raise ValueError('invalid judge verdict')
            return verdict
        time.sleep(0.05)
    raise TimeoutError('judge timed out')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('prompt', type=Path)
    parser.add_argument('evidence', type=Path)
    args = parser.parse_args()
    try:
        directory = Path(os.environ['AGENTGENOME_JUDGE_DIR'])
        verdict = evaluate(args.prompt, args.evidence, directory)
    except Exception as error:
        print(json.dumps({'passed': False, 'reason': str(error)}))
        return 2
    print(json.dumps(verdict, ensure_ascii=False))
    return 0 if verdict['passed'] is True else 1


if __name__ == '__main__':
    raise SystemExit(main())
