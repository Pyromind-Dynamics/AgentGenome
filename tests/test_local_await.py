import json
from pathlib import Path

from agentgenome.local_host import LocalAwait


def test_local_await_validates_receipt_and_never_substitutes_status_for_product(tmp_path):
    port = LocalAwait(tmp_path)
    receipt = tmp_path / 'receipt.json'
    for payload in ['bad json', '[]', '{}']:
        receipt.write_text(payload)
        assert port.probe(str(receipt)).status == 'failed'
    output = tmp_path / 'output'
    output.mkdir()
    receipt.write_text(json.dumps({'task_id': 't1', 'output_dir': 'output'}))
    assert port.probe(str(receipt)).status == 'pending'
    (output / 'report.json').write_text('{"status":"succeeded"}')
    result = port.probe(str(receipt))
    assert result.status == 'pending' and result.product is None
    (output / 'processed.jsonl').write_text('actual\n')
    result = port.probe(str(receipt))
    assert result.status == 'completed'
    assert Path(result.product).read_text() == 'actual\n'
