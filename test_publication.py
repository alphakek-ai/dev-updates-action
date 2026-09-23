import subprocess
import urllib.error

import pytest

from publication import Journal, git, prepare, publish, seconds


CHANNELS = [{'name': 'team', 'type': 'telegram', 'mode': 'dev'},
            {'name': 'public', 'type': 'telegram', 'mode': 'community'}]
SUMMARIES = {'dev': 'Technical update', 'community': 'Public update'}


@pytest.fixture
def history(tmp_path, monkeypatch):
    remote = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    work = tmp_path / 'work'
    work.mkdir()
    monkeypatch.chdir(work)
    git('init')
    git('remote', 'add', 'origin', str(remote))
    commits = []
    for i in range(3):
        (work / 'source.txt').write_text(str(i))
        git('add', 'source.txt')
        git('commit', '-m', f'feat: change {i}')
        commits.append(git('rev-parse', 'HEAD'))
    journal = Journal('dev-updates-state/test')
    journal.state = {'version': 1, 'last_sha': commits[0], 'last_at': 100, 'batch': None}
    journal.save()
    return journal, commits


def deliver(journal, commits, sender):
    return publish(journal, CHANNELS, commits[0], commits[1], SUMMARIES,
                   'owner/repo', lambda: 200, {'telegram': sender})


def test_september_replay_does_not_repeat_completed_range(history):
    journal, commits = history
    calls = []
    deliver(journal, commits, lambda *args: calls.append(args))
    assert [call[0]['name'] for call in calls] == ['team', 'public']
    assert prepare(journal, commits[1], '12h', 300, CHANNELS) == {'skip': 'true'}
    assert prepare(journal, commits[2], '12h', 400, CHANNELS) == {'skip': 'true'}
    result = prepare(journal, commits[2], '12h', 50000, CHANNELS)
    assert result['before'] == commits[1] and result['after'] == commits[2]
    assert journal.load()['batch'] is None


@pytest.mark.parametrize('event', ['push', 'schedule'])
def test_cooldown_applies_to_every_trigger(history, monkeypatch, event):
    journal, commits = history
    monkeypatch.setenv('GITHUB_EVENT_NAME', event)
    assert prepare(journal, commits[1], '12h', 101, CHANNELS) == {'skip': 'true'}
    assert prepare(journal, commits[1], '12h', 43300, CHANNELS)['skip'] == 'false'


def test_required_channel_failure_does_not_resend_success(history):
    journal, commits = history
    calls = []
    def sender(ch, *args):
        calls.append(ch['name'])
        if ch['name'] == 'public':
            raise urllib.error.HTTPError('redacted', 429, 'Rate limited', {}, None)
    with pytest.raises(RuntimeError, match='unfinished'):
        deliver(journal, commits, sender)
    assert journal.load()['batch']['deliveries'] == {'team': 'sent', 'public': 'ready'}
    assert prepare(journal, commits[2], '12h', 201, CHANNELS)['resume'] == 'true'
    deliver(journal, commits, lambda ch, *args: calls.append(ch['name']))
    assert calls == ['team', 'public', 'public']


def test_timeout_after_remote_acceptance_is_not_retried(history):
    journal, commits = history
    calls = []
    def sender(ch, *args):
        calls.append(ch['name'])
        if ch['name'] == 'public':
            raise TimeoutError('Response lost after server accepted message')
    with pytest.raises(RuntimeError, match='unfinished'):
        deliver(journal, commits, sender)
    with pytest.raises(RuntimeError, match='unfinished'):
        deliver(journal, commits, sender)
    assert calls == ['team', 'public']
    assert journal.load()['batch']['deliveries']['public'] == 'pending'


def test_failed_write_after_send_leaves_pending_not_resend(history, monkeypatch):
    journal, commits = history
    calls = []
    real_save = journal.save
    def save():
        if journal.state['batch']['deliveries']['team'] == 'sent':
            raise RuntimeError('State write unavailable')
        real_save()
    monkeypatch.setattr(journal, 'save', save)
    with pytest.raises(RuntimeError, match='unavailable'):
        deliver(journal, commits, lambda ch, *args: calls.append(ch['name']))
    monkeypatch.setattr(journal, 'save', real_save)
    with pytest.raises(RuntimeError, match='unfinished'):
        deliver(journal, commits, lambda ch, *args: calls.append(ch['name']))
    assert calls == ['team', 'public']
    assert journal.load()['batch']['deliveries']['team'] == 'pending'


def test_failed_pre_send_write_sends_nothing(history, monkeypatch):
    journal, commits = history
    calls = []
    def fail():
        raise RuntimeError('State unavailable')
    monkeypatch.setattr(journal, 'save', fail)
    with pytest.raises(RuntimeError, match='unavailable'):
        deliver(journal, commits, lambda *args: calls.append(args))
    assert calls == []


def test_stale_reader_cannot_overwrite_newer_checkpoint(history):
    journal, commits = history
    stale = Journal('dev-updates-state/test')
    stale.load()
    deliver(journal, commits, lambda *args: None)
    stale.state['last_at'] = 999
    with pytest.raises(subprocess.CalledProcessError):
        stale.save()
    assert journal.load()['last_sha'] == commits[1]


def test_concurrent_publisher_loses_lease_before_sending(history, monkeypatch):
    journal, commits = history
    competitor = Journal('dev-updates-state/test')
    original_save = journal.save
    calls = []
    def racing_save():
        deliver(competitor, commits, lambda ch, *args: calls.append(ch['name']))
        original_save()
    monkeypatch.setattr(journal, 'save', racing_save)
    with pytest.raises(subprocess.CalledProcessError):
        deliver(journal, commits, lambda *args: pytest.fail('Stale publisher sent'))
    assert calls == ['team', 'public']


def test_missing_remote_fails_closed(history):
    journal, commits = history
    git('remote', 'set-url', 'origin', '/does-not-exist/publication.git')
    with pytest.raises(subprocess.CalledProcessError):
        prepare(journal, commits[1], '', 1000, CHANNELS)


def test_missing_branch_does_not_bootstrap_implicitly(history):
    _, commits = history
    with pytest.raises(RuntimeError, match='explicitly initialize'):
        prepare(Journal('dev-updates-state/missing'), commits[1], '', 1000, CHANNELS)


def test_initialization_cannot_overwrite_existing_state(history):
    journal, commits = history
    other = Journal('dev-updates-state/test')
    other.state = dict(journal.state, last_sha=commits[2])
    with pytest.raises(subprocess.CalledProcessError):
        other.save()
    assert journal.load()['last_sha'] == commits[0]


def test_no_fallback_for_divergent_history(history):
    journal, commits = history
    git('checkout', '--orphan', 'other-history')
    git('commit', '-m', 'feat: divergent history')
    with pytest.raises(RuntimeError, match='ancestor'):
        prepare(journal, git('rev-parse', 'HEAD'), '', 1000, CHANNELS)


def test_batch_retains_original_text_when_generation_changes(history):
    journal, commits = history
    def reject(ch, *args):
        raise urllib.error.HTTPError('redacted', 429, '', {}, None)
    with pytest.raises(RuntimeError):
        deliver(journal, commits, reject)
    texts = []
    publish(journal, CHANNELS, commits[0], commits[1], {}, 'owner/repo', lambda: 201,
            {'telegram': lambda ch, content, *args: texts.append(content)})
    assert texts == [SUMMARIES['dev'], SUMMARIES['community']]


def test_optional_definitive_rejection_does_not_block_completed_batch(history):
    journal, commits = history
    channels = [CHANNELS[0], dict(CHANNELS[1], required=False)]
    def sender(ch, *args):
        if ch['name'] == 'public':
            raise urllib.error.HTTPError('redacted', 403, '', {}, None)
    publish(journal, channels, commits[0], commits[1], SUMMARIES, 'owner/repo', lambda: 201,
            {'telegram': sender})
    assert journal.load()['last_sha'] == commits[1]


def test_changed_channel_configuration_blocks_batch_resume(history):
    journal, commits = history
    def timeout(*args):
        raise TimeoutError()
    with pytest.raises(RuntimeError):
        deliver(journal, commits, timeout)
    with pytest.raises(RuntimeError, match='changing channels'):
        prepare(journal, commits[2], '', 1000, [dict(CHANNELS[0], chat_id='new')])


@pytest.mark.parametrize('value', ['oops', '-1', '1hour'])
def test_bad_cooldown_is_rejected(value):
    with pytest.raises(ValueError):
        seconds(value)


def test_resolve_ambiguous_delivery_then_retry_only_missing_channel(history, monkeypatch):
    from publication import main
    journal, commits = history
    calls = []
    def sender(ch, *args):
        calls.append(ch['name'])
        if ch['name'] == 'public':
            raise TimeoutError()
    with pytest.raises(RuntimeError):
        deliver(journal, commits, sender)
    monkeypatch.setattr('sys.argv', ['publication.py', 'resolve', '--branch',
                        'dev-updates-state/test', '--channel', 'public', '--outcome', 'sent'])
    main()
    deliver(journal, commits, lambda *args: pytest.fail('Already delivered'))
    assert journal.load()['last_sha'] == commits[1]
    assert calls == ['team', 'public']


def test_failure_finalizing_batch_does_not_resend_any_channel(history, monkeypatch):
    journal, commits = history
    original_save = journal.save
    def fail_finalize():
        if journal.state['batch'] is None:
            raise RuntimeError('Cannot finalize')
        original_save()
    monkeypatch.setattr(journal, 'save', fail_finalize)
    with pytest.raises(RuntimeError, match='finalize'):
        deliver(journal, commits, lambda *args: None)
    monkeypatch.setattr(journal, 'save', original_save)
    deliver(journal, commits, lambda *args: pytest.fail('Already delivered'))
    assert journal.load()['last_sha'] == commits[1]
