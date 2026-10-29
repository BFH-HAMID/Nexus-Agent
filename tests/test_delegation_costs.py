"""Conservative operator-price allocation, never invented provider/electricity bills."""
import pytest
import yaml

from nexus.delegation.core import Delegations, specification
from nexus.jarvis.runtime import Runtime, execute
from nexus.server import ServerState


@pytest.fixture
def service(repo):
    (repo / 'autonomy.yaml').write_text(yaml.safe_dump({'runtime': {'enabled': True, 'local_model': 'fixture-local'}}))
    return Delegations(ServerState(repo, quiet=True).workbench.studio)


def approve(service, cost):
    job = service.create('Review bounded local task', max_cost_usd=cost)
    service.queue.review(job['id'], 'approve', 'APPROVE QUEUED TASK ' + job['id'])
    return job


def pricing(service):
    service.studio.wb.save_settings({'pricing': {'fixture-local': {'input_per_million': 1, 'output_per_million': 1,
                                                                 'source': 'operator-provided fixture token allocation, not billing'}}})


@pytest.mark.parametrize('value', [-1, True, float('inf'), float('nan'), '1', 1001])
def test_bad_caps_are_not_silently_ignored(value):
    with pytest.raises(ValueError):
        specification({'schema': 1, 'max_seconds': 10, 'max_cost_usd': value})


def test_unknown_price_blocks_without_reserving_or_model_wire(service):
    job = approve(service, .01)
    assert Runtime(service.studio).claim() is None
    assert Runtime(service.studio).budget()['runs_reserved'] == 0
    assert 'unknown_model_price' in service.view(job['id'])['blockers']
    pricing(service)
    claimed = Runtime(service.studio).claim()
    assert claimed['model_price']['source']
    assert service.view(job['id'])['cost_limit_enforced']


def test_zero_cap_blocks_nonzero_allocation_before_wire(service, monkeypatch):
    from nexus.agent import NexusAgent
    pricing(service)
    approve(service, 0)
    job = Runtime(service.studio).claim()
    reached_wire = []
    def run(agent, *args, **kwargs):
        agent.providers.wire_guard('selected-ollama', {'model': 'fixture-local', 'max_tokens': 32, 'messages': []})
        reached_wire.append(True)
        pytest.fail('must stop before any actual model I/O')
    monkeypatch.setattr(NexusAgent, 'run', run)
    result = execute(service.studio, job['id'], job['lease'])
    assert result['status'] == 'error' and not reached_wire
    assert result['result']['actual_cost_usd'] is None


def test_80_percent_priced_alert_persists_once_and_stops_next_over_cap_wire(service, monkeypatch):
    from nexus.agent import NexusAgent
    pricing(service)
    approve(service, .001)
    job = Runtime(service.studio).claim()
    admitted = []
    def run(agent, *args, **kwargs):
        payload = {'model': 'fixture-local', 'max_tokens': 32, 'messages': [{'role': 'user', 'content': 'x' * 760}]}
        agent.providers.wire_guard('selected-ollama', payload)
        admitted.append(True)
        agent.providers.wire_guard('selected-ollama', payload)
        pytest.fail('must refuse second reservation')
    monkeypatch.setattr(NexusAgent, 'run', run)
    result = execute(service.studio, job['id'], job['lease'])
    assert len(admitted) == 1 and result['status'] == 'error'
    view = service.view(job['id'])
    assert [a['kind'] for a in view['alerts']] == ['model_cost_80_percent']
    assert view['actual_cost_usd'] is None


def test_rate_change_revokes_remaining_allocations(service, monkeypatch):
    from nexus.agent import NexusAgent
    pricing(service)
    approve(service, 1)
    job = Runtime(service.studio).claim()
    def run(agent, *args, **kwargs):
        service.studio.wb.save_settings({'pricing': {}})
        agent.providers.wire_guard('selected-ollama', {'model': 'fixture-local', 'max_tokens': 32})
        pytest.fail('must stop before wire under changed rates')
    monkeypatch.setattr(NexusAgent, 'run', run)
    assert execute(service.studio, job['id'], job['lease'])['status'] == 'error'


def test_unknown_price_does_not_starve_unpriced_approved_work(service):
    approve(service, .01)
    other = service.queue.enqueue('Other explicitly unpriced work')
    service.queue.review(other['id'], 'approve', 'APPROVE QUEUED TASK ' + other['id'])
    assert Runtime(service.studio).claim()['id'] == other['id']


def test_interrupted_unknown_usage_is_not_calculated_as_zero_dollars(service):
    pricing(service)
    approve(service, .01)
    job = Runtime(service.studio).claim()
    result = Runtime(service.studio).finish(job, 'interrupted', {'summary': 'Outcome unknown, worker crashed'})
    assert result['result'].get('calculated_model_cost_usd') is None
