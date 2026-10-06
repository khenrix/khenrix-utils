"""Synthetic authenticated storage fixtures, never native/live certification."""
from dataclasses import asdict, replace
import hashlib

import pytest

from shared.lib import fanout
from shared.lib.fanout import diagnostic
from shared.lib.fanout.artifacts import ArtifactStore
from shared.lib.fanout.broker import BrokerPermit
from shared.lib.fanout.container_broker import OutboundRequestIdentity, PermitLedger
from shared.lib.fanout.controller import write_evidence
from shared.lib.fanout.errors import ProviderRequestError


@pytest.fixture
def predecessor(tmp_path):
    stores = []

    def create(provider, *, route=None, terminal_session=None, settled=True,
               fenced=True, raw_changed=False, foreign_row=False):
        controller = fanout.create_lifecycle_controller(tmp_path / 'controller')
        ledger = PermitLedger(controller.root / 'locks/spend', controller)
        store = ArtifactStore(tmp_path / 'raw')
        stores.append(store)
        profile = fanout.ProviderRegistry.default().select(provider, 'read-only', 'standard')
        assigned = 'fd6f840b-4b79-4fd2-91d0-14d3d8fba655' if provider == 'claude' else None
        observed = assigned or '99b510c5-ddcf-4607-966c-90a2c3e5d537'
        expected_route = 'initial:assigned:fd6f840b-4b79-4fd2-91d0-14d3d8fba655' if provider == 'claude' else 'initial:unassigned'
        permit = BrokerPermit(provider, 'run', 'task', 'target', 'seat', 'initial',
            profile.requested_model, 'disposable-placeholder', 1, 1024, 2048, 8192,
            inputs_digest='a' * 64, session_id=assigned)
        bindings = {name: 'b' * 64 for name in
            ('campaign', 'casebook', 'source', 'projection', 'profile', 'inputs')}
        bindings['inputs'] = 'a' * 64
        original = {'role': 'candidate-seat', 'phase': 'initial', 'condition': 'candidate',
            'epoch': 1, 'permit': asdict(permit), 'blocker': None, 'initial': None,
            'profile': profile.digest, 'catalog': 'c' * 64 if provider == 'codex' else None,
            'process_seconds': 30, 'outer_seconds': 60}
        resume = {**original, 'phase': 'resume', 'epoch': 2,
            'permit': {**asdict(permit), 'route': 'resume'},
            'initial': diagnostic.quality_row_identity(original)}
        ref = store.write_json('allocation.json', {'schema': 'fanout-quality-case-allocation-v1',
            'case_id': 'normal-01', 'controller_id': controller.controller_id,
            'bindings': bindings, 'maka_output_tokens': 8192, 'rows': [original, resume]})
        authority = diagnostic.seal_quality_allocation(controller, store, ref, bindings=bindings)
        permit_id = diagnostic.register_quality_permit(controller, store, authority, ledger,
            role='candidate-seat', phase='initial', condition='candidate', permit=permit,
            epoch=1, bindings=bindings)
        ledger.record_attempt(permit_id, 1)
        wire_model = 'gemini-3.8-flash' if provider == 'agy' else profile.requested_model
        path = {'claude': '/v1/messages', 'codex': '/v1/responses',
            'agy': '/v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse'}[provider]
        ledger.reserve(permit_id, 1, 'synthetic-request', OutboundRequestIdentity(
            'POST', path, 'd' * 64, 8192, wire_model))
        if settled:
            ledger.settle('synthetic-request', 'e' * 64)
        if fenced:
            ledger.fence_epoch(permit_id, 1)
        stdout = b'synthetic terminal bytes, not a native transcript'
        terminal = write_evidence(controller, 'brokered-provider-diagnostic', {
            'schema_version': 'fanout-brokered-diagnostic-v1', 'run_id': 'run',
            'task_id': 'task', 'target_id': 'target', 'seat_id': 'seat',
            'inputs_digest': 'a' * 64, 'profile_digest': profile.digest,
            'boundary_receipt_digest': 'f' * 64,
            'route': expected_route if route is None else route,
            'session_id': observed if terminal_session is None else terminal_session,
            'valid': True, 'reason': 'ok', 'process_status': 'exit', 'exit_code': 0,
            'stdout_sha256': hashlib.sha256(stdout).hexdigest(),
            'stderr_sha256': hashlib.sha256(b'').hexdigest(),
            'observed_model': profile.requested_model, 'broker_model': profile.requested_model,
            'effective_output_tokens': [8192], 'refused_agy_auxiliary_requests': 0})
        command = write_evidence(controller, 'quality-native-command', {
            'allocation': list(authority), 'store': str(store.root), 'bindings': bindings,
            'registration': {'row': {**original, 'profile': '0' * 64} if foreign_row else original,
                'permit_id': permit_id, 'permit': asdict(permit)}})
        proof = write_evidence(controller, 'quality-readonly-turn', {
            'terminal': list(terminal), 'command': list(command),
            'records': list(ledger.request_records(permit_id, 1)),
            'stdout': asdict(store.write_bytes('stdout.bin', b'changed' if raw_changed else stdout)),
            'stderr': asdict(store.write_bytes('stderr.bin', b'')),
            'final': asdict(store.write_bytes('final.txt', b'synthetic fixture final'))})
        return controller, store, authority, ledger, bindings, proof, replace(
            permit, route='resume', session_id=observed)

    yield create
    for store in stores:
        store.close()


def register(case, *, permit=None):
    controller, store, authority, ledger, bindings, proof, expected = case
    return diagnostic.register_quality_permit(controller, store, authority, ledger,
        role='candidate-seat', phase='resume', condition='candidate',
        permit=expected if permit is None else permit, epoch=2, bindings=bindings,
        initial=proof)


@pytest.mark.parametrize('provider', ('claude', 'codex', 'agy'))
def test_exact_native_initial_route_resolves_completed_original(predecessor, provider):
    case = predecessor(provider)
    permit_id = register(case)
    case[3].require_permit(permit_id, 2, case[-1])
    with pytest.raises(ProviderRequestError, match='consumed'):
        register(case)


@pytest.mark.parametrize('provider,route', (
    ('claude', 'initial'), ('codex', 'initial'), ('agy', 'initial'),
    ('claude', 'initial:unassigned'),
    ('claude', 'initial:assigned:fd6f840b-4b79-4fd2-91d0-14d3d8fba655-extra'),
    ('claude', 'initial:assigned:foreign-session'),
    ('codex', 'initial:assigned:99b510c5-ddcf-4607-966c-90a2c3e5d537'),
))
def test_wrong_initial_route_refuses_before_resume_registration(predecessor, provider, route):
    case = predecessor(provider, route=route)
    with pytest.raises(ProviderRequestError, match='completed original'):
        register(case)
    with case[3]._locked() as state:
        assert len(state['permits']) == 1


def test_assigned_initial_session_must_match_completed_session(predecessor):
    case = predecessor('claude', terminal_session='foreign-session')
    with pytest.raises(ProviderRequestError, match='completed original'):
        register(case)


@pytest.mark.parametrize('provider', ('claude', 'codex', 'agy'))
def test_supplied_resume_session_cannot_replace_original(predecessor, provider):
    case = predecessor(provider)
    with pytest.raises(ProviderRequestError):
        register(case, permit=replace(case[-1], session_id='foreign-session'))
    with case[3]._locked() as state:
        assert len(state['permits']) == 1


@pytest.mark.parametrize('change', ({'settled': False}, {'fenced': False},
    {'raw_changed': True}, {'foreign_row': True}))
def test_native_route_fix_preserves_settlement_fencing_and_raw_checks(predecessor, change):
    case = predecessor('codex', **change)
    with pytest.raises(ProviderRequestError, match='completed original'):
        register(case)
