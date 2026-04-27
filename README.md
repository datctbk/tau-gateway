# tau-gateway

Multi-platform messaging gateway for tau.

Current adapters include:
- Telegram
- Discord
- Slack
- API Server

This guide focuses on finishing Telegram setup for local and production use.

## Quick Start (Telegram)

1) Install dependencies

```bash
cd /Users/trantandat/Documents/claud-code-leak
python3 -m pip install "python-telegram-bot>=20" pyyaml
```

2) Configure credentials

Option A: environment variables

```bash
export TAU_GATEWAY_TELEGRAM_TOKEN="YOUR_BOT_TOKEN"
export TAU_GATEWAY_PROVIDER="openai"
export TAU_GATEWAY_MODEL="gpt-4o-mini"
```

Option B: `~/.tau/gateway.yaml` (recommended)

```yaml
platforms:
  telegram:
    platform: telegram
    enabled: true
    token: "YOUR_BOT_TOKEN"
    reply_to_mode: always
provider: openai
model: gpt-4o-mini
max_tokens: 8192
max_turns: 20
```

3) Run gateway

```bash
python3 tau-gateway/__main__.py
```

4) Verify in Telegram

- Send `/start`
- Send `/status`
- Send a normal message and confirm agent response

## Runtime Notes

- Telegram adapter currently uses polling mode.
- In tau REPL (with gateway extension loaded), run `/gateway-setup telegram`
  to print setup commands and a config template.
- `reply_to_mode`:
  - `always`: reply to all messages
  - `mention`: in groups, only reply when bot is mentioned
  - `never`: do not auto-reply

## Troubleshooting

- `Telegram token is required`
  - set `TAU_GATEWAY_TELEGRAM_TOKEN` or put `token` in `~/.tau/gateway.yaml`
- `python-telegram-bot is required`
  - install: `python3 -m pip install "python-telegram-bot>=20"`
- Gateway starts but no reply
  - verify bot token, check `reply_to_mode`, and confirm provider credentials are set
- No adapters initialized
  - check `enabled: true` under `platforms.telegram`

## Production Checklist (Telegram)

1) Security
- Store bot token in env/secret manager, never commit it.
- Restrict shell/user permissions on host.
- Rotate token if leaked.

2) Reliability
- Run gateway under process manager (`systemd`, `supervisord`, or container restart policy).
- Enable restart on failure.
- Add startup dependency checks (network, provider key presence).

3) Observability
- Capture stdout/stderr logs centrally.
- Monitor process uptime and restart count.
- Alert on repeated adapter failures or provider auth errors.

4) Safety
- Keep `reply_to_mode: mention` for busy group chats.
- Define policy profile appropriate for external channels.
- Validate tool permissions before enabling destructive capabilities.

5) Capacity
- Start with one bot instance per token.
- Watch latency under load (provider + network).
- If needed, split traffic by channel/bot.

6) Operations
- Keep a known-good `gateway.yaml`.
- Document deploy, rollback, and token-rotation runbooks.
- Test `/status`, message send/receive, and provider response after each deploy.

## Local Dev Testing

```bash
PYTHONPATH=tau-gateway pytest -q tau-gateway/tests
```

If async tests fail in your environment, install:

```bash
python3 -m pip install pytest-asyncio
```
