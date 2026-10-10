import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import summarize

UPDATE = {'dev': '**Faster sync**\n\n- ⚡ `sync_all` batches writes\n',
          'community': '**Faster sync**\n\n- ⚡ Your data syncs faster\n'}
SUCCESS = json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False})


def agent(update=UPDATE, stdout=SUCCESS, check=None):
    """Fake CLI run that writes <mode>.md in its working directory like the agent does."""
    def run(command, **kwargs):
        if check:
            check(command, **kwargs)
        for mode, markdown in (update or {}).items():
            (Path(kwargs['cwd']) / f'{mode}.md').write_text(markdown)
        return SimpleNamespace(stdout=stdout)
    return run


@pytest.fixture
def generation(monkeypatch, tmp_path):
    for key, value in {'HAS_DEV': 'true', 'HAS_COMMUNITY': 'true', 'BEFORE': 'before', 'AFTER': 'after',
                       'TITLE_STYLE': 'short', 'MAX_BULLETS': '5', 'DEV_RULES': 'technical',
                       'COMMUNITY_RULES': 'user benefits', 'GH_TOKEN': 'must-not-leak',
                       'UPDATE_DIR': str(tmp_path / 'work'),
                       'TELEGRAM_BOT_TOKEN': 'must-not-leak', 'CLAUDE_CODE_OAUTH_TOKEN': 'test-oauth'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(summarize.subprocess, 'check_output', lambda *args, **kwargs: 'change context')
    return tmp_path / 'work'


def test_agent_is_locked_down_to_reading_writing_update_and_checking(generation, monkeypatch):
    def check(command, **kwargs):
        option = lambda name: command[command.index(name) + 1]
        script = f"{sys.executable} {Path(summarize.__file__).with_name('submit.py').resolve()}"
        settings = json.loads(option('--settings'))
        assert '--safe-mode' not in command and option('--setting-sources') == ''
        assert option('--permission-mode') == 'dontAsk'
        assert option('--tools') == 'Read,Grep,Glob,Write,Bash'
        assert option('--mcp-config') == '{"mcpServers":{}}' and '--strict-mcp-config' in command
        assert settings['permissions']['allow'] == [
            'Read', 'Grep', 'Glob', f'Edit(/{generation}/dev.md)', f'Edit(/{generation}/community.md)', f'Bash({script})']
        assert settings['hooks'] == {
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': script + ' --guard'}]}],
            'Stop': [{'hooks': [{'type': 'command', 'command': script + ' --hook'}]}]}
        assert 'GH_TOKEN' not in kwargs['env'] and 'TELEGRAM_BOT_TOKEN' not in kwargs['env']
        assert kwargs['env']['CLAUDE_CODE_OAUTH_TOKEN'] == 'test-oauth'
        assert kwargs['cwd'] == generation
        assert 'change context' in kwargs['input'] and script in kwargs['input']
    monkeypatch.setattr(summarize.subprocess, 'run', agent(check=check))
    summarize.generate()
    assert {mode: (generation / f'{mode}.md').read_text() for mode in UPDATE} == UPDATE


@pytest.mark.parametrize('stdout', [
    {'type': 'result', 'subtype': 'error_max_turns', 'is_error': True},
    {'type': 'result', 'subtype': 'success', 'is_error': True},
    {'subtype': 'success'}, [], None, [{'type': 'assistant'}],
    [{'type': 'result', 'subtype': 'error_max_turns', 'is_error': True}],
])
def test_failed_generation_is_rejected(generation, monkeypatch, stdout):
    monkeypatch.setattr(summarize.subprocess, 'run', agent(stdout=json.dumps(stdout)))
    with pytest.raises((ValueError, RuntimeError)):
        summarize.generate()


@pytest.mark.parametrize('update', [
    None,  # The agent never wrote the file.
    {'dev': 'x' * 1001, 'community': UPDATE['community']},
])
def test_invalid_update_fails_generation(generation, monkeypatch, update):
    monkeypatch.setattr(summarize.subprocess, 'run', agent(update=update))
    with pytest.raises(ValueError, match='invalid'):
        summarize.generate()


@pytest.mark.parametrize('terminal_only', [False, True])
def test_captured_cli_response_is_accepted(generation, monkeypatch, terminal_only):
    # Captured 2026-09-23 with CLI 2.1.270 on backend 7d35dde6..6bf8f831.
    # Preserve event order and result fields; remove message/account/session data
    # and replace summary text. Replay also covers the single-result output mode.
    response = json.loads((Path(__file__).parent / 'fixtures/claude-2.1.270-generation.json').read_text())
    if terminal_only:
        response = response[-1]
    monkeypatch.setattr(summarize.subprocess, 'run', agent(stdout=json.dumps(response)))
    summarize.generate()
    assert {mode: (generation / f'{mode}.md').read_text() for mode in UPDATE} == UPDATE


def test_stale_update_from_a_previous_run_is_not_reused(generation, monkeypatch):
    generation.mkdir()
    (generation / 'dev.md').write_text(UPDATE['dev'])
    monkeypatch.setattr(summarize.subprocess, 'run', agent(update=None))
    with pytest.raises(ValueError, match='does not exist'):
        summarize.generate()


def test_large_range_has_bounded_prompt(generation, monkeypatch):
    monkeypatch.setattr(summarize.subprocess, 'check_output', lambda *args, **kwargs: 'large diff\n' * 100000)
    def check(command, **kwargs):
        assert len(kwargs['input'].encode()) < 85000
        assert '[Truncated;' in kwargs['input']
    monkeypatch.setattr(summarize.subprocess, 'run', agent(check=check))
    summarize.generate()
    assert (generation / 'diff.patch').stat().st_size > 65536


@pytest.mark.parametrize('secret', ['test-oauth', 'sk-ant-' + 'sensitive-token-' * 4])
def test_credentials_in_model_output_are_not_saved(generation, monkeypatch, secret):
    update = {**UPDATE, 'community': f'**Update**\n\n- {secret}\n'}
    monkeypatch.setattr(summarize.subprocess, 'run', agent(update=update))
    with pytest.raises(ValueError, match='credential'):
        summarize.generate()
    assert not list(generation.glob('*.md'))


def test_literal_credential_prefix_is_not_treated_as_a_secret(generation, monkeypatch):
    update = {**UPDATE, 'dev': '**Update**\n\n- Validate the sk-ant- prefix\n'}
    monkeypatch.setattr(summarize.subprocess, 'run', agent(update=update))
    summarize.generate()
    assert 'sk-ant-' in (generation / 'dev.md').read_text()


def test_non_utf8_diff_is_decoded_without_blocking_generation(generation, monkeypatch):
    # Execute a real subprocess with invalid UTF-8 output using the same decoding options.
    import subprocess
    import sys
    real_run = subprocess.run
    def source(command, **kwargs):
        return real_run([sys.executable, '-c', "import sys; sys.stdout.buffer.write(b'caf\\xe9')"],
                        stdout=subprocess.PIPE, check=True, **kwargs).stdout
    monkeypatch.setattr(summarize.subprocess, 'check_output', source)
    def check(command, **kwargs):
        assert 'caf�' in kwargs['input']
    monkeypatch.setattr(summarize.subprocess, 'run', agent(check=check))
    summarize.generate()
