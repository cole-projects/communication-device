# Tanya — Telegram Coaching Bot

A Telegram bot that lets clients chat with Tanya using Claude and the full tanya_brain vault as context.

## Setup (5 minutes)

### 1. Create the Telegram Bot

1. Open Telegram and message **@BotFather**
2. Send `/newbot`
3. Pick a name (e.g. "Tanya Coach") and a username (e.g. `tanya_coach_bot`)
4. Copy the token it gives you

### 2. Get an Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an API key
3. Copy it

### 3. Configure

```bash
cd "Communication Device"
cp .env.example .env
```

Edit `.env` and paste in your two keys:

```
TELEGRAM_TOKEN=paste-your-telegram-token
ANTHROPIC_API_KEY=paste-your-anthropic-key
```

Optional: add Telegram usernames to `ALLOWED_USERS` to restrict who can talk to the bot.

### 4. Install & Run

```bash
pip install -r requirements.txt
python tanya_bot.py
```

That's it. Open Telegram, find your bot, and send `/start`.

## Commands

| Command | What it does |
|---------|-------------|
| `/start` | Begin a new conversation |
| `/reset` | Clear conversation history and start fresh |

## How It Works

- Every message you send goes to Claude with Tanya's full vault context (voice profile, coaching frameworks, response protocol)
- Claude responds as Tanya — following the exact response protocol
- Every exchange is auto-logged to `tanya_brain/07-Conversations.md`
- Conversation history is maintained per chat so Tanya remembers what you've been talking about

## Notes

- The bot loads all vault `.md` files at startup, so if you update the vault, restart the bot
- Runs on your machine — no server needed (keep the terminal open)
- API costs: each message costs a few cents through the Anthropic API
