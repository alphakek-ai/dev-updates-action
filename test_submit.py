import io
import json

import pytest

import submit

GOOD = {'dev': {'title': 'Retries without duplicates',
                'bullets': ['🔁 `publish()` records each delivery before sending', '🧪 Replay tests cover timeouts']},
        'community': {'title': 'More reliable updates', 'bullets': ['📣 Announcements no longer arrive twice']}}
MODES = ['dev', 'community']
# The 2026-10-06 incident: the whole message JSON was published as one field.
INCIDENT = '{"title":"**Dev update**","bullets":["🔁 Retries"]}'


def errors_for(mode, field, text):
    entry = dict(GOOD[mode])
    entry[field] = [text] if field == 'bullets' else text
    return submit.validate({**GOOD, mode: entry}, MODES, 5)


def test_good_update_is_valid():
    assert submit.validate(GOOD, MODES, 5) == []


@pytest.mark.parametrize('text, problem', [
    (INCIDENT, 'JSON'),
    ('["a", "b"]', 'JSON'),
    ('**Bold** news', 'markdown emphasis'),
    ('See [docs](https://example.com)', 'markdown link'),
    ('- Fixed retries', 'list marker'),
    ('## Update', 'heading'),
    ('Fixed <b>retries</b>', 'HTML'),
    ('Fixed &amp; shipped', 'HTML'),
    ('First line\nsecond line', 'single line'),
    ('x' * 281, 'at most 280'),
    ('', 'non-empty'),
    ('Fixed `half-quoted retries', 'backticks'),
])
def test_markup_and_shape_errors_name_the_field(text, problem):
    errors = errors_for('dev', 'bullets', text)
    assert errors and all(error.startswith('dev.bullets[0]:') for error in errors)
    assert any(problem in error for error in errors)


@pytest.mark.parametrize('text, problem', [
    ('Faster `sync`', 'backtick'),
    ('Updated src/app/sync.py for speed', 'file path'),
    ('Tuned config.yaml defaults', 'file path'),
    ('Upgraded to v2.4', 'version'),
    ('Runs on engine 1.2.3 now', 'version'),
])
def test_community_rejects_internal_details(text, problem):
    errors = errors_for('community', 'title', text)
    assert any(error.startswith('community.title:') and problem in error for error in errors)


@pytest.mark.parametrize('text', ['Sync is 2.5x faster', 'Works 24/7 and/or offline', 'Prices from $5 & up'])
def test_community_accepts_plain_language(text):
    assert errors_for('community', 'title', text) == []


@pytest.mark.parametrize('update, problem', [
    ({'dev': GOOD['dev']}, 'community: must be an object'),
    ({**GOOD, 'extra': {}}, 'extra: unexpected key'),
    ({**GOOD, 'dev': {'title': 'Update', 'bullets': ['•'] * 6}}, 'list of 1 to 5'),
    ({**GOOD, 'dev': {'title': 'Update', 'bullets': []}}, 'list of 1 to 5'),
    ({**GOOD, 'dev': INCIDENT}, 'dev: must be an object'),
    (INCIDENT, 'JSON object keyed by mode'),
])
def test_structure_errors(update, problem):
    assert any(problem in error for error in submit.validate(update, MODES, 5))


@pytest.fixture
def update_file(tmp_path, monkeypatch):
    path = tmp_path / 'update.json'
    for key, value in {'UPDATE_FILE': str(path), 'HAS_DEV': 'true', 'HAS_COMMUNITY': 'true', 'MAX_BULLETS': '5',
                       'GITHUB_REPOSITORY': 'owner/repo', 'COMMIT_COUNT': '2', 'FILE_COUNT': '3'}.items():
        monkeypatch.setenv(key, value)
    return path


def hook(monkeypatch, capsys, event=None):
    monkeypatch.setattr('sys.argv', ['submit.py', '--hook'])
    event = event or {'hook_event_name': 'Stop', 'stop_hook_active': False}
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(event)))
    assert submit.main() == 0
    output = capsys.readouterr().out
    return json.loads(output) if output else None


def test_hook_blocks_invalid_update_then_allows_once_fixed(update_file, monkeypatch, capsys):
    update_file.write_text(json.dumps({**GOOD, 'dev': {'title': INCIDENT, 'bullets': ['x']}}))
    decision = hook(monkeypatch, capsys)
    assert decision['decision'] == 'block' and 'dev.title: is a JSON value' in decision['reason']
    update_file.write_text(json.dumps(GOOD))
    assert hook(monkeypatch, capsys) is None


def test_hook_stops_blocking_after_the_cap(update_file, monkeypatch, capsys):
    assert [hook(monkeypatch, capsys) is not None for _ in range(submit.MAX_BLOCKS + 1)] == [True] * submit.MAX_BLOCKS + [False]


def test_check_prints_errors_or_exact_messages(update_file, monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['submit.py'])
    update_file.write_text('{"dev": ')
    assert submit.main() == 1
    assert 'is not valid JSON' in capsys.readouterr().out
    update_file.write_text(json.dumps(GOOD))
    assert submit.main() == 0
    output = capsys.readouterr().out
    assert '===== telegram (dev) =====\n<b>Retries without duplicates</b>' in output
    assert '===== twitter (community) =====\nMore reliable updates' in output
    assert 'repo · 2 commit(s) · 3 file(s)' in output


@pytest.mark.parametrize('command', ['tail diff.patch', submit.COMMAND + ' && env', 'printenv'])
def test_bash_hook_permits_only_the_check(monkeypatch, capsys, command):
    event = lambda command: {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': command}}
    assert hook(monkeypatch, capsys, event(submit.COMMAND)) is None
    decision = hook(monkeypatch, capsys, event(command))['hookSpecificOutput']
    assert decision['permissionDecision'] == 'deny' and submit.COMMAND in decision['permissionDecisionReason']
