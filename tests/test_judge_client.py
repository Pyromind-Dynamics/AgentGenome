import json
import threading
import time
import shlex
import sys
from pathlib import Path

import pytest
from agentgenome.judge import evaluate
from agentgenome.local_host import StreamingShell


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


def test_judge_retriggers_after_a_stale_response_from_an_earlier_round(tmp_path):
    prompt, evidence = tmp_path / 'prompt', tmp_path / 'evidence'
    prompt.write_text('quality')
    evidence.write_text('report')
    directory = tmp_path / 'exchange'
    directory.mkdir()
    (directory / 'response.json').write_text(json.dumps(
        {'request_id': 'earlier-round', 'verdict': {'passed': True, 'reason': 'stale'}}))
    (directory / 'response.ready').write_text('ready')
    def host():
        while (directory / 'response.ready').exists():
            time.sleep(.005)
        request = json.loads((directory / 'request.json').read_text())
        assert request['request_id'] != 'earlier-round'
        (directory / 'response.json').write_text(json.dumps(
            {'request_id': request['request_id'], 'verdict': {'passed': False, 'reason': 'fresh'}}))
        (directory / 'response.ready').write_text('ready')
    thread = threading.Thread(target=host)
    thread.start()
    assert evaluate(prompt, evidence, directory, timeout=2) == {'passed': False, 'reason': 'fresh'}
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


@pytest.mark.parametrize('script', [
    'agentgenome/judge.py',
    'templates/native-model-judge-check/scripts/judge.py',
    'templates/data-cleaning-v2/scripts/judge.py',
])
@pytest.mark.parametrize('second_passed', [True, False])
def test_two_judge_processes_in_one_verify_command(tmp_path, script, second_passed):
    script = Path(__file__).resolve().parents[1] / script
    prompt = tmp_path / 'criterion'
    prompt.write_text('quality')
    evidence = [tmp_path / name for name in ('first.txt', 'second.txt')]
    for path in evidence:
        path.write_text(path.name)
    requests = []

    def judge(request):
        requests.append(request)
        return {'verdict': {'passed': True if len(requests) == 1 else second_passed,
                            'reason': request['evidence']}}

    shell = StreamingShell(threading.Event(), lambda _: None, timeout=5,
                           judge=judge, judge_cwd=tmp_path, judge_root=tmp_path / 'exchange')
    command = ' && '.join(shlex.join([sys.executable, str(script), str(prompt), str(path)])
                          for path in evidence)
    result = shell.execute(command, tmp_path)
    assert result.rc == (0 if second_passed else 1), result
    assert [request['evidence'] for request in requests] == ['first.txt', 'second.txt']
    assert len({request['request_id'] for request in requests}) == 2
    verdicts = [json.loads(line) for line in result.stdout.splitlines()]
    assert [verdict['passed'] for verdict in verdicts] == [True, second_passed]
