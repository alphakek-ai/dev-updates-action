"""Dispatch summaries to configured channels.

Reads channel config from CHANNELS env var (YAML),
loads the appropriate summary (private/public),
and sends to each channel via its native API.

Supported channel types: telegram, discord, slack, twitter (planned).
"""

import json
import os
import sys
import urllib.request


def parse_channels(yaml_text: str) -> list[dict]:
    """Minimal YAML list parser — handles `- key: value` blocks."""
    channels = []
    current: dict[str, str] = {}
    for line in yaml_text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            if current:
                channels.append(current)
            current = {}
            line = line[2:]
        if ":" in line:
            key, val = line.split(":", 1)
            current[key.strip()] = val.strip().strip("\"'")
    if current:
        channels.append(current)
    return channels


def load_summary(mode: str) -> str:
    """Load summary markdown file. Returns empty string if not found."""
    path = f"/tmp/summary_{mode}.md"
    try:
        return open(path).read().strip()
    except FileNotFoundError:
        return ""


def send_telegram(ch: dict, content: str, repo: str, repo_name: str, commits: str, files: str) -> None:
    from telegramify_markdown import markdownify

    chat_id = ch.get("chat_id", "")
    thread_id = ch.get("thread_id")
    bot_token_env = ch.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    token = os.environ.get(bot_token_env, "")

    if not token:
        print(f"  WARNING: {bot_token_env} not set, skipping")
        return

    md_text = f"{content}\n\n[{repo_name} · {commits} commit(s) · {files} file(s)](https://github.com/{repo})"
    text = markdownify(md_text)

    if len(text) > 4000:
        text = text[:3997] + "..."

    payload: dict = {
        "chat_id": chat_id,
        "parse_mode": "MarkdownV2",
        "text": text,
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)


def send_discord(ch: dict, content: str, repo: str, repo_name: str, commits: str, files: str) -> None:
    webhook_url = ch.get("webhook_url") or os.environ.get(ch.get("webhook_url_env", ""), "")
    if not webhook_url:
        print("  WARNING: No webhook URL, skipping")
        return

    text = f"{content}\n\n[{repo_name}](https://github.com/{repo}) · {commits} commit(s) · {files} file(s)"
    payload = {"content": text[:2000]}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)


def send_slack(ch: dict, content: str, repo: str, repo_name: str, commits: str, files: str) -> None:
    webhook_url = ch.get("webhook_url") or os.environ.get(ch.get("webhook_url_env", ""), "")
    if not webhook_url:
        print("  WARNING: No webhook URL, skipping")
        return

    text = f"{content}\n\n<https://github.com/{repo}|{repo_name}> · {commits} commit(s) · {files} file(s)"
    payload = {"text": text[:3000]}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)


def send_twitter(ch: dict, content: str, repo: str, repo_name: str, commits: str, files: str) -> None:
    """Post tweet using OAuth 1.0a (static keys, no token rotation)."""
    import re

    import tweepy

    api_key = os.environ.get(ch.get("api_key_env", "TWITTER_API_KEY"), "")
    api_secret = os.environ.get(ch.get("api_secret_env", "TWITTER_API_SECRET"), "")
    access_token = os.environ.get(ch.get("access_token_env", "TWITTER_ACCESS_TOKEN"), "")
    access_token_secret = os.environ.get(ch.get("access_token_secret_env", "TWITTER_ACCESS_TOKEN_SECRET"), "")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        print("  WARNING: Twitter credentials incomplete, skipping")
        return

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )

    # Strip markdown — Twitter renders plain text only
    plain = content
    plain = re.sub(r"```.*?```", "", plain, flags=re.DOTALL)  # code blocks
    plain = re.sub(r"`([^`]+)`", r"\1", plain)  # inline code
    plain = re.sub(r"\*\*(.+?)\*\*", r"\1", plain)  # bold
    plain = re.sub(r"\*(.+?)\*", r"\1", plain)  # italic
    plain = re.sub(r"_(.+?)_", r"\1", plain)  # italic
    plain = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", plain)  # links → text only
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()  # collapse blank lines

    link = f"https://github.com/{repo}"
    max_content = 280 - len(link) - 2
    text = plain[:max_content].rsplit("\n", 1)[0] if len(plain) > max_content else plain
    tweet = f"{text}\n\n{link}"

    client.create_tweet(text=tweet)


DISPATCHERS = {
    "telegram": send_telegram,
    "discord": send_discord,
    "slack": send_slack,
    "twitter": send_twitter,
}


def main() -> None:
    channels = parse_channels(os.environ.get("CHANNELS", ""))
    repo = os.environ.get("REPO", "")
    repo_name = repo.split("/")[-1] if repo else ""
    commits = os.environ.get("COMMITS", "0")
    files = os.environ.get("FILES", "0")

    if not channels:
        print("ERROR: No channels configured")
        sys.exit(1)

    successes = 0
    failures = 0

    for ch in channels:
        name = ch.get("name", ch.get("type", "unknown"))
        ch_type = ch.get("type", "telegram")
        mode = ch.get("mode", "private")

        content = load_summary(mode)
        if not content:
            print(f"WARNING: No {mode} summary generated, skipping {name}")
            continue

        dispatcher = DISPATCHERS.get(ch_type)
        if not dispatcher:
            print(f"WARNING: Unknown channel type '{ch_type}' for {name}")
            continue

        try:
            dispatcher(ch, content, repo, repo_name, commits, files)
            print(f"OK: {name} ({ch_type}, {mode})")
            successes += 1
        except Exception as e:
            print(f"ERROR: {name} ({ch_type}): {e}")
            failures += 1

    if successes == 0 and failures > 0:
        print(f"FATAL: All {failures} channel(s) failed")
        sys.exit(1)
    elif failures > 0:
        print(f"WARNING: {failures}/{successes + failures} channel(s) failed")


if __name__ == "__main__":
    main()
