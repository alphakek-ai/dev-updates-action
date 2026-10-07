"""Validate the generated <mode>.md updates and preview the exact message per channel.

The summarizing agent may run this script at any time; Claude Code runs it with
--hook on Stop and --guard before Bash; publication re-validates before sending.
"""

import json
import os
from pathlib import Path
import shlex
import sys

from dispatch import DIALECTS, render

MAX_BLOCKS = 4
SOURCE_MAX = 32000  # Telegram's rich-message limit is 32768; the rest is left for the footer.
COMMAND = f'{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))}'


def validate(update, modes):
    """Errors for an update mapping each active mode to its markdown; empty means it can be published."""
    if not isinstance(update, dict) or set(update) != set(modes):
        return [f'the update must have exactly these modes: {", ".join(modes)}']
    errors = []
    for mode, source in update.items():
        if not isinstance(source, str) or not source.strip():
            errors.append(f'{mode}.md: is empty; write the update as markdown')
            continue
        try:
            if isinstance(json.loads(source), (dict, list)):
                errors.append(f'{mode}.md: is JSON; write markdown instead')
        except ValueError:
            pass
        if len(source) > SOURCE_MAX:
            errors.append(f'{mode}.md: is {len(source)} characters; shorten it to at most {SOURCE_MAX}')
    return errors


def load(directory, modes):
    """Read and validate <mode>.md files; returns (update, errors)."""
    update, errors = {}, []
    for mode in modes:
        try:
            update[mode] = (Path(directory) / f'{mode}.md').read_text()
        except FileNotFoundError:
            errors.append(f'{Path(directory) / f"{mode}.md"}: does not exist; write it with the Write tool')
        except (OSError, UnicodeDecodeError) as error:
            errors.append(f'{Path(directory) / f"{mode}.md"}: cannot be read ({error}); rewrite it')
    return update, errors or validate(update, modes)


def check():
    directory = Path(os.environ['UPDATE_DIR'])
    modes = [mode for mode in ('dev', 'community') if os.environ.get('HAS_' + mode.upper()) == 'true']
    return directory, modes, *load(directory, modes)


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
        # load() reports file problems; any other failure is a setup fault that summarize.py turns into a failed run.
        return stop_hook()
    directory, modes, update, errors = check()
    if errors:
        print('INVALID - fix these errors and run this check again:')
        print('\n'.join(f'- {error}' for error in errors))
        return 1
    repo = os.environ.get('GITHUB_REPOSITORY', 'owner/repo')
    commits, files = os.environ.get('COMMIT_COUNT', '?'), os.environ.get('FILE_COUNT', '?')
    print('VALID - these are the exact messages that will be published:')
    for mode in modes:
        for channel in ['telegram', *DIALECTS]:
            print(f'\n===== {channel} ({mode}) =====\n{render(channel, update[mode], mode, repo, commits, files)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
