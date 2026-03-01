# discord-llm-ai-claude-bot
*Long name for SEO poisoning lol*

Feature-rich Discord chatbot with Anthropic API. Supports web search, web fetch, Wolfram Alpha, image search, file reading, persistent memory with SQLite, agentic tool chaining, and context summarization. Drop-in configurable with a system prompt and alias file

Uses Claude Haiku 4.5 by default. I don't recommend using better models because of costs — Haiku is more than good enough for Discord chat, but whales go wild

Made over the course of 2 days with help from Claude *(shameful I know, this won't be in my resume)*. Started as just a chatbot for a server with friends, ended up being too drawn in and made it way too capable to not share. The code is actually pretty clean, but I'm not a regular Python dev

Doesn't support DMs and technically doesn't support cross-server usage (input format doesn't distinguish servers, only channels), open an issue or a pull request if you need it

---

## Features

- Many configurable variables within the code
- Rolling message history with automatic summarization on truncation
- Per-user rate limiting (very basic)
- Web search via DuckDuckGo
- Web fetch with HTML stripping
- Wolfram Alpha integration for math, science, and unit conversions
- Image search via DuckDuckGo (for some reason, ran into the most trouble with this, use at own discretion) (opted for this instead of image generation, I am against it)
- Image analysis via vision
- PDF reading (native document block)
- Code and text file reading (`.py`, `.cpp`, `.json`, `.md` and more)
- Persistent memory via SQLite with recall, store, and delete
- Agentic tool chaining with `continue_task` for multi-step autonomous work
- Discord embed parsing
- Message chunking for responses over 2000 characters

---

## Requirements

- Python 3.9 or later
- **Anthropic API key** — get it from [platform.claude.com/settings/keys](https://platform.claude.com/settings/keys) after making an account and paying at least $5 in credits
- **Discord bot token** — get it from [discord.com/developers/applications](https://discord.com/developers/applications)
- **Wolfram Alpha App ID** *(optional)* — get it from [developer.wolframalpha.com](https://developer.wolframalpha.com). Required for math and science queries. Free tier gives 2000 queries/month

### Bot permissions

Enable **Message Content Intent** and the following permissions:

- Read Messages / View Channels
- Send Messages
- Read Message History
- Embed Links
- Attach Files *(not strictly needed, but it might do it — this amalgamation is beyond my understanding at this point)*

---

## Setup

1. Clone the repo or download the latest .zip in Releases
2. Install dependencies:
   ```
   pip install py-cord anthropic aiohttp python-dotenv ddgs
   ```
3. Fill in `.env`:
   ```
   ANTHROPIC_API_KEY=your_key_here
   DISCORD_TOKEN=your_token_here
   WOLFRAM_APP_ID=your_id_here
   ```
4. Edit `systemprompt.txt` to give the bot a personality and any formatting rules
5. Edit `aliases.json` to map Discord usernames to nicknames. Or map any text with anything else
6. Run `run_bot.bat` (Windows) or `run_bot.sh` (Linux)

---

## Configuration

All tunable values are at the top of `claudebot.py`, these are the most important ones:

| Variable | Default | Description |
|---|---|---|
| `MODEL` | `claude-haiku-4-5` | Model to use |
| `TRIGGER_KEYWORDS` | `["claude", "clanker"]` | Keywords that trigger a response |
| `IGNORED_USERS` | `["SamAltman", "MEE6"]` | Usernames to ignore |
| `MAX_MESSAGES` | `150` | Message history size before truncation |
| `TRUNCATION` | `50` | Message count to summarize and drop to |
| `MAX_TOKENS` | `5000` | Max output tokens per response |
| `RANDOM_RESPONSE_CHANCE` | `0.005` | Chance to respond unprompted (1 in 200 default) |
| `RATE_LIMIT_WINDOW` | `5` | Rate limit window in seconds |
| `RATE_LIMIT_MESSAGES` | `2` | Max messages per user per window |

---

## Recommended practices

- **Do NOT use this on a large server** — the rate limiting is minimal and wasn't designed for high traffic, unless you're a whale of course
- The default system prompt is already tuned for maximum capability, just add a personality and some formatting rules on top
- Expect it to burn a decent amount of tokens, especially with the default system prompt that encourages tool chaining and autonomous reasoning

---

Was this project worth it?
