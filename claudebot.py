import json
import re
import sqlite3
import discord
import anthropic
import aiohttp
import random
import os
import base64
import asyncio
from datetime import datetime
from dotenv import load_dotenv
from ddgs import DDGS

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None

# ── Configuration ─────────────────────────────────────────────────────────────
# .env file should contain: ANTHROPIC_API_KEY, DISCORD_TOKEN, WOLFRAM_APP_ID (optional)

MODEL = "claude-haiku-4-5"  # model to use
SUMMARY_MODEL = "claude-haiku-4-5" # model to use to summarize context, changing not recommended
TRIGGER_KEYWORDS = ["claude", "clanker"]  # keywords that trigger a response
IGNORED_USERS = ["SamAltman", "MEE6"]  # usernames to ignore, case sensitive

MAX_MESSAGES = 150  # max messages before truncation and summary
TRUNCATION = 50  # how many messages are left after truncating
MAX_TOKENS = 5000  # max output tokens per response
INPUT_CHAR_CAP = 20000  # max characters for any single text input (web fetch, file reads)
RANDOM_RESPONSE_CHANCE = 0.001  # chance to respond unprompted (1 in 1000 default)
RATE_LIMIT_WINDOW = 5  # rate limit window in seconds
RATE_LIMIT_MESSAGES = 2  # max messages per user within the window

USE_LONG_CACHE = False  # True for 1h cache, False for 5m cache. change according to bot usage frequency. most use cases benefit from False
CACHE_BLOCK_SIZE = 10  # number of messages per cache block (cached_count = n - (n % CACHE_BLOCK_SIZE))

STARTING_PROMPT_FILE = 'systemprompt.txt'  # system prompt file
MESSAGES_FILE = 'messages.txt'  # rolling message log file... yes you python nerds i should've done this in a dictionary or whatever, but too late now

# values used for cost estimation in dollars per million tokens, uses haiku 4.5's costs by default but change according to the pricing table if you want more accurate estimates
INPUT_TOKENS_COST = 1.0
OUTPUT_TOKENS_COST = 5.0
# actual cost may vary

# ──────────────────────────────────────────────────────────────────────────────

CACHE_WRITE_COST_MULTIPLIER = 2.0 if USE_LONG_CACHE else 1.25  # these are the price differences between cache lengths, don't touch

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Bot(intents=intents)

anthropic_client = anthropic.AsyncAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
discord_token = os.getenv('DISCORD_TOKEN')
wolfram_app_id = os.getenv('WOLFRAM_APP_ID')
tavily_client = TavilyClient() if TavilyClient and os.getenv('TAVILY_API_KEY') else None

TEXT_EXTENSIONS = {
    '.txt', '.md', '.py', '.js', '.ts', '.cpp', '.c', '.h', '.hpp',
    '.java', '.cs', '.go', '.rs', '.rb', '.php', '.html', '.css',
    '.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg',
    '.sh', '.bat', '.ps1', '.sql', '.r', '.swift', '.kt',
    '.rst', '.csv', '.log', '.tex'
}

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}

BLOCKED_EXTENSIONS = {
    '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.zst',
    '.exe', '.dll', '.so', '.dylib', '.bin', '.jar', '.class', '.war', '.ear', '.apk', '.ipa',
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v',
    '.mp3', '.wav', '.flac', '.ogg', '.aac', '.m4a', '.wma',
    '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls', '.odt', '.ods', '.odp',
    '.iso', '.img', '.dmg', '.deb', '.rpm', '.msi', '.pkg',
    '.ttf', '.otf', '.woff', '.woff2',
    '.pyc', '.pyd', '.o', '.a',
    '.db', '.sqlite', '.sqlite3',
    '.bmp', '.tiff', '.ico', '.svg',
}

user_message_times = {}

CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"} if USE_LONG_CACHE else {"type": "ephemeral"}

with open(STARTING_PROMPT_FILE, 'r', encoding='utf-8') as file:
    system_prompt = file.read()

with open('aliases.json', 'r', encoding='utf-8') as f:
    aliases = json.load(f)

def init_db():
    conn = sqlite3.connect('memory.db')
    conn.execute('''CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory TEXT NOT NULL,
        timestamp TEXT NOT NULL
    )''')
    conn.commit()
    conn.close()

def save_memory(memories):
    if isinstance(memories, str):
        memories = [memories]
    conn = sqlite3.connect('memory.db')
    conn.executemany('INSERT INTO memories (memory, timestamp) VALUES (?, ?)',
                 [(m, datetime.now().isoformat()) for m in memories])
    conn.commit()
    conn.close()

def recall_memories(query: str) -> list:
    conn = sqlite3.connect('memory.db')
    words = [w for w in query.lower().split() if w]
    if not words:
        conn.close()
        return []
    sql = "SELECT id, memory FROM memories WHERE " + " AND ".join(["LOWER(memory) LIKE ?" for _ in words])
    params = [f'%{w}%' for w in words]
    rows = conn.execute(sql + " ORDER BY timestamp DESC LIMIT 10", params).fetchall()
    conn.close()
    return [f"[id:{r[0]}] {r[1]}" for r in rows]

def delete_memory(memory_ids):
    if isinstance(memory_ids, int):
        memory_ids = [memory_ids]
    conn = sqlite3.connect('memory.db')
    conn.execute(f'DELETE FROM memories WHERE id IN ({",".join("?" * len(memory_ids))})', memory_ids)
    conn.commit()
    conn.close()

init_db()

def load_messages():
    with open(MESSAGES_FILE, 'r', encoding='utf-8') as f:
        content = f.read()
        return [l for l in content.splitlines() if l.strip()]

def save_messages(lines):
    with open(MESSAGES_FILE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

def format_message(message):
    channel = message.channel
    if isinstance(channel, discord.DMChannel):
        channel_name = "DM"
    elif isinstance(channel, discord.Thread):
        parent_name = channel.parent.name if channel.parent else "unknown"
        channel_name = f"{parent_name}/{channel.name}"
    elif channel.type == discord.ChannelType.voice:
        channel_name = f"{channel.name}[VC text]"
    else:
        channel_name = channel.name
    user_name = message.author.name
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    if user_name == client.user.name:
        user_name = "[YOU]"

    content = message.content
    for user in message.mentions:
        content = content.replace(f"<@{user.id}>", f"@{user.display_name}")
    for role in message.role_mentions:
        content = content.replace(f"<@&{role.id}>", f"@{role.name}")
    content = content.replace('\n', '  ')

    if message.embeds:
        for embed in message.embeds:
            parts = []
            if embed.author and embed.author.name:
                parts.append(embed.author.name)
            if embed.title:
                parts.append(embed.title)
            if embed.description:
                parts.append(embed.description)
            if parts:
                content += " [embed: " + " | ".join(parts) + "]"

    for attachment in message.attachments:
        content += f" [file: {attachment.filename} ({attachment.url})]"

    line = f'#{channel_name} {timestamp} @{user_name}: "{content.strip()}"'

    if message.reference and isinstance(message.reference.resolved, discord.Message):
        ref = message.reference.resolved
        ref_user = "[YOU]" if ref.author.name == client.user.name else ref.author.name
        ref_content = ref.content.strip()
        if len(ref_content) > 24:
            ref_content = ref_content[:10] + "..." + ref_content[-10:]
        line += f' [replying to @{ref_user}: "{ref_content}"]'
    
    for original, alias in aliases.items():
        line = line.replace(original, alias)

    return line

def should_respond(message):
    if message.author == client.user:
        return False
    if message.author.name in IGNORED_USERS:
        return False
    content = message.content.lower()
    return (
        client.user.mentioned_in(message)
        or any(k in content for k in TRIGGER_KEYWORDS)
        or random.random() < RANDOM_RESPONSE_CHANCE
    )

async def web_search(query: str) -> str:
    if tavily_client:
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, lambda: tavily_client.search(query, max_results=3))
            results = response.get("results", [])
            if not results:
                return "No results found."
            return "\n".join(
                f"{r['title']}: {r.get('content', '')}" for r in results
            )
        except Exception as e:
            return f"Search failed: {e}"
    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, lambda: list(DDGS().text(query, max_results=3)))
        if not results:
            return "No results found."
        return "\n".join(
            f"{r['title']}: {r['body']}" for r in results
        )
    except Exception as e:
        return f"Search failed: {e}"
    
async def web_fetch(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers={"User-Agent": "curl/7.54"}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                text = re.sub(r'<[^>]+>', '', text)
                text = re.sub(r'\s+', ' ', text).strip()
                return text[:INPUT_CHAR_CAP]
    except Exception as e:
        return f"Fetch failed: {e}"
    
async def wolfram_query(query: str) -> str:
    if not wolfram_app_id:
        return "Wolfram Alpha is not configured."
    try:
        async with aiohttp.ClientSession() as session:
            params = {"appid": wolfram_app_id, "input": query, "maxchars": 2000}
            async with session.get("https://www.wolframalpha.com/api/v1/llm-api", params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return await resp.text()
    except Exception as e:
        return f"Wolfram query failed: {e}"

async def image_search(query: str):
    try:
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(None, lambda: list(DDGS().images(query, max_results=5, safesearch='off', type_image="photo")))
        if not results:
            return None, "DDGS returned no results."
        async with aiohttp.ClientSession() as session:
            for result in results:
                try:
                    async with session.get(result["image"], timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.content_type.startswith("image/"):
                            return result["image"], None
                except:
                    continue
        return None, f"All {len(results)} image URLs failed to fetch."
    except Exception as e:
        return None, f"Image search failed: {e}"

@client.event
async def on_message_edit(before, after):
    if before.content == after.content and not before.embeds and after.embeds:
        lines = load_messages()
        new_line = format_message(after)
        for i, line in enumerate(lines):
            if f'@{after.author.name}' in line and line.endswith('""'):
                lines[i] = new_line
                save_messages(lines)
                return
        lines.append(new_line)
        save_messages(lines)

@client.event
async def on_message(message):
    if isinstance(message.channel, discord.DMChannel):
        return

    lines = load_messages()
    new_line = format_message(message)
    lines.append(new_line)
    save_messages(lines)

    if not should_respond(message):
        return

    if len(lines) > MAX_MESSAGES:
        to_summarize = lines[:len(lines) - TRUNCATION]
        lines = lines[len(lines) - TRUNCATION:]
        print(f"\n\nContext limit hit. Summarizing and truncating...")
        try:
            summary_response = await anthropic_client.messages.create(
                model=SUMMARY_MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": "Summarize these chat messages briefly for a chatbot without formatting, preserving key topics, names, and context:\n" + "\n".join(to_summarize)}]
            )
            summary = summary_response.content[0].text
            summary_tokens_in = summary_response.usage.input_tokens
            summary_tokens_out = summary_response.usage.output_tokens
            summary_cache_read = getattr(summary_response.usage, 'cache_read_input_tokens', 0)
            summary_cache_write = getattr(summary_response.usage, 'cache_creation_input_tokens', 0)
            summary_cost = (
                (summary_tokens_in / 1_000_000) * INPUT_TOKENS_COST +
                (summary_tokens_out / 1_000_000) * OUTPUT_TOKENS_COST +
                (summary_cache_write / 1_000_000) * (INPUT_TOKENS_COST * 1.25) +
                (summary_cache_read / 1_000_000) * (INPUT_TOKENS_COST * 0.1)
            )
            print(f"SUMMARY TOKENS IN: {summary_tokens_in} OUT: {summary_tokens_out} | CACHE WRITE: {summary_cache_write} READ: {summary_cache_read} | ESTIMATED COST: ${summary_cost:.6f}")
        except Exception:
            summary = "Earlier conversation context unavailable."
        lines.insert(0, f"[Summary of earlier messages: {summary}]")
        save_messages(lines)

    now = datetime.now().timestamp()
    user_id = message.author.id
    times = user_message_times.get(user_id, [])
    times = [t for t in times if now - t < RATE_LIMIT_WINDOW]
    if len(times) >= RATE_LIMIT_MESSAGES:
        await message.reply("You're sending messages faster than I can process them. Back off.")
        return
    times.append(now)
    user_message_times[user_id] = times

    async with message.channel.typing():
        tool_notes = []

        all_lines = lines[:-1]
        n = len(all_lines)
        cached_count = n - (n % CACHE_BLOCK_SIZE)
        cached_lines = all_lines[:cached_count]
        live_lines = all_lines[cached_count:]

        content = []
        if cached_lines:
            content.append({
                "type": "text",
                "text": "\n".join(cached_lines),
                "cache_control": CACHE_CONTROL
            })
        live_text = ("\n".join(live_lines) + "\n" if live_lines else "") + "RESPOND TO: " + new_line
        content.append({"type": "text", "text": live_text})

        messages = [{"role": "user", "content": content}]
        image_to_send = None
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_read_tokens = 0
        total_cache_write_tokens = 0
        response_text = "*no response*"
        try:
            while True:
                try:
                    response = await anthropic_client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=[{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": CACHE_CONTROL
                    }],
                    tools=[{
                        "name": "web_search",
                        "description": "Search the web for current information",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Search query"}
                            },
                            "required": ["query"]
                        },
                    },
                    {
                        "name": "web_fetch",
                        "description": "Fetch the contents of a webpage by URL. Optionally chain with web_search to get details from a search.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "The URL to fetch"}
                            },
                            "required": ["url"]
                        }
                    },
                    {
                        "name": "wolfram_query",
                        "description": "Query Wolfram Alpha for mathematical calculations, scientific facts, unit conversions, and real-world data",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "The query to send to Wolfram Alpha"}
                            },
                            "required": ["query"]
                        }
                    },
                    {
                        "name": "remember",
                        "description": "Save one or more facts to long-term memory. Use conservatively — only for clear facts, preferences, or important context. Do NOT use for casual conversation. Do NOT use special characters, brackets or punctuation.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "memory": {"type": ["string", "array"], "items": {"type": "string"}, "description": "A fact or list of facts to remember"}
                            },
                            "required": ["memory"]
                        }
                    },
                    {
                        "name": "recall_memory",
                        "description": "Search long-term memory for stored facts. Use when you need to remember something about a user or the server. Use non-specific KEYWORDS, NOT phrases.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Keyword to search memories for"}
                            },
                        "required": ["query"]
                        }
                    },
                    {
                        "name": "delete_memory",
                        "description": "Delete one or more memory entries by ID. Use recall_memory first to find IDs, then delete. Chain with remember to correct outdated information.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "memory_id": {"type": ["integer", "array"], "items": {"type": "integer"}, "description": "An ID or list of IDs to delete"}
                            },
                            "required": ["memory_id"]
                        }
                    },
                    {
                        "name": "continue_task",
                        "description": "Use this to think through a multi-step task or plan your next action. You MUST call another tool immediately after this one — NEVER generate a raw response mid-task. Put all reasoning and planning in the task field instead of responding with text. Only stop calling tools when the task is fully complete.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "task": {"type": "string", "description": "What you're doing next"}
                            },
                            "required": ["task"]
                        }
                    },
                    {
                        "name": "image_search",
                        "description": "Search for an image and send it in chat",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "Image search query"}
                            },
                            "required": ["query"]
                        }
                    },
                    {
                        "name": "file_fetch",
                        "description": f"Fetch a non-text file from a URL and analyze it. Use for images ({', '.join(IMAGE_EXTENSIONS)}) and PDFs. For text files ({', '.join(TEXT_EXTENSIONS)}), use web_fetch instead.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string", "description": "Direct URL to the file"}
                            },
                            "required": ["url"]
                        }
                    }],
                    messages=messages
                )
                except Exception as e:
                    response_text = f"*tool call failed: {e}*"
                    break

                if response.stop_reason == "tool_use":
                    total_input_tokens += response.usage.input_tokens
                    total_output_tokens += response.usage.output_tokens
                    total_cache_read_tokens += getattr(response.usage, 'cache_read_input_tokens', 0)
                    total_cache_write_tokens += getattr(response.usage, 'cache_creation_input_tokens', 0)
                    tool_uses = [b for b in response.content if b.type == "tool_use"]
                    tool_results = []
                    
                    for tool_use in tool_uses:
                        print(f"TOOL: {tool_use.name} | INPUT: {tool_use.input}")
                        if tool_use.name == "web_search":
                            query = tool_use.input["query"]
                            result = await web_search(query)
                            tool_notes.append(f"-# Searched the web: {query}")
                        elif tool_use.name == "web_fetch":
                            url = tool_use.input["url"]
                            ext = os.path.splitext(url.split('?')[0])[1].lower()
                            if ext in BLOCKED_EXTENSIONS:
                                result = "Unsupported file format, cannot read this file type."
                                tool_notes.append(f"-# Web fetch failed")
                            elif ext in IMAGE_EXTENSIONS or ext == '.pdf':
                                result = "Cannot fetch binary file with web_fetch. Use file_fetch instead."
                                tool_notes.append(f"-# Web fetch failed")
                            else:
                                result = await web_fetch(url)
                                tool_notes.append(f"-# Fetched: <{url}>")
                        elif tool_use.name == "wolfram_query":
                            query = tool_use.input["query"]
                            result = await wolfram_query(query)
                            tool_notes.append(f"-# Queried Wolfram Alpha: {query}")
                        elif tool_use.name == "remember":
                            memory = tool_use.input["memory"]
                            memories = memory if isinstance(memory, list) else [memory]
                            save_memory(memories)
                            result = f"Remembered {len(memories)} item(s)"
                            for m in memories:
                                tool_notes.append(f"-# Remembered: {m}")
                        elif tool_use.name == "recall_memory":
                            query = tool_use.input["query"]
                            results = recall_memories(query)
                            result = "\n".join(results) if results else "Nothing found."
                            tool_notes.append(f"-# Recalled memories: {query}")
                        elif tool_use.name == "delete_memory":
                            memory_id = tool_use.input["memory_id"]
                            ids = memory_id if isinstance(memory_id, list) else [memory_id]
                            delete_memory(ids)
                            result = f"Deleted {len(ids)} memory entry(s)"
                            for i in ids:
                                tool_notes.append(f"-# Deleted memory entry: {i}")
                        elif tool_use.name == "continue_task":
                            task = tool_use.input["task"]
                            result = "Continuing..."
                            print(f"THINKING: {task}")
                            await message.channel.send("-# Thinking...")
                        elif tool_use.name == "image_search":
                            query = tool_use.input["query"]
                            img_url, error = await image_search(query)
                            if img_url:
                                image_to_send = img_url
                                result = f"Found image for '{query}'"
                                tool_notes.append(f"-# Found image: {query}")
                            else:
                                result = error
                                tool_notes.append(f"-# Image search failed for {query}: {error}")
                        elif tool_use.name == "file_fetch":
                            url = tool_use.input["url"]
                            ext = os.path.splitext(url.split('?')[0])[1].lower()
                            if ext in BLOCKED_EXTENSIONS:
                                result = "Unsupported file format, cannot read this file type."
                                tool_notes.append(f"-# File fetch failed")
                            elif ext in TEXT_EXTENSIONS:
                                result = "Cannot fetch text file with file_fetch. Use web_fetch instead."
                                tool_notes.append(f"-# File fetch failed")
                            else:
                                try:
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                            file_data = await resp.read()
                                    if ext == '.pdf':
                                        tool_results.append({
                                            "type": "tool_result",
                                            "tool_use_id": tool_use.id,
                                            "content": [{
                                                "type": "document",
                                                "source": {
                                                    "type": "base64",
                                                    "media_type": "application/pdf",
                                                    "data": base64.b64encode(file_data).decode("utf-8")
                                                }
                                            }]
                                        })
                                    else:
                                        if file_data[:8] == b'\x89PNG\r\n\x1a\n':
                                            media_type = "image/png"
                                        elif file_data[:3] == b'\xff\xd8\xff':
                                            media_type = "image/jpeg"
                                        elif file_data[:4] == b'GIF8':
                                            media_type = "image/gif"
                                        elif file_data[:4] == b'RIFF' and file_data[8:12] == b'WEBP':
                                            media_type = "image/webp"
                                        else:
                                            media_type = "image/jpeg"
                                        tool_results.append({
                                            "type": "tool_result",
                                            "tool_use_id": tool_use.id,
                                            "content": [{
                                                "type": "image",
                                                "source": {
                                                    "type": "base64",
                                                    "media_type": media_type,
                                                    "data": base64.b64encode(file_data).decode("utf-8")
                                                }
                                            }]
                                        })
                                    tool_notes.append(f"-# Fetched file: <{url}>")
                                    continue
                                except Exception as e:
                                    result = f"File fetch failed: {e}, have you tried web_fetch instead?"
                                    tool_notes.append(f"-# File fetch failed: {e}")
                        else:
                            result = "Tool not recognized."
                        
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result
                        })
                    
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})
                else:
                    total_input_tokens += response.usage.input_tokens
                    total_output_tokens += response.usage.output_tokens
                    total_cache_read_tokens += getattr(response.usage, 'cache_read_input_tokens', 0)
                    total_cache_write_tokens += getattr(response.usage, 'cache_creation_input_tokens', 0)
                    response_text = next((b.text for b in response.content if hasattr(b, "text")), "")
                    break
        except Exception as e:
            await message.reply("*something went wrong*")
            print(f"ERROR: {e}\n")
            return

        response_text = response_text.replace('\n\n', '\n').replace('@', '')
        if not response_text.strip():
            response_text = "*no response*"

        if tool_notes:
            response_text = "\n".join(tool_notes) + "\n" + response_text

        if len(response_text) > 2000:
            chunks = [response_text[i:i+2000] for i in range(0, len(response_text), 2000)]
            await message.reply(chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        else:
            await message.reply(response_text)
        if image_to_send:
            await message.channel.send(image_to_send)
        cost = (
            (total_input_tokens / 1000000) * INPUT_TOKENS_COST +
            (total_output_tokens / 1000000) * OUTPUT_TOKENS_COST +
            (total_cache_write_tokens / 1000000) * (INPUT_TOKENS_COST * 1.25) +
            (total_cache_read_tokens / 1000000) * (INPUT_TOKENS_COST * 0.1)
        )
        print(f"\nDEBUG: {new_line}\nTOKENS IN: {total_input_tokens} OUT: {total_output_tokens} | CACHE WRITE: {total_cache_write_tokens} READ: {total_cache_read_tokens}\nESTIMATED COST: ${cost:.5f} MODEL: {MODEL} \nRESPONSE: {response_text}\n")
client.run(discord_token)
