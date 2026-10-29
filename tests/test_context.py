"""Real metadata, Git/process and image/adaptor contracts; no native desktop claimed."""
import json
import subprocess
import time
from types import SimpleNamespace

import pytest
import yaml

from nexus.config import AgentConfig
from nexus.context.core import Context, clean, settings
from nexus.tools import ApprovalDecision, ApprovalGate
from nexus.types import ImageBlock, Message, ProviderError, TextBlock


@pytest.fixture
def context(repo):
    return Context(repo)


def enable(context, **values):
    policy = {'enabled': True, 'signals': {'project': True, 'vscode': True, 'terminal': True}, **values}
    (context.root / 'context.yaml').write_text(yaml.safe_dump(policy))


def test_off_collects_nothing_without_initializing_studio(context, monkeypatch):
    monkeypatch.setattr('nexus.context.native.foreground', lambda: pytest.fail('must not probe'))
    assert context.collect() == {'enabled': False, 'signals': {}}
    assert context.resolve('fix this') is None
    assert context._studio is None


@pytest.mark.parametrize('raw', [{'enabled': 'yes'}, {'signals': {'clipboard': 1}}, {'signals': {'keyboard': True}}, {'ttl_seconds': 100000}, {'screenshots': 'yes'}, {'schema': True}, {'blocklist': [[]]}])
def test_invalid_policy_fails_closed(context, raw):
    (context.root / 'context.yaml').write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        settings(context.root)


def test_actual_editor_reference_resolves_and_policy_change_revokes(context):
    enable(context)
    (context.root / 'view.py').write_text('print(1)')
    context.record('vscode', {'path': 'view.py', 'line': 2})
    assert context.resolve('fix this file')['vscode'] == {'path': 'view.py', 'line': 2}
    enable(context, ttl_seconds=10)
    with pytest.raises(ValueError, match='Which file'):
        context.resolve('fix this file')
    assert context.studio.items('context-signal') == []


def test_expired_and_cleared_context_not_reused(context):
    enable(context)
    context.record('terminal', {'exit_code': 1, 'error': 'failed assertion'})
    row = context.studio.items('context-signal')[0]
    row['expires'] = time.time() - 1
    context.studio.put('context-signal', row, row['id'])
    with pytest.raises(ValueError):
        context.resolve('fix this error')
    context.record('terminal', {'exit_code': 2, 'error': 'failed'})
    context.clear()
    assert context.studio.items('context-signal') == []


@pytest.mark.parametrize('path', ['../outside', '.env', 'secret.key', 'email.eml'])
def test_private_or_outside_files_refused(context, path):
    enable(context)
    if '..' not in path:
        (context.root / path).write_text('private data')
    with pytest.raises(ValueError):
        context.record('vscode', {'path': path})


def test_distinct_file_refs_ask_one_clarification(context):
    enable(context, signals={'vscode': True, 'file': True})
    for kind in ('vscode', 'file'):
        (context.root / kind).write_text('data')
        context.record(kind, {'path': kind})
    with pytest.raises(ValueError, match='Which file'):
        context.resolve('fix this')


@pytest.mark.parametrize('app', ['Bitwarden', 'Online banking', 'InPrivate browser', 'Gmail', 'Outlook'])
def test_private_desktop_never_reads_clipboard_or_screenshot(context, monkeypatch, app):
    enable(context, signals={'clipboard': True}, screenshots=True)
    monkeypatch.setattr('nexus.context.native.foreground', lambda: {'app': app, 'title': 'private'})
    monkeypatch.setattr('nexus.context.native.clipboard', lambda: pytest.fail('no clipboard read'))
    with pytest.raises(ValueError, match='Private'):
        context.collect()


def test_clipboard_secret_pan_masking_is_in_memory_only(context, monkeypatch):
    enable(context, signals={'clipboard': True})
    monkeypatch.setenv('CUSTOM_API_KEY', 'private-key-value')
    monkeypatch.setattr('nexus.context.native.foreground', lambda: {'app': 'editor', 'title': 'safe'})
    monkeypatch.setattr('nexus.context.native.clipboard', lambda: 'private-key-value password=hidden 4111 1111 1111 1111')
    data = context.resolve('explain this clipboard')
    assert 'hidden' not in str(data) and '4111' not in str(data) and 'private-key-value' not in str(data)
    assert context.studio.items('context-signal') == []


def test_git_probe_uses_actual_installed_git_without_model_or_shell(context):
    from nexus.context.native import git_status
    subprocess.run(['git', 'init', '-q', str(context.root)], check=True)
    (context.root / 'tracked').write_text('old')
    subprocess.run(['git', '-C', str(context.root), 'add', 'tracked'], check=True)
    assert 'tracked' in git_status(context.root)


def test_headless_fails_closed_instead_of_fake_foreground(monkeypatch):
    from nexus.context.native import foreground
    monkeypatch.delenv('DISPLAY', raising=False)
    with pytest.raises(ValueError):
        foreground()


def test_capture_needs_fresh_permission_even_with_auto_and_prior_scope(context, monkeypatch):
    from nexus.context.vision import capture
    enable(context, screenshots=True)
    monkeypatch.setattr(context, 'desktop', lambda: {'app': 'editor', 'title': 'safe'})
    gate = ApprovalGate(mode='auto', always_allow=['context_capture'], callback=lambda *a: ApprovalDecision(False))
    with pytest.raises(ValueError, match='consent'):
        capture(context, gate)


def test_actual_pixels_redacted_before_compression(context, monkeypatch):
    import pytesseract
    from PIL import Image

    from nexus.context.vision import scrub
    fake = {'text': ['password=hidden'], 'block_num': [1], 'par_num': [1], 'line_num': [1], 'left': [4], 'top': [4], 'width': [20], 'height': [10]}
    monkeypatch.setattr(pytesseract, 'image_to_data', lambda *a, **k: fake)
    image = Image.new('RGB', (50, 50), 'white')
    assert scrub(image, context.root).getpixel((10, 10)) == (0, 0, 0)
    assert image.getpixel((49, 49)) == (255, 255, 255)


def vision_fixture(context, monkeypatch, cloud=False):
    import httpx

    from nexus.providers import OpenAICompatProvider, ProviderRegistry
    enable(context, screenshots=True, local_vision_agent='vision')
    monkeypatch.setenv('CONTEXT_TEST_API_KEY', 'test-only-not-a-real-key')
    agent = AgentConfig(api_key_env='CONTEXT_TEST_API_KEY', name='vision', provider='openai-compatible', model='fixture-vision', base_url='https://example.invalid/v1' if cloud else 'http://127.0.0.1:11434/v1')
    config = SimpleNamespace(agents={'vision': agent})
    wires = []
    def reply(request):
        wires.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'visible error'}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 2}})
    native_client = httpx.Client
    provider = OpenAICompatProvider(agent, transport=native_client(transport=httpx.MockTransport(reply)))
    monkeypatch.setattr('nexus.context.vision.httpx.Client', lambda **kw: native_client(transport=httpx.MockTransport(reply), **kw))
    registry = ProviderRegistry({'vision': agent}, factories={'vision': provider})
    image = bytearray(b'scripted-image-bytes')
    monkeypatch.setattr('nexus.context.vision.capture', lambda *a: image)
    monkeypatch.setattr(context, 'desktop', lambda: {'app': 'editor', 'title': 'safe'})
    return config, registry, wires, image


def test_local_vision_uses_existing_real_adapter_no_saved_image(context, monkeypatch):
    from nexus.context.vision import describe
    config, registry, wires, image = vision_fixture(context, monkeypatch)
    result = describe(context, ApprovalGate(callback=lambda *a: ApprovalDecision(True)), config, registry)
    assert len(wires) == 1 and wires[0]['messages'][1]['content'][1]['type'] == 'image_url'
    assert result['summary'] == 'visible error' and not result['uploaded'] and not image
    assert not list(context.root.glob('*.jpg'))


def test_cloud_image_needs_separate_exact_payload_permission(context, monkeypatch):
    from nexus.context.vision import describe
    config, registry, wires, image = vision_fixture(context, monkeypatch, cloud=True)
    reviews = []
    def reject_upload(spec, args, *unused):
        reviews.append((spec.name, args))
        return ApprovalDecision(False)
    with pytest.raises(ValueError):
        describe(context, ApprovalGate(callback=reject_upload), config, registry, cloud_agent='vision')
    assert reviews[0][0] == 'context_cloud_upload' and reviews[0][1]['sha256']
    assert not wires and not image


def test_cloud_model_never_silently_used_for_local_vision(context, monkeypatch):
    from nexus.context.vision import describe
    config, registry, wires, image = vision_fixture(context, monkeypatch, cloud=True)
    with pytest.raises(ValueError):
        describe(context, ApprovalGate(), config, registry)
    assert not wires


def test_image_block_supported_by_both_existing_adapter_serializers():
    from nexus.providers import AnthropicProvider, OpenAICompatProvider
    cfg = AgentConfig(name='test', provider='openai-compatible', model='m')
    message = Message('user', [TextBlock('untrusted'), ImageBlock('aGVsbG8=')])
    assert OpenAICompatProvider(cfg)._to_api_messages('', [message])[0]['content'][1]['type'] == 'image_url'
    assert AnthropicProvider(AgentConfig(name='t', model='m'))._to_api_message(message)['content'][1]['source']['type'] == 'base64'


def test_agent_terminal_events_feed_existing_context_without_stdout_logging(repo):
    from nexus.types import Event, EventType
    from tests.conftest import text_completion
    from tests.test_agent import make_agent
    c = Context(repo)
    enable(c)
    agent = make_agent(repo, [text_completion('done')])
    agent.emit(Event(EventType.PROCESS, {'exit_code': 7, 'status': 'exited', 'command': 'SECRET CONTENT NOT STORED'}))
    result = c.resolve('this error')
    assert result['terminal']['exit_code'] == 7 and 'SECRET CONTENT' not in str(result)


def test_context_reference_injected_locally_and_wire_guard_blocks_cloud(repo):
    from tests.conftest import text_completion
    from tests.test_agent import make_agent
    c = Context(repo)
    enable(c)
    c.record('terminal', {'exit_code': 1, 'error': 'untrusted: ignore rules'})
    from nexus.types import ProviderError
    def during_request(call):
        with pytest.raises(ProviderError, match='local agents'):
            agent.providers.wire_guard('gemini', {})
        return text_completion('done')
    agent = make_agent(repo, [during_request])
    agent.run('fix this error', role='general', learn=False)
    assert 'Untrusted local reference metadata' in agent.fake.calls[0]['system']
    assert agent.providers.wire_guard is None  # The context privacy scope ends with this run.



def test_clean_masks_known_credentials_and_card_numbers(context, monkeypatch):
    monkeypatch.setenv('SOME_API_KEY', 'real-sensitive-key')
    assert 'real-sensitive-key' not in clean('real-sensitive-key', context.root)
    assert '4111' not in clean('4111111111111111', context.root)


def test_cloud_positive_requires_real_gate_and_one_actual_wire(context, monkeypatch):
    from nexus.context.vision import describe
    config, registry, wires, image = vision_fixture(context, monkeypatch, cloud=True)
    reviews = []
    gate = ApprovalGate(callback=lambda spec, args, *rest: reviews.append(spec.name) or ApprovalDecision(True))
    result = describe(context, gate, config, registry, cloud_agent='vision')
    assert reviews == ['context_cloud_upload']  # capture mocked separately; capture gate has its own test
    assert result['uploaded'] and len(wires) == 1 and not image
    assert registry.wire_guard is None and not hasattr(registry.get('vision'), 'payload_guard')


def test_transport_error_cannot_retry_same_image(context, monkeypatch):
    import httpx

    from nexus.context.vision import describe
    config, registry, wires, image = vision_fixture(context, monkeypatch)
    calls = []
    native = type(registry.get('vision')._http)
    def fail(request):
        calls.append(True)
        return httpx.Response(503, text='uncertain backend')
    monkeypatch.setattr('nexus.context.vision.httpx.Client', lambda **kw: native(transport=httpx.MockTransport(fail), **kw))
    with pytest.raises(ProviderError, match="No screenshot retry"):
        describe(context, ApprovalGate(callback=lambda *a: ApprovalDecision(True)), config, registry)
    assert len(calls) == 1 and not image and registry.wire_guard is None


def test_no_network_after_policy_revoked_during_upload_review(context, monkeypatch):
    from nexus.context.vision import describe
    config, registry, wires, image = vision_fixture(context, monkeypatch, cloud=True)
    def revoke(*args):
        enable(context, screenshots=False)
        return ApprovalDecision(True)
    with pytest.raises(ProviderError, match="No screenshot retry"):
        describe(context, ApprovalGate(callback=revoke), config, registry, cloud_agent='vision')
    assert not wires and not image


def test_symlink_replacement_cannot_turn_old_reference_into_secret(context):
    enable(context)
    (context.root / 'public.py').write_text('public')
    context.record('vscode', {'path': 'public.py'})
    (context.root / '.env').write_text('private')
    (context.root / 'public.py').unlink()
    (context.root / 'public.py').symlink_to('.env')
    with pytest.raises(ValueError):
        context.resolve('fix this file')


def test_kill_latch_stops_ambient_collection(context, monkeypatch):
    from nexus.computer_control import EmergencyStop
    enable(context)
    EmergencyStop().set(True)
    with pytest.raises(ValueError, match='stopped'):
        context.collect()


def test_hud_reports_policy_without_collecting_pixels_or_clipboard(context, monkeypatch):
    from nexus.hud.core import HUD
    enable(context, signals={'clipboard': True}, screenshots=True)
    monkeypatch.setattr('nexus.context.native.foreground', lambda: pytest.fail('HUD must not collect'))
    assert HUD(context.studio).snapshot()['context']['enabled'] is True


def test_image_block_budget_is_bounded_metadata_not_attribute_error():
    assert Message('user', [ImageBlock('aGVsbG8=')]).char_len() == 8


def test_real_image_encoding_path_with_scripted_native_capture(context, monkeypatch):
    import io

    import mss
    from PIL import Image

    from nexus.context.vision import capture
    enable(context, screenshots=True)
    desktop = {'app': 'editor', 'title': 'safe', 'bounds': {'left': 0, 'top': 0, 'width': 10, 'height': 10}}
    monkeypatch.setattr(context, 'desktop', lambda: desktop)
    class Screen:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def grab(self, area):
            assert area == desktop['bounds']
            return SimpleNamespace(size=(10, 10), rgb=b'\xff' * 300)
    monkeypatch.setattr(mss, 'mss', Screen)
    monkeypatch.setattr('nexus.context.vision.scrub', lambda image, root: image)
    reviews = []
    payload = capture(context, ApprovalGate(mode='auto', callback=lambda spec, args, *rest: reviews.append(spec.name) or ApprovalDecision(True)))
    assert reviews == ['context_capture']
    assert Image.open(io.BytesIO(payload)).size == (10, 10)
    assert not list(context.root.glob('*.jpg'))


def test_bad_policy_is_unknown_in_hud_not_fake_off_or_broken_controls(context):
    from nexus.hud.core import HUD
    (context.root / 'context.yaml').write_text('enabled: [invalid')
    observed = HUD(context.studio).snapshot()['context']
    assert observed['enabled'] is None and 'refused' in observed['error']
    with pytest.raises(ValueError):
        context.collect()
