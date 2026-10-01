import json
import shutil
from pathlib import Path

import pytest
from agentgenome.parameter_patch import apply_parameter_patch
from builder import compile


def test_only_declared_parameter_bindings_can_change(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'templates/data-cleaning'
    package = tmp_path / 'asset'
    shutil.copytree(source, package)
    before = compile(package)
    policy = {'editable_parameters': ['file', 'data_file'], 'editable_bindings': ['clean-sop.input.data_file']}
    with pytest.raises(ValueError, match='not editable'):
        apply_parameter_patch(package, {'bindings': {'clean-sop.clean.input.data_file': 'params.file'}}, policy)
    with pytest.raises(ValueError, match='preserve existing'):
        apply_parameter_patch(package, {'declarations': {'data_file': None}}, policy)
    with pytest.raises(ValueError, match='require defaults'):
        apply_parameter_patch(package, {'declarations': {'file': {'type': 'path'}}}, policy)
    policy['editable_bindings'].append('clean-sop.input.file')
    apply_parameter_patch(package, {'declarations': {'file': {'type': 'path', 'default': 'fallback.csv'}}, 'bindings': {'clean-sop.input.file': 'params.file'}}, policy)
    after = compile(package)
    assert after.params == frozenset({'file', 'data_file'})
    assert [(r.kind, r.value) for r in after.node.verify] == [(r.kind, r.value) for r in before.node.verify]
    assert [c.do for c in after.node.children] == [c.do for c in before.node.children]
    from agentgenome.service import GenomeService
    asset = json.loads((package / 'manifest.json').read_text())
    assert GenomeService.validate_params(asset, {'data_file': 'old.csv'}) == {'data_file': 'old.csv', 'file': 'fallback.csv'}


def test_legacy_policy_allows_optional_parameter_without_breaking_old_calls(tmp_path):
    from agentgenome.service import GenomeService
    source = Path(__file__).resolve().parents[1] / 'templates/data-cleaning'
    service = GenomeService(tmp_path / 'registry')
    service.import_asset(source)
    state = {'asset_id': 'data-cleaning', 'version': '1.0.0'}
    policy = service.revisions.legacy_policy(state, ['scripts/clean.py'], ['scripts/verify_report.py'])
    package = tmp_path / 'copy'
    shutil.copytree(source, package)
    patch = {'declarations': {'delimiter': {'type': 'string', 'default': ','}},
             'bindings': {'clean-sop.input.delimiter': 'params.delimiter'}}
    apply_parameter_patch(package, patch, policy)
    manifest = json.loads((package / 'manifest.json').read_text())
    assert GenomeService.validate_params(manifest, {'data_file': 'old.csv'}) == {'data_file': 'old.csv', 'delimiter': ','}
    assert compile(package).params == frozenset({'data_file', 'delimiter'})
    with pytest.raises(ValueError, match='not editable'):
        apply_parameter_patch(package, {'bindings': {'missing.input.x': 'params.delimiter'}}, policy)
