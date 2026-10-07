"""Render validated markdown updates and dispatch them to configured channels.

An update is any markdown (validated by submit.py). Telegram receives the markdown
itself; other channels get a rendering of its parse tree.

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

    return MarkdownIt("commonmark").enable(["table", "strikethrough"])  # The GFM extensions Telegram also parses


def _inline(tokens: list, dialect: dict) -> str:
    out: list[str] = []
    opened: list[tuple[int, object]] = []
    for token in tokens:
        if token.type == "text":
            out.append(dialect["text"](token.content))
        elif token.type == "code_inline":
            out.append(dialect["code"](token.content))
        elif token.type in ("softbreak", "hardbreak"):
            out.append(" ")
        elif token.type == "image":
            out.append(_inline(token.children, dialect))  # Alt text
        elif token.nesting == 1:
            opened.append((len(out), token))
        elif token.nesting == -1:
            start, opening = opened.pop()
            inner = "".join(out[start:])
            del out[start:]
            if opening.type == "link_open":
                # markdown-it percent-encodes hrefs except parentheses, which would end a markdown link early.
                out.append(dialect["link"](inner, str(opening.attrs["href"]).replace("(", "%28").replace(")", "%29")))
            else:
                out.append(dialect.get(opening.type, "{}").format(inner))
    return "".join(out)


def _lines(markdown: str, dialect: dict) -> list[str]:
    """Render any markdown into the dialect, one output line per block or list item."""
    lines: list[str] = []
    lists: list[int | None] = []  # Next number per open list; None for bullets.
    marker, heading, quote, row = None, False, 0, None
    for token in markdown_parser().parse(markdown):
        if token.level == 0 and token.nesting != -1 and lines and lines[-1]:
            lines.append("")
        if token.type == "bullet_list_open":
            lists.append(None)
        elif token.type == "ordered_list_open":
            lists.append(int(token.attrs.get("start", 1)))
        elif token.type in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif token.type == "list_item_open":
            marker = "  " * (len(lists) - 1) + (dialect["bullet"] if lists[-1] is None else f"{lists[-1]}. ")
            if lists[-1] is not None:
                lists[-1] += 1
        elif token.type in ("heading_open", "heading_close"):
            heading = token.type == "heading_open"
        elif token.type in ("blockquote_open", "blockquote_close"):
            quote += token.nesting
        elif token.type == "tr_open":
            row = []
        elif token.type == "tr_close":
            lines.append(" | ".join(row))
            row = None
        elif token.type == "inline":
            text = _inline(token.children, dialect)
            if row is not None:
                row.append(text)
                continue
            text = dialect["heading"].format(text) if heading else text
            lines.append("> " * quote + (marker or "  " * len(lists)) + text)
            marker = None
        elif token.type == "html_block":
            lines.append(dialect["text"](token.content.strip()))
        elif token.type in ("fence", "code_block"):
            lines.append(dialect["block"](token.content.rstrip("\n")))
        elif token.type == "hr":
            lines.append("———")
    return lines or [""]


def _discord_escape(text: str) -> str:
    return re.sub(r"([\\*_~`|>#\[\]()<-])", r"\\\1", text)


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code(code: str, block: bool = False) -> str:
    """Wrap code in a fence longer than any run of backticks inside it."""
    ticks = "`" * max(3 if block else 1, max(map(len, re.findall(r"`+", code)), default=0) + 1)
    if block:
        return f"{ticks}\n{code}\n{ticks}"
    return f"{ticks} {code} {ticks}" if "`" in code else f"{ticks}{code}{ticks}"


DIALECTS = {
    "discord": {"text": _discord_escape, "code": _code, "strong_open": "**{}**", "em_open": "*{}*",
                "s_open": "~~{}~~", "link": lambda text, url: f"[{text}]({url})", "heading": "**{}**", "bullet": "- ",
                "block": lambda code: _code(code, block=True)},
    "slack": {"text": _slack_escape, "code": lambda code: _code(_slack_escape(code)), "strong_open": "*{}*",
              "em_open": "_{}_", "s_open": "~{}~", "link": lambda text, url: f"<{_slack_escape(url)}|{text}>",
              "heading": "*{}*", "bullet": "• ", "block": lambda code: _code(_slack_escape(code), block=True)},
    "twitter": {"text": str, "code": str, "link": lambda text, url: f"{text} ({url})", "heading": "{}", "bullet": "• ",
                "block": str},
}
LIMITS = {"discord": 2000, "slack": 3000}


def render_telegram(markdown: str, mode: str, repo: str, commits: str, files: str) -> str:
    name = re.sub(r"([!-/:-@\[-`{-~])", r"\\\1", repo.split("/")[-1])  # Keep the repo name literal markdown text.
    footer = f"{name} · {commits} commit(s) · {files} file(s)"
    if mode == "dev":
        footer = f"[{footer}](https://github.com/{repo})"
    return f"{markdown.strip()}\n\n{footer}"


def render(kind: str, markdown: str, mode: str, repo: str, commits: str, files: str, limit: int = 0) -> str:
    """Render for a channel type, dropping trailing lines (then truncating) until the message fits its limit."""
    if kind == "telegram":
        return render_telegram(markdown, mode, repo, commits, files)
    name = repo.split("/")[-1]
    footer = {
        "discord": f"[{_discord_escape(name)}](https://github.com/{repo}) · {commits} commit(s) · {files} file(s)",
        "slack": f"<https://github.com/{repo}|{_slack_escape(name)}> · {commits} commit(s) · {files} file(s)",
        "twitter": _stats(repo, commits, files) + (f"\n\nhttps://github.com/{repo}" if mode == "dev" else ""),
    }[kind]
    lines = _lines(markdown, DIALECTS[kind])
    if kind == "twitter":
        lines = _limit_cashtags("\n".join(lines)).split("\n")  # X allows at most one cashtag per post
    limit = limit or LIMITS.get(kind, 0)
    for count in range(len(lines), 0, -1):
        body = "\n".join(lines[:count]).strip()
        if len([line for line in lines if line.strip()]) > 1 and "\n" not in body:
            break  # Truncate instead of posting only the first line.
        if not limit or len(body) + len(footer) + 2 <= limit:
            return f"{body}\n\n{footer}"
    room = limit - len(footer) - 3
    if room <= 0:
        raise DeliveryNotAttempted(f"The footer alone exceeds the {kind} limit of {limit} characters")
    # Cut at a word boundary; markup spanning the cut (a link or code span) can still be left open.
    cut = "\n".join(lines).strip()[:room].rsplit(" ", 1)[0].rstrip("\\")
    return f"{cut}…\n\n{footer}"


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
        try:
            description = json.loads(error.read())["description"]
        except Exception:
            description = ""  # Never mask the HTTP error, which decides whether the delivery is retryable.
        print(f"::error::Telegram rejected the rich message ({error.code}): {description!r}")
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
