# progress-thread-redirect

Hermes Agent plugin that redirects Telegram tool-progress bubbles to a dedicated
thread (a forum topic in a group) so the main chat stays clean.

## Features

- Renders a single left-to-right progress bar (`▬`/`▭`, 10 steps, percent label)
  plus one `Working…` line per tool progress bubble.
- Sends progress exclusively to a target group + thread.
- **24h auto-delete**: every sent progress bubble's message id + timestamp is
  recorded in a small JSON file. On each send, any recorded bubble older than
  24 hours is deleted. Newer bubbles accumulate and stay visible until the
  24h window passes. Replaces any immediate delete-previous behavior.

## Installation

Place this directory under your Hermes plugins dir (e.g. `/opt/data/plugins/`)
and enable the plugin in the Hermes config:

```yaml
plugins:
  enabled:
    - progress-thread-redirect
```

## Configuration (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `PROGRESS_TARGET_CHAT_ID` | `-1003628710660` | Target group chat id (`-100…`) |
| `PROGRESS_TARGET_THREAD_ID` | `3235` | Target forum thread id |
| `PROGRESS_PLUGIN_HISTORY` | `/tmp/progress_bubble_history.json` | Path to the bubble history JSON file (records sent bubbles for the 24h sweep) |

Default values match the author's setup. Override them per-install via the
Hermes `.env` / environment for your own group and thread.

### Getting chat/thread ids

For a topic link `https://t.me/c/3628710660/3235`:
- chat id = `-100` + the first number → `-1003628710660`
- thread id = the second number → `3235`

## Behavior notes

- Only bubbles sent by this plugin are tracked/cleaned. Nothing else in the
  target thread is touched.
- The sweep runs on each send; if the gateway is idle for long stretches, the
  sweep only runs on the next send. Send volume is low, so this is fine.
- Telegram only allows bots to delete their own messages within 48h of send;
  a 24h window is comfortably inside that limit.
- History file is capped at 300 entries.
