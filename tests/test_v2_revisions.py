import json
import shutil
from pathlib import Path
from dataclasses import replace

import pytest

from agentgenome.dispatch import invoke
from agentgenome.service import GenomeService
from agentgenome.local_host import LocalExecutionHost
from builder import package_pin

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates/data-cleaning'



def csv_case(case_id, delimiter=',', with_param=False):
    return {'id': case_id, 'files': {'input.csv': f'name{delimiter}age\nAda{delimiter}36\nAda{delimiter}36\nBob{delimiter}\n'},
            'params': {'data_file': 'fixture:input.csv', **({'delimiter': ','} if with_param else {})},
            'assertions': [{'node': 'clean-sop', 'output': 'report', 'kind': 'json', 'expected': {'rows': 1, 'columns': ['name', 'age']}},
                           {'node': 'clean-sop.clean', 'output': 'cleaned', 'kind': 'csv', 'expected': [['name', 'age'], ['Ada', '36']]}]}

class RevisionHost(LocalExecutionHost):
    """Contract fake; OS protection is tested at the SDK execution boundary."""
    def prepare_revision(self, revision_id, package, allowed):
        directory = self.root / 'drafts' / revision_id
        for name in allowed:
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((package / name).read_bytes())
        return {'editable_dir': str(directory)}

    def collect_revision(self, draft, allowed):
        return {name: (Path(draft['editable_dir']) / name).read_bytes() for name in allowed}

    def prepare(self, *args, verification_package=None):
        ports, params = super().prepare(*args)
        return replace(ports, verification_cwd=verification_package), params


def test_revision_freezes_scripts_and_keeps_original_verification(tmp_path):
    source = tmp_path / 'asset'
    shutil.copytree(TEMPLATE, source)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest['revision'] = {'safe_to_rerun': True, 'editable_scripts': ['scripts/clean.py'], 'verification_resources': ['scripts/verify_report.py']}
    (source / 'manifest.json').write_text(json.dumps(manifest))
    (source / 'scripts/clean.py').write_text("import sys\nif 'bad-input' in sys.argv[1]: raise ValueError('bad naming')\n" + (TEMPLATE / 'scripts/clean.py').read_text())
    service = GenomeService(tmp_path / 'catalog')
    service.import_asset(source)
    host = RevisionHost(service.root, tmp_path)
    data = tmp_path / 'bad-input.csv'
    data.write_text('name,age\nAda,36\nAda,36\nBob,\n')
    state = service.submit('one', 'first', 'data-cleaning', '1.0.0', {'data_file': str(data)}, allow_draft=True)
    failed = service.execute('one', state['id'], host, lambda e: None)
    assert failed['status'] == 'failed'
    recovery = invoke(service, 'one', 'status', {'run_id': state['id']}, 'inspect',
                      capabilities={'revision', 'protected_verification'})['recovery']
    assert recovery['revision']['available']
    draft = service.revisions.prepare('one', state['id'], 'draft', host)
    assert service.revisions.prepare('one', state['id'], 'draft', host) == draft
    directory = Path(draft['editable_dir'])
    assert not (directory / 'scripts/verify_report.py').exists()
    (directory / 'scripts/clean.py').write_bytes((TEMPLATE / 'scripts/clean.py').read_bytes())
    child = service.revisions.submit('one', 'second', draft['revision_id'], state['params'], 'fixed naming', host, baseline_cases=[csv_case('old')], regression_cases=[csv_case('new')])
    assert child['parent_run_id'] == state['id']
    assert service.revisions.submit('one', 'second', draft['revision_id'], state['params'], 'fixed naming', host, baseline_cases=[csv_case('old')], regression_cases=[csv_case('new')])['id'] == child['id']
    with pytest.raises(ValueError, match='revision is still active'):
        service.revisions.takeover('one', state['id'], 'cannot use it')
    (directory / 'scripts/clean.py').write_text('raise ValueError("late edit")')
    result = service.execute('one', child['id'], host, lambda e: None)
    assert result['status'] == 'succeeded'
    assert service.status('one', state['id'])['status'] == 'failed'
    assert package_pin(service.root / 'assets/data-cleaning/1.0.0') == state['pin']
    assert service.list()[0]['version'] == '1.0.1'
    again = service.revisions.prepare('one', child['id'], 'third', host, 'needs further adaptation')
    assert again['parent_run_id'] == child['id']
    with pytest.raises(ValueError, match='conversation'):
        service.revisions.prepare('two', state['id'], 'other', host)


def test_successful_checks_can_still_require_takeover(tmp_path):
    """The v1 comma parser passes checks on a semicolon CSV, but misses intent."""
    service = GenomeService(tmp_path / 'catalog')
    service.import_asset(TEMPLATE)
    data = tmp_path / 'semicolon.csv'
    data.write_text('name;age\nAda;36\nAda;36\nBob;\nCy;12\n')
    state = service.submit('one', 'first', 'data-cleaning', '1.0.0',
                           {'data_file': str(data)}, allow_draft=True)
    host = LocalExecutionHost(service.root, tmp_path)
    result = service.execute('one', state['id'], host, lambda e: None)
    assert result['status'] == 'succeeded'
    service.publish('data-cleaning', '1.0.0')
    asset = invoke(service, 'one', 'get', {'asset_id': 'data-cleaning', 'version': '1.0.0'}, 'get')
    assert not asset['revision_availability']['available']
    assert len(asset['revision_availability']['reasons']) == 1
    status = invoke(service, 'one', 'status', {'run_id': state['id']}, 'status')
    assert not status['recovery']['revision']['available']
    assert status['recovery']['takeover']['available']
    with pytest.raises(ValueError, match='genome_takeover'):
        invoke(service, 'one', 'prepare_revision', {'run_id': state['id']}, 'draft')
    with pytest.raises(ValueError, match='conversation'):
        invoke(service, 'other', 'takeover', {'run_id': state['id'], 'reason': 'mismatch'}, 'take')
    arguments = {'run_id': state['id'], 'reason': 'checks passed but results do not satisfy the task'}
    taken = invoke(service, 'one', 'takeover', arguments, 'take')
    assert taken['taken_over']
    assert taken['status'] == 'succeeded'
    assert taken['result'] == result['result']
    assert invoke(service, 'one', 'takeover', arguments, 'repeat') == taken
    with pytest.raises(ValueError, match='conflicting'):
        invoke(service, 'one', 'takeover', {**arguments, 'reason': 'different'}, 'conflict')
    restarted = GenomeService(tmp_path / 'catalog')
    recovered = invoke(restarted, 'one', 'status', {'run_id': state['id']}, 'status')
    assert recovered['status'] == 'succeeded'
    assert recovered['recovery']['takeover']['taken_over']
    assert recovered['recovery']['takeover']['takeover_reason'] == arguments['reason']


@pytest.mark.parametrize('status', ['queued', 'running', 'cancelling'])
def test_takeover_rejects_active_execution(tmp_path, status):
    service = GenomeService(tmp_path / 'catalog')
    service.import_asset(TEMPLATE)
    state = service.submit('one', 'first', 'data-cleaning', '1.0.0',
                           {'data_file': 'input.csv'}, allow_draft=True)
    with service._db() as db:
        db.execute('UPDATE runs SET status=? WHERE id=?', (status, state['id']))
    with pytest.raises(ValueError, match='stopped run'):
        service.revisions.takeover('one', state['id'], 'mismatch')


@pytest.mark.parametrize('status', ['failed', 'interrupted', 'cancelled'])
def test_takeover_preserves_stopped_status(tmp_path, status):
    service = GenomeService(tmp_path / 'catalog')
    service.import_asset(TEMPLATE)
    state = service.submit('one', 'first', 'data-cleaning', '1.0.0',
                           {'data_file': 'input.csv'}, allow_draft=True)
    with service._db() as db:
        db.execute('UPDATE runs SET status=? WHERE id=?', (status, state['id']))
    with pytest.raises(ValueError, match='reason'):
        service.revisions.takeover('one', state['id'], ' ')
    taken = service.revisions.takeover('one', state['id'], 'mismatch')
    assert taken['status'] == status and taken['taken_over']


class PromptRevisionHost(RevisionHost):
    def prepare_revision(self, revision_id, package, allowed):
        directory = self.root / 'drafts' / revision_id
        shutil.copytree(package, directory)
        return {'editable_dir': str(directory), 'verification_dir': str(directory),
                'verification_protection': 'prompt'}

    def prepare(self, *args, verification_package=None):
        return LocalExecutionHost.prepare(self, *args)


@pytest.mark.parametrize('fix', ['parameter', 'script'])
def test_prompt_only_revision_of_successful_semicolon_csv(tmp_path, fix):
    service = GenomeService(tmp_path / 'catalog')
    template = TEMPLATE.parent / 'data-cleaning-revision'
    service.import_asset(template)
    host = PromptRevisionHost(service.root, tmp_path)
    data = tmp_path / 'semicolon.csv'
    data.write_text('name;age\nAda;36\nAda;36\nBob;\nCy;12\n')
    params = {'data_file': str(data), 'delimiter': ','}
    state = service.submit('one', 'first', 'data-cleaning', '1.1.0', params, allow_draft=True)
    result = service.execute('one', state['id'], host, lambda e: None)
    assert result['status'] == 'succeeded'
    report = json.loads(Path(result['result']['outputs']['report']['execution_path']).read_text())
    assert report['rows'] == 3 and report['columns'] == ['name;age']
    capabilities = {'revision'}
    args = {'run_id': state['id']}
    with pytest.raises(ValueError, match='reason is required'):
        invoke(service, 'one', 'prepare_revision', args, 'draft', capabilities=capabilities, host=host)
    args['reason'] = 'semicolon input was parsed as a single column'
    draft = invoke(service, 'one', 'prepare_revision', args, 'draft', capabilities=capabilities, host=host)
    assert draft['verification_protection'] == 'prompt'
    assert '不要修改验收脚本' in draft['instruction']
    assert draft['verification_resources'] == ['scripts/verify_report.py']
    restarted = GenomeService(service.root)
    assert invoke(restarted, 'one', 'prepare_revision', args, 'draft', capabilities=capabilities, host=host) == draft
    with pytest.raises(ValueError, match='conflicting revision reason'):
        invoke(service, 'one', 'prepare_revision', {**args, 'reason': 'other'}, 'draft', capabilities=capabilities, host=host)
    directory = Path(draft['editable_dir'])
    (directory / 'scripts/verify_report.py').write_text('raise SystemExit(0)')
    (directory / 'graph.yaml').write_text('changed graph rules')
    if fix == 'parameter':
        params = {**params, 'delimiter': ';'}
    else:
        script = directory / 'scripts/clean.py'
        script.write_text(script.read_text().replace('rows = list(csv.reader(f, delimiter=sys.argv[2]))', "sample = f.read(4096); f.seek(0)\n        rows = list(csv.reader(f, delimiter=csv.Sniffer().sniff(sample, delimiters=',;').delimiter))"))
    submit = {'revision_id': draft['revision_id'], 'params': params, 'change_summary': fix}
    if fix == 'script':
        submit.update(baseline_cases=[csv_case('comma', with_param=True)], regression_cases=[csv_case('semicolon', ';', True)])
    child = invoke(service, 'one', 'run', submit, 'rerun', capabilities=capabilities, host=host)
    assert invoke(service, 'one', 'run', submit, 'rerun', capabilities=capabilities, host=host)['id'] == child['id']
    row = service.revisions.for_run(child['id'])
    assert row['reason'] == args['reason']
    assert (Path(row['package']) / 'scripts/verify_report.py').read_bytes() == (template / 'scripts/verify_report.py').read_bytes()
    assert (Path(row['package']) / 'graph.yaml').read_bytes() == (template / 'graph.yaml').read_bytes()
    result = service.execute('one', child['id'], host, lambda e: None)
    assert result['status'] == 'succeeded', result
    report = json.loads(Path(result['result']['outputs']['report']['execution_path']).read_text())
    assert report['rows'] == 2 and report['columns'] == ['name', 'age']
    assert package_pin(service.root / 'assets/data-cleaning/1.1.0') == state['pin']
    again = service.revisions.prepare('one', child['id'], 'again', host, 'still unsuitable')
    assert again['params'] == params
    with pytest.raises(ValueError, match='already submitted'):
        service.revisions.prepare('one', state['id'], 'another', host, args['reason'])
    service.revisions.takeover('one', child['id'], 'remaining work needs a different method')


def test_legacy_revision_table_migrates_without_losing_drafts(tmp_path):
    import sqlite3
    with sqlite3.connect(tmp_path / 'state.sqlite3') as db:
        db.execute('CREATE TABLE revisions (id TEXT PRIMARY KEY, scope TEXT NOT NULL, parent TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL, draft TEXT NOT NULL, package TEXT, pin TEXT, child TEXT, UNIQUE(scope,request_id))')
        db.execute("INSERT INTO revisions(id,scope,parent,request_id,draft) VALUES('draft','one','parent','request','{}')")
    service = GenomeService(tmp_path)
    with service._db() as db:
        row = db.execute('SELECT * FROM revisions').fetchone()
    assert row['id'] == 'draft' and row['reason'] is None and row['draft'] == '{}'
    GenomeService(tmp_path)  # migration is repeatable


@pytest.mark.parametrize('outcome', ['publish', 'old_case_fails', 'cancel', 'conflict', 'changed'])
def test_legacy_candidate_publication_gate(tmp_path, outcome):
    service = GenomeService(tmp_path / 'catalog')
    service.import_asset(TEMPLATE)
    host = PromptRevisionHost(service.root, tmp_path)
    data = tmp_path / 'data.csv'
    data.write_text('name;age\nAda;36\nAda;36\nBob;\n')
    state = service.submit('one', 'first', 'data-cleaning', '1.0.0', {'data_file': str(data)}, allow_draft=True)
    assert service.execute('one', state['id'], host, lambda e: None)['status'] == 'succeeded'
    service.publish('data-cleaning', '1.0.0')
    draft = service.revisions.prepare('one', state['id'], 'draft', host, 'support semicolons too', ['scripts/clean.py'], ['scripts/verify_report.py'])
    script = Path(draft['editable_dir']) / 'scripts/clean.py'
    replacement = "rows = list(csv.reader(f, delimiter=';'))" if outcome == 'old_case_fails' else "sample=f.read(4096); f.seek(0)\n        rows = list(csv.reader(f, delimiter=csv.Sniffer().sniff(sample, delimiters=',;').delimiter))"
    script.write_text(script.read_text().replace('rows = list(csv.reader(f))', replacement))
    kwargs = {'baseline_cases': [csv_case('comma')], 'regression_cases': [csv_case('semicolon', ';')]}
    child = service.revisions.submit('one', 'rerun', draft['revision_id'], state['params'], 'expand dialect coverage', host, **kwargs)
    assert child['candidate_version'] == '1.0.1'
    assert service.revisions.submit('one', 'rerun', draft['revision_id'], state['params'], 'expand dialect coverage', host, **kwargs)['id'] == child['id']
    assert service.list()[0]['version'] == '1.0.0'
    with pytest.raises(ValueError, match='regression suite'):
        service.publish('data-cleaning', '1.0.1')
    events = []
    def emit(event):
        events.append(event)
        if event.get('event') == 'regression_started' and outcome == 'cancel':
            service.cancel('one', child['id'])
        if event.get('event') == 'regression_passed' and event.get('validation_case') == 'candidate:semicolon':
            if outcome == 'conflict':
                with service._db() as db:
                    db.execute("UPDATE assets SET publication=99,pin='different' WHERE id='data-cleaning' AND version='1.0.0'")
            if outcome == 'changed':
                (service.root / 'assets/data-cleaning/1.0.1/scripts/clean.py').write_text('changed')
    result = service.execute('one', child['id'], host, emit)
    if outcome == 'publish':
        assert result['status'] == 'succeeded', result
        assert result['result']['publication'] == {'status': 'published', 'version': '1.0.1'}
        assert service.list()[0]['version'] == '1.0.1'
        assert len(service.regressions.suite('data-cleaning', '1.0.1')) == 2
        assert service.get('data-cleaning', '1.0.0')['pin'] == state['pin']
        assert service.execute('one', child['id'], host, emit) == result
    else:
        assert service.list()[0]['version'] == '1.0.0'
        if outcome == 'conflict':
            assert result['result']['publication']['status'] == 'conflict'
        else:
            assert result['status'] == ('cancelled' if outcome == 'cancel' else 'failed'), result
    assert any(e.get('validation_case') for e in events)


def test_revisions_can_continue_after_draft_case_and_script_failures(tmp_path):
    service = GenomeService(tmp_path / 'registry')
    service.import_asset(TEMPLATE)
    host = PromptRevisionHost(service.root, tmp_path)
    data = tmp_path / 'semicolon.csv'
    data.write_text('name;age\nAda;36\nAda;36\nBob;\n')
    first = service.submit('one', 'first', 'data-cleaning', '1.0.0', {'data_file': str(data)}, allow_draft=True)
    service.execute('one', first['id'], host, lambda e: None)
    service.publish('data-cleaning', '1.0.0')
    draft = service.revisions.prepare('one', first['id'], 'draft1', host, 'semicolon parsing', ['scripts/clean.py'], ['scripts/verify_report.py'])
    original = (TEMPLATE / 'scripts/clean.py').read_text()
    fixed = original.replace('rows = list(csv.reader(f))', "sample=f.read(4096); f.seek(0)\n        rows = list(csv.reader(f, delimiter=csv.Sniffer().sniff(sample, delimiters=',;').delimiter))")
    (Path(draft['editable_dir']) / 'scripts/clean.py').write_text(fixed)
    incorrect = csv_case('comma')
    incorrect['assertions'][1]['expected'] = [['Ada', '36']]  # session's missing header
    child = service.revisions.submit('one', 'run1', draft['revision_id'], first['params'], 'dialect support', host,
                                     baseline_cases=[incorrect], regression_cases=[csv_case('semicolon', ';')])
    assert service.execute('one', child['id'], host, lambda e: None)['status'] == 'failed'
    assert service.regressions.suite('data-cleaning', '1.0.0') == []
    # Existing persisted records need no migration or script replay.
    with service._db() as db:
        row = service.revisions.for_run(child['id'])
        promotion = json.loads(row['promotion'])
        promotion.pop('baseline_version')
        promotion.pop('baseline_pin')
        old_result = service.status('one', child['id'])['result']
        old_result['revision_remaining'] = 0
        old_result['instruction'] = 'A failed revision cannot be revised again.'
        db.execute('UPDATE runs SET result=? WHERE id=?', (json.dumps(old_result), child['id']))
        db.execute('UPDATE revisions SET promotion=? WHERE id=?', (json.dumps(promotion), row['id']))
    service = GenomeService(service.root)
    visible = service.status('one', child['id'])['result']
    assert 'revision_remaining' not in visible
    assert 'no revision-count limit' in visible['instruction']
    draft2 = service.revisions.prepare('one', child['id'], 'draft2', host)
    assert (Path(draft2['editable_dir']) / 'scripts/clean.py').read_text() == fixed
    assert draft2['baseline_cases'] == [incorrect]
    assert draft2['pending_regression_cases'] == [csv_case('semicolon', ';')]
    # The second attempt fixes the draft test but accidentally breaks old inputs.
    (Path(draft2['editable_dir']) / 'scripts/clean.py').write_text(original.replace('rows = list(csv.reader(f))', "rows = list(csv.reader(f, delimiter=';'))"))
    child2 = service.revisions.submit('one', 'run2', draft2['revision_id'], child['params'], 'correct case', host,
                                      baseline_cases=[csv_case('comma')])
    with pytest.raises(ValueError, match='still active'):
        service.revisions.takeover('one', first['id'], 'stop')
    assert service.execute('one', child2['id'], host, lambda e: None)['status'] == 'failed'
    assert service.list()[0]['version'] == '1.0.0'
    assert service.revisions.recovery(service.status('one', child2['id']), {'revision'})['revision']['available']
    draft3 = service.revisions.prepare('one', child2['id'], 'draft3', host)
    (Path(draft3['editable_dir']) / 'scripts/clean.py').write_text(fixed)
    child3 = service.revisions.submit('one', 'run3', draft3['revision_id'], child2['params'], 'keep both dialects', host)
    assert service.revisions.submit('one', 'run3', draft3['revision_id'], child2['params'], 'keep both dialects', host)['id'] == child3['id']
    result = service.execute('one', child3['id'], host, lambda e: None)
    assert result['status'] == 'succeeded', result
    assert result['result']['publication'] == {'status': 'published', 'version': '1.0.3'}
    assert service.list()[0]['version'] == '1.0.3'
    assert service.get('data-cleaning', '1.0.0')['pin'] == first['pin']
    # A published revision becomes the baseline for further work, retaining cases.
    draft4 = service.revisions.prepare('one', child3['id'], 'draft4', host, 'another scenario')
    assert draft4['baseline_version'] == '1.0.3'
    assert len(draft4['regression_cases']) == 2
    with pytest.raises(ValueError, match='inherited regression cases'):
        service.revisions.submit('one', 'run4bad', draft4['revision_id'], child3['params'], 'bad case', host,
                                 baseline_cases=[incorrect], regression_cases=[csv_case('extra')],
                                 parameter_patch={'declarations': {'optional': {'type': 'string', 'default': 'x'}}, 'bindings': {'clean-sop.input.optional': 'params.optional'}})
    service.revisions.takeover('one', child3['id'], 'different method is better')
    with pytest.raises(ValueError, match='handed over'):
        service.revisions.prepare('one', child3['id'], 'draft5', host, 'again')
