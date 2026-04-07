"""Tests for dispatch.py — channel parsing, summary loading."""

import os

from dispatch import _normalize_mode, load_summary, parse_channels


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
