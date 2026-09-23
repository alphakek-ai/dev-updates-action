import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import summarize


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
        return SimpleNamespace(stdout=json.dumps({'structured_output': {'dev': 'Dev', 'community': 'Public'}}))
    monkeypatch.setattr(summarize.subprocess, 'run', run)
    summarize.generate()
    assert (generation / 'summary_dev.md').read_text() == 'Dev'
    assert (generation / 'summary_community.md').read_text() == 'Public'


@pytest.mark.parametrize('response', [
    {'is_error': True}, {'structured_output': {'dev': 'Only one'}},
    {'structured_output': {'dev': 'Dev', 'community': ''}},
])
def test_invalid_generation_does_not_write_partial_summaries(generation, monkeypatch, response):
    monkeypatch.setattr(summarize.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(response)))
    with pytest.raises((ValueError, RuntimeError)):
        summarize.generate()
    assert list(generation.iterdir()) == []


def test_large_range_has_bounded_prompt(generation, monkeypatch):
    monkeypatch.setattr(summarize.subprocess, 'check_output', lambda *args, **kwargs: 'large diff\n' * 100000)
    def run(command, **kwargs):
        assert len(kwargs['input'].encode()) < 85000
        assert '[Truncated;' in kwargs['input']
        return SimpleNamespace(stdout=json.dumps({'structured_output': {'dev': 'Dev', 'community': 'Public'}}))
    monkeypatch.setattr(summarize.subprocess, 'run', run)
    summarize.generate()
    assert (generation / 'summary_dev.md').read_text() == 'Dev'


@pytest.mark.parametrize('secret', ['test-oauth', 'sk-ant-unexpected-token'])
def test_credentials_in_model_output_are_not_saved(generation, monkeypatch, secret):
    monkeypatch.setattr(summarize.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(
        stdout=json.dumps({'structured_output': {'dev': 'Dev', 'community': secret}})))
    with pytest.raises(ValueError, match='credential'):
        summarize.generate()
    assert list(generation.iterdir()) == []
