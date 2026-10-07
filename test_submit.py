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
# The 2026-10-06 incident: the whole message JSON was published as text.
INCIDENT = '{"title":"**Dev update**","bullets":["🔁 Retries"]}'


def test_good_update_is_valid():
    assert submit.validate(GOOD, MODES, 5) == []


@pytest.mark.parametrize('markdown, problem', [
    (INCIDENT, 'is JSON'),
    ('', 'is empty'),
    ('Update\n\n- item', ''),  # A plain title line is fine.
    ('**Update**\n\nIntro paragraph.\n\n- item', 'found [paragraph, paragraph, bullet_list]'),
    ('**Update**\n\n1. item', 'found [paragraph, ordered_list]'),
    ('**Update**\n\n| a |\n|---|\n| b |', 'found [paragraph, table]'),
    ('**Update**\n\n- item\n\n```\ncode\n```', 'found [paragraph, bullet_list, fence]'),
    ('**Update**\n\n- item\n  - nested', 'each bullet must be one paragraph'),
    ('**Update**\n\n' + '- item\n' * 6, 'has 6 bullets; write 1 to 5'),
    ('**Update**\n\n- item ' + 'x' * 32000, 'shorten it to at most 32000'),
])
def test_errors_are_specific(markdown, problem):
    errors = submit.validate({**GOOD, 'dev': markdown}, MODES, 5)
    assert all(error.startswith('dev.md:') for error in errors)
    assert any(problem in error for error in errors) if problem else errors == []


@pytest.fixture
def update_dir(tmp_path, monkeypatch):
    for key, value in {'UPDATE_DIR': str(tmp_path), 'HAS_DEV': 'true', 'HAS_COMMUNITY': 'true', 'MAX_BULLETS': '5',
                       'GITHUB_REPOSITORY': 'owner/repo', 'COMMIT_COUNT': '2', 'FILE_COUNT': '3'}.items():
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
    write(update_dir, {**GOOD, 'dev': INCIDENT})
    decision = hook(monkeypatch, capsys)
    assert decision['decision'] == 'block' and 'dev.md: is JSON' in decision['reason']
    write(update_dir, GOOD)
    assert hook(monkeypatch, capsys) is None


def test_hook_stops_blocking_after_the_cap(update_dir, monkeypatch, capsys):
    assert [hook(monkeypatch, capsys) is not None for _ in range(submit.MAX_BLOCKS + 1)] == [True] * submit.MAX_BLOCKS + [False]


def test_check_prints_errors_or_exact_messages(update_dir, monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['submit.py'])
    write(update_dir, {'dev': DEV})
    assert submit.main() == 1
    assert 'community.md: does not exist' in capsys.readouterr().out
    write(update_dir, GOOD)
    assert submit.main() == 0
    output = capsys.readouterr().out
    assert f'===== telegram (dev) =====\n{DEV.strip()}\n\n[repo · 2 commit(s) · 3 file(s)](https://github.com/owner/repo)' in output
    assert '===== twitter (community) =====\nMore reliable updates\n\n• 📣 Announcements no longer arrive twice' in output


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
