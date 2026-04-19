# hermes-miniapp

A Telegram Mini App that gives you a full-screen mobile chat interface for your [Hermes](https://hermesagent.ai) AI agent — directly inside Telegram.

Tap **Open App** in the bot menu or send `/app` to launch the interface. Features streaming responses, a live model/pricing selector, and slash command autocomplete.

---

## Features

- **Streaming chat** — responses appear token-by-token with tool-use indicators
- **Model selector** — browse all OpenRouter models with live input/output pricing; tap to switch
- **Slash command autocomplete** — type `/` to see all available commands, filter as you type
- **Persistent sessions** — conversation history survives app restarts
- **Telegram theming** — automatically matches the user's Telegram light/dark color scheme
- **Markdown rendering** — code blocks, bold, italics, links

---

## Prerequisites

- [Hermes agent](https://hermesagent.ai) installed (`~/.hermes/hermes-agent/`)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- A public HTTPS URL pointing at your Hermes API server (port 8642)

> Telegram Mini Apps require HTTPS. For local setups, use [ngrok](https://ngrok.com) or [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/).

---

## Install

```bash
git clone https://github.com/yourhandle/hermes-miniapp
cd hermes-miniapp
python3 install.py
```

The installer patches two files in `~/.hermes/hermes-agent/` and creates a git hook so the changes survive Hermes auto-updates. It is idempotent — safe to run multiple times.

**Preview changes without applying them:**
```bash
python3 install.py --dry-run
```

---

## Configuration

After installing, add the following to `~/.hermes/.env`:

```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_WEBAPP_URL=https://your-public-url.com
API_SERVER_CORS_ORIGINS=*
```

And to `~/.hermes/config.yaml`:

```yaml
platforms:
  telegram:
    enabled: true
    extra:
      webapp_url: https://your-public-url.com
  api_server:
    enabled: true
    extra:
      host: 0.0.0.0        # expose beyond localhost so ngrok/tunnel can reach it
      public_url: https://your-public-url.com
```

Then restart:
```bash
hermes gateway run
```

---

## Exposing publicly

### ngrok (quickest)

```bash
ngrok http 8642
```

Copy the `https://xxxx.ngrok-free.app` URL into your config. Free accounts can claim one **static** subdomain at [dashboard.ngrok.com/domains](https://dashboard.ngrok.com/domains) so the URL doesn't change on restart.

### Cloudflare Tunnel (permanent, free)

```bash
brew install cloudflare/cloudflare/cloudflared
cloudflared tunnel login
cloudflared tunnel create hermes
cloudflared tunnel route dns hermes hermes.yourdomain.com
cloudflared tunnel run --url http://localhost:8642 hermes
```

---

## Usage

- **Menu button** — tap **Open App** at the bottom of any chat with your bot
- **/app** — send `/app` in chat to get an inline button

---

## Uninstall

```bash
python3 install.py --uninstall
hermes gateway run
```

---

## How it works

The installer makes targeted string insertions into two Hermes files:

| File | What changes |
|------|-------------|
| `gateway/platforms/api_server.py` | 7 new routes + handlers: `/webapp`, `/v1/webapp/auth`, `/v1/webapp/chat`, `/v1/webapp/models`, `/v1/webapp/model`, `/v1/webapp/commands` |
| `gateway/platforms/telegram.py` | `_webapp_url` config, menu button setup, `/app` command |
| `gateway/platforms/webapp/index.html` | Created — the full mini app UI |
| `.git/hooks/post-merge` | Re-applies patches after Hermes auto-updates |

The mini app authenticates via [Telegram WebApp initData](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app) (HMAC-SHA256 signed by your bot token). Sessions are scoped to the Telegram user ID and stored in Hermes's session database.

---

## API endpoints added

| Method | Path | Description |
|--------|------|-------------|
| GET | `/webapp` | Serves the mini app HTML |
| POST | `/v1/webapp/auth` | Validates Telegram initData, returns session token |
| POST | `/v1/webapp/chat` | Streaming SSE chat |
| GET | `/v1/webapp/models` | OpenRouter model list with live pricing |
| POST | `/v1/webapp/model` | Set model for session |
| GET | `/v1/webapp/commands` | Available slash commands |
