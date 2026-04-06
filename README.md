# Dev Updates Action

AI-powered dev update notifications with privacy modes and multi-channel dispatch.

Uses [Claude Code](https://claude.ai/claude-code) to read your git diff and generate human-readable summaries, then dispatches them to any combination of Telegram, Discord, Slack, and Twitter/X channels.

## Features

- **Privacy modes**: Generate separate private (technical) and public (user-facing) summaries from the same push
- **Multi-channel**: Send to any number of channels — each with its own mode
- **AI-powered**: Claude Code reads the actual diff and writes the summary (not just commit messages)
- **Configurable**: Custom rules for what to include/exclude per mode
- **Cooldown + aggregation**: Avoid notification spam — aggregate changes over a configurable period

## Quick Start

```yaml
# .github/workflows/dev-updates.yml
name: Dev Updates
on:
  push:
    branches: [main]

jobs:
  notify:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 50

      - uses: alphakek-ai/dev-updates-action@v1
        with:
          channels: |
            - name: team-chat
              type: telegram
              chat_id: "-100123456789"
              thread_id: 4
              mode: private

            - name: announcements
              type: telegram
              chat_id: "@mychannel"
              mode: public
        env:
          CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
```

## Channel Configuration

Each channel is a YAML block with:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Display name for logging |
| `type` | Yes | `telegram`, `discord`, or `slack` |
| `mode` | Yes | `private` (technical details) or `public` (user-facing) |

### Telegram

| Field | Required | Description |
|-------|----------|-------------|
| `chat_id` | Yes | Chat/channel ID (e.g., `"-100123456789"` or `"@channelname"`) |
| `thread_id` | No | Topic/thread ID for supergroups |
| `bot_token_env` | No | Env var name for bot token (default: `TELEGRAM_BOT_TOKEN`) |

### Discord

| Field | Required | Description |
|-------|----------|-------------|
| `webhook_url` | Yes* | Discord webhook URL |
| `webhook_url_env` | Yes* | Or: env var name containing the webhook URL |

### Slack

| Field | Required | Description |
|-------|----------|-------------|
| `webhook_url` | Yes* | Slack incoming webhook URL |
| `webhook_url_env` | Yes* | Or: env var name containing the webhook URL |

### Twitter / X

| Field | Required | Description |
|-------|----------|-------------|
| `api_key_env` | No | Env var name for API key (default: `TWITTER_API_KEY`) |
| `api_secret_env` | No | Env var name for API secret (default: `TWITTER_API_SECRET`) |
| `access_token_env` | No | Env var name for access token (default: `TWITTER_ACCESS_TOKEN`) |
| `access_token_secret_env` | No | Env var name for access token secret (default: `TWITTER_ACCESS_TOKEN_SECRET`) |

Tweets are auto-truncated to 280 chars with a link to the repo.

## Privacy Modes

**Private** summaries include technical details — file paths, function names, what specifically changed. Good for dev team chats.

**Public** summaries describe changes in terms of user-facing impact. No internal details. Good for announcement channels, Twitter, community updates.

You can customize the rules:

```yaml
- uses: alphakek-ai/dev-updates-action@v1
  with:
    public_rules: |
      Describe changes as product improvements.
      Never mention database, API, or infrastructure changes.
      Write in an excited, marketing-friendly tone.
    private_rules: |
      Include file paths and function names.
      Note any breaking changes or migration steps.
      Mention test coverage changes.
    channels: |
      ...
```

## Example Output

**Private** (team chat):
```
📦 DB Pool Reconnect Fix

🔧 _ensure_pool() raises InterfaceError instead of RuntimeError
🧪 Added 10 tests for reconnect/retry logic
🚀 Evolution worker recovers from Cloud SQL blips automatically

backend · 2 commits · 4 files
```

**Public** (announcement channel):
```
📦 Infrastructure Reliability Update

🔧 Improved database connection resilience
🧪 Expanded test coverage for critical paths
🚀 Background workers now self-heal from transient failures

backend · 2 commits · 4 files
```

## Cooldown + Aggregation

By default, the action posts on every push. To reduce noise, set a `cooldown`:

```yaml
name: Dev Updates
on:
  push:
    branches: [main]
  schedule:
    - cron: '0 */6 * * *'  # safety net — catches skipped updates

jobs:
  notify:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
      actions: read  # required for cooldown state (reads previous run artifacts)
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 50

      - uses: alphakek-ai/dev-updates-action@v1
        with:
          cooldown: '6h'  # post at most once per 6 hours
          channels: |
            ...
```

How it works:
- **First push** after cooldown expires → posts immediately with all changes since last notification
- **Subsequent pushes** within cooldown → skipped silently
- **Cron trigger** → catches any skipped updates (set cron interval to match cooldown)
- State is stored in GitHub Actions variables (`DEV_UPDATES_LAST_SHA`, `DEV_UPDATES_LAST_AT`)

Supported cooldown formats: `30m`, `6h`, `1d`, or raw seconds.

State is stored as a workflow artifact (90-day retention). No extra permissions or PATs needed beyond the default `GITHUB_TOKEN`.

## Requirements

- `CLAUDE_CODE_OAUTH_TOKEN` secret — for Claude Code ([get one here](https://console.anthropic.com))
- Channel-specific tokens/webhooks as secrets
- `actions: read` permission (only if using cooldown, for reading previous run artifacts)

## License

Apache 2.0
