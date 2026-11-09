import sys

import pytest

from nexus.redaction import StreamMask


@pytest.mark.parametrize('cut', [1, 5, 8, 12, 20])
def test_literal_credentials_are_masked_across_arbitrary_boundaries(cut):
    token = 'opaque-credential-for-stream'
    text = 'before ' + token + ' after'
    redactor = StreamMask([token])
    output = ''.join(redactor.feed(text[i:i + cut]) for i in range(0, len(text), cut)) + redactor.feed('', final=True)
    assert token not in output and 'before' in output and 'after' in output and '[REDACTED_SECRET]' in output


def test_real_command_cannot_print_environment_token_to_live_events(repo, monkeypatch):
    from nexus.live_process import stream_shell
    token = 'opaque-credential-for-stream'
    monkeypatch.setenv('GITHUB_TOKEN', token)
    events = []
    output, code = stream_shell([sys.executable, '-c', 'import os,sys,time; s=os.environ["GITHUB_TOKEN"]; sys.stdout.write(s[:10]); sys.stdout.flush(); time.sleep(.1); print(s[10:])'], repo, timeout=5, emit=events.append)
    assert code == 0 and token not in output and token not in repr(events)
    assert '[REDACTED_SECRET]' in output


def test_shared_word_streaming_is_bounded_and_flushes_normal_text():
    redactor = StreamMask([], hold_words=True)
    assert redactor.feed('a' * 5000) == '[REDACTED_LONG_TOKEN]'
    assert not redactor.pending
    assert redactor.feed('a' * 5000) == ''
    assert redactor.feed(' words are useful') == ' words are '
    assert redactor.feed('', final=True) == 'useful'
    assert redactor.feed('nd1_' + 'e' * 20) == ''
    assert redactor.feed('', final=True) == '[REDACTED_SECRET]'
