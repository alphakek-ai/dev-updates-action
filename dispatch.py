"""Render validated markdown updates and dispatch them to configured channels.

An update is a markdown title line plus one bullet list (validated by submit.py).
Telegram receives the markdown itself; other channels get a rendering of its parse tree.

Supported channel types: telegram, discord, slack, twitter.
"""

import functools
import json
import os
import re
import urllib.error
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


def _stats(repo: str, commits: str, files: str) -> str:
    return f"{repo.split('/')[-1]} · {commits} commit(s) · {files} file(s)"


@functools.cache
def markdown_parser():
    # Imported lazily: `publication.py prepare` runs before the locked dependencies are installed.
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").enable(["table", "strikethrough"])  # GFM syntax parses, so it can be rejected


def outline(markdown: str) -> tuple[list, list[list]]:
    """Inline tokens of the title and of each bullet of a validated update."""
    inlines = [token.children for token in markdown_parser().parse(markdown) if token.type == "inline"]
    title = [token for token in inlines[0] if token.type != "text" or token.content]
    if title[0].type == "strong_open":  # "**Title**" paragraph; each channel applies its own bold
        title = title[1:-1]
    return title, inlines[1:]


def _inline(tokens: list, text, code, strong: str, em: str, link) -> str:
    out: list[str] = []
    opened: list[tuple[int, object]] = []
    for token in tokens:
        if token.type == "text":
            out.append(text(token.content))
        elif token.type == "code_inline":
            out.append(code(token.content))
        elif token.nesting == 1:
            opened.append((len(out), token))
        elif token.nesting == -1:
            start, opening = opened.pop()
            inner = "".join(out[start:])
            del out[start:]
            if opening.type == "link_open":
                # markdown-it percent-encodes hrefs except parentheses, which would end a markdown link early.
                out.append(link(inner, str(opening.attrs["href"]).replace("(", "%28").replace(")", "%29")))
            else:
                out.append({"strong_open": strong, "em_open": em}[opening.type].format(inner))
    return "".join(out)


def render_telegram(markdown: str, mode: str, repo: str, commits: str, files: str) -> str:
    name = re.sub(r"([!-/:-@\[-`{-~])", r"\\\1", repo.split("/")[-1])  # Keep the repo name literal markdown text.
    footer = f"{name} · {commits} commit(s) · {files} file(s)"
    if mode == "dev":
        footer = f"[{footer}](https://github.com/{repo})"
    return f"{markdown.strip()}\n\n{footer}"


def _discord_escape(text: str) -> str:
    return re.sub(r"([\\*_~`|>#\[\]()<-])", r"\\\1", text)


def render_discord(title: list, bullets: list, mode: str, repo: str, commits: str, files: str) -> str:
    line = lambda tokens: _inline(tokens, _discord_escape, lambda code: f"`{code}`", "**{}**", "*{}*", lambda text, url: f"[{text}]({url})")
    footer = f"[{_discord_escape(repo.split('/')[-1])}](https://github.com/{repo}) · {commits} commit(s) · {files} file(s)"
    return "\n".join([f"**{line(title)}**", "", *(f"- {line(b)}" for b in bullets), "", footer])


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_slack(title: list, bullets: list, mode: str, repo: str, commits: str, files: str) -> str:
    line = lambda tokens: _inline(tokens, _slack_escape, lambda code: f"`{_slack_escape(code)}`", "*{}*", "_{}_",
                                  lambda text, url: f"<{_slack_escape(url)}|{text}>")
    footer = f"<https://github.com/{repo}|{_slack_escape(repo.split('/')[-1])}> · {commits} commit(s) · {files} file(s)"
    return "\n".join([f"*{line(title)}*", "", *(f"• {line(b)}" for b in bullets), "", footer])


def render_twitter(title: list, bullets: list, mode: str, repo: str, commits: str, files: str) -> str:
    line = lambda tokens: _inline(tokens, str, str, "{}", "{}", lambda text, url: f"{text} ({url})")
    lines = [line(title), "", *(f"• {line(b)}" for b in bullets), "", _stats(repo, commits, files)]
    if mode == "dev":
        lines += ["", f"https://github.com/{repo}"]
    return _limit_cashtags("\n".join(lines))  # X allows at most one cashtag per post


RENDERERS = {"discord": render_discord, "slack": render_slack, "twitter": render_twitter}
LIMITS = {"discord": 2000, "slack": 3000}


def render(kind: str, markdown: str, mode: str, repo: str, commits: str, files: str, limit: int = 0) -> str:
    """Render for a channel type, dropping trailing bullets until the message fits its limit."""
    if kind == "telegram":
        return render_telegram(markdown, mode, repo, commits, files)
    title, bullets = outline(markdown)
    limit = limit or LIMITS.get(kind, 0)
    for count in range(len(bullets), 0, -1):
        text = RENDERERS[kind](title, bullets[:count], mode, repo, commits, files)
        if not limit or len(text) <= limit:
            return text
    raise DeliveryNotAttempted(f"Even one bullet exceeds the {kind} limit of {limit} characters")


def send_telegram(ch: dict, markdown: str, repo: str, commits: str, files: str) -> None:
    chat_id = ch.get("chat_id", "")
    thread_id = ch.get("thread_id")
    bot_token_env = ch.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    token = os.environ.get(bot_token_env, "")

    if not token:
        raise DeliveryNotAttempted(f"{bot_token_env} not set")

    payload: dict = {
        "chat_id": chat_id,
        "rich_message": {"markdown": render("telegram", markdown, _normalize_mode(ch.get("mode", "dev")), repo, commits, files)},
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendRichMessage",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        # No plain-text fallback: a rejected update fails this delivery and stays retryable.
        print(f"::error::Telegram rejected the rich message: {error.read().decode(errors='replace')}")
        raise
    if not result.get('ok') or not result.get('result', {}).get('message_id'):
        raise RuntimeError('Telegram did not confirm a message ID')


def send_discord(ch: dict, markdown: str, repo: str, commits: str, files: str) -> None:
    webhook_url = ch.get("webhook_url") or os.environ.get(ch.get("webhook_url_env", ""), "")
    if not webhook_url:
        raise DeliveryNotAttempted("No webhook URL configured")

    text = render("discord", markdown, _normalize_mode(ch.get("mode", "dev")), repo, commits, files)
    payload = {"content": text, "allowed_mentions": {"parse": []}}

    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def send_slack(ch: dict, markdown: str, repo: str, commits: str, files: str) -> None:
    webhook_url = ch.get("webhook_url") or os.environ.get(ch.get("webhook_url_env", ""), "")
    if not webhook_url:
        raise DeliveryNotAttempted("No webhook URL configured")

    text = render("slack", markdown, _normalize_mode(ch.get("mode", "dev")), repo, commits, files)
    payload = {"text": text}

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


def send_twitter(ch: dict, markdown: str, repo: str, commits: str, files: str) -> None:
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

    max_length = int(ch.get("max_length", "0"))  # 0 = no cropping (X Premium)
    client.create_tweet(text=render("twitter", markdown, _normalize_mode(ch.get("mode", "dev")), repo, commits, files, max_length))


DISPATCHERS = {
    "telegram": send_telegram,
    "discord": send_discord,
    "slack": send_slack,
    "twitter": send_twitter,
}
