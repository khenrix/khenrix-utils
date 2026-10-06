"""Immutable guest dummy admission; native dummies and broker auth stay variable."""
from dataclasses import asdict
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_fanout_execute_skill import _NeverMemory, _registry, _repository, fanout
from test_fanout_quality_case import _execute, _declared_container_backend, HELPER, ROOT


def _case_fixture(tmp_path, monkeypatch, *, backend, placeholders):
    from test_fanout_container_upstream import _route
    quality_case = importlib.import_module(fanout.__name__ + '.quality_case')
    quality = importlib.import_module(fanout.__name__ + '.quality')
    diagnostic = importlib.import_module(fanout.__name__ + '.diagnostic')
    BrokerPermit = importlib.import_module(fanout.__name__ + '.broker').BrokerPermit
    runtime_source_digest = importlib.import_module(fanout.__name__ + '.container_source').runtime_source_digest
    helper = _execute()
    spec = importlib.util.spec_from_file_location('_quality_placeholder_planner',
        ROOT / 'shared/skills/llm-fanout-plan/scripts/plan.py')
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    book_bytes = (ROOT / 'evals/llm-fanout/quality-cases.json').read_bytes()
    # The redistributable casebook is not the owner's separately pinned original.
    monkeypatch.setattr(quality_case, '_CASEBOOK', hashlib.sha256(book_bytes).hexdigest())
    question = quality.case_prompt(json.loads(book_bytes), 'normal-01')
    source = planner._question_source(question, 'normal').encode()
    draft = planner._question_draft(source, (), 'Standalone original fixture.',
        ('claude', 'codex', 'agy'), 'normal')
    draft['tasks'][0]['reconciliation_policy'] = 'synthesis-required'
    skills = tmp_path / 'skills'
    skills.mkdir()
    compiled = planner._compile(fanout, source, 'fanout-inputs/question.md',
        fanout.canonical_json(draft), [str(skills)])
    packet = planner._ingress_packet(fanout, kind='question', source=source.decode(), draft=draft,
        compiled=compiled, admission={'kind': 'direct-question', 'approval': 'invocation-authorized',
            'quality_tier': 'normal'})
    private = tmp_path / 'private'
    private.mkdir(mode=0o700)
    no_provider = lambda *_args, **_kwargs: pytest.fail('provider launched')
    common = dict(runtime=fanout, registry=_registry(), memory_factory=_NeverMemory,
        memory_preflight=lambda: True, provider_runner=no_provider)
    helper.prepare_run(packet, repo_root=_repository(tmp_path), run_root=tmp_path / 'run',
        authority_root=tmp_path / 'authority', private_root=private, skill_roots=(skills,), budget=6, **common)
    opened = helper.open_run(private, **common)
    service, store, controller = opened.service, opened.artifacts, opened.service.lifecycle_controller
    task_id = next(iter(service.preparations))
    preparation = service.preparations[task_id]
    book_ref = store.write_bytes('quality/original-casebook.json', book_bytes)
    input_ref = store.write_json('quality/input.json', {'schema': 'fanout-quality-case-input-v1',
        'casebook': quality_case._CASEBOOK, 'case_id': 'normal-01', 'prompt': question})
    catalog_ref = store.write_json('quality/synthetic-catalog.json', {'synthetic': True})
    catalog = {'original': asdict(catalog_ref), 'projected': asdict(catalog_ref)}
    value = dict.fromkeys(quality_case._FIELDS)
    value.update(schema='fanout-quality-installed-case-attachment-v1', case_id='normal-01',
        run_id=service.inputs.run_id, task_id=task_id, target_id='quality-normal-01',
        condition='candidate', execution_class='read-only', inputs_digest=service.inputs.digest,
        casebook=asdict(book_ref), case_input=asdict(input_ref), catalog=catalog,
        manifest=str(tmp_path / 'synthetic-manifest'), policy=str(tmp_path / 'synthetic-policy'),
        manifest_digest='a' * 64, policy_digest='b' * 64, installation={'synthetic': True})
    if backend == 'container-read-only':
        value.update(schema='fanout-quality-installed-case-attachment-v2', backend=backend,
            container={'image_digest': 'fanout-seat@sha256:' + 'c' * 64,
                'cli_digests': dict.fromkeys(('claude', 'codex', 'agy'), 'd' * 64),
                'docker_controls': ['e' * 64 + '.json', 'e' * 64]})
    bindings = {'campaign': 'a' * 64, 'casebook': book_ref.digest, 'source': runtime_source_digest(),
        'projection': hashlib.sha256(fanout.canonical_json(quality_case._quality_input_projection(value))).hexdigest(),
        'profile': hashlib.sha256(fanout.canonical_json(dict(service.inputs.provider_profiles))).hexdigest(),
        'inputs': service.inputs.digest}
    rows, seats, routes = [], {}, {}
    for seat in preparation.seats:
        provider = seat.executor_id
        profile, timeout = service.coordinator._selected_profile(preparation.packet, seat,
            preparation.policy, service.inputs)
        route = routes[provider] = _route(provider)
        p = route.profile
        path = '/v1/responses' if provider == 'codex' else (
            f'/v1/projects/{p.project}/locations/{p.location}/publishers/' +
            (f'anthropic/models/{p.effective_model}:streamRawPredict' if provider == 'claude' else
             f'google/models/{p.effective_model}:streamGenerateContent?alt=sse'))
        permit = BrokerPermit(provider, service.inputs.run_id, task_id, value['target_id'],
            seat.seat_id, 'initial', profile.requested_model, placeholders[len(rows)], 4,
            1048576, 8388608, 8192, response_deadline=300, inputs_digest=service.inputs.digest,
            session_id='fd6f840b-4b79-4fd2-91d0-14d3d8fba655' if provider == 'claude' else None,
            clamp_output_tokens=provider != 'codex', upstream_route_digest=route.digest,
            upstream_origin=p.origin, upstream_model=p.effective_model, upstream_path=path)
        row = {'role': 'candidate-seat', 'phase': 'initial', 'condition': 'candidate',
            'epoch': len(rows) + 1, 'permit': asdict(permit), 'blocker': None, 'initial': None,
            'profile': profile.digest, 'catalog': catalog_ref.digest if provider == 'codex' else None,
            'process_seconds': timeout, 'outer_seconds': timeout + 90}
        rows.extend((row, {**row, 'phase': 'resume', 'epoch': len(rows) + 2,
            'initial': diagnostic.quality_row_identity(row),
            'permit': {**asdict(permit), 'route': 'resume', 'session_id': None,
                'placeholder_key': placeholders[len(rows) + 1]}}))
        seats[provider] = {'seat_id': seat.seat_id,
            'workspace_digest': seat.workspace_verification.evidence_digest,
            'profile_digest': profile.digest, 'route_id': route.profile.route_id}
    original = next(row for row in rows if row['permit']['provider'] == 'claude' and row['phase'] == 'initial')
    rows.append({**original, 'role': 'owner-synthesis', 'epoch': 7,
        'permit': {**original['permit'], 'placeholder_key': placeholders[6],
            'session_id': '9f2ba67e-264d-44c8-a061-203c265fbc42',
            'seat_id': original['permit']['seat_id'] + ('-quality-owner' if backend == 'container-read-only' else '')}})
    ref = store.write_json('quality/allocation.json', {'schema': 'fanout-quality-case-allocation-v1',
        'case_id': 'normal-01', 'controller_id': controller.controller_id, 'bindings': bindings,
        'maka_output_tokens': 8192, 'rows': rows})
    authority = diagnostic.seal_quality_allocation(controller, store, ref, bindings=bindings)
    value.update(allocation=list(authority), bindings=bindings, seats=seats)
    # External installed-owner/catalog/machine-route contexts are synthetic;
    # service, source/question, allocation authentication and row admission are real.
    monkeypatch.setattr(quality_case, 'require_quality_installation', lambda *_args: None)
    monkeypatch.setattr(quality_case, '_reopen_quality_catalog', lambda *_args: b'{}\n')
    monkeypatch.setattr(quality_case, 'read_machine_routes', lambda *_args: SimpleNamespace(
        manifest_digest='a' * 64, policy_digest='b' * 64))
    monkeypatch.setattr(quality_case, 'select_route', lambda _manifest, **kwargs: routes[kwargs['provider']])
    monkeypatch.setattr(quality_case.HostCredentialSource, 'load',
        lambda *_args: pytest.fail('credential read'))
    container_boundary = importlib.import_module(fanout.__name__ + '.container_boundary')
    monkeypatch.setattr(container_boundary, '_run_bounded_command',
        lambda *_args, **_kwargs: pytest.fail('Docker work'))
    return SimpleNamespace(module=quality_case, helper=helper, opened=opened, value=value,
        rows=rows, controller=controller, store=store, authority=authority)


@pytest.mark.parametrize('bad_row', range(7))
@pytest.mark.parametrize('entry', ['adopt', 'reopen'])
def test_container_case_refuses_any_frozen_dummy_mismatch_before_dispatch(tmp_path, monkeypatch, bad_row, entry):
    placeholders = ['disposable-placeholder'] * 7
    placeholders[bad_row] = 'fanout-quality-pilot-placeholder'
    f = _case_fixture(tmp_path, monkeypatch, backend='container-read-only', placeholders=placeholders)
    with f.opened:
        allocation_bytes = (f.controller.root / 'locks/quality-allocation.json').read_bytes()
        if entry == 'reopen':
            pointer = f.module.write_evidence(f.controller, 'quality-installed-case-attachment', f.value)
            attachment = f.module.QualityCaseAttachment(*pointer)
        with pytest.raises(f.module.ProviderRequestError, match='placeholder'):
            if entry == 'adopt':
                f.module.adopt_quality_case(f.opened.service, f.opened.owner, f.value,
                    helper=HELPER, runtime_digest=f.helper._runtime_digest(fanout))
            else:
                f.module.reopen_quality_case(f.opened.service, attachment,
                    helper=HELPER, runtime_digest=f.helper._runtime_digest(fanout))
        assert (f.controller.root / 'locks/quality-allocation.json').read_bytes() == allocation_bytes
        assert not (f.controller.root / 'locks/spend').exists()
        assert not list((f.controller.root / 'locks').glob('quality-row-*'))
        assert not f.opened.journal.state.seat_phases
        assert not f.opened.service._execution_state(f.value['task_id']).barriers


@pytest.mark.parametrize('backend,placeholder', [('container-read-only', 'disposable-placeholder'),
    ('native', 'fanout-quality-pilot-placeholder'), ('native', 'different-native-placeholder')])
def test_matching_container_and_variable_native_cases_adopt_and_reopen_all_seven_rows(tmp_path, monkeypatch,
        backend, placeholder):
    f = _case_fixture(tmp_path, monkeypatch, backend=backend, placeholders=[placeholder] * 7)
    with f.opened:
        original = [row['permit']['placeholder_key'] for row in f.rows]
        attachment = f.module.adopt_quality_case(f.opened.service, f.opened.owner, f.value,
            helper=HELPER, runtime_digest=f.helper._runtime_digest(fanout))
        value, (_, _, allocation, _) = f.module.reopen_quality_case(f.opened.service, attachment,
            helper=HELPER, runtime_digest=f.helper._runtime_digest(fanout))
        assert value == f.value
        assert [row['permit']['placeholder_key'] for row in allocation['rows']] == original
        assert len(allocation['rows']) == 7
        assert not (f.controller.root / 'locks/spend').exists()
        assert not f.opened.journal.state.seat_phases


@pytest.mark.parametrize('row_index', range(7))
def test_backend_consumer_refuses_authenticated_mismatch_without_consuming_row(tmp_path, row_index):
    module, controller, store, authority, allocation, value, bindings = _declared_container_backend(
        tmp_path, placeholder='fanout-quality-pilot-placeholder')
    with store:
        pointer = module.write_evidence(controller, 'quality-installed-case-attachment', value)
        before = (controller.root / 'locks/quality-allocation.json').read_bytes()
        with pytest.raises(module.ProviderRequestError, match='placeholder'):
            module._read_quality_backend_attachment(controller, store, pointer,
                authority=authority, bindings=bindings, row=allocation['rows'][row_index])
        assert (controller.root / 'locks/quality-allocation.json').read_bytes() == before
        assert not list((controller.root / 'locks').glob('quality-row-*'))
        assert not (controller.root / 'locks/spend').exists()
