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

Before enabling this workflow, [initialize its publication journal](#upgrading-and-initializing-state), including for a new installation.

```yaml
# .github/workflows/dev-updates.yml
name: Dev Updates
on:
  push:
    branches: [main]

concurrency:
  group: dev-updates-publication
  cancel-in-progress: false

jobs:
  notify:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          persist-credentials: false

      - uses: alphakek-ai/dev-updates-action@v2
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
| `type` | Yes | `telegram`, `discord`, `slack`, or `twitter` |
| `mode` | Yes | `dev` (technical details) or `community` (user-facing) |
| `required` | No | `true` (default) or `false`. A `required: false` channel may fail without failing the run — use for flaky external channels (e.g. Twitter/X) so one outage doesn't red the build or trigger duplicate re-posts on the channels that succeeded. Ambiguous failures on required channels need operator reconciliation. Optional failures are not retried for that batch. The run still fails if a required channel fails, or if *no* channel delivers. |

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

**Dev** (`mode: dev`) summaries explain *how* the system changed — business logic, architecture, data flow — citing files/functions as supporting evidence rather than enumerating them. Good for dev team chats.

**Community** (`mode: community`) summaries describe each change as the user value it delivers, in plain language. No file names, technologies, or version numbers. Good for announcement channels, Twitter, community updates.

You can customize the rules:

```yaml
- uses: alphakek-ai/dev-updates-action@v2
  with:
    community_rules: |
      Lead each bullet with the user benefit, in plain language.
      Never mention database, API, or infrastructure changes.
      Write in an excited, marketing-friendly tone.
    dev_rules: |
      Lead with the architecture / business-logic change; cite files as support.
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
    - cron: '17 * * * *'  # check hourly; cooldown limits publication to every 12 hours

concurrency:
  group: dev-updates-publication
  cancel-in-progress: false

jobs:
  notify:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          persist-credentials: false

      - uses: alphakek-ai/dev-updates-action@v2
        with:
          cooldown: '12h'  # post at most once per 12 hours
          channels: |
            ...
```

How it works:
- **First push** after cooldown expires → posts immediately with all changes since last notification
- **Subsequent pushes** within cooldown → skipped silently
- **Cron trigger** → checks for pending changes hourly; generation runs only when publication is due
- Pushes and scheduled runs both respect the cooldown.
- State and frozen messages live in a dedicated `dev-updates-state/*` Git branch, updated with an explicit compare-and-swap lease. GitHub run listings and expiring artifacts are not used.
- Successful channel deliveries are recorded individually. Retrying an unfinished batch sends only unattempted channels or those whose previous attempt was definitively rejected.
- Timeouts, crashes during delivery, and failed post-delivery journal writes leave a `pending` record. The action reports an error instead of automatically sending that message again.

## Upgrading and initializing state

Version 2 requires `contents: write`, full Git history, and an initialized state branch. Version 1 remains unchanged. Use one state branch and one concurrency group per publisher; do not cancel a running publisher.

Stop the old publisher during cutover. Inspect its latest successful delivery logs and the corresponding `dev-updates-state.json` artifact; verify every configured channel delivered before taking its `last_sha` and `last_at`. From a full checkout of the consumer repository, with the new action code available locally, run:

```sh
python3 /path/to/dev-updates-action/publication.py initialize \
  --branch dev-updates-state/default --sha FULL_LAST_PUBLISHED_SHA --at UNIX_TIMESTAMP
```

This creates only the dedicated state branch. It refuses to overwrite existing state. For a new publisher, explicitly choose the commit before the first changes you want announced and use timestamp 0. Set the action's `state_branch` input if not using the default. Enable the updated workflow after initialization. Source-branch history is untouched.

The journal contains generated summaries; use a private repository for private summaries. Grant branch writes only to the publisher and trusted operators. Never delete or reset the journal to recover a failed run.

## Reconciling uncertain delivery

A `pending` required channel means delivery may have happened. Inspect the destination and the journal's frozen batch before choosing an outcome. Optional channels are abandoned without retry on any failure, including uncertain delivery, so they cannot block required channels:

```sh
python3 /path/to/dev-updates-action/publication.py resolve \
  --branch dev-updates-state/default --channel CHANNEL_NAME --outcome sent
```

Use `--outcome retry` only after verifying that the message was not delivered and that the original publisher has stopped. Use `--outcome abandon` to explicitly skip an unfinished delivery, including a permanent rejection (deleted chat/thread or malformed frozen message). Then rerun with the original channel configuration to finish that batch before changing channel configuration. Already-sent channels remain skipped. Resolution itself sends nothing. Do not resolve while a publisher is running.

There is no exactly-once guarantee across Git and messaging APIs: a lost response cannot prove whether a message was delivered. The journal makes that uncertainty explicit and prevents blind retries.

If every channel is optional and all fail, the batch is abandoned and the run reports an error. Its changes are not automatically replayed, since an unacknowledged optional message may already exist at its destination.

If generation itself repeatedly fails and no batch exists, an operator can intentionally skip an unpublished range:

```sh
python3 /path/to/dev-updates-action/publication.py advance \
  --branch dev-updates-state/default --sha FULL_DESCENDANT_SHA --reason 'Why these changes will not be announced'
```

This sends nothing, refuses backward/divergent moves and outstanding batches, and records the skipped range and reason in the journal. Pause the publisher while making this decision. Generation bounds the supplied log/stat/diff to about 80 KiB; read-only tools remain available for additional source context.

Supported cooldown formats: `30m`, `6h`, `1d`, or raw seconds.

## Requirements

- `CLAUDE_CODE_OAUTH_TOKEN` secret — for Claude Code ([get one here](https://console.anthropic.com))
- Channel-specific tokens/webhooks as secrets
- `contents: write` permission for journal writes, an initialized state branch, and `fetch-depth: 0`
- `persist-credentials: false` on checkout; only preparation and publication receive the Git token

Summary generation uses read-only model tools in Claude Code safe mode; trusted code captures the returned JSON and writes the summaries. Delivery dependencies are version- and hash-locked in `requirements.txt`.

## License

Apache 2.0
