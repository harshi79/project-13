# Yori Telegram Image Studio

Advanced Telegram image generator bot using your API endpoint.

Made by `@yorifederation`
Developer: `@yorichiiprime`
Main channel: `@yorifederation`

## New UI upgrade

- Welcome image stays as the main UI message
- Inline buttons update the **same welcome message** instead of sending extra panel/help/model messages
- Uses safe message editing for photo captions, so it does not break on image welcome messages
- Premium styled captions and cleaner compact layout
- Owner-only admin panel inside the welcome UI
- Per-user render queue: if a user sends another prompt while a render is running, it gets queued automatically
- Result cards still send generated images normally

## Features

- Direct polling only, no webhook
- Welcome image on `/start`
- Custom buttons under welcome message
- Model, size, quality switching with buttons
- Prompt enhance toggle
- Negative prompt support
- One-shot `/img --enhance`
- One-shot `/img --neg ...`
- Animated loading status while generating
- Quick rerender buttons under each result
- Per-user queue system for back-to-back prompts
- Admin-only broadcast system
- Owner-only admin panel with live audience + queue stats
- Persistent chat registry for broadcast targets
- Supports normal image URLs and base64 API responses
- Private chat plain-text prompt support

## Supported models

- `gpt-image-1`
- `dalle3`
- `othmaker2`

## Commands

### User

- `/start` — open or update studio welcome UI
- `/img your prompt` — generate image
- `/img dalle3 | your prompt` — one-time model override
- `/img knight in storm --enhance` — one-time prompt enhance
- `/img forest spirit --neg text, watermark, blurry` — one-time negative prompt
- `/settings` — open inline studio panel
- `/panel` — same as settings
- `/setneg` — wait for next text as negative prompt
- `/setneg blurry, low quality, text` — save negative prompt directly
- `/clearneg` — clear saved negative prompt
- `/enhance` — toggle enhance mode
- `/enhance on` — force enhance on
- `/enhance off` — force enhance off
- `/cancel` — cancel pending negative prompt input
- `/help` — open help UI

### Admin only

- `/admin` — open owner panel
- `/users` — audience count
- Reply to any message with `/broadcast` — copy-broadcast that message to all known chats
- `/broadcast your text here` — text broadcast to all known chats

## Setup

### 1. Create bot token

Get a bot token from `@BotFather`.

### 2. Configure env

```bash
cd telegram-image-bot
cp .env.example .env
```

Put your real bot token in `.env`:

```env
TELEGRAM_BOT_TOKEN=your_real_token_here
IMAGE_API_URL=https://nepcoderapis.pages.dev/api/v1/images/generations
DEFAULT_MODEL=gpt-image-1
DEFAULT_SIZE=1024x1024
DEFAULT_QUALITY=standard
DEFAULT_ENHANCE=off
REQUEST_TIMEOUT=180
WELCOME_IMAGE_URL=https://files.catbox.moe/xmqm6h.png
DEVELOPER_URL=https://t.me/yorichiiprime
CHANNEL_URL=https://t.me/yorifederation
ADMIN_IDS=7728424218
```

### 3. Install

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python bot.py
```

## Usage examples

```text
/img a futuristic bike racing through neon tokyo at night
```

```text
/img cinematic fox mage with glowing staff in misty forest --enhance
```

```text
/img dark castle in rain --neg blurry, extra fingers, watermark, text
```

In private chat, users can also just send plain text:

```text
cinematic fox mage with glowing staff in misty forest
```

## Notes

- Bot runs with long polling only.
- Button interactions update the same stored UI message whenever possible.
- Prompt history is temporary in memory, but broadcast audience registry is saved in `data/registry.json`.
- Admin broadcast works only for chats that already interacted with the bot.
