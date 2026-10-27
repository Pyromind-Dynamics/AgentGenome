import json

import pytest

from agentgenome.service import GenomeService
from core.ports import PortError


@pytest.mark.parametrize('result,receipt,expected', [
    (None, None, 'Host returned no stage result'),
    ({'status': 'aborted'}, None, 'Model round did not complete successfully'),
    ({'status': 'completed', 'execution_id': 'current'}, None, 'Missing genome_step_result receipt'),
    ({'status': 'completed', 'execution_id': 'current'},
     {'execution_id': 'old', 'outcome': 'completed'}, 'Receipt execution_id does not match'),
])
def test_stage_diagnostic_identifies_the_actual_failure(tmp_path, result, receipt, expected):
    service = GenomeService(tmp_path / 'catalog')

    def emit(event):
        if receipt:
            with service._db() as db:
                db.execute('UPDATE stage_requests SET receipt=? WHERE id=?',
                           (json.dumps(receipt), event['request']['request_id']))
        return result

    with pytest.raises(PortError, match=expected) as error:
        service.stages.execute('scope', 'run', {'node_id': 'node'}, emit, lambda: None)
    assert 'llm/human' not in str(error.value)
    if result is not None:
        assert 'pi -p' not in str(error.value)
