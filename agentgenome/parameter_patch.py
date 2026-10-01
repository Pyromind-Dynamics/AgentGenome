"""Apply explicitly permitted input declarations and bindings, never graph structure."""
import json
from pathlib import Path

import yaml


def apply_parameter_patch(package: Path, patch: dict, policy: dict) -> None:
    if set(patch) - {'declarations', 'bindings'}:
        raise ValueError('unsupported parameter patch')
    declarations, bindings = patch.get('declarations', {}), patch.get('bindings', {})
    if not isinstance(declarations, dict) or not isinstance(bindings, dict):
        raise ValueError('invalid parameter patch')
    manifest = json.loads((package / 'manifest.json').read_text())
    original = manifest['parameters']
    permitted = set(policy.get('editable_parameters', []))
    if policy.get('allow_new_parameters'):
        permitted.update(set(declarations) - set(original))
    if set(declarations) - permitted:
        raise ValueError('parameter declaration is not editable')
    for key, spec in declarations.items():
        if key in original and (not isinstance(spec, dict) or spec.get('type') != original[key]['type'] or spec.get('default') != original[key].get('default')):
            raise ValueError('revision must preserve existing parameter types and defaults')
        if key not in original and (not isinstance(spec, dict) or 'default' not in spec):
            raise ValueError('new parameters require defaults for old callers')
        if spec is None:
            manifest['parameters'].pop(key, None)
            continue
        if not isinstance(spec, dict) or spec.get('type') not in {'path', 'string', 'number', 'boolean'}:
            raise ValueError('invalid parameter declaration')
        manifest['parameters'][key] = spec
    graph = yaml.safe_load((package / 'graph.yaml').read_text())
    nodes = {}
    def index(node, parent=''):
        path = f"{parent}.{node['id']}" if parent else node['id']
        nodes[path] = node
        for child in node.get('children', []):
            index(child, path)
    index(graph['node'])
    permitted_bindings = set(policy.get('editable_bindings', []))
    if policy.get('allow_new_parameters'):
        permitted_bindings.update(f"{graph['node']['id']}.input.{key}" for key in declarations if key not in json.loads((package / 'manifest.json').read_text())['parameters'])
    if set(bindings) - permitted_bindings:
        raise ValueError('input binding is not editable')
    for target, source in bindings.items():
        node_path, key = target.rsplit('.input.', 1)
        node = nodes[node_path]
        if not isinstance(source, str) or not source.startswith('params.') or source[7:] not in manifest['parameters']:
            raise ValueError('binding must name a declared parameter')
        node.setdefault('input', {})[key] = {'type': {'string': 'text', 'boolean': 'bool'}.get(manifest['parameters'][source[7:]]['type'], manifest['parameters'][source[7:]]['type']), 'from': source}
    (package / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False))
    (package / 'graph.yaml').write_text(yaml.safe_dump(graph, sort_keys=False, allow_unicode=True))
