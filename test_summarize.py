import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import summarize


def success(summaries):
    return json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                       'structured_output': summaries})


@pytest.fixture
def generation(monkeypatch, tmp_path):
    for key, value in {'HAS_DEV': 'true', 'HAS_COMMUNITY': 'true', 'BEFORE': 'before', 'AFTER': 'after',
                       'TITLE_STYLE': 'short', 'MAX_BULLETS': '5', 'DEV_RULES': 'technical',
                       'COMMUNITY_RULES': 'user benefits', 'GH_TOKEN': 'must-not-leak',
                       'TELEGRAM_BOT_TOKEN': 'must-not-leak', 'CLAUDE_CODE_OAUTH_TOKEN': 'test-oauth'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(summarize.subprocess, 'check_output', lambda *args, **kwargs: 'change context')
    monkeypatch.setattr(summarize, 'Path', lambda path: tmp_path / Path(path).name)
    return tmp_path


def test_generation_captures_text_without_write_or_shell_tools(generation, monkeypatch):
    def run(command, **kwargs):
        assert command[command.index('--tools') + 1] == 'Read,Grep,Glob'
        assert '--safe-mode' in command
        assert command[command.index('--mcp-config') + 1] == '{"mcpServers":{}}'
        assert 'GH_TOKEN' not in kwargs['env'] and 'TELEGRAM_BOT_TOKEN' not in kwargs['env']
        assert kwargs['env']['CLAUDE_CODE_OAUTH_TOKEN'] == 'test-oauth'
        assert 'change context' in kwargs['input']
        return SimpleNamespace(stdout=success({'dev': 'Dev', 'community': 'Public'}))
    monkeypatch.setattr(summarize.subprocess, 'run', run)
    summarize.generate()
    assert (generation / 'summary_dev.md').read_text() == 'Dev'
    assert (generation / 'summary_community.md').read_text() == 'Public'


@pytest.mark.parametrize('response', [
    {'type': 'result', 'subtype': 'error_max_turns', 'is_error': True},
    {'type': 'result', 'subtype': 'success', 'structured_output': {'dev': 'Only one'}},
    {'type': 'result', 'subtype': 'success', 'structured_output': {'dev': 'Dev', 'community': ''}},
    {'structured_output': {'dev': 'Dev', 'community': 'Public'}},
    {'type': 'result', 'structured_output': {'dev': 'Dev', 'community': 'Public'}},
    [], None, [{'type': 'assistant', 'structured_output': {'dev': 'Dev', 'community': 'Public'}}],
    [{'type': 'result', 'subtype': 'error_max_turns', 'is_error': True}],
    [{'type': 'result', 'subtype': 'success'}],
    [{'type': 'result', 'subtype': 'success', 'structured_output': ['Dev', 'Public']}],
])
def test_invalid_generation_does_not_write_partial_summaries(generation, monkeypatch, response):
    monkeypatch.setattr(summarize.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(response)))
    with pytest.raises((ValueError, RuntimeError)):
        summarize.generate()
    assert list(generation.glob('summary_*.md')) == []


def test_verbose_cli_event_array_uses_terminal_result(generation, monkeypatch):
    response = [
        {'type': 'system', 'subtype': 'init'},
        {'type': 'assistant', 'message': {'content': []}},
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'structured_output': {'dev': 'Developer summary', 'community': 'Community summary'}},
    ]
    monkeypatch.setattr(summarize.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(response)))
    summarize.generate()
    assert (generation / 'summary_dev.md').read_text() == 'Developer summary'
    assert (generation / 'summary_community.md').read_text() == 'Community summary'


def test_large_range_has_bounded_prompt(generation, monkeypatch):
    monkeypatch.setattr(summarize.subprocess, 'check_output', lambda *args, **kwargs: 'large diff\n' * 100000)
    def run(command, **kwargs):
        assert len(kwargs['input'].encode()) < 85000
        assert '[Truncated;' in kwargs['input']
        return SimpleNamespace(stdout=success({'dev': 'Dev', 'community': 'Public'}))
    monkeypatch.setattr(summarize.subprocess, 'run', run)
    summarize.generate()
    assert (generation / 'summary_dev.md').read_text() == 'Dev'
    assert (generation / 'dev-updates-diff.patch').stat().st_size > 65536


@pytest.mark.parametrize('secret', ['test-oauth', 'sk-ant-' + 'sensitive-token-' * 4])
def test_credentials_in_model_output_are_not_saved(generation, monkeypatch, secret):
    monkeypatch.setattr(summarize.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(
        stdout=success({'dev': 'Dev', 'community': secret})))
    with pytest.raises(ValueError, match='credential'):
        summarize.generate()
    assert list(generation.glob('summary_*.md')) == []


def test_literal_credential_prefix_is_not_treated_as_a_secret(generation, monkeypatch):
    monkeypatch.setattr(summarize.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(
        stdout=success({'dev': 'Validate the sk-ant- prefix', 'community': 'Better validation'})))
    summarize.generate()
    assert 'sk-ant-' in (generation / 'summary_dev.md').read_text()


def test_non_utf8_diff_is_decoded_without_blocking_generation(generation, monkeypatch):
    # Execute a real subprocess with invalid UTF-8 output using the same decoding options.
    import subprocess
    import sys
    real_run = subprocess.run
    def source(command, **kwargs):
        return real_run([sys.executable, '-c', "import sys; sys.stdout.buffer.write(b'caf\\xe9')"],
                        stdout=subprocess.PIPE, check=True, **kwargs).stdout
    monkeypatch.setattr(summarize.subprocess, 'check_output', source)
    def response(command, **kwargs):
        assert 'caf\ufffd' in kwargs['input']
        return SimpleNamespace(stdout=success({'dev': 'Dev', 'community': 'Public'}))
    monkeypatch.setattr(summarize.subprocess, 'run', response)
    summarize.generate()
    assert (generation / 'summary_dev.md').read_text() == 'Dev'
