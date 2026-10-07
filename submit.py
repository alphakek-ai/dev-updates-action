"""Validate the generated <mode>.md updates and preview the exact message per channel.

The summarizing agent may run this script at any time; Claude Code runs it with
--hook on Stop and --guard before Bash; publication re-validates before sending.
"""

import json
import os
from pathlib import Path
import shlex
import sys

from dispatch import RENDERERS, markdown_parser, render

MAX_BLOCKS = 4
SOURCE_MAX = 32000  # Telegram's rich-message limit is 32768; the rest is left for the footer.
COMMAND = f'{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}'
SHAPE = 'Write a title line (**Title** or # Title), a blank line, then one "- " bullet list.'


def validate_markdown(source, mode, max_bullets):
    """Errors for one mode's markdown; empty means every channel can render it."""
    if not isinstance(source, str) or not source.strip():
        return [f'{mode}.md: is empty. {SHAPE}']
    try:
        if isinstance(json.loads(source), (dict, list)):
            return [f'{mode}.md: is JSON; write markdown instead. {SHAPE}']
    except ValueError:
        pass
    if len(source) > SOURCE_MAX:
        return [f'{mode}.md: is {len(source)} characters; shorten it to at most {SOURCE_MAX}']
    tokens = markdown_parser().parse(source)
    blocks = [token.type.removesuffix('_open') for token in tokens if token.level == 0 and token.nesting != -1]
    if blocks not in (['paragraph', 'bullet_list'], ['heading', 'bullet_list']):
        return [f'{mode}.md: found [{", ".join(blocks)}]. {SHAPE} Nothing else.']
    items = [i for i, token in enumerate(tokens) if token.type == 'list_item_open' and token.level == 1]
    # Each bullet must be one paragraph so the other channels can rebuild it line by line.
    shape = ['paragraph_open', 'inline', 'paragraph_close', 'list_item_close']
    if any([token.type for token in tokens[i + 1:i + 5]] != shape for i in items):
        return [f'{mode}.md: each bullet must be one paragraph, with no nested lists or blocks']
    if not 1 <= len(items) <= max_bullets:
        return [f'{mode}.md: has {len(items)} bullets; write 1 to {max_bullets}']
    return []


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
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f'{Path(directory) / f"{mode}.md"}: cannot be read ({error}); rewrite it')
    return update, errors or validate(update, modes, max_bullets)


def check():
    directory = Path(os.environ['UPDATE_DIR'])
    modes = [mode for mode in ('dev', 'community') if os.environ.get('HAS_' + mode.upper()) == 'true']
    return directory, modes, *load(directory, modes, int(os.environ['MAX_BULLETS']))


def stop_hook():
    directory, _, _, errors = check()
    attempts = directory / 'stop-attempts'
    blocked = int(attempts.read_text()) if attempts.exists() else 0
    # Publication re-validates, so a capped run still cannot publish an invalid update.
    if errors and blocked < MAX_BLOCKS:
        attempts.write_text(str(blocked + 1))
        reason = 'The update is not valid yet. Fix these errors, then stop:\n' + '\n'.join(errors)
        print(json.dumps({'decision': 'block', 'reason': reason}))
    return 0


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
    if '--hook' in sys.argv:
        sys.stdin.read()
        try:
            return stop_hook()
        except Exception as error:
            # A crashing Stop hook would let the agent stop without being told why.
            print(json.dumps({'decision': 'block', 'reason': f'The update check failed: {error!r}'}))
            return 0
    directory, modes, update, errors = check()
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
