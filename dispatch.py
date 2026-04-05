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


def load_summary(mode: str) -> tuple[str, str]:
    """Load summary file and split into title + body."""
    path = f"/tmp/summary_{mode}.txt"
    try:
        content = open(path).read().strip()
    except FileNotFoundError:
        return "", ""
    lines = content.split("\n")
    title = lines[0] if lines else "Update"
    body = "\n".join(lines[2:]) if len(lines) > 2 else ""
    return title, body


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(ch: dict, title: str, body: str, repo: str, repo_name: str, commits: str, files: str) -> None:
    chat_id = ch.get("chat_id", "")
    thread_id = ch.get("thread_id")
    bot_token_env = ch.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    token = os.environ.get(bot_token_env, "")

    if not token:
        print(f"  WARNING: {bot_token_env} not set, skipping")
        return

    title_h = escape_html(title)
    body_h = escape_html(body)
    text = (
        f'📦 <b>{title_h}</b>\n\n{body_h}\n\n'
        f'<a href="https://github.com/{repo}">{repo_name} · {commits} commit(s) · {files} file(s)</a>'
    )
    if len(text) > 4000:
        text = text[:3997] + "..."

    payload: dict = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
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


def send_discord(ch: dict, title: str, body: str, repo: str, repo_name: str, commits: str, files: str) -> None:
    webhook_url = ch.get("webhook_url") or os.environ.get(ch.get("webhook_url_env", ""), "")
    if not webhook_url:
        print("  WARNING: No webhook URL, skipping")
        return

    text = f"📦 **{title}**\n\n{body}\n\n[{repo_name}](https://github.com/{repo}) · {commits} commit(s) · {files} file(s)"
    payload = {"content": text[:2000]}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)


def send_slack(ch: dict, title: str, body: str, repo: str, repo_name: str, commits: str, files: str) -> None:
    webhook_url = ch.get("webhook_url") or os.environ.get(ch.get("webhook_url_env", ""), "")
    if not webhook_url:
        print("  WARNING: No webhook URL, skipping")
        return

    text = f"📦 *{title}*\n\n{body}\n\n<https://github.com/{repo}|{repo_name}> · {commits} commit(s) · {files} file(s)"
    payload = {"text": text[:3000]}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)


DISPATCHERS = {
    "telegram": send_telegram,
    "discord": send_discord,
    "slack": send_slack,
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

        title, body = load_summary(mode)
        if not title:
            print(f"WARNING: No {mode} summary generated, skipping {name}")
            continue

        dispatcher = DISPATCHERS.get(ch_type)
        if not dispatcher:
            print(f"WARNING: Unknown channel type '{ch_type}' for {name}")
            continue

        try:
            dispatcher(ch, title, body, repo, repo_name, commits, files)
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
