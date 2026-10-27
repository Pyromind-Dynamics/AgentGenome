"""Cross-task regressions: IO provenance, input revisions and plan repair."""
import json
from pathlib import Path

import pytest

from agentgenome.capture.distill import (
    DistillError, _check_chain, _check_llm_promotions, _generated_root_issues,
    _handover_steps, _review_scope, _slice_boundary_issues, draft_distill,
    plan_steps,
)
from agentgenome.capture.plan import build_candidate_plan
from agentgenome.capture.trace import capture_trace, parse_command
from agentgenome.dispatch import invoke
from agentgenome.local_host import LocalExecutionHost
from core import Answer


@pytest.mark.parametrize('command,reads,writes,slices', [
    ('python3 convert.py --input data/a.parquet --out out/b.csv && head -5 out/b.csv',
     ('convert.py', 'data/a.parquet'), ('out/b.csv',), ()),
    ('python3 render.py --out out/image.png | head -40', ('render.py',), ('out/image.png',), ()),
    ('cat data/items.jsonl | head -3 | tee out/subset.jsonl',
     ('data/items.jsonl',), ('out/subset.jsonl',), ('out/subset.jsonl',)),
    ('head -2 data/items.csv > out/slice.csv && python3 save.py --out out/full.csv',
     ('data/items.csv', 'save.py'), ('out/slice.csv', 'out/full.csv'), ('out/slice.csv',)),
    ('python3 transform.py --input out/x.csv --out out/x.csv',
     ('transform.py', 'out/x.csv'), ('out/x.csv',), ()),
    ('python3 render.py --title "head image" --out out/photo.png',
     ('render.py',), ('out/photo.png',), ()),
    ('head -c 8 data/raw.bin > out/prefix.bin',
     ('data/raw.bin',), ('out/prefix.bin',), ('out/prefix.bin',)),
    ('python3 transform.py --out ./out/table.tsv && head -5 out/table.tsv',
     ('transform.py',), ('./out/table.tsv',), ()),
])
def test_shell_products_follow_their_own_commands(command, reads, writes, slices):
    parsed = parse_command(command)
    assert parsed.read_paths == reads
    assert parsed.write_paths == writes
    assert parsed.slice_paths == slices


def test_preview_is_not_a_slice_but_a_real_slice_root_still_blocks(trace_factory, tool_entry):
    preview = trace_factory([tool_entry('a', 'python3 build.py --out out/full.json && head -3 out/full.json')])
    assert _slice_boundary_issues([preview], {'source': 'out/full.json'}) == []
    sliced = trace_factory([tool_entry('b', 'cat data/raw.json | head -2 > out/small.json')])
    assert _slice_boundary_issues([sliced], {'source': 'out/small.json'})
    steps = plan_steps([{'run': 'head -2 {source} > {subset}'}])
    assert _slice_boundary_issues([sliced], {'source': 'data/raw.json', 'subset': 'out/small.json'}, steps) == []
    mixed = trace_factory([tool_entry('c','python3 convert.py --input data/other.csv --out out/full.csv '
                                      '&& head -3 data/selected.csv > out/slice.csv')])
    issue = _slice_boundary_issues([mixed],{'source':'out/slice.csv'})[0]
    assert '源文件 `data/selected.csv`' in issue


@pytest.mark.parametrize('created,read_fails,expected', [(False,False,False), (True,False,True), (False,True,True)])
def test_revised_user_input_is_distinct_from_generated_data(trace_factory, tool_entry, created, read_fails, expected):
    entries = []
    if created:
        entries.append(tool_entry('create', '', tool='write', args={'path':'config/schema.toml'}))
    read = tool_entry('read', '', tool='read', args={'path':'config/schema.toml'})
    if read_fails:
        read['result']['is_error'] = True
    entries += [read, tool_entry('edit','',tool='edit',args={'path':'config/schema.toml'})]
    trace = trace_factory(entries)
    issues = _generated_root_issues([trace], {'schema':'config/schema.toml'}, ())
    assert bool(issues) is expected
    assert ('config/schema.toml' in trace.revised_inputs) is not expected


@pytest.mark.parametrize('parameterized',[False,True])
def test_revised_input_remains_a_root_through_the_full_chain_check(trace_factory,tool_entry,parameterized):
    trace = trace_factory([tool_entry('r','',tool='read',args={'path':'config/mapping.json'}),
                           tool_entry('e','',tool='edit',args={'path':'config/mapping.json'})])
    source = '{mapping}' if parameterized else 'config/mapping.json'
    steps = plan_steps([{'run':f'python3 convert.py --config {source} > out/result.tsv'}])
    boundaries = _check_chain([trace],steps,{'mapping':'config/mapping.json'} if parameterized else None)
    assert len(boundaries) == 1
    assert '首次修改前已读取' in boundaries[0][1]


def test_shell_readback_can_establish_an_existing_input_before_edit(trace_factory,tool_entry):
    trace = trace_factory([tool_entry('r','cat config/aliases.yaml | head -20'),
                           tool_entry('e','',tool='edit',args={'path':'./config/aliases.yaml'})])
    assert trace.revised_inputs == ('config/aliases.yaml',)


def test_draft_keeps_unrelated_batch_in_branches_not_scope_warnings(trace_factory, tool_entry):
    trace = trace_factory([
        tool_entry('a','python3 scripts/summarize.py data/a.json > out/a.txt'),
        tool_entry('b','python3 scripts/summarize.py data/b.json > out/b.txt'),
        {'kind':'user','text':'请按 config/schema.toml 处理 data/b.json，完成后给我报告'},
    ])
    draft = draft_distill(label='summarize', steps=[{'run':'python3 scripts/summarize.py data/b.json > out/b.txt'}],
                          traces=[trace], final_artifact='out/b.txt')
    assert len(draft.branches) == 1
    assert not draft.scope_warnings
    assert _review_scope(draft.steps, [trace], None, False, None)[1] is None


def agent_trace():
    return capture_trace(json.dumps({'trace_version':1, 'trace_id':'semantic', 'steps':[
        {'seq':0,'kind':'tool','tool':'write','args':{'path':'out/summary.txt','content':'ORIGINAL BYTES MUST NOT REPLAY'},
         'agent_context':{'complete':True,'instruction':'Summarize the supplied document',
                          'intent':'Read the document and write a summary', 'inputs':['data/document.txt']},
         'result':{'text':'written','is_error':False}}
    ]}))


def test_semantic_writer_has_a_replay_task_not_literal_bytes():
    trace = agent_trace()
    candidate = build_candidate_plan([trace], final_artifact='out/summary.txt')
    assert candidate.steps[0]['llm']
    assert 'ORIGINAL BYTES' not in json.dumps(candidate.steps)
    with pytest.raises(DistillError, match='origin_reason'):
        _check_llm_promotions(plan_steps(candidate.steps), [trace], 'scripts-first', candidate.params)
    steps = plan_steps([{'llm':'Summarize data/document.txt into out/summary.txt',
                         'input':['data/document.txt'], 'output':'out/summary.txt',
                         'verify':'test -s out/summary.txt', 'from':'semantic:0',
                         'origin_reason':'The user requested a summary; the model chose its wording.'}])
    _check_llm_promotions(steps, [trace], 'scripts-first')
    for change in [{'verify':None}, {'input':['data/unobserved.txt']}, {'output':'out/other.txt'}]:
        node = {'llm':'Summarize the document','input':['data/document.txt'],
                'output':'out/summary.txt','verify':'test -s out/summary.txt','from':'semantic:0', **change}
        with pytest.raises(DistillError):
            _check_llm_promotions(plan_steps([node]), [trace], 'scripts-first')


@pytest.mark.parametrize('operation,complete',[('edit',True),('write',False)])
def test_editor_changes_and_incomplete_context_cannot_be_promoted(operation,complete):
    from dataclasses import replace
    trace = agent_trace()
    old = trace.steps[0]
    trace = replace(trace,steps=(replace(old,tool_name=operation,
                        agent_context={**old.agent_context,'complete':complete}),))
    steps = plan_steps([{'llm':'Summarize data/document.txt into out/summary.txt',
                        'input':['data/document.txt'],'output':'out/summary.txt',
                        'verify':'test -s out/summary.txt','from':'semantic:0',
                        'origin_reason':'The model wrote the requested summary.'}])
    with pytest.raises(DistillError):
        _check_llm_promotions(steps,[trace],'scripts-first')


def test_plan_revision_is_scoped_idempotent_and_invalidates_old_confirmation(service):
    store = service.distill_plans
    analysis = {'review':{'blocking_issues':['issue'], 'validation':{'ready':False}}}
    first = store.create(scope='s',request_id='a',arguments={'label':'generic'},analysis=analysis)
    second = store.create(scope='s',request_id='b',arguments={'label':'fixed'},analysis=analysis,
                          plan_id=first['plan_id'],base_digest=first['digest'])
    assert second['plan_id'] == first['plan_id']
    assert second['revision'] == 2
    assert second['digest'] != first['digest']
    retried = store.create(scope='s',request_id='b',arguments={'label':'fixed'},analysis=analysis,
                           plan_id=first['plan_id'],base_digest=first['digest'])
    assert retried['digest'] == second['digest']
    assert retried['revision'] == 2
    with pytest.raises(ValueError, match='digest changed'):
        store.begin_confirmation('s',first['plan_id'],digest=first['digest'])
    with pytest.raises(ValueError, match='digest changed'):
        store.create(scope='s',request_id='c',arguments={},analysis=analysis,
                     plan_id=first['plan_id'],base_digest=first['digest'])
    with pytest.raises(ValueError, match='not found'):
        store.create(scope='other',request_id='x',arguments={},analysis=analysis,
                     plan_id=first['plan_id'],base_digest=second['digest'])
    assert len(store.previews('s')) == 1
    assert store.previews('other') == []


def test_same_error_does_not_hide_a_changed_plan(service):
    analysis = {'review':{'blocking_issues':['same issue']}}
    a = service.distill_plans.create(scope='s',request_id='a',arguments={'source':'a.csv'},analysis=analysis)
    b = service.distill_plans.create(scope='s',request_id='b',arguments={'source':'b.png'},analysis=analysis)
    assert a['plan_id'] != b['plan_id']
    assert b['arguments']['source'] == 'b.png'


def test_dispatch_queries_and_revises_without_creating_capture(service, host, pipeline_workspace):
    args = {'label':'table export', 'steps':[{'run':'python3 scripts/clean.py {source} > out/table.csv'}],
            'params':{'source':'out/review.jsonl'}, 'final_artifact':'out/table.csv',
            'validation_params':{'source':'out/review.jsonl'}, 'execution_scope':{'mode':'sample'}}
    first = invoke(service,'s','distill',args,'a',host=host)
    current = invoke(service,'s','distill',{'plan_id':first['plan_id']},'query')
    assert current['digest'] == first['digest']
    revised = invoke(service,'s','distill',{**args,'label':'revised export','plan_id':first['plan_id'],
                                          'digest':first['digest']},'b',host=host)
    assert revised['plan_id'] == first['plan_id']
    assert revised['revision'] == 2
    assert len(invoke(service,'s','list',{},'list')['distill_plans']) == 1
    stored = service.distill_plans.get('s',first['plan_id'])
    assert stored['capture_id'] is None and stored['run_id'] is None
    with pytest.raises(ValueError,match='digest'):
        invoke(service,'s','confirm_distill',{'plan_id':first['plan_id'],'digest':first['digest']},'old',host=host)


def test_mechanical_candidate_keeps_semantic_producer_and_script_consumer(service,host,pipeline_workspace):
    document = {'trace_version':1,'trace_id':'mixed','steps':[
        {'seq':0,'kind':'tool','tool':'write','args':{'path':'out/answers.jsonl'},
         'agent_context':{'complete':True,'instruction':'Extract structured records',
                          'intent':'Read the input and extract fields','inputs':['data/raw.csv']},
         'result':{'is_error':False,'text':'written'}},
        {'seq':1,'kind':'tool','tool':'bash',
         'args':{'command':'python3 scripts/clean.py out/answers.jsonl > out/table.csv && head -3 out/table.csv'},
         'result':{'is_error':False,'text':'preview'}}]}
    pipeline_workspace.write('mixed.json',json.dumps(document))
    plan = invoke(service,'s','distill',{'label':'extract and export',
                  'trace_refs':[{'trace_id':'mixed','path':'mixed.json'}],
                  'final_artifact':'out/table.csv','execution_scope':{'mode':'sample'},
                  'validation_params':{}},'candidate',host=host)
    assert plan['planning']['mode'] == 'mechanical_candidate'
    assert [step['kind'] for step in plan['review']['steps']] == ['llm','run']
    assert not plan['review']['validation']['ready']
    assert any('origin_reason' in issue for issue in plan['blocking_issues'])
    revised = invoke(service, 's', 'distill', {
        'plan_id': plan['plan_id'], 'digest': plan['digest'], 'label': 'extract and export',
        'trace_refs': [{'trace_id': 'mixed', 'path': 'mixed.json'}],
        'final_artifact': 'out/table.csv', 'execution_scope': {'mode': 'sample'},
        'validation_params': {}, 'params': {'source': 'data/raw.csv'},
        'steps': [{'llm': 'Extract records from {source} into out/answers.jsonl',
                   'input': ['{source}'], 'output': 'out/answers.jsonl', 'from': 'mixed:0',
                   'verify': 'test -s out/answers.jsonl',
                   'origin_reason': 'The user requested extraction; the model selected the records.'},
                  {'run': 'python3 scripts/clean.py out/answers.jsonl > out/table.csv'}],
    }, 'classified', host=host)
    assert revised['plan_id'] == plan['plan_id']
    assert revised['review']['validation']['ready'], revised['blocking_issues']


def test_evidenced_semantic_task_replays_then_parks_and_resumes_at_human_gate(service, pipeline_workspace):
    """Exercise the graph runtime, not equivalent handwritten shell commands.

    The model/human ports are deterministic test doubles; the actual compiler,
    checkpoints, artifact checks and publication path execute in a temporary registry.
    """
    class Reviewer:
        calls = 0

        def ask(self, question, evidence):
            self.calls += 1
            if self.calls == 1:
                return Answer(False, pending=True)
            Path(evidence['artifact']).write_text('{"text":"approved", "label":"a"}\n')
            return Answer(True)

        def capabilities(self):
            return {'interactive':True}

    human = Reviewer()
    host = LocalExecutionHost(service.root,pipeline_workspace.root,human=human)
    document = {'trace_version':1,'trace_id':'semantic','steps':[
        {'seq':0,'kind':'tool','tool':'write','args':{'path':'out/answers.jsonl'},
         'agent_context':{'complete':True,'instruction':'Extract the records',
                          'intent':'Read the source and produce structured records', 'inputs':['data/raw.csv']},
         'result':{'is_error':False,'text':'written'}}]}
    pipeline_workspace.write('trace.json',json.dumps(document))
    args = {'label':'extract review export','asset_id':'extract-review-export',
            'trace_refs':[{'trace_id':'semantic','path':'trace.json'}], 'source_traces':['semantic'],
            'steps':[{'llm':'Read {source} and extract records into out/answers.jsonl',
                      'input':['{source}'],'output':'out/answers.jsonl','verify':'test -s out/answers.jsonl',
                      'from':'semantic:0',
                      'origin_reason':'The user requested extraction; the model selected the records.'},
                     {'human':'Review out/answers.jsonl and save approved records to out/review.jsonl',
                      'input':['out/answers.jsonl'],'output':'out/review.jsonl'},
                     {'run':'python3 scripts/clean.py out/review.jsonl > out/table.csv'}],
            'params':{'source':'data/raw.csv'},'validation_params':{'source':'data/raw.csv'},
            'execution_scope':{'mode':'sample'},'final_artifact':'out/table.csv'}
    planned = invoke(service,'s','distill',args,'plan',host=host)
    assert planned['review']['validation']['ready'], planned['blocking_issues']
    confirmed = invoke(service,'s','confirm_distill',{'plan_id':planned['plan_id'],'digest':planned['digest']},
                       'confirm',host=host)

    def model(event):
        if event.get('event') == 'waiting_agent':
            request = event['request']
            output = Path(request['expected_outputs'][0])
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text('{"text":"extracted", "label":"a"}\n')
            service.stages.receipt('s',request['request_id'],'completed','done','round')
            return {'status':'completed','execution_id':'round'}

    parked = service.execute('s',confirmed['run_id'],host,model)
    assert parked['status'] == 'waiting', parked['result']
    assert service.get(confirmed['asset_id'],confirmed['version'])['published'] is False
    service.resume('s',confirmed['run_id'])
    finished = service.execute('s',confirmed['run_id'],host,model)
    assert finished['status'] == 'succeeded', finished.get('result')
    assert human.calls == 2
    assert service.get(confirmed['asset_id'],confirmed['version'])['published'] is True


# --- P1-A：确认必须绑定用户实际审阅的 digest ----------------------------------------


def _ready_analysis():
    return {'review': {'blocking_issues': [], 'validation': {'ready': True}}}


def test_confirmation_requires_the_current_digest_even_for_the_first_revision(service):
    store = service.distill_plans
    first = store.create(scope='s', request_id='a', arguments={'label': 'generic'},
                         analysis={'review': {'blocking_issues': ['issue']}})
    second = store.create(scope='s', request_id='b', arguments={'label': 'fixed'},
                          analysis=_ready_analysis(),
                          plan_id=first['plan_id'], base_digest=first['digest'])
    assert second['revision'] == 2
    # 省略 digest：不能证明用户看过当前版本。
    with pytest.raises(ValueError, match='digest'):
        store.begin_confirmation('s', first['plan_id'])
    # 旧版本的 digest：同样拒绝。
    with pytest.raises(ValueError, match='digest'):
        store.begin_confirmation('s', first['plan_id'], digest=first['digest'])
    assert store.get('s', first['plan_id'])['status'] == 'planned'
    state = store.begin_confirmation('s', first['plan_id'], digest=second['digest'])
    assert state['status'] == 'confirming'
    store.release_confirmation('s', first['plan_id'])
    assert store.get('s', first['plan_id'])['status'] == 'planned'


def test_confirm_distill_without_the_reviewed_digest_changes_nothing(
        service, host, pipeline_workspace):
    args = {'label': 'table export',
            'steps': [{'run': 'python3 scripts/clean.py {source} > out/table.csv'}],
            'params': {'source': 'out/review.jsonl'}, 'final_artifact': 'out/table.csv',
            'validation_params': {'source': 'out/review.jsonl'},
            'execution_scope': {'mode': 'sample'}}
    plan = invoke(service, 's', 'distill', args, 'a', host=host)
    for bad in ({}, {'digest': 'stale'}):
        with pytest.raises(ValueError, match='digest'):
            invoke(service, 's', 'confirm_distill',
                   {'plan_id': plan['plan_id'], **bad}, 'confirm', host=host)
        assert service.distill_plans.get('s', plan['plan_id'])['status'] == 'planned'
    assert not (service.root / 'captures').exists()


# --- P1-B：用户口述的决定不是模型裁量 ----------------------------------------------


def _writer_trace(trace_id, instruction, content, origin, *, inputs=None):
    steps = [{'seq': index, 'kind': 'tool', 'tool': 'read',
              'args': {'path': path}, 'result': {'is_error': False, 'text': 'items'}}
             for index, path in enumerate(inputs or [])]
    context = {'complete': True, 'instruction': instruction,
               'intent': 'Record the task result', 'inputs': list(inputs or [])}
    if origin is not None:
        context['origin'] = origin
    steps.append({'seq': len(steps), 'kind': 'tool', 'tool': 'write',
                  'args': {'path': 'out/decisions.json', 'content': content},
                  'agent_context': context,
                  'result': {'is_error': False, 'text': 'written'}})
    return capture_trace(json.dumps(
        {'trace_version': 1, 'trace_id': trace_id, 'steps': steps}))


DICTATED_INSTRUCTION = ('I have manually reviewed the records. '
                        'Keep item 1 and reject item 2; '
                        'save my decisions to out/decisions.json.')
DICTATED_CONTENT = '{"records": [{"item": 1, "decision": "keep"}, {"item": 2, "decision": "reject"}]}'


def test_reviewed_manual_writer_becomes_a_human_gate():
    trace = _writer_trace('dictated', DICTATED_INSTRUCTION, DICTATED_CONTENT,
                          {'records': 2, 'dictated': 2, 'single_value': 0},
                          inputs=['data/review-queue.json'])
    node = {'human': 'Review data/review-queue.json and save your decisions to out/decisions.json',
            'input': ['data/review-queue.json'], 'output': 'out/decisions.json',
            'from': 'dictated:1'}
    _check_llm_promotions(plan_steps([node]), [trace], 'scripts-first')
    draft = draft_distill(label='manual decisions', steps=[node], traces=[trace],
                          final_artifact='out/decisions.json')
    assert len(draft.review({})['human_gates']) == 1


def test_reviewed_model_writer_keeps_its_reason_and_declares_every_input():
    trace = _writer_trace('generated', 'Label each record as spam or ham',
                          '{"1": "spam", "2": "ham"}', None,
                          inputs=['data/review-queue.json'])
    node = {'llm': 'Classify data/review-queue.json into out/decisions.json',
            'input': ['data/review-queue.json'], 'output': 'out/decisions.json',
            'verify': 'test -s out/decisions.json', 'from': 'generated:1',
            'origin_reason': 'The user supplied categories; the model chose each record label.'}
    steps = plan_steps([node])
    _check_llm_promotions(steps, [trace], 'scripts-first')
    assert steps[0].payload()['origin_reason'] == node['origin_reason']
    draft = draft_distill(label='classify', steps=[node], traces=[trace],
                          final_artifact='out/decisions.json')
    assert draft.review({})['steps'][0]['origin_reason'] == node['origin_reason']
    changed = draft_distill(label='classify', steps=[{**node, 'origin_reason': 'Different basis'}],
                            traces=[trace], final_artifact='out/decisions.json')
    assert changed.digest != draft.digest
    with pytest.raises(DistillError, match='data/review-queue.json'):
        _check_llm_promotions(plan_steps([{**node, 'input': []}]), [trace], 'scripts-first')


# --- P2-D：人工交接按用户点名的材料选上游 -------------------------------------------


def _handover_trace(entries):
    steps = list(entries)
    steps.append({'kind': 'tool', 'tool': 'bash',
                  'args': {'command': 'python3 scripts/clean.py out/review.jsonl > out/table.csv'},
                  'result': {'is_error': False, 'text': 'ok'}})
    return capture_trace(json.dumps(
        {'trace_version': 1, 'trace_id': 'session-1', 'steps': steps}))


def test_handover_follows_the_material_the_user_named(trace_factory, tool_entry):
    trace = _handover_trace([
        tool_entry('p', 'python3 scripts/prepare.py data/raw.csv --out out/prepared.jsonl'),
        tool_entry('d', 'echo diagnostic > out/probe.txt'),
        {'kind': 'user', 'text': 'I have reviewed out/prepared.jsonl '
                                 'and exported out/review.jsonl.'},
    ])
    planned = plan_steps([{'run': 'python3 scripts/clean.py out/review.jsonl > out/table.csv'}])
    handover = _handover_steps(planned, [trace], None)
    # 用户点名的是 prepared.jsonl 的来源，不是距离最近的 probe 诊断。
    assert [step.seq for _, step in handover] == [0]


def test_handover_keeps_every_named_material_and_its_ancestors(trace_factory, tool_entry):
    trace = _handover_trace([
        tool_entry('a', 'python3 scripts/prepare.py data/a.csv --out out/a.jsonl'),
        tool_entry('b', 'python3 scripts/prepare.py data/b.csv --out out/b.jsonl'),
        tool_entry('c', 'echo diagnostic > out/probe.txt'),
        {'kind': 'user', 'text': 'I have reviewed out/a.jsonl and out/b.jsonl '
                                 'and exported out/review.jsonl.'},
    ])
    planned = plan_steps([{'run': 'python3 scripts/clean.py out/review.jsonl > out/table.csv'}])
    handover = _handover_steps(planned, [trace], None)
    assert [step.seq for _, step in handover] == [0, 1]


def test_handover_without_a_named_material_lists_the_candidates(trace_factory, tool_entry):
    trace = _handover_trace([
        tool_entry('p', 'python3 scripts/prepare.py data/raw.csv --out out/prepared.jsonl'),
        tool_entry('d', 'echo diagnostic > out/probe.txt'),
        {'kind': 'user', 'text': 'I have reviewed everything '
                                 'and exported out/review.jsonl.'},
    ])
    planned = plan_steps([{'run': 'python3 scripts/clean.py out/review.jsonl > out/table.csv'}])
    handover = _handover_steps(planned, [trace], None)
    # 点名不了具体材料时全部列为候选，交由用户复核，而不是猜最近的一个。
    assert [step.seq for _, step in handover] == [0, 1]


# --- P2-E：request_id 只能代表一个创建/修订操作 -------------------------------------


@pytest.mark.parametrize('instruction,content,origin', [
    ('For records 101 and 102, choose keep or reject using data/items.json',
     '[{"id":101,"decision":"keep"},{"id":102,"decision":"reject"}]',
     {'records': 2, 'dictated': 2, 'single_value': 0}),
    ('For every row in data/items.json, my decision is approve',
     '[{"id":101,"decision":"approve"}]',
     {'records': 1, 'dictated': 0, 'single_value': 0}),
    ('I approve this request; record my decision', '["approve"]',
     {'records': 1, 'dictated': 0, 'single_value': 1}),
    ('Keep item 1 and classify the rest yourself',
     '[{"id":1,"decision":"keep"},{"summary":"spam"}]',
     {'records': 2, 'dictated': 1, 'single_value': 1}),
    ('Summarize the input document', 'A summary', None),
])
def test_writer_requires_reviewed_classification(instruction, content, origin):
    trace = _writer_trace('writer', instruction, content, origin,
                          inputs=['data/items.json'])
    candidate = build_candidate_plan([trace], final_artifact='out/decisions.json')
    assert not any('human' in step for step in candidate.steps)
    for policy in ('scripts-first', 'adaptive-first'):
        with pytest.raises(DistillError, match='origin_reason'):
            _check_llm_promotions(plan_steps(candidate.steps), [trace], policy, candidate.params)


@pytest.mark.parametrize('prefix', [
    'true || python3 produce.py --out data/existing.json',
    'true && true || python3 produce.py --out data/existing.json',
])
def test_mixed_conditional_chain_keeps_external_reads(prefix):
    command = prefix + ' && python3 consume.py --input data/existing.json --out out/result.json'
    assert 'data/existing.json' in parse_command(command).read_paths


@pytest.mark.parametrize('input_path,diagnostic', [
    ('./out/base.jsonl', 'out/probe.txt'),
    ('out/base.jsonl', 'out/base.jsonl'),
])
def test_handover_ancestors_follow_canonical_paths_and_time(tool_entry, input_path, diagnostic):
    trace = _handover_trace([
        tool_entry('a', 'python3 scripts/prepare.py data/raw.csv --out out/base.jsonl'),
        tool_entry('b', f'python3 scripts/enrich.py {input_path} --out out/prepared.jsonl'),
        tool_entry('d', f'echo diagnostic > {diagnostic}'),
        {'kind': 'user', 'text': 'I have reviewed out/prepared.jsonl and exported out/review.jsonl.'},
    ])
    planned = plan_steps([{'run': 'python3 scripts/clean.py out/review.jsonl > out/table.csv'}])
    assert [step.seq for _, step in _handover_steps(planned, [trace], None)] == [0, 1]


def _blocked_analysis():
    return {'review': {'blocking_issues': ['issue']}}


def test_one_request_id_cannot_create_one_plan_and_revise_another(service):
    store = service.distill_plans
    a = store.create(scope='s', request_id='one', arguments={'label': 'a'}, analysis=_blocked_analysis())
    b = store.create(scope='s', request_id='two', arguments={'label': 'b'}, analysis=_blocked_analysis())
    with pytest.raises(ValueError, match='request id'):
        store.create(scope='s', request_id='one', arguments={'label': 'b revised'},
                     analysis=_ready_analysis(), plan_id=b['plan_id'], base_digest=b['digest'])
    assert store.get('s', b['plan_id'])['revision'] == 1
    assert store.get('s', a['plan_id'])['revision'] == 1


def test_legacy_create_request_still_blocks_reuse_after_upgrade(service):
    store = service.distill_plans
    a = store.create(scope='s', request_id='one', arguments={'label': 'a'}, analysis=_blocked_analysis())
    b = store.create(scope='s', request_id='two', arguments={'label': 'b'}, analysis=_blocked_analysis())
    # 升级前创建的请求只记录在 distill_plans 里：删掉回执模拟旧库。
    with service._db() as db:
        db.execute('DELETE FROM distill_plan_requests WHERE scope=? AND request_id=?', ('s', 'one'))
    with pytest.raises(ValueError, match='request id'):
        store.create(scope='s', request_id='one', arguments={'label': 'b revised'},
                     analysis=_ready_analysis(), plan_id=b['plan_id'], base_digest=b['digest'])


def test_request_receipts_are_scoped_and_replay_their_own_result(service):
    store = service.distill_plans
    a = store.create(scope='s', request_id='one', arguments={'label': 'a'}, analysis=_blocked_analysis())
    other = store.create(scope='other', request_id='one', arguments={'label': 'other'},
                         analysis=_blocked_analysis())
    assert other['plan_id'] != a['plan_id']
    # 同一请求重发返回当时的回执，不重复增加 revision，也不冒充最新版。
    again = store.create(scope='s', request_id='one', arguments={'label': 'a'},
                         analysis=_blocked_analysis())
    assert again['plan_id'] == a['plan_id']
    assert again['revision'] == 1
    revised = store.create(scope='s', request_id='two', arguments={'label': 'fixed'},
                           analysis=_ready_analysis(), plan_id=a['plan_id'],
                           base_digest=a['digest'])
    replay = store.create(scope='s', request_id='one', arguments={'label': 'a'},
                          analysis=_blocked_analysis())
    assert replay.get('repeated') is True
    assert replay['revision'] == 1
    assert store.get('s', a['plan_id'])['revision'] == 2
