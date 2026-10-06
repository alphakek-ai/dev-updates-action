"""Render validated updates and dispatch them to configured channels.

An update is {"title": str, "bullets": [str]} of plain text (see submit.py);
all channel formatting and escaping happens here.

Supported channel types: telegram, discord, slack, twitter.
"""

import html
import json
import os
import re
import urllib.request

# Mode aliases for backward compatibility
_MODE_ALIASES = {"private": "dev", "public": "community"}


class DeliveryNotAttempted(RuntimeError):
    """Configuration failed before a request could be sent."""


def _normalize_mode(mode: str) -> str:
    """Normalize mode name, supporting old private/public aliases."""
    return _MODE_ALIASES.get(mode, mode)


def _is_required(ch: dict) -> bool:
    """Whether a delivery failure must prevent completion of the batch."""
    val = ch.get("required", True)
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ("false", "0", "no")


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


def _footer(repo: str, commits: str, files: str) -> str:
    return f"{repo.split('/')[-1]} · {commits} commit(s) · {files} file(s)"


def _code_spans(text: str, plain, code) -> str:
    """Apply `plain` to text and `code` to backtick-quoted spans (dev identifiers)."""
    parts = text.split("`")
    return "".join(code(part) if i % 2 else plain(part) for i, part in enumerate(parts))


def render_telegram(update: dict, mode: str, repo: str, commits: str, files: str) -> str:
    escape = lambda text: html.escape(text, quote=False)
    line = lambda text: _code_spans(text, escape, lambda code: f"<code>{escape(code)}</code>")
    footer = escape(_footer(repo, commits, files))
    if mode == "dev":
        footer = f'<a href="https://github.com/{html.escape(repo)}">{footer}</a>'
    return "\n".join([f"<b>{line(update['title'])}</b>", "", *map(line, update["bullets"]), "", footer])


def _discord_escape(text: str) -> str:
    return re.sub(r"([\\*_~`|>#\[\]()<-])", r"\\\1", text)


def render_discord(update: dict, mode: str, repo: str, commits: str, files: str) -> str:
    line = lambda text: _code_spans(text, _discord_escape, lambda code: f"`{code}`")
    footer = f"[{_discord_escape(repo.split('/')[-1])}](https://github.com/{repo}) · {commits} commit(s) · {files} file(s)"
    return "\n".join([f"**{line(update['title'])}**", "", *map(line, update["bullets"]), "", footer])


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_slack(update: dict, mode: str, repo: str, commits: str, files: str) -> str:
    line = lambda text: _code_spans(text, _slack_escape, lambda code: f"`{_slack_escape(code)}`")
    footer = f"<https://github.com/{repo}|{_slack_escape(repo.split('/')[-1])}> · {commits} commit(s) · {files} file(s)"
    return "\n".join([f"*{line(update['title'])}*", "", *map(line, update["bullets"]), "", footer])


def render_twitter(update: dict, mode: str, repo: str, commits: str, files: str, max_length: int = 0) -> str:
    lines = [update["title"], "", *update["bullets"]]
    plain = _limit_cashtags("\n".join(lines).replace("`", ""))  # X allows at most one cashtag per post
    suffix = f"\n\n{_footer(repo, commits, files)}"
    if mode == "dev":
        suffix += f"\n\nhttps://github.com/{repo}"
    if max_length > 0 and len(plain) + len(suffix) > max_length:
        # Crop whole bullet lines to fit, keeping footer and link intact
        plain = plain[:max_length - len(suffix)].rsplit("\n", 1)[0]
    return plain + suffix


def send_telegram(ch: dict, update: dict, repo: str, commits: str, files: str) -> None:
    chat_id = ch.get("chat_id", "")
    thread_id = ch.get("thread_id")
    bot_token_env = ch.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    token = os.environ.get(bot_token_env, "")

    if not token:
        raise DeliveryNotAttempted(f"{bot_token_env} not set")

    payload: dict = {
        "chat_id": chat_id,
        "parse_mode": "HTML",
        "text": render_telegram(update, _normalize_mode(ch.get("mode", "dev")), repo, commits, files),
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.load(response)
    if not result.get('ok') or not result.get('result', {}).get('message_id'):
        raise RuntimeError('Telegram did not confirm a message ID')


def send_discord(ch: dict, update: dict, repo: str, commits: str, files: str) -> None:
    webhook_url = ch.get("webhook_url") or os.environ.get(ch.get("webhook_url_env", ""), "")
    if not webhook_url:
        raise DeliveryNotAttempted("No webhook URL configured")

    text = render_discord(update, _normalize_mode(ch.get("mode", "dev")), repo, commits, files)
    payload = {"content": text[:2000], "allowed_mentions": {"parse": []}}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def send_slack(ch: dict, update: dict, repo: str, commits: str, files: str) -> None:
    webhook_url = ch.get("webhook_url") or os.environ.get(ch.get("webhook_url_env", ""), "")
    if not webhook_url:
        raise DeliveryNotAttempted("No webhook URL configured")

    text = render_slack(update, _normalize_mode(ch.get("mode", "dev")), repo, commits, files)
    payload = {"text": text[:3000]}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


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


def send_twitter(ch: dict, update: dict, repo: str, commits: str, files: str) -> None:
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
        raise DeliveryNotAttempted(f"Twitter credentials missing: {', '.join(missing)}")

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
    )

    mode = _normalize_mode(ch.get("mode", "dev"))
    max_length = int(ch.get("max_length", "0"))  # 0 = no cropping (X Premium)
    tweet = render_twitter(update, mode, repo, commits, files, max_length)
    client.create_tweet(text=tweet)


DISPATCHERS = {
    "telegram": send_telegram,
    "discord": send_discord,
    "slack": send_slack,
    "twitter": send_twitter,
}
