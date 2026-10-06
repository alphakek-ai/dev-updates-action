"""Validate the generated <mode>.md updates and preview the exact message per channel.

The summarizing agent may run this script at any time; Claude Code runs it with
--hook on Stop and --guard before Bash; publication re-validates before sending.
"""

import json
import os
from pathlib import Path
import re
import shlex
import sys

from dispatch import RENDERERS, markdown_parser, render

TITLE_MAX = 80
BULLET_MAX = 280
MAX_BLOCKS = 4
SOURCE_MAX = 32000  # Telegram's rich-message limit is 32768; the rest is left for the footer.
COMMAND = f'{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}'
SHAPE = ('Write exactly a bold title line (**Title**) or a plain heading (# Title), a blank line, '
         'then one "- " bullet list.')
INLINE = {'softbreak': 'must be a single line', 'hardbreak': 'must be a single line',
          'html_inline': 'contains raw HTML; write plain markdown', 'image': 'contains an image; remove it',
          's_open': 'contains strikethrough; remove it', 'code_inline': 'contains code; describe it in plain words',
          'link_open': 'contains a link; remove it', 'strong_open': 'contains bold text; remove the emphasis',
          'em_open': 'contains italic text; remove the emphasis'}
ALLOWED = {('title', 'dev'): {'text', 'code_inline'}, ('title', 'community'): {'text'},
           ('bullet', 'dev'): {'text', 'code_inline', 'strong_open', 'em_open', 'link_open'},
           ('bullet', 'community'): {'text', 'strong_open', 'em_open'}}
COMMUNITY = [
    (re.compile(r'(?:^|[\s(])(?:\.{0,2}/)?(?:[\w.-]+/)+[\w-]*[A-Za-z][\w-]*\.[A-Za-z]\w*|\b[\w-]+\.(?:py|pyi|js|jsx|'
                r'ts|tsx|json|ya?ml|toml|md|sql|sh|go|rs|rb|java|kt|css|html|lock|env|ini|cfg)\b|(?:^|\s)/?(?:[\w.-]+/){2,}'),
     'mentions a file path or file-like name ({!r}); describe the user-facing effect instead'),
    (re.compile(r'\bv\d+(?:\.\d+)*\b|\b\d+\.\d+\.\d+\b|@\d'),
     'mentions a version number ({!r}); community text must not name versions'),
]


def _inline_errors(where, tokens, kind, mode):
    errors = []
    for token in tokens:
        if token.nesting == -1 or token.type in ALLOWED[kind, mode]:
            # The diff is untrusted input; links may not lead readers off GitHub.
            if token.type == 'link_open' and not str(token.attrs.get('href', '')).startswith('https://github.com/'):
                errors.append(f'{where}: links must point to https://github.com/; remove other links')
            if token.type == 'code_inline' and '`' in token.content:
                errors.append(f'{where}: has a backtick inside inline code; remove it')
            continue
        message = INLINE.get(token.type, f'contains unsupported markdown ({token.type}); remove it')
        errors.append(f'{where}: {message}')
    text = ''.join(token.content for token in tokens if token.type in ('text', 'code_inline'))
    limit = TITLE_MAX if kind == 'title' else BULLET_MAX
    if not text.strip():
        errors.append(f'{where}: is empty')
    if len(text) > limit:
        errors.append(f'{where}: is {len(text)} characters of text; shorten it to at most {limit}')
    try:
        if isinstance(json.loads(text), (dict, list)):
            errors.append(f'{where}: is a JSON value; write the text itself')
    except ValueError:
        pass
    prose = ' '.join(token.content for token in tokens if token.type == 'text')
    match = re.search(r'[*_~`]|\|\||==', prose)
    if match:
        errors.append(f'{where}: has a literal {match.group()!r}, which some channels format; rephrase without it')
    if re.search(r'://|\bwww\.', prose):
        errors.append(f'{where}: contains a bare URL; use a markdown link to https://github.com/ (dev) or remove it')
    if re.search(r'(?<![\w@])@[A-Za-z_]', prose):
        errors.append(f'{where}: mentions an @account, which notifies it on Telegram; remove the mention')
    if re.search(r'\$\d', prose):
        errors.append(f'{where}: has "$" before a digit, which Telegram renders as math; write amounts like "5 USD"')
    for pattern, message in COMMUNITY if mode == 'community' else []:
        match = pattern.search(prose)
        if match:
            errors.append(f'{where}: ' + message.format(match.group().strip()))
    return errors


def validate_markdown(source, mode, max_bullets):
    """Return actionable errors for one mode's markdown; empty means it can be published."""
    if not isinstance(source, str) or not source.strip():
        return [f'{mode}.md: is empty. {SHAPE}']
    try:
        if isinstance(json.loads(source), (dict, list)):
            return [f'{mode}.md: is JSON; write markdown, not JSON. {SHAPE}']
    except ValueError:
        pass
    if len(source) > SOURCE_MAX:
        return [f'{mode}.md: is {len(source)} characters; shorten it to at most {SOURCE_MAX}']
    tokens = markdown_parser().parse(source)
    blocks = [token for token in tokens if token.level == 0 and token.nesting != -1]
    if [token.type for token in blocks[1:]] != ['bullet_list_open'] or blocks[0].type not in ('paragraph_open', 'heading_open'):
        found = ', '.join(token.type.removesuffix('_open') for token in blocks)
        return [f'{mode}.md: found blocks [{found}]. {SHAPE} Nothing else (no HTML, images, tables, quotes or code blocks).']
    title = [token for token in tokens[1].children if token.type != 'text' or token.content]
    if blocks[0].type == 'paragraph_open':
        if not title or title[0].type != 'strong_open' or title[-1].type != 'strong_close':
            return [f'{mode}.md: the title must be one bold line (**Title**) or a heading (# Title)']
        title = title[1:-1]
    errors = _inline_errors(f'{mode}.md title', title, 'title', mode)
    items = [i for i, token in enumerate(tokens) if token.type == 'list_item_open' and token.level == 1]
    if not 1 <= len(items) <= max_bullets:
        errors.append(f'{mode}.md: has {len(items)} bullets; write 1 to {max_bullets}')
    for n, i in enumerate(items, 1):
        if [token.type for token in tokens[i + 1:i + 4]] != ['paragraph_open', 'inline', 'paragraph_close'] \
                or tokens[i + 4].type != 'list_item_close':
            errors.append(f'{mode}.md bullet {n}: must be one line of text (no nested lists, quotes or code blocks)')
            continue
        errors += _inline_errors(f'{mode}.md bullet {n}', tokens[i + 2].children, 'bullet', mode)
    return errors


def validate(update, modes, max_bullets):
    """Errors for an update mapping each active mode to its markdown."""
    if not isinstance(update, dict) or set(update) != set(modes):
        return [f'the update must have exactly these modes: {", ".join(modes)}']
    return [error for mode in modes for error in validate_markdown(update[mode], mode, max_bullets)]


def load(directory, modes, max_bullets):
    """Read and validate <mode>.md files; returns (update, errors)."""
    update, errors = {}, []
    for mode in modes:
        try:
            update[mode] = (Path(directory) / f'{mode}.md').read_text()
        except FileNotFoundError:
            errors.append(f'{Path(directory) / f"{mode}.md"}: does not exist; write it with the Write tool')
    return update, errors or validate(update, modes, max_bullets)


def main():
    if '--guard' in sys.argv:
        # PreToolUse hook: Claude Code auto-approves read-only commands in working directories.
        try:
            allowed = json.load(sys.stdin)['tool_input']['command'] == COMMAND
        except Exception:
            allowed = False
        if not allowed:
            print(f'The only permitted shell command is: {COMMAND}', file=sys.stderr)
            return 2  # Exit code 2 denies the tool call.
        return 0
    directory = Path(os.environ['UPDATE_DIR'])
    modes = [mode for mode in ('dev', 'community') if os.environ.get('HAS_' + mode.upper()) == 'true']
    update, errors = load(directory, modes, int(os.environ['MAX_BULLETS']))
    if '--hook' in sys.argv:
        sys.stdin.read()
        attempts = directory / 'stop-attempts'
        blocked = int(attempts.read_text()) if attempts.exists() else 0
        # Publication re-validates, so a capped run still cannot publish an invalid update.
        if errors and blocked < MAX_BLOCKS:
            attempts.write_text(str(blocked + 1))
            reason = 'The update is not valid yet. Fix these errors, then stop:\n' + '\n'.join(errors)
            print(json.dumps({'decision': 'block', 'reason': reason}))
        return 0
    if errors:
        print('INVALID - fix these errors and run this check again:')
        print('\n'.join(f'- {error}' for error in errors))
        return 1
    repo = os.environ.get('GITHUB_REPOSITORY', 'owner/repo')
    commits, files = os.environ.get('COMMIT_COUNT', '?'), os.environ.get('FILE_COUNT', '?')
    print('VALID - these are the exact messages that will be published:')
    for mode in modes:
        for channel in ['telegram', *RENDERERS]:
            print(f'\n===== {channel} ({mode}) =====\n{render(channel, update[mode], mode, repo, commits, files)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
