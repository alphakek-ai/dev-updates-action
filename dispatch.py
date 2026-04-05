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
    """Post tweet using OAuth 2.0 with PKCE (token refresh flow).

    Uses the same OAuth 2.0 pattern as aikek-backend's TwitterClient.
    The refresh token rotates on each use — the new one is written to
    /tmp/twitter_refresh_token.txt for the action to update the GitHub secret.
    """
    import tweepy

    client_id = os.environ.get(ch.get("client_id_env", "TWITTER_CLIENT_ID"), "")
    client_secret = os.environ.get(ch.get("client_secret_env", "TWITTER_CLIENT_SECRET"), "")
    refresh_token = os.environ.get(ch.get("refresh_token_env", "TWITTER_REFRESH_TOKEN"), "")

    if not all([client_id, client_secret, refresh_token]):
        print("  WARNING: Twitter OAuth2 credentials incomplete, skipping")
        return

    # Refresh the access token (same pattern as aikek_legacy/bots/twitter/client.py:227)
    oauth2_handler = tweepy.OAuth2UserHandler(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri="https://alphakek.ai/callback",
        scope=["tweet.read", "tweet.write", "users.read", "offline.access"],
    )

    token_data = oauth2_handler.refresh_token(
        token_url="https://api.twitter.com/2/oauth2/token",
        refresh_token=refresh_token,
    )

    new_access_token = token_data["access_token"]
    new_refresh_token = token_data.get("refresh_token", refresh_token)

    # Save the new refresh token so the action can update the GitHub secret
    with open("/tmp/twitter_refresh_token.txt", "w") as f:
        f.write(new_refresh_token)

    # Post tweet
    client = tweepy.Client(new_access_token)

    link = f"https://github.com/{repo}"
    max_content = 280 - len(link) - 2
    text = content[:max_content].rsplit("\n", 1)[0] if len(content) > max_content else content
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
