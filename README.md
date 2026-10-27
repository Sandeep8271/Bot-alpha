# Alpha Update Telegram Bot

## Setup
1. @BotFather se bot banao, token lo.
2. `pip install -r requirements.txt` (aur ffmpeg install hona chahiye)
3. Run:
   ```
   export BOT_TOKEN=123456:ABC...
   python bot.py
   ```

## Optional env vars
- `ALLOWED_USERS` = `12345,67890` (sirf ye Telegram user ids use kar sakenge)
- `ARTIST_NAME`, `ALBUM_NAME` (default `@king75683`)

## Title rule
1. File ke metadata me title hai -> wahi same title (tag + file name).
2. Title nahi hai -> file name se title (`Ep_12_xyz` -> `Ep 12 xyz`).

Cover: `static/cover.jpg` | Limit: Telegram bot sirf 20 MB tak download kar sakta hai.
