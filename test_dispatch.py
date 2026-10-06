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

UPDATE = {'title': 'Q&A <fixes>', 'bullets': ['🔧 `parse_channels()` keeps *quoted* <names> & ids', '🚀 Ships $AIKEK and $KEK']}
COMMUNITY = {'title': 'Smoother chats', 'bullets': ['💬 Replies arrive [faster] & more_reliably']}


@pytest.mark.parametrize('render, mode, update, expected', [
    (dispatch.render_telegram, 'dev', UPDATE,
     '<b>Q&amp;A &lt;fixes&gt;</b>\n\n'
     '🔧 <code>parse_channels()</code> keeps *quoted* &lt;names&gt; &amp; ids\n🚀 Ships $AIKEK and $KEK\n\n'
     '<a href="https://github.com/owner/repo">repo · 2 commit(s) · 3 file(s)</a>'),
    (dispatch.render_telegram, 'community', COMMUNITY,
     '<b>Smoother chats</b>\n\n💬 Replies arrive [faster] &amp; more_reliably\n\nrepo · 2 commit(s) · 3 file(s)'),
    (dispatch.render_discord, 'dev', UPDATE,
     '**Q&A \\<fixes\\>**\n\n'
     '🔧 `parse_channels()` keeps \\*quoted\\* \\<names\\> & ids\n🚀 Ships $AIKEK and $KEK\n\n'
     '[repo](https://github.com/owner/repo) · 2 commit(s) · 3 file(s)'),
    (dispatch.render_discord, 'community', COMMUNITY,
     '**Smoother chats**\n\n💬 Replies arrive \\[faster\\] & more\\_reliably\n\n'
     '[repo](https://github.com/owner/repo) · 2 commit(s) · 3 file(s)'),
    (dispatch.render_slack, 'dev', UPDATE,
     '*Q&amp;A &lt;fixes&gt;*\n\n'
     '🔧 `parse_channels()` keeps *quoted* &lt;names&gt; &amp; ids\n🚀 Ships $AIKEK and $KEK\n\n'
     '<https://github.com/owner/repo|repo> · 2 commit(s) · 3 file(s)'),
    (dispatch.render_twitter, 'dev', UPDATE,
     'Q&A <fixes>\n\n🔧 parse_channels() keeps *quoted* <names> & ids\n🚀 Ships $AIKEK and KEK\n\n'
     'repo · 2 commit(s) · 3 file(s)\n\nhttps://github.com/owner/repo'),
    (dispatch.render_twitter, 'community', COMMUNITY,
     'Smoother chats\n\n💬 Replies arrive [faster] & more_reliably\n\nrepo · 2 commit(s) · 3 file(s)'),
])
def test_renderers_produce_exact_escaped_messages(render, mode, update, expected):
    assert render(update, mode, 'owner/repo', '2', '3') == expected


def test_twitter_crops_whole_bullets_and_keeps_footer():
    update = {'title': 'Update', 'bullets': ['one', 'two ' * 30]}
    tweet = dispatch.render_twitter(update, 'community', 'owner/repo', '2', '3', max_length=60)
    assert tweet == 'Update\n\none\n\nrepo · 2 commit(s) · 3 file(s)'


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
    assert requests[0][0]['parse_mode'] == 'HTML'
    assert requests[0][0]['text'] == dispatch.render_telegram(COMMUNITY, 'dev', 'owner/repo', '1', '1')
    assert requests[0][1] == 30


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
