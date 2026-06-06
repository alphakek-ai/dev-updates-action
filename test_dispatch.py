"""Tests for dispatch.py — channel parsing, summary loading."""

import os

import dispatch
from dispatch import (
    _is_required,
    _limit_cashtags,
    _normalize_mode,
    _resolve_exit,
    load_summary,
    parse_channels,
)


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


class TestResolveExit:
    def test_all_succeed(self):
        assert _resolve_exit(required_failures=0, successes=3) == 0

    def test_optional_failure_with_a_success_passes(self):
        # The Twitter-403 scenario: required channels OK, an optional one failed.
        assert _resolve_exit(required_failures=0, successes=2) == 0

    def test_required_failure_fails(self):
        assert _resolve_exit(required_failures=1, successes=2) == 1

    def test_total_failure_fails_even_if_all_optional(self):
        # Nothing delivered must never be silent, even with no required channels.
        assert _resolve_exit(required_failures=0, successes=0) == 1


class TestMainWarnSuffix:
    """The WARN line must only promise 'not failing the run' when it's true."""

    def _run(self, channels_yaml, dispatchers, monkeypatch, capsys):
        monkeypatch.setenv("CHANNELS", channels_yaml)
        monkeypatch.setenv("REPO", "owner/repo")
        monkeypatch.setattr(dispatch, "DISPATCHERS", dispatchers)
        with open("/tmp/summary_dev.md", "w") as f:
            f.write("**title**\n- x")
        try:
            dispatch.main()
            code = 0
        except SystemExit as e:
            code = e.code
        os.unlink("/tmp/summary_dev.md")
        return code, capsys.readouterr().out

    def test_optional_only_failure_says_not_failing(self, monkeypatch, capsys):
        ok = lambda *a, **k: None  # noqa: E731
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("403"))  # noqa: E731
        yaml = (
            "- name: tg\n  type: telegram\n  mode: dev\n"
            "- name: x\n  type: twitter\n  mode: dev\n  required: false\n"
        )
        code, out = self._run(yaml, {"telegram": ok, "twitter": boom}, monkeypatch, capsys)
        assert code == 0
        assert "WARN:" in out and "not failing the run" in out

    def test_required_also_fails_drops_not_failing(self, monkeypatch, capsys):
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # noqa: E731
        yaml = (
            "- name: tg\n  type: telegram\n  mode: dev\n"
            "- name: x\n  type: twitter\n  mode: dev\n  required: false\n"
        )
        code, out = self._run(yaml, {"telegram": boom, "twitter": boom}, monkeypatch, capsys)
        assert code == 1
        assert "WARN:" in out  # optional failure still reported
        assert "not failing the run" not in out  # ...but the run IS failing
        assert "FATAL: 1 required channel(s) failed" in out


class TestLoadSummary:
    def test_returns_empty_for_missing_file(self):
        assert load_summary("nonexistent_mode_xyz") == ""

    def test_loads_content(self):
        with open("/tmp/summary_testmode.md", "w") as f:
            f.write("📦 **My Update**\n\n🔧 Fixed a bug\n🚀 Added a feature")

        content = load_summary("testmode")
        assert "My Update" in content
        assert "Fixed a bug" in content
        assert "Added a feature" in content
        os.unlink("/tmp/summary_testmode.md")

    def test_strips_whitespace(self):
        with open("/tmp/summary_striptest.md", "w") as f:
            f.write("  \n  content here  \n  ")

        content = load_summary("striptest")
        assert content == "content here"
        os.unlink("/tmp/summary_striptest.md")
