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

# ── Configuration ─────────────────────────────────────────────────────────────
# .env file should contain: ANTHROPIC_API_KEY, DISCORD_TOKEN, WOLFRAM_APP_ID (optional)

STARTING_PROMPT_FILE = 'systemprompt.txt'  # system prompt file
MESSAGES_FILE = 'messages.txt'  # rolling message log
MAX_MESSAGES = 150  # max messages before truncation
TRUNCATION = 75  # how many messages to summarize and drop
MAX_TOKENS = 5000  # max output tokens per response
RANDOM_RESPONSE_CHANCE = 0.005  # chance to respond unprompted (1 in 200)
RATE_LIMIT_MESSAGES = 2  # max messages per user within the window
RATE_LIMIT_WINDOW = 5  # rate limit window in seconds

MODEL = "claude-haiku-4-5"  # model to use
SUMMARY_MODEL = "claude-haiku-4-5" # model to use to summarize context, not recommended to change
IGNORED_USERS = ["SamAltman", "MEE6"]  # usernames to ignore, case sensitive
TRIGGER_KEYWORDS = ["claude", "clanker"]  # keywords that trigger a response
# ──────────────────────────────────────────────────────────────────────────────

# uncomment the line below to clear the messages file on launch.
# not sure why you would do that though
# open(MESSAGES_FILE, 'w', encoding='utf-8').close()

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

anthropic_client = anthropic.AsyncAnthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
discord_token = os.getenv('DISCORD_TOKEN')
wolfram_app_id = os.getenv('WOLFRAM_APP_ID')

user_message_times = {}

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

def save_memory(memory: str):
    conn = sqlite3.connect('memory.db')
    conn.execute('INSERT INTO memories (memory, timestamp) VALUES (?, ?)',
                 (memory, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def recall_memories(query: str) -> list:
    conn = sqlite3.connect('memory.db')
    words = query.lower().split()
    sql = "SELECT id, memory FROM memories WHERE " + " AND ".join(["LOWER(memory) LIKE ?" for _ in words])
    params = [f'%{w}%' for w in words]
    rows = conn.execute(sql + " ORDER BY timestamp DESC LIMIT 10", params).fetchall()
    conn.close()
    return [f"[id:{r[0]}] {r[1]}" for r in rows]

def delete_memory(memory_id: int):
    conn = sqlite3.connect('memory.db')
    conn.execute('DELETE FROM memories WHERE id = ?', (memory_id,))
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
    channel_name = message.channel.name
    user_name = message.author.name
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')

    if user_name == client.user.name:
        user_name = "[YOU]"

    content = message.content
    for user in message.mentions:
        content = content.replace(f"<@{user.id}>", f"@{user.display_name}")
    for role in message.role_mentions:
        content = content.replace(f"<@&{role.id}>", f"@{role.name}")

    if message.embeds:
        for embed in message.embeds:
            parts = []
            if embed.author.name:
                parts.append(embed.author.name)
            if embed.title:
                parts.append(embed.title)
            if embed.description:
                parts.append(embed.description)
            if parts:
                content += " [embed: " + " | ".join(parts) + "]"

    for attachment in message.attachments:
        content += f" [file: {attachment.filename}]"

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

def web_search(query: str) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
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
                return text[:10000]
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
        loop = asyncio.get_event_loop()
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
    lines = load_messages()
    new_line = format_message(message)
    lines.append(new_line)

    if len(lines) > MAX_MESSAGES:
        to_summarize = lines[:TRUNCATION]
        lines = lines[TRUNCATION:]
        print(f"\n\nContext limit hit. Summarizing and truncating...")
        try:
            summary_response = await anthropic_client.messages.create(
                model=SUMMARY_MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": "Summarize these chat messages briefly for a chatbot without formatting, preserving key topics, names, and context. The amount of messages is low because we're testing:\n" + "\n".join(to_summarize)}]
            )
            summary = summary_response.content[0].text
        except Exception:
            summary = "Earlier conversation context unavailable."
        lines.insert(0, f"[Summary of earlier messages: {summary}]")

    save_messages(lines)

    if not should_respond(message):
        return

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

        conversation_history = "\n".join(lines[:-1]) + "\nRESPOND TO: " + new_line

        content = [{"type": "text", "text": conversation_history}]

        TEXT_EXTENSIONS = {
            '.txt', '.md', '.py', '.js', '.ts', '.cpp', '.c', '.h', '.hpp',
            '.java', '.cs', '.go', '.rs', '.rb', '.php', '.html', '.css',
            '.json', '.xml', '.yaml', '.yml', '.toml', '.ini', '.cfg',
            '.sh', '.bat', '.ps1', '.sql', '.r', '.swift', '.kt'
        }

        if message.attachments:
            for attachment in message.attachments:
                ct = attachment.content_type or ""
                ext = os.path.splitext(attachment.filename)[1].lower()

                async with aiohttp.ClientSession() as session:
                    async with session.get(attachment.url) as resp:
                        file_data = await resp.read()

                if ct.startswith("image/"):
                    tool_notes.append(f"-# Analyzed {attachment.filename}")
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": ct,
                            "data": base64.b64encode(file_data).decode("utf-8")
                        }
                    })
                elif ct == "application/pdf" or ext == ".pdf":
                    tool_notes.append(f"-# Read {attachment.filename}")
                    content.append({
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(file_data).decode("utf-8")
                        }
                    })
                elif ext in TEXT_EXTENSIONS or ct.startswith("text/"):
                    try:
                        text_content = file_data.decode("utf-8")
                        tool_notes.append(f"-# Read {attachment.filename}")
                        content.append({
                            "type": "text",
                            "text": f"[File: {attachment.filename}]\n{text_content}"
                        })
                    except UnicodeDecodeError:
                        tool_notes.append(f"-# Failed to read {attachment.filename} (encoding error)")
                else:
                    tool_notes.append(f"-# Skipped {attachment.filename} (unsupported type)")

        messages = [{"role": "user", "content": content}]
        image_to_send = None
        try:
            while True:
                response = await anthropic_client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
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
                        "description": "Save something worth remembering to long-term memory. Use conservatively — only for clear facts, preferences, or important context. Do NOT use for casual conversation. Do NOT use special characters, brackets or punctuation.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "memory": {"type": "string", "description": "The fact to remember"}
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
                        "description": "Delete a specific memory entry by ID. Use recall_memory first to find the ID, then delete it. Chain with remember to correct outdated information.",
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "memory_id": {"type": "integer", "description": "The ID of the memory to delete"}
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
                    }],
                    messages=messages
                )

                if response.stop_reason == "tool_use":
                    tool_uses = [b for b in response.content if b.type == "tool_use"]
                    tool_results = []
                    
                    for tool_use in tool_uses:
                        if tool_use.name == "web_search":
                            query = tool_use.input["query"]
                            result = web_search(query)
                            tool_notes.append(f"-# Searched the web for {query}")
                        elif tool_use.name == "web_fetch":
                            url = tool_use.input["url"]
                            result = await web_fetch(url)
                            tool_notes.append(f"-# Fetched <{url}>")
                        elif tool_use.name == "wolfram_query":
                            query = tool_use.input["query"]
                            result = await wolfram_query(query)
                            tool_notes.append(f"-# Queried Wolfram Alpha for {query}")
                        elif tool_use.name == "remember":
                            memory = tool_use.input["memory"]
                            save_memory(memory)
                            result = f"Remembered: {memory}"
                            tool_notes.append(f"-# Remembered: {memory}")
                        elif tool_use.name == "recall_memory":
                            query = tool_use.input["query"]
                            results = recall_memories(query)
                            result = "\n".join(results) if results else "Nothing found."
                            tool_notes.append(f"-# Recalled memories for {query}")
                        elif tool_use.name == "delete_memory":
                            memory_id = tool_use.input["memory_id"]
                            delete_memory(memory_id)
                            result = f"Deleted memory {memory_id}"
                            tool_notes.append(f"-# Deleted memory entry {memory_id}")
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
                                tool_notes.append(f"-# Found image for {query}")
                            else:
                                result = error
                                tool_notes.append(f"-# Image search failed for {query}: {error}")
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
                    response_text = next((b.text for b in response.content if hasattr(b, "text")), "")
                    break
        except Exception as e:
            await message.reply("*something went wrong*")
            print(f"ERROR: {e}")
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
            embed = discord.Embed()
            embed.set_image(url=image_to_send)
            await message.channel.send(embed=embed)
        print(f"\nDEBUG: {new_line}\nTOKENS IN: {response.usage.input_tokens} OUT: {response.usage.output_tokens}\nRESPONSE: {response_text}")
client.run(discord_token)



# managed to keep all this under 500 lines yay