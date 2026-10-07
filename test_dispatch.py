"""Tests for dispatch.py — channel parsing and rendering."""

import io
import json

import pytest

import dispatch
from dispatch import (
    _is_required,
    _limit_cashtags,
    _normalize_mode,
    parse_channels,
)

DEV = ('**Q&A \\<fixes\\>**\n\n'
       '- 🔧 `parse_channels()` keeps *quoted* \\<names\\> & ids ([PR](https://github.com/owner/repo/pull/1#(x)))\n'
       '- 🚀 Ships $AIKEK and **$KEK**\n')
COMMUNITY = '# Smoother chats\n\n- 💬 Replies arrive \\[faster\\] & more\\_reliably\n'


@pytest.mark.parametrize('kind, mode, markdown, expected', [
    ('telegram', 'dev', DEV, DEV.strip() + '\n\n[repo · 2 commit(s) · 3 file(s)](https://github.com/owner/repo)'),
    ('telegram', 'community', COMMUNITY, COMMUNITY.strip() + '\n\nrepo · 2 commit(s) · 3 file(s)'),
    ('discord', 'dev', DEV,
     '**Q&A \\<fixes\\>**\n\n'
     '- 🔧 `parse_channels()` keeps *quoted* \\<names\\> & ids \\([PR](https://github.com/owner/repo/pull/1#%28x%29)\\)\n'
     '- 🚀 Ships $AIKEK and **$KEK**\n\n'
     '[repo](https://github.com/owner/repo) · 2 commit(s) · 3 file(s)'),
    ('discord', 'community', COMMUNITY,
     '**Smoother chats**\n\n- 💬 Replies arrive \\[faster\\] & more\\_reliably\n\n'
     '[repo](https://github.com/owner/repo) · 2 commit(s) · 3 file(s)'),
    ('slack', 'dev', DEV,
     '*Q&amp;A &lt;fixes&gt;*\n\n'
     '• 🔧 `parse_channels()` keeps _quoted_ &lt;names&gt; &amp; ids (<https://github.com/owner/repo/pull/1#%28x%29|PR>)\n'
     '• 🚀 Ships $AIKEK and *$KEK*\n\n'
     '<https://github.com/owner/repo|repo> · 2 commit(s) · 3 file(s)'),
    ('twitter', 'dev', DEV,
     'Q&A <fixes>\n\n• 🔧 parse_channels() keeps quoted <names> & ids (PR (https://github.com/owner/repo/pull/1#%28x%29))\n'
     '• 🚀 Ships $AIKEK and KEK\n\nrepo · 2 commit(s) · 3 file(s)\n\nhttps://github.com/owner/repo'),
    ('twitter', 'community', COMMUNITY,
     'Smoother chats\n\n• 💬 Replies arrive [faster] & more_reliably\n\nrepo · 2 commit(s) · 3 file(s)'),
])
def test_channels_receive_exact_messages(kind, mode, markdown, expected):
    assert dispatch.render(kind, markdown, mode, 'owner/repo', '2', '3') == expected


def test_unvalidated_inline_markup_degrades_to_text():
    markdown = '**Update**\n\n- see ~~old~~ <b>x</b> ![img](https://e.x/i.png) line\n  wrapped\n'
    assert dispatch.render('twitter', markdown, 'community', 'o/r', '1', '1').splitlines()[2] == '• see old x  line wrapped'


def test_telegram_footer_keeps_repo_name_literal():
    text = dispatch.render('telegram', COMMUNITY, 'community', 'owner/_my-repo_', '2', '3')
    footer = dispatch.markdown_parser().parse(text)[-2].children
    assert [(token.type, token.content) for token in footer] == [('text', '_my-repo_ · 2 commit(s) · 3 file(s)')]


@pytest.mark.parametrize('kind, limit', [('twitter', 60), ('discord', 0)])
def test_long_messages_drop_trailing_bullets_and_keep_footer(kind, limit):
    markdown = '**Update**\n\n- one\n- ' + 'x' * 2000 + '\n- three\n'
    text = dispatch.render(kind, markdown, 'community', 'owner/repo', '2', '3', limit)
    assert 'one' in text and 'three' not in text and text.endswith('3 file(s)')


def test_message_without_room_for_a_bullet_is_not_sent():
    with pytest.raises(dispatch.DeliveryNotAttempted, match='twitter limit'):
        dispatch.render('twitter', '**Update**\n\n- ' + 'x' * 250 + '\n', 'community', 'owner/repo', '2', '3', 280)


def test_slack_escapes_link_targets():
    text = dispatch.render('slack', '**Update**\n\n- [diff](https://github.com/o/r/compare?a=1&b=2)\n', 'dev', 'o/r', '1', '1')
    assert '<https://github.com/o/r/compare?a=1&amp;b=2|diff>' in text


@pytest.mark.parametrize('body', [
    {'ok': False, 'error_code': 429},
    {'ok': True, 'result': {}},
])
def test_telegram_requires_confirmed_message_id(monkeypatch, body):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    monkeypatch.setattr(dispatch.urllib.request, 'urlopen',
                        lambda req, timeout: io.BytesIO(json.dumps(body).encode()))
    with pytest.raises(RuntimeError, match='confirm'):
        dispatch.send_telegram({'chat_id': 'test'}, COMMUNITY, 'owner/repo', '1', '1')


def test_telegram_success_consumes_acknowledgement(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    requests = []
    def post(req, timeout):
        requests.append((json.loads(req.data), timeout))
        return io.BytesIO(b'{"ok":true,"result":{"message_id":123}}')
    monkeypatch.setattr(dispatch.urllib.request, 'urlopen', post)
    dispatch.send_telegram({'chat_id': 'test'}, COMMUNITY, 'owner/repo', '1', '1')
    assert requests[0][0]['chat_id'] == 'test'
    assert requests[0][0]['rich_message'] == {'markdown': dispatch.render('telegram', COMMUNITY, 'dev', 'owner/repo', '1', '1')}
    assert requests[0][1] == 30


def test_telegram_rejection_fails_loudly_without_fallback(monkeypatch, capsys):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-token')
    requests = []
    def reject(req, timeout):
        requests.append(req.full_url)
        raise dispatch.urllib.error.HTTPError(req.full_url, 400, 'Bad Request', {},
                                              io.BytesIO(b'{"ok":false,"description":"RICH_MESSAGE_MARKDOWN_INVALID"}'))
    monkeypatch.setattr(dispatch.urllib.request, 'urlopen', reject)
    with pytest.raises(dispatch.urllib.error.HTTPError):
        dispatch.send_telegram({'chat_id': 'test'}, COMMUNITY, 'owner/repo', '1', '1')
    assert requests == ['https://api.telegram.org/bottest-token/sendRichMessage']
    assert 'RICH_MESSAGE_MARKDOWN_INVALID' in capsys.readouterr().out


class TestLimitCashtags:
    def test_keeps_first_demotes_rest(self):
        out = _limit_cashtags("Shipped $AIKEK, $MYCO, and $KEK today")
        assert out.count("$") == 1
        assert "$AIKEK" in out
        assert "MYCO" in out and "$MYCO" not in out
        assert "KEK" in out and "$KEK" not in out

    def test_single_cashtag_untouched(self):
        assert _limit_cashtags("Only $AIKEK here") == "Only $AIKEK here"

    def test_prices_and_midword_untouched(self):
        # Not cashtags: '$'+digit (price), and '$' mid-word (blocked by the lookbehind).
        assert _limit_cashtags("costs $100 and $5k") == "costs $100 and $5k"
        assert _limit_cashtags("mid-word like foo$BAR stays") == "mid-word like foo$BAR stays"

    def test_repeated_symbol_collapses_to_one(self):
        # Even repeats of the same symbol must collapse — X limits cashtag occurrences.
        assert _limit_cashtags("$AIKEK up, $AIKEK strong").count("$") == 1


class TestParseChannels:
    def test_single_channel(self):
        yaml = """
        - name: team
          type: telegram
          chat_id: "-100123"
          mode: private
        """
        channels = parse_channels(yaml)
        assert len(channels) == 1
        assert channels[0]["name"] == "team"
        assert channels[0]["type"] == "telegram"
        assert channels[0]["chat_id"] == "-100123"
        assert channels[0]["mode"] == "private"

    def test_multiple_channels(self):
        yaml = """
        - name: team
          type: telegram
          chat_id: "-100123"
          mode: private

        - name: public
          type: telegram
          chat_id: "@mychannel"
          mode: public

        - name: discord-dev
          type: discord
          webhook_url_env: DISCORD_WEBHOOK
          mode: private
        """
        channels = parse_channels(yaml)
        assert len(channels) == 3
        assert channels[0]["name"] == "team"
        assert channels[1]["name"] == "public"
        assert channels[1]["chat_id"] == "@mychannel"
        assert channels[2]["type"] == "discord"

    def test_empty_input(self):
        assert parse_channels("") == []
        assert parse_channels("   ") == []

    def test_comments_ignored(self):
        yaml = """
        # This is a comment
        - name: team
          type: telegram
          # inline comment
          chat_id: "-100123"
          mode: private
        """
        channels = parse_channels(yaml)
        assert len(channels) == 1
        assert channels[0]["name"] == "team"

    def test_quoted_values_stripped(self):
        yaml = """
        - name: "team chat"
          type: 'telegram'
          chat_id: "-100123"
          mode: private
        """
        channels = parse_channels(yaml)
        assert channels[0]["name"] == "team chat"
        assert channels[0]["type"] == "telegram"

    def test_thread_id_preserved(self):
        yaml = """
        - name: team
          type: telegram
          chat_id: "-100123"
          thread_id: 4
          mode: private
        """
        channels = parse_channels(yaml)
        assert channels[0]["thread_id"] == "4"

    def test_custom_bot_token_env(self):
        yaml = """
        - name: alerts
          type: telegram
          chat_id: "-100123"
          bot_token_env: ALERT_BOT_TOKEN
          mode: private
        """
        channels = parse_channels(yaml)
        assert channels[0]["bot_token_env"] == "ALERT_BOT_TOKEN"


class TestNormalizeMode:
    def test_new_names_pass_through(self):
        assert _normalize_mode("dev") == "dev"
        assert _normalize_mode("community") == "community"

    def test_old_names_aliased(self):
        assert _normalize_mode("private") == "dev"
        assert _normalize_mode("public") == "community"

    def test_unknown_passes_through(self):
        assert _normalize_mode("custom") == "custom"


class TestIsRequired:
    def test_default_is_required(self):
        # Absent `required` field → channel is required (backward compatible).
        assert _is_required({"name": "team", "type": "telegram"}) is True

    def test_explicit_true(self):
        assert _is_required({"required": "true"}) is True

    def test_false_variants_are_optional(self):
        for val in ("false", "False", "FALSE", "0", "no", "No"):
            assert _is_required({"required": val}) is False, val

    def test_unrecognized_value_stays_required(self):
        # Anything not clearly false stays required — fail safe, not silent.
        assert _is_required({"required": "maybe"}) is True

    def test_empty_value_stays_required(self):
        # `required:` with no value → parse_channels yields "" → fail-safe to required.
        assert _is_required({"required": ""}) is True

    def test_native_bool_values(self):
        # If parse_channels is ever swapped for real YAML, native bools must work.
        assert _is_required({"required": True}) is True
        assert _is_required({"required": False}) is False
