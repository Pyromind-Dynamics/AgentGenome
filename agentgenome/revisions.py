"""Run-scoped drafts; only allowlisted files can enter an immutable revision."""
from __future__ import annotations

import json
import re
import shutil

import yaml
from pathlib import Path, PurePosixPath
from uuid import uuid4

from builder import package_pin, compile
from .parameter_patch import apply_parameter_patch
from .regressions import validate_cases, encoded

REVISION_INSTRUCTION = (
    '只修改本次副本中允许的业务脚本和参数。不要修改验收脚本、验收提示词、阈值、'
    'graph 验收规则或伪造验收结果。无法在保持验收标准的前提下完成适配时，调用 genome_takeover。'
)

RECOVERY_INSTRUCTION = (
    'Assess whether another revision can solve the remaining problem. There is no revision-count limit: '
    'use genome_prepare_revision with the latest run ID to continue from its frozen scripts, or '
    'genome_takeover if a different approach is better. Never change verification or weaken frozen '
    'regression cases. Unvalidated draft cases may be corrected and must pass the original baseline again.'
)

STOPPED = {'succeeded', 'failed', 'interrupted', 'cancelled'}


class Revisions:
    def __init__(self, service):
        self.service = service
        with service._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS revisions (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, parent TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL, draft TEXT NOT NULL, package TEXT, pin TEXT,
                child TEXT, UNIQUE(scope,request_id));
                CREATE TABLE IF NOT EXISTS takeovers (
                run_id TEXT PRIMARY KEY, scope TEXT NOT NULL, reason TEXT NOT NULL);
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(revisions)')}
            for name in ('reason', 'promotion'):
                if name not in columns:
                    db.execute(f'ALTER TABLE revisions ADD COLUMN {name} TEXT')

    @staticmethod
    def availability(manifest, capabilities):
        reasons = []
        if 'revision' not in capabilities:
            reasons.append('host cannot prepare and collect revision files')
        if 'revision' in manifest and manifest['revision'].get('safe_to_rerun') is not True:
            reasons.append('asset does not permit safe revision reruns')
        return {'available': not reasons, 'reasons': reasons}

    def context(self, state):
        """Return the latest frozen scripts and their public verification baseline."""
        chain = []
        current = state
        while previous := self.for_run(current['id']):
            chain.append(previous)
            current = self.service.status(previous['scope'], previous['parent'])
        base = current
        for previous in reversed(chain):
            promotion = json.loads(previous['promotion']) if previous['promotion'] else None
            if promotion:
                asset = self.service.get(state['asset_id'], promotion['version'])
                if asset['published']:
                    base = {**state, 'version': asset['version'], 'pin': asset['pin']}
        package = (Path(chain[0]['package']) if chain else
                   self.service.root / 'assets' / state['asset_id'] / state['version'])
        if package_pin(package) != state['pin']:
            raise ValueError('frozen package has changed')
        manifest = json.loads((package / 'manifest.json').read_text())
        if chain and 'revision' not in manifest:
            draft = json.loads(chain[0]['draft'])
            manifest['revision'] = {
                'safe_to_rerun': True, 'editable_scripts': draft['allowed_scripts'],
                'verification_resources': draft['verification_resources'],
                'editable_parameters': draft.get('editable_parameters', []),
                'editable_bindings': draft.get('editable_bindings', []),
                'allow_new_parameters': draft.get('allow_new_parameters', False),
            }
        return manifest, package, base

    def recovery(self, state, capabilities):
        manifest = self.context(state)[0]
        revision = self.availability(manifest, capabilities)
        try:
            self.policy(state)
        except ValueError as exc:
            if str(exc) not in revision['reasons']:
                revision['reasons'].append(str(exc))
        with self.service._db() as db:
            child = db.execute('SELECT child FROM revisions WHERE parent=?', (state['id'],)).fetchone()
            if child and child['child']:
                revision['reasons'].append(f"revision already submitted; continue from run {child['child']}")
                revision['next_run_id'] = child['child']
            taken = db.execute('SELECT reason FROM takeovers WHERE run_id=?', (state['id'],)).fetchone()
            blocker = self._takeover_blocker(state, db)
        revision['available'] = not revision['reasons']
        revision['reason_required'] = state['status'] == 'succeeded'
        return {'revision': revision,
                'takeover': {'available': blocker is None, 'reason': blocker,
                             'taken_over': bool(taken), 'takeover_reason': taken['reason'] if taken else None}}

    @staticmethod
    def _takeover_blocker(state, db):
        if state['status'] not in STOPPED:
            return 'takeover requires a stopped run; cancel active execution first'
        current = state['id']
        while child := db.execute('SELECT runs.id,runs.status FROM revisions JOIN runs ON runs.id=revisions.child WHERE revisions.parent=?', (current,)).fetchone():
            if child['status'] not in STOPPED:
                return 'revision is still active; wait for it to stop or cancel it first'
            current = child['id']
        return None

    def policy(self, state):
        manifest, package, _ = self.context(state)
        policy = manifest.get('revision', {})
        if 'revision' in manifest and policy.get('safe_to_rerun') is not True:
            raise ValueError('asset does not permit safe revision reruns')
        with self.service._db() as db:
            current = state['id']
            while True:
                if db.execute('SELECT 1 FROM takeovers WHERE run_id=?', (current,)).fetchone():
                    raise ValueError('run has already been handed over')
                parent = db.execute('SELECT parent FROM revisions WHERE child=?', (current,)).fetchone()
                if parent is None:
                    break
                current = parent['parent']
        if state['status'] not in {'failed', 'succeeded'}:
            raise ValueError('only failed or succeeded runs may be revised')
        if 'revision' not in manifest:
            graph = compile(package)
            if self.service._requires_agent(graph.node):
                raise ValueError('legacy default revision supports fixed script graphs only')
        allowed = policy.get('editable_scripts', [])
        protected = policy.get('verification_resources', [])
        for name in [*allowed, *protected]:
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or str(path) != name or name in {'manifest.json', 'graph.yaml'}:
                raise ValueError('invalid revision resource path')
        if set(allowed) & set(protected):
            raise ValueError('verification resources cannot be editable')
        return manifest, allowed

    def legacy_policy(self, state, allowed, protected):
        graph = yaml.safe_load((self.service.root / 'assets' / state['asset_id'] / state['version'] / 'graph.yaml').read_text())
        bindings = []
        def visit(node, parent=''):
            path = f"{parent}.{node['id']}" if parent else node['id']
            bindings.extend(f'{path}.input.{key}' for key in node.get('input', {}))
            for child in node.get('children', []):
                visit(child, path)
        visit(graph['node'])
        return {'safe_to_rerun': True, 'editable_scripts': allowed,
                'verification_resources': protected, 'allow_new_parameters': True,
                'editable_bindings': bindings}

    def prepare(self, scope, run_id, request_id, host, reason=None, editable_scripts=None, verification_resources=None):
        state = self.service.status(scope, run_id)
        manifest, allowed = self.policy(state)
        if 'revision' not in manifest:
            if not editable_scripts or not isinstance(verification_resources, list):
                raise ValueError('legacy package requires editable_scripts and verification_resources declarations')
            allowed = editable_scripts
            for name in [*allowed, *verification_resources]:
                path = PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or str(path) != name or name in {'manifest.json', 'graph.yaml'}:
                    raise ValueError('invalid revision resource path')
            if set(allowed) & set(verification_resources):
                raise ValueError('verification resources cannot be editable')
            manifest = {**manifest, 'revision': self.legacy_policy(state, allowed, verification_resources)}
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise ValueError('reason must be a non-empty string')
        if state['status'] == 'succeeded' and reason is None:
            raise ValueError('reason is required when successful results do not satisfy the task')
        with self.service._lock, self.service._db() as db:
            previous = db.execute('SELECT * FROM revisions WHERE parent=? OR (scope=? AND request_id=?)',
                                  (run_id, scope, request_id)).fetchone()
            if previous:
                if previous['scope'] != scope or previous['parent'] != run_id:
                    raise ValueError('conflicting revision request')
                if previous['child']:
                    raise ValueError(f"revision already submitted; continue from run {previous['child']}")
                if json.loads(previous['draft'])['allowed_scripts'] != allowed:
                    raise ValueError('conflicting editable scripts')
                if json.loads(previous['draft'])['verification_resources'] != manifest['revision'].get('verification_resources', []):
                    raise ValueError('conflicting verification resources')
                if previous['reason'] != reason:
                    raise ValueError('conflicting revision reason')
                return {**json.loads(previous['draft']), 'reason': previous['reason'],
                        'instruction': REVISION_INSTRUCTION}
            revision_id = uuid4().hex
            _, package, baseline = self.context(state)
            prior = self.for_run(run_id)
            pending = json.loads(prior['promotion']) if prior and prior['promotion'] else None
            inherited = self.service.regressions.suite(state['asset_id'], baseline['version'])
            pending_baseline = pending['baseline_cases'] if pending else []
            pending_new = [c for c in pending['cases'] if c['id'] not in {b['id'] for b in pending_baseline}] if pending else []
            draft = host.prepare_revision(revision_id, package, allowed)
            result = {**draft, 'revision_id': revision_id, 'parent_run_id': run_id,
                      'params': state['params'], 'allowed_scripts': allowed,
                      'reason': reason, 'instruction': REVISION_INSTRUCTION,
                      'verification_resources': manifest['revision'].get('verification_resources', []),
                      'regression_cases': inherited,
                      'baseline_cases': inherited or pending_baseline,
                      'pending_regression_cases': pending_new if baseline['version'] != (pending or {}).get('version') else [],
                      'baseline_version': baseline['version'], 'baseline_pin': baseline['pin'],
                      'parameters': manifest['parameters'],
                      'editable_parameters': manifest['revision'].get('editable_parameters', []),
                      'allow_new_parameters': manifest['revision'].get('allow_new_parameters', False),
                      'editable_bindings': manifest['revision'].get('editable_bindings', [])}
            db.execute('INSERT INTO revisions(id,scope,parent,request_id,draft,reason) VALUES(?,?,?,?,?,?)',
                       (revision_id, scope, run_id, request_id, json.dumps(result), reason))
            return result

    def submit(self, scope, request_id, revision_id, params, change_summary, host, parameter_patch=None, capabilities=None, baseline_cases=None, regression_cases=None, version=None):
        if not isinstance(change_summary, str) or not change_summary.strip():
            raise ValueError('change_summary is required')
        with self.service._lock:
            with self.service._db() as db:
                row = db.execute('SELECT * FROM revisions WHERE id=? AND scope=?', (revision_id, scope)).fetchone()
            if row is None:
                raise ValueError('revision not found in this conversation')
            state = self.service.status(scope, row['parent'])
            manifest, allowed = self.policy(state)
            draft = json.loads(row['draft'])
            if 'revision' not in manifest:
                allowed = draft['allowed_scripts']
                manifest = {**manifest, 'revision': self.legacy_policy(state, allowed, draft['verification_resources'])}
            if state['status'] == 'succeeded' and not row['reason']:
                raise ValueError('successful run revision requires a recorded reason')
            if capabilities is not None:
                self.service.check_capabilities(state['asset_id'], state['version'], capabilities)
            if row['child']:
                child = self.service.status(scope, row['child'])
                with self.service._db() as db:
                    receipt = db.execute('SELECT request_id,request FROM runs WHERE id=?', (row['child'],)).fetchone()
                if (receipt['request_id'] != request_id or json.loads(receipt['request']) !=
                        {'revision_id': revision_id, 'params': params, 'change_summary': change_summary, 'parameter_patch': parameter_patch or {}, 'baseline_cases': baseline_cases or [], 'regression_cases': regression_cases or [], 'version': version}):
                    raise ValueError('revision already submitted')
                return child
            _, source, baseline = self.context(state)
            original = self.service.root / 'assets' / state['asset_id'] / baseline['version']
            if package_pin(original) != baseline['pin']:
                raise ValueError('frozen verification package has changed')
            created_asset = None
            destination = self.service.root / 'revisions' / revision_id / uuid4().hex
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
            try:
                draft = json.loads(row['draft'])
                files = host.collect_revision(draft, allowed)
                if set(files) != set(allowed):
                    raise ValueError('revision files do not match allowed scripts')
                for name, content in files.items():
                    target = destination / name
                    if not target.is_file() or target.is_symlink():
                        raise ValueError('editable script is not a regular package file')
                    target.write_bytes(content)
                if parameter_patch:
                    apply_parameter_patch(destination, parameter_patch, manifest['revision'])
                revised_manifest = json.loads((destination / 'manifest.json').read_text())
                graph = compile(destination)
                self.service._supported_graph(graph.node)
                if set(revised_manifest['parameters']) != set(graph.params):
                    raise ValueError('parameter declarations do not match graph inputs')
                resolved_params = self.service.validate_params(revised_manifest, params)
                content_changed = package_pin(destination) != baseline['pin']
                promotion = None
                if content_changed:
                    old_cases = self.service.regressions.suite(state['asset_id'], baseline['version'])
                    if old_cases and baseline_cases and old_cases != baseline_cases:
                        raise ValueError('inherited regression cases cannot be changed')
                    old_cases = old_cases or validate_cases(baseline_cases if baseline_cases is not None else draft.get('baseline_cases', []))
                    new_cases = validate_cases(regression_cases if regression_cases is not None else draft.get('pending_regression_cases', []))
                    if not old_cases or not new_cases:
                        raise ValueError('changed packages require baseline_cases and new regression_cases before publication')
                    cases = validate_cases([*old_cases, *new_cases])
                    promotion = {'asset_id': state['asset_id'], 'baseline_cases': old_cases, 'cases': cases,
                                 'baseline_version': baseline['version'], 'baseline_pin': baseline['pin']}
                pin = package_pin(destination)
                # The reservation and child insert share the same SQLite transaction.
                with self.service._db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if db.execute('SELECT child FROM revisions WHERE id=?', (revision_id,)).fetchone()['child']:
                        raise ValueError('revision already submitted')
                    if promotion is not None:
                        versions = [r[0] for r in db.execute('SELECT version FROM assets WHERE id=?', (state['asset_id'],))]
                        candidate_version = version
                        if candidate_version is None:
                            if not all(re.fullmatch(r'\d+\.\d+\.\d+', v) for v in versions):
                                raise ValueError('non-semantic asset versions require explicit version')
                            major, minor, patch = max(tuple(map(int, v.split('.'))) for v in versions)
                            candidate_version = f'{major}.{minor}.{patch + 1}'
                        if not isinstance(candidate_version, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', candidate_version) or candidate_version in versions:
                            raise ValueError('candidate version must be a new valid version')
                        promotion.update(version=candidate_version, baseline_latest={'version': baseline['version'], 'pin': baseline['pin']} if self.service.get(state['asset_id'], baseline['version'])['published'] else None)
                        revised_manifest.update(version=candidate_version, revision=manifest['revision'])
                        (destination / 'manifest.json').write_text(encoded(revised_manifest))
                        pin = package_pin(destination)
                        asset_dir = self.service.root / 'assets' / state['asset_id'] / candidate_version
                        if asset_dir.exists():
                            raise ValueError('candidate directory already exists')
                        shutil.copytree(destination, asset_dir)
                        created_asset = asset_dir
                        db.execute('INSERT INTO assets(id,version,pin,manifest) VALUES(?,?,?,?)', (state['asset_id'], candidate_version, pin, encoded(revised_manifest)))
                    child_id = uuid4().hex
                    request = json.dumps({'revision_id': revision_id, 'params': params, 'change_summary': change_summary, 'parameter_patch': parameter_patch or {}, 'baseline_cases': baseline_cases or [], 'regression_cases': regression_cases or [], 'version': version}, sort_keys=True)
                    db.execute('INSERT INTO runs(id,scope,request_id,request,asset_id,version,pin,params,status) VALUES(?,?,?,?,?,?,?,?,?)',
                               (child_id, scope, request_id, request, state['asset_id'], baseline['version'], pin,
                                json.dumps(resolved_params), 'queued'))
                    db.execute('UPDATE revisions SET child=?,package=?,pin=?,promotion=? WHERE id=?',
                               (child_id, str(destination), pin, encoded(promotion) if promotion else None, revision_id))
            except BaseException:
                shutil.rmtree(destination)
                if created_asset is not None:
                    shutil.rmtree(created_asset)
                raise
        return self.service.status(scope, child_id)

    def for_run(self, run_id):
        with self.service._db() as db:
            row = db.execute('SELECT * FROM revisions WHERE child=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def takeover(self, scope, run_id, reason):
        state = self.service.status(scope, run_id)
        if not reason.strip():
            raise ValueError('takeover requires a reason')
        with self.service._lock, self.service._db() as db:
            blocker = self._takeover_blocker(state, db)
            if blocker:
                raise ValueError(blocker)
            row = db.execute('SELECT reason FROM takeovers WHERE run_id=?', (run_id,)).fetchone()
            if row and row['reason'] != reason:
                raise ValueError('conflicting takeover reason')
            db.execute('INSERT OR IGNORE INTO takeovers VALUES(?,?,?)', (run_id, scope, reason))
        return {**state, 'taken_over': True, 'reason': reason,
                'instruction': 'Use the available results and business Skills to finish the remaining task. Do not resume this graph.'}
