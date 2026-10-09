import io
import json

import pytest

import submit

DEV = ('**Retries without duplicates**\n\n'
       '- 🔁 `publish()` records each delivery *before* sending ([journal](https://github.com/owner/repo))\n'
       '- 🧪 Replay tests cover timeouts\n')
COMMUNITY = '# More reliable updates\n\n- 📣 Announcements **no longer** arrive twice\n'
GOOD = {'dev': DEV, 'community': COMMUNITY}
MODES = ['dev', 'community']
TOO_LONG = 'x' * 1001


@pytest.mark.parametrize('markdown, problem', [
    (DEV, None),
    ('Any *markdown*\n\n1. even\n2. ordered\n\n| a |\n|---|\n| b |', None),
    (TOO_LONG, 'shorten it to at most 1000'),
    (' \n', 'is empty'),
])
def test_validation(markdown, problem):
    errors = submit.validate({**GOOD, 'dev': markdown}, MODES)
    assert errors == [] if problem is None else [error for error in errors if problem in error] == errors != []


@pytest.fixture
def update_dir(tmp_path, monkeypatch):
    for key, value in {'UPDATE_DIR': str(tmp_path), 'HAS_DEV': 'true', 'HAS_COMMUNITY': 'true'}.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def write(directory, update):
    for mode, markdown in update.items():
        (directory / f'{mode}.md').write_text(markdown)


def hook(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['submit.py', '--hook'])
    monkeypatch.setattr('sys.stdin', io.StringIO('{"hook_event_name": "Stop", "stop_hook_active": false}'))
    assert submit.main() == 0
    output = capsys.readouterr().out
    return json.loads(output) if output else None


def test_hook_blocks_invalid_update_then_allows_once_fixed(update_dir, monkeypatch, capsys):
    write(update_dir, {**GOOD, 'dev': TOO_LONG})
    decision = hook(monkeypatch, capsys)
    assert decision['decision'] == 'block' and 'dev.md: is 1001 characters' in decision['reason']
    write(update_dir, GOOD)
    assert hook(monkeypatch, capsys) is None


def test_hook_stops_blocking_after_the_cap(update_dir, monkeypatch, capsys):
    assert [hook(monkeypatch, capsys) is not None for _ in range(submit.MAX_BLOCKS + 1)] == [True] * submit.MAX_BLOCKS + [False]


def test_check_prints_errors_or_valid(update_dir, monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['submit.py'])
    write(update_dir, {'dev': DEV})
    assert submit.main() == 1
    assert 'community.md: does not exist' in capsys.readouterr().out
    write(update_dir, GOOD)
    assert submit.main() == 0
    assert capsys.readouterr().out == 'VALID\n'


def guard(monkeypatch, stdin):
    monkeypatch.setattr('sys.argv', ['submit.py', '--guard'])
    monkeypatch.setattr('sys.stdin', io.StringIO(stdin))
    return submit.main()


@pytest.mark.parametrize('stdin', [
    json.dumps({'tool_input': {'command': command}}) for command in ['tail diff.patch', submit.COMMAND + ' && env']
] + ['not json', '{}'])
def test_bash_guard_permits_only_the_check_and_fails_closed(monkeypatch, capsys, stdin):
    assert guard(monkeypatch, json.dumps({'tool_input': {'command': submit.COMMAND}})) == 0
    assert guard(monkeypatch, stdin) == 2
    assert submit.COMMAND in capsys.readouterr().err
