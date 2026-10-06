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


def errors_for(mode, bullet):
    return submit.validate({**GOOD, mode: f'**Update**\n\n- {bullet}\n'}, MODES, 5)


def test_good_update_is_valid():
    assert submit.validate(GOOD, MODES, 5) == []


@pytest.mark.parametrize('markdown, problem', [
    (INCIDENT, 'is JSON'),
    ('**Update**\n\n- ' + INCIDENT, 'JSON value'),
    ('Update\n\n- item', 'title must be one bold line'),
    ('**Update**\n\nIntro paragraph.\n\n- item', 'found blocks [paragraph, paragraph, bullet_list]'),
    ('**Update**\n\n1. item', 'found blocks [paragraph, ordered_list]'),
    ('**Update**\n\n| a |\n|---|\n| b |', 'found blocks [paragraph, table]'),
    ('**Update**\n\n<div>x</div>', 'found blocks [paragraph, html_block]'),
    ('**Update**\n\n- item\n\n```\ncode\n```', 'found blocks [paragraph, bullet_list, fence]'),
    ('**Update**\n\n- item\n  - nested', 'bullet 1: must be one line'),
    ('**Update**\n\n' + '- item\n' * 6, 'has 6 bullets; write 1 to 5'),
    ('**' + 'x' * 81 + '**\n\n- item', 'title: is 81 characters'),
    ('**Update**\n\n- item ' + 'x' * 32000, 'shorten it to at most 32000'),
])
def test_structure_errors_are_specific(markdown, problem):
    errors = submit.validate({**GOOD, 'dev': markdown}, MODES, 5)
    assert any(problem in error for error in errors), errors


@pytest.mark.parametrize('bullet, problem', [
    ('first line\n  second line', 'single line'),
    ('see <b>this</b>', 'raw HTML'),
    ('![chart](https://example.com/c.png)', 'image'),
    ('~~gone~~', 'strikethrough'),
    ('[docs](ftp://example.com)', 'http(s) URL'),
    ('x' * 281, 'at most 280'),
    ('Fees from $5 and up', 'math'),
    ('5 \\* 3 and snake\\_case', 'literal "*" or "_"'),
])
def test_dev_inline_errors_name_the_bullet(bullet, problem):
    errors = errors_for('dev', bullet)
    assert errors and all(error.startswith('dev.md bullet 1:') for error in errors)
    assert any(problem in error for error in errors)


@pytest.mark.parametrize('bullet, problem', [
    ('Faster `sync`', 'contains code'),
    ('See [docs](https://example.com)', 'contains a link'),
    ('Updated src/app/sync.py for speed', 'file path'),
    ('Tuned config.yaml defaults', 'file path'),
    ('Upgraded to v2.4', 'version'),
    ('Runs on engine 1.2.3 now', 'version'),
])
def test_community_rejects_internal_details(bullet, problem):
    assert any(error.startswith('community.md bullet 1:') and problem in error
               for error in errors_for('community', bullet))


@pytest.mark.parametrize('bullet', ['Sync is 2.5x faster', 'Works 24/7 and/or offline', 'Loads in 1/2.5x the time',
                                    'Shipped for $AIKEK holders'])
def test_community_accepts_plain_language(bullet):
    assert errors_for('community', bullet) == []


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
