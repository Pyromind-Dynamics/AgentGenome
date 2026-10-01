import json
import threading
import time
from pathlib import Path

import pytest
from agentgenome.judge import evaluate


@pytest.mark.parametrize('passed', [True, False])
def test_judge_waits_for_complete_matching_response(tmp_path, passed):
    prompt, evidence = tmp_path / 'prompt', tmp_path / 'evidence'
    prompt.write_text('quality')
    evidence.write_text('report')
    directory = tmp_path / 'exchange'
    def host():
        while not (directory / 'request.json').exists():
            time.sleep(.005)
        request = json.loads((directory / 'request.json').read_text())
        (directory / 'response.json').write_text(json.dumps({'request_id': request['request_id'], 'verdict': {'passed': passed, 'reason': 'evidence'}}))
        (directory / 'response.ready').write_text('ready')
    thread = threading.Thread(target=host)
    thread.start()
    assert evaluate(prompt, evidence, directory, timeout=2) == {'passed': passed, 'reason': 'evidence'}
    thread.join(2)


def test_judge_timeout_and_input_limit(tmp_path):
    prompt, evidence = tmp_path / 'prompt', tmp_path / 'evidence'
    prompt.write_text('quality')
    evidence.write_text('report')
    with pytest.raises(TimeoutError):
        evaluate(prompt, evidence, tmp_path / 'request', timeout=.01)
    evidence.write_text('x' * 262145)
    with pytest.raises(ValueError, match='256 KiB'):
        evaluate(prompt, evidence, tmp_path / 'large')
