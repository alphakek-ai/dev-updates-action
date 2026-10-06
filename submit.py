"""Validate the generated update.json and preview the exact message per channel.

The summarizing agent may run `python3 submit.py` at any time; Claude Code hooks
run `python3 submit.py --hook`; publication re-validates before sending.
"""

import json
import os
from pathlib import Path
import re
import shlex
import sys

from dispatch import render_discord, render_slack, render_telegram, render_twitter

TITLE_MAX = 80
BULLET_MAX = 280
TOTAL_MAX = 3500  # Leaves room for the footer under Telegram's 4096-character limit.
MAX_BLOCKS = 4
COMMAND = 'python3 ' + shlex.quote(str(Path(__file__).resolve()))
RENDERERS = {'telegram': render_telegram, 'discord': render_discord,
             'slack': render_slack, 'twitter': render_twitter}

MARKUP = [
    (re.compile(r'\*\*|__|~~'), 'contains markdown emphasis ({!r}); write plain text'),
    (re.compile(r'\[[^\]]*\]\([^)]*\)'), 'contains a markdown link ({!r}); write plain text without links'),
    (re.compile(r'^\s*(?:#|>|[-*+•]\s|\d+[.)]\s)'), 'starts with a markdown heading, quote or list marker ({!r}); '
                                                    'the renderer lays out the title and bullets itself'),
    (re.compile(r'</?[A-Za-z][^>]*>|&#?\w+;'), 'contains HTML markup ({!r}); write plain text'),
    (re.compile(r'\{\s*"'), 'contains a JSON object ({!r}); put plain text in each field, not JSON'),
]
COMMUNITY = [
    (re.compile(r'`'), 'contains a backtick; community text must not quote code'),
    (re.compile(r'(?:^|[\s(])(?:\.{0,2}/)?(?:[\w.-]+/)+[\w-]*\.\w+|\b[\w-]+\.(?:py|pyi|js|jsx|ts|tsx|json|ya?ml|toml|md|'
                r'sql|sh|go|rs|rb|java|kt|css|html|lock|env|ini|cfg)\b|(?:^|\s)/?(?:[\w.-]+/){2,}'),
     'mentions a file path ({!r}); describe the user-facing effect instead'),
    (re.compile(r'\bv\d+(?:\.\d+)*\b|\b\d+\.\d+\.\d+\b|@\d'),
     'mentions a version number ({!r}); community text must not name versions'),
]


def _field_errors(where, text, mode, limit):
    if not isinstance(text, str) or not text.strip():
        return [f'{where}: must be a non-empty string']
    errors = []
    if len(text) > limit:
        errors.append(f'{where}: is {len(text)} characters; shorten it to at most {limit}')
    if any(ord(char) < 32 for char in text):
        errors.append(f'{where}: must be a single line without tabs or control characters')
    try:
        if isinstance(json.loads(text), (dict, list)):
            errors.append(f'{where}: is a JSON value; put plain text in each field, not JSON')
    except ValueError:
        pass
    prose = text
    if mode == 'dev':
        if text.count('`') % 2 or '``' in text:
            errors.append(f'{where}: has unbalanced or empty backticks; quote each identifier as `name`')
        prose = re.sub(r'`[^`]*`', 'x', text)
    for pattern, message in MARKUP + (COMMUNITY if mode == 'community' else []):
        match = pattern.search(prose)
        if match:
            errors.append(f'{where}: ' + message.format(match.group().strip()))
    return errors


def validate(update, modes, max_bullets):
    """Return a list of actionable errors; empty means the update can be published."""
    if not isinstance(update, dict):
        return ['update.json: must be a JSON object keyed by mode, e.g. '
                + json.dumps({mode: {'title': '...', 'bullets': ['...']} for mode in modes})]
    errors = [f'{key}: unexpected key; only {", ".join(modes)} are published, remove it'
              for key in update if key not in modes]
    for mode in modes:
        entry = update.get(mode)
        if not isinstance(entry, dict) or set(entry) != {'title', 'bullets'}:
            errors.append(f'{mode}: must be an object with exactly the keys "title" and "bullets"')
            continue
        errors += _field_errors(f'{mode}.title', entry['title'], mode, TITLE_MAX)
        bullets = entry['bullets']
        if not isinstance(bullets, list) or not 1 <= len(bullets) <= max_bullets:
            errors.append(f'{mode}.bullets: must be a list of 1 to {max_bullets} strings')
            continue
        for i, bullet in enumerate(bullets):
            errors += _field_errors(f'{mode}.bullets[{i}]', bullet, mode, BULLET_MAX)
        texts = [entry['title'], *bullets]
        if all(isinstance(text, str) for text in texts) and sum(map(len, texts)) > TOTAL_MAX:
            errors.append(f'{mode}: title and bullets total more than {TOTAL_MAX} characters; shorten them')
    return errors


def load(path, modes, max_bullets):
    """Read and validate an update file; returns (update, errors)."""
    try:
        update = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None, [f'{path}: does not exist; write it with the Write tool']
    except ValueError as error:
        return None, [f'{path}: is not valid JSON ({error}); rewrite the whole file']
    return update, validate(update, modes, max_bullets)


def main():
    event = json.load(sys.stdin) if '--hook' in sys.argv else {}
    if event.get('hook_event_name') == 'PreToolUse':
        # Claude Code auto-approves read-only commands in working directories; allow only this check.
        if event.get('tool_input', {}).get('command') != COMMAND:
            print(json.dumps({'hookSpecificOutput': {
                'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                'permissionDecisionReason': f'The only permitted shell command is: {COMMAND}'}}))
        return 0
    path = Path(os.environ['UPDATE_FILE'])
    modes = [mode for mode in ('dev', 'community') if os.environ.get('HAS_' + mode.upper()) == 'true']
    update, errors = load(path, modes, int(os.environ['MAX_BULLETS']))
    if event:
        attempts = path.with_name('stop-attempts')
        blocked = int(attempts.read_text()) if attempts.exists() else 0
        # Publication re-validates, so a capped run still cannot publish an invalid file.
        if errors and blocked < MAX_BLOCKS:
            attempts.write_text(str(blocked + 1))
            reason = 'update.json is not valid yet. Fix these errors, then stop:\n' + '\n'.join(errors)
            print(json.dumps({'decision': 'block', 'reason': reason}))
        return 0
    if errors:
        print('INVALID - fix these errors in update.json and run this check again:')
        print('\n'.join(f'- {error}' for error in errors))
        return 1
    repo = os.environ.get('GITHUB_REPOSITORY', 'owner/repo')
    commits, files = os.environ.get('COMMIT_COUNT', '?'), os.environ.get('FILE_COUNT', '?')
    print('VALID - these are the exact messages that will be published:')
    for mode in modes:
        for channel, render in RENDERERS.items():
            print(f'\n===== {channel} ({mode}) =====\n{render(update[mode], mode, repo, commits, files)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
