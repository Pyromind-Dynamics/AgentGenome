"""Frozen, small regression fixtures; every case runs through the execution host."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
from dataclasses import replace
from pathlib import Path, PurePosixPath
from uuid import uuid4

from builder import compile, package_pin
from core.api import run, handshake


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def validate_cases(cases):
    if not isinstance(cases, list) or len(cases) > 30 or len(encoded(cases).encode()) > 1024 * 1024:
        raise ValueError('regression cases must be a small list (at most 30 / 1 MiB)')
    names = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'id', 'files', 'params', 'assertions'}:
            raise ValueError('case requires id, files, params and assertions')
        if not isinstance(case['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', case['id']) or case['id'] in names:
            raise ValueError('case IDs must be unique simple names')
        names.add(case['id'])
        if not isinstance(case['files'], dict) or not isinstance(case['params'], dict):
            raise ValueError('invalid fixture files or parameters')
        for name, content in case['files'].items():
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or str(path) != name or not isinstance(content, str) or len(content.encode()) > 262144:
                raise ValueError('fixture must be a small text file with a relative path')
        for value in case['params'].values():
            if isinstance(value, str) and value.startswith('fixture:') and value[8:] not in case['files']:
                raise ValueError('unknown fixture reference')
        if not isinstance(case['assertions'], list) or not case['assertions']:
            raise ValueError('each case requires expected output assertions')
        for assertion in case['assertions']:
            if not isinstance(assertion, dict) or set(assertion) != {'node', 'output', 'kind', 'expected'} or assertion['kind'] not in {'json', 'csv', 'text'}:
                raise ValueError('assertion requires node, output, kind (json/csv/text), expected')
    return cases


class RegressionFailure(ValueError):
    pass


class Regressions:
    def __init__(self, service):
        self.service = service
        with service._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS regression_suites (asset_id TEXT, version TEXT, cases TEXT NOT NULL, pin TEXT NOT NULL, PRIMARY KEY(asset_id,version))')

    def suite(self, asset_id, version):
        with self.service._db() as db:
            row = db.execute('SELECT * FROM regression_suites WHERE asset_id=? AND version=?', (asset_id, version)).fetchone()
        if row is None:
            return []
        cases = json.loads(row['cases'])
        if digest(cases) != row['pin']:
            raise ValueError('frozen regression suite changed')
        return cases

    def freeze_baseline(self, asset_id, version, cases):
        with self.service._lock, self.service._db() as db:
            row = db.execute('SELECT pin FROM regression_suites WHERE asset_id=? AND version=?', (asset_id, version)).fetchone()
            if row and row['pin'] != digest(cases):
                raise ValueError('another validated baseline suite already exists')
            db.execute('INSERT OR IGNORE INTO regression_suites VALUES(?,?,?,?)', (asset_id, version, encoded(cases), digest(cases)))

    def execute_case(self, package, case, host, cancel, emit, check_cancel, evidence, original=None, agent_task=None):
        check_cancel()
        graph = compile(package)
        manifest = json.loads((package / 'manifest.json').read_text())
        params = self.service.validate_params(manifest, case['params'])
        # Fixture bytes are copied into this case only, never read from business data.
        staged = evidence / 'package'
        shutil.copytree(package, staged)
        fixture_root = staged / '.regression-inputs'
        if fixture_root.exists():
            raise ValueError('reserved regression input directory exists in asset')
        for name, content in case['files'].items():
            target = fixture_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        fixtures = {k: v[8:] for k, v in params.items() if isinstance(v, str) and v.startswith('fixture:')}
        plain = {k: v for k, v in params.items() if k not in fixtures}
        path_params = [k for k, spec in manifest['parameters'].items() if spec['type'] == 'path' and k in plain]
        ports, resolved = host.prepare(uuid4().hex, staged, plain, path_params, cancel, emit,
                                       **({'verification_package': original} if original else {}))
        resolved.update({k: str(ports.cwd / '.regression-inputs' / name) for k, name in fixtures.items()})
        ports = replace(ports, check_cancel=check_cancel, on_event=emit, agent_task=agent_task)
        if handshake(graph, ports):
            raise RegressionFailure('host lacks capabilities required by regression graph')
        outcome = run(graph, resolved, ports, evidence)
        if outcome.status != 'succeeded':
            raise RegressionFailure(f"case {case['id']}: graph execution failed")
        nodes = json.loads((evidence / 'checkpoint.json').read_text())['nodes']
        for assertion in case['assertions']:
            try:
                artifact = nodes[assertion['node']]['outputs'][assertion['output']]
                text = ports.artifacts.read_text(artifact['path'])
                kind, expected = assertion['kind'], assertion['expected']
                if kind == 'json':
                    value = json.loads(text)
                    matched = (all(value.get(k) == v for k, v in expected.items())
                               if isinstance(value, dict) and isinstance(expected, dict) else value == expected)
                elif kind == 'csv':
                    matched = list(csv.reader(io.StringIO(text))) == expected
                else:
                    matched = text == expected
                if not matched:
                    raise RegressionFailure(f"case {case['id']}: {assertion['node']}.{assertion['output']} does not match expected {kind}")
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RegressionFailure(f"case {case['id']}: invalid or missing output") from exc
        check_cancel()

    def validate(self, revision, host, cancel, emit, check_cancel, evidence):
        promotion = json.loads(revision['promotion'])
        parent = self.service.status(revision['scope'], revision['parent'])
        version = promotion.get('baseline_version', parent['version'])
        pin = promotion.get('baseline_pin', parent['pin'])
        original = self.service.root / 'assets' / parent['asset_id'] / version
        if package_pin(original) != pin:
            raise ValueError('original package changed')
        old = self.suite(parent['asset_id'], version)
        if old and digest(old) != digest(promotion['baseline_cases']):
            raise ValueError('baseline regression suite changed')
        plans = []
        if not old:
            plans.extend(('baseline', original, c) for c in promotion['baseline_cases'])
        plans.extend(('candidate', Path(revision['package']), c) for c in promotion['cases'])
        for index, (phase, package, case) in enumerate(plans):
            case_id = f'{phase}:{case["id"]}'
            def event(payload):
                return emit({**payload, 'validation_case': case_id, 'candidate_version': promotion['version']})
            event({'event': 'regression_started', 'validation_case': case_id})
            try:
                self.execute_case(package, case, host, cancel, event, check_cancel,
                                  evidence / f'{index}-{case["id"]}', original if phase == 'candidate' else None,
                                  lambda request: self.service.stages.execute(revision['scope'], revision['child'], request, event, check_cancel))
            except Exception:
                event({'event': 'regression_failed'})
                raise
            event({'event': 'regression_passed'})
        self.freeze_baseline(parent['asset_id'], version, promotion['baseline_cases'])
        return promotion

    def publish(self, db, revision, promotion):
        package = Path(revision['package'])
        if package_pin(package) != revision['pin']:
            raise ValueError('candidate changed after validation')
        parent = self.service.status(revision['scope'], revision['parent'])
        if package_pin(self.service.root / 'assets' / parent['asset_id'] / promotion['version']) != revision['pin']:
            raise ValueError('candidate asset changed after validation')
        if package_pin(self.service.root / 'assets' / parent['asset_id'] / promotion.get('baseline_version', parent['version'])) != promotion.get('baseline_pin', parent['pin']):
            raise ValueError('original package changed after validation')
        latest = db.execute('SELECT version,pin FROM assets WHERE id=? AND published=1 ORDER BY publication DESC LIMIT 1', (parent['asset_id'],)).fetchone()
        actual = dict(latest) if latest else None
        if actual != promotion['baseline_latest']:
            return {'status': 'conflict', 'version': promotion['version'], 'reason': 'latest changed; candidate retained unpublished'}
        db.execute('INSERT INTO regression_suites VALUES(?,?,?,?)',
                   (parent['asset_id'], promotion['version'], encoded(promotion['cases']), digest(promotion['cases'])))
        db.execute('UPDATE assets SET published=1, publication=(SELECT COALESCE(MAX(publication),0)+1 FROM assets) WHERE id=? AND version=?',
                   (parent['asset_id'], promotion['version']))
        return {'status': 'published', 'version': promotion['version']}
