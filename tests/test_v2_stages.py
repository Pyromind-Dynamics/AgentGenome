import json
from pathlib import Path

import pytest

from agentgenome.service import GenomeService
from agentgenome.local_host import LocalExecutionHost


def package(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'manifest.json').write_text(json.dumps(dict(id='report', version='1.0.0', name='report', description='report', parameters={})))
    (source / 'graph.yaml').write_text('''version: "gt/1.0"
node:
  id: report
  do: {llm: "Write hello to {artifact}"}
  output:
    report: {type: path}
  verify:
    - run: "test -s {artifact}"
''')
    service = GenomeService(tmp_path / 'catalog')
    service.import_asset(source)
    return service


@pytest.mark.parametrize('mode,expected', [('ok','succeeded'), ('no_receipt','failed'), ('wrong_round','failed'), ('no_file','failed'), ('failed','failed'), ('cancel','cancelled')])
def test_stage_requires_matching_round_receipt_and_artifact(tmp_path, mode, expected):
    service = package(tmp_path)
    state = service.submit('one', 'request', 'report', '1.0.0', {}, allow_draft=True)
    requests = []
    def emit(event):
        if event['event'] != 'waiting_agent':
            return None
        request = event['request']
        requests.append(request)
        if mode != 'no_file':
            output = Path(request['expected_outputs'][0])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('hello')
        if mode == 'cancel':
            service.cancel('one', state['id'])
            return {'status': 'cancelled'}
        if mode != 'no_receipt':
            with pytest.raises(ValueError, match='conversation'):
                service.stages.receipt('two', request['request_id'], 'completed', 'done', 'round')
            result = service.stages.receipt('one', request['request_id'], 'failed' if mode == 'failed' else 'completed', 'done', 'round')
            assert result['awaiting_verification']
            assert service.stages.receipt('one', request['request_id'], 'failed' if mode == 'failed' else 'completed', 'done', 'round') == result
            with pytest.raises(ValueError, match='conflicting'):
                service.stages.receipt('one', request['request_id'], 'completed', 'other', 'round')
        return {'status': 'completed', 'execution_id': 'other' if mode == 'wrong_round' else 'round'}
    result = service.execute('one', state['id'], LocalExecutionHost(service.root, tmp_path), emit)
    assert result['status'] == expected
    assert len(requests) == 1
    with pytest.raises(ValueError, match='active'):
        service.stages.receipt('one', requests[0]['request_id'], 'completed', 'done', 'round')


def test_native_preflight_rejects_agent_node(tmp_path):
    service = package(tmp_path)
    with pytest.raises(ValueError, match='agent_task'):
        service.check_capabilities('report', '1.0.0', set())
