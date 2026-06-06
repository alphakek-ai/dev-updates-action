"""Dispatch summaries to configured channels.

Reads channel config from CHANNELS env var (YAML),
loads the appropriate summary (dev/community),
and sends to each channel via its native API.

Supported channel types: telegram, discord, slack, twitter.
"""

import json
import os
import re
import sys
import urllib.request

# Mode aliases for backward compatibility
_MODE_ALIASES = {"private": "dev", "public": "community"}


def _normalize_mode(mode: str) -> str:
    """Normalize mode name, supporting old private/public aliases."""
    return _MODE_ALIASES.get(mode, mode)


def _is_required(ch: dict) -> bool:
    """Channels are required by default. A channel may set `required: false` so a
    flaky external service (e.g. X/Twitter rate limits, a downed webhook) can fail
    without failing the whole run — and without blocking state-save, which would
    otherwise re-post to the channels that DID succeed on the next run.

    Handles both the string values today's parse_channels yields and native bools,
    in case parse_channels is ever swapped for real YAML parsing."""
    val = ch.get("required", True)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("false", "0", "no")


def _resolve_exit(required_failures: int, successes: int) -> int:
    """Exit code from channel outcomes. Fail (1) if any REQUIRED channel failed,
    or if nothing was delivered at all (even when every channel was optional —
    a total delivery failure must never be silent). Otherwise 0."""
    if required_failures > 0:
        return 1
    if successes == 0:
        return 1
    return 0


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
        raise RuntimeError(f"{bot_token_env} not set")

    mode = _normalize_mode(ch.get("mode", "dev"))
    if mode == "community":
        footer = f"{repo_name} · {commits} commit(s) · {files} file(s)"
    else:
        footer = f"[{repo_name} · {commits} commit(s) · {files} file(s)](https://github.com/{repo})"
    md_text = f"{content}\n\n{footer}"
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
        raise RuntimeError("No webhook URL configured")

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
        raise RuntimeError("No webhook URL configured")

    text = f"{content}\n\n<https://github.com/{repo}|{repo_name}> · {commits} commit(s) · {files} file(s)"
    payload = {"text": text[:3000]}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)


def _limit_cashtags(text: str) -> str:
    """Keep the first cashtag, strip '$' from the rest (X allows only one). Prices
    ('$100', '$5k') and mid-word '$' are untouched — a cashtag is '$'+letter at a word
    boundary."""
    found = False

    def demote_extra(m: re.Match[str]) -> str:
        nonlocal found
        if found:
            return m.group(0)[1:]
        found = True
        return m.group(0)

    return re.sub(r"(?<!\w)\$[A-Za-z][A-Za-z0-9]*", demote_extra, text)


def send_twitter(ch: dict, content: str, repo: str, repo_name: str, commits: str, files: str) -> None:
    """Post tweet using OAuth 1.0a (static keys, no token rotation)."""
    import tweepy

    api_key = os.environ.get(ch.get("api_key_env", "TWITTER_API_KEY"), "")
    api_secret = os.environ.get(ch.get("api_secret_env", "TWITTER_API_SECRET"), "")
    access_token = os.environ.get(ch.get("access_token_env", "TWITTER_ACCESS_TOKEN"), "")
    access_token_secret = os.environ.get(ch.get("access_token_secret_env", "TWITTER_ACCESS_TOKEN_SECRET"), "")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        missing = [name for name, val in [
            ("TWITTER_API_KEY", api_key), ("TWITTER_API_SECRET", api_secret),
            ("TWITTER_ACCESS_TOKEN", access_token), ("TWITTER_ACCESS_TOKEN_SECRET", access_token_secret),
        ] if not val]
        raise RuntimeError(f"Twitter credentials missing: {', '.join(missing)}")

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
    plain = _limit_cashtags(plain)  # X allows at most one cashtag per post

    mode = _normalize_mode(ch.get("mode", "dev"))
    max_length = int(ch.get("max_length", "0"))  # 0 = no cropping (X Premium)
    footer = f"{repo_name} · {commits} commit(s) · {files} file(s)"
    link = f"https://github.com/{repo}" if mode == "dev" else ""

    parts = [plain, footer]
    if link:
        parts.append(link)
    tweet = "\n\n".join(parts)

    if max_length > 0 and len(tweet) > max_length:
        # Crop content to fit, keeping footer and link intact
        suffix = f"\n\n{footer}"
        if link:
            suffix += f"\n{link}"
        available = max_length - len(suffix)
        text = plain[:available].rsplit("\n", 1)[0] if len(plain) > available else plain
        tweet = text + suffix

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
    required_failures = 0
    optional_failures = 0

    for ch in channels:
        name = ch.get("name", ch.get("type", "unknown"))
        ch_type = ch.get("type", "telegram")
        mode = _normalize_mode(ch.get("mode", "dev"))
        required = _is_required(ch)
        tag = "required" if required else "optional"

        content = load_summary(mode)
        dispatcher = DISPATCHERS.get(ch_type)

        error = None
        if not content:
            error = f"No {mode} summary generated"
        elif not dispatcher:
            error = f"Unknown channel type '{ch_type}'"
        else:
            try:
                dispatcher(ch, content, repo, repo_name, commits, files)
            except Exception as e:
                error = str(e)

        if error is None:
            print(f"OK: {name} ({ch_type}, {mode}, {tag})")
            successes += 1
        else:
            print(f"ERROR: {name} ({ch_type}, {mode}, {tag}): {error}")
            if required:
                required_failures += 1
            else:
                optional_failures += 1

    if optional_failures > 0:
        print(f"WARN: {optional_failures} optional channel(s) failed — not failing the run")
    if required_failures > 0:
        print(f"FATAL: {required_failures} required channel(s) failed")
    elif successes == 0:
        print("FATAL: all channels failed — nothing delivered")

    sys.exit(_resolve_exit(required_failures, successes))


if __name__ == "__main__":
    main()
