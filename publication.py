"""Durable publication checkpoint and per-channel delivery journal."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import time

from dispatch import DISPATCHERS, DeliveryNotAttempted, _is_required, _normalize_mode, load_summary, parse_channels


def git(*args, input=None):
    auth = {}
    if os.environ.get('GH_TOKEN'):
        credential = base64.b64encode(('x-access-token:' + os.environ['GH_TOKEN']).encode()).decode()
        auth = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.https://github.com/.extraheader',
                'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + credential}
    return subprocess.check_output(
        ['git', *args], input=input, text=True, timeout=60,
        env={**os.environ, **auth, 'GIT_AUTHOR_NAME': 'github-actions[bot]',
             'GIT_AUTHOR_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com',
             'GIT_COMMITTER_NAME': 'github-actions[bot]',
             'GIT_COMMITTER_EMAIL': '41898282+github-actions[bot]@users.noreply.github.com'},
    ).strip()


class Journal:
    def __init__(self, branch):
        if not branch.startswith('dev-updates-state/'):
            raise ValueError('State branch must start with dev-updates-state/')
        self.ref = 'refs/heads/' + branch
        git('check-ref-format', self.ref)
        self.revision = ''
        self.state = None

    def load(self):
        # An empty successful response means absent; authentication/network errors
        # propagate. Never infer a checkpoint from workflow-run listing order.
        remote = git('ls-remote', '--refs', 'origin', self.ref)
        if not remote:
            raise RuntimeError('State branch missing; explicitly initialize the publication checkpoint')
        git('fetch', '--no-tags', 'origin', self.ref)
        self.revision = git('rev-parse', 'FETCH_HEAD')
        self.state = json.loads(git('show', self.revision + ':state.json'))
        if self.state['version'] != 1 or not re.fullmatch(r'[0-9a-f]{40}', self.state['last_sha']):
            raise ValueError('Invalid publication checkpoint')
        if not isinstance(self.state['last_at'], int) or self.state['last_at'] < 0:
            raise ValueError('Invalid publication timestamp')
        return self.state

    def save(self):
        blob = git('hash-object', '-w', '--stdin', input=json.dumps(self.state, sort_keys=True))
        tree = git('mktree', input=f'100644 blob {blob}\tstate.json\n')
        parent = ['-p', self.revision] if self.revision else []
        commit = git('commit-tree', tree, *parent, '-m', 'chore: record publication state')
        # The explicit lease also rejects a stale reader before it can send.
        git('push', f'--force-with-lease={self.ref}:{self.revision}',
            'origin', f'{commit}:{self.ref}')
        self.revision = commit


def seconds(value):
    match = re.fullmatch(r'(\d+)([smhd]?)', value or '0')
    if not match:
        raise ValueError('Invalid cooldown')
    return int(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]


def ancestor(before, after):
    result = subprocess.run(['git', 'merge-base', '--is-ancestor', before, after], timeout=60)
    if result.returncode not in (0, 1):
        raise RuntimeError('Cannot verify commit ancestry; fetch full history')
    return result.returncode == 0


def channel_fingerprint(channels):
    names = [ch['name'] for ch in channels]
    if not names or len(set(names)) != len(names):
        raise ValueError('Channels require unique names')
    # Only a hash is persisted: channel configuration can contain webhook secrets.
    return hashlib.sha256(json.dumps(channels, sort_keys=True).encode()).hexdigest()


def prepare(journal, head, cooldown, now, channels):
    state = journal.load()
    fingerprint = channel_fingerprint(channels)
    batch = state['batch']
    if batch:
        if batch['channels'] != fingerprint:
            raise RuntimeError('Resolve the outstanding batch before changing channels')
        return {'skip': 'false', 'resume': 'true', 'before': batch['before'], 'after': batch['after']}
    last = state['last_sha']
    if ancestor(head, last):
        return {'skip': 'true'}  # Includes re-runs of older commits.
    if not ancestor(last, head):
        raise RuntimeError('Checkpoint is not an ancestor of HEAD; refusing to replay a guessed range')
    if now < state['last_at']:
        raise RuntimeError('Checkpoint timestamp is in the future')
    if now - state['last_at'] < seconds(cooldown):
        return {'skip': 'true'}  # Applies to scheduled runs as well as pushes.
    return {'skip': 'false', 'resume': 'false', 'before': last, 'after': head}


def publish(journal, channels, before, after, summaries, repo, now, senders=DISPATCHERS):
    state = journal.load()
    fingerprint = channel_fingerprint(channels)
    batch = state['batch']
    if not batch:
        if state['last_sha'] != before or not ancestor(before, after) or before == after:
            raise RuntimeError('Checkpoint changed after preparation; refusing stale publication')
        modes = {_normalize_mode(ch.get('mode', 'dev')) for ch in channels}
        if any(not summaries.get(mode) for mode in modes):
            raise ValueError('Missing generated summary')
        batch = state['batch'] = {
            'before': before, 'after': after, 'channels': fingerprint,
            'summaries': summaries, 'deliveries': {ch['name']: 'ready' for ch in channels},
            'commits': git('rev-list', '--count', f'{before}..{after}'),
            'files': str(len(git('diff', '--name-only', before, after).splitlines())),
        }
        journal.save()
    if (batch['before'], batch['after'], batch['channels']) != (before, after, fingerprint):
        raise RuntimeError('Outstanding publication does not match prepared range/channels')
    for ch in channels:
        name = ch['name']
        status = batch['deliveries'][name]
        if status in ('sent', 'skipped'):
            continue
        if status == 'pending':
            if not _is_required(ch):
                batch['deliveries'][name] = 'skipped'
                journal.save()
                print(f'WARN: {name}: uncertain optional delivery abandoned without retry')
                continue
            print(f'ERROR: {name}: delivery outcome unknown; operator reconciliation required')
            continue
        if status != 'ready':
            raise ValueError('Invalid delivery status')
        sender = senders[ch.get('type', 'telegram')]
        content = batch['summaries'][_normalize_mode(ch.get('mode', 'dev'))]
        batch['deliveries'][name] = 'pending'
        journal.save()  # Must succeed before any external delivery.
        try:
            sender(ch, content, repo, repo.split('/')[-1], batch['commits'], batch['files'])
        except Exception as error:
            # Only definitive rejection permits a retry. Timeouts/5xx may follow
            # successful delivery; leave the durable pending marker untouched.
            code = getattr(error, 'code', None)
            if code is None and getattr(error, 'response', None) is not None:
                code = error.response.status_code
            if not _is_required(ch) or isinstance(error, DeliveryNotAttempted) or code in (400, 401, 402, 403, 404, 422, 429):
                batch['deliveries'][name] = 'ready' if _is_required(ch) else 'skipped'
                journal.save()
            print(f'ERROR: {name}: {type(error).__name__}; status={code}')
            continue
        batch['deliveries'][name] = 'sent'
        state['last_at'] = int(now())
        journal.save()
        print(f'OK: {name}')
    statuses = set(batch['deliveries'].values())
    if statuses == {'skipped'}:
        batch['deliveries'] = dict.fromkeys(batch['deliveries'], 'ready')
        journal.save()
    if not statuses <= {'sent', 'skipped'} or 'sent' not in statuses:
        raise RuntimeError('Publication unfinished; successful channels will not be resent')
    state['last_sha'] = after
    state['batch'] = None
    journal.save()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'publish', 'initialize', 'resolve'])
    parser.add_argument('--branch', default=os.environ.get('STATE_BRANCH', 'dev-updates-state/default'))
    parser.add_argument('--sha')
    parser.add_argument('--at', type=int)
    parser.add_argument('--channel')
    parser.add_argument('--outcome', choices=['sent', 'retry'])
    args = parser.parse_args()
    journal = Journal(args.branch)
    if args.command == 'initialize':
        if not args.sha or args.at is None or args.at < 0:
            parser.error('initialize requires --sha and --at from a verified last publication')
        sha = git('rev-parse', '--verify', args.sha + '^{commit}')
        journal.state = {'version': 1, 'last_sha': sha, 'last_at': args.at, 'batch': None}
        journal.save()  # Empty lease: initialization can never overwrite existing state.
        return
    if args.command == 'resolve':
        if not args.channel or not args.outcome:
            parser.error('resolve requires --channel and --outcome')
        state = journal.load()
        if not state['batch'] or state['batch']['deliveries'].get(args.channel) != 'pending':
            raise RuntimeError('Only pending deliveries can be reconciled')
        state['batch']['deliveries'][args.channel] = 'sent' if args.outcome == 'sent' else 'ready'
        if args.outcome == 'sent':
            state['last_at'] = int(time.time())
        journal.save()
        return
    channels = parse_channels(os.environ['CHANNELS'])
    if args.command == 'prepare':
        result = prepare(journal, git('rev-parse', 'HEAD'), os.environ.get('COOLDOWN', ''), int(time.time()), channels)
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            for key, value in result.items():
                output.write(f'{key}={value}\n')
        print(json.dumps(result))
    else:
        publish(journal, channels, os.environ['BEFORE'], os.environ['AFTER'],
                {mode: load_summary(mode) for mode in ('dev', 'community')},
                os.environ['GITHUB_REPOSITORY'], time.time)


if __name__ == '__main__':
    main()
