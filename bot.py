import os
import json
import re
import sqlite3
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
import gspread
from anthropic import Anthropic
from dotenv import load_dotenv


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

GOOGLE_CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH", "/run/secrets/google-service-account.json")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SHEET_NAME = os.getenv("SHEET_NAME")

FIANCEE_DISCORD_ID = int(os.getenv("FIANCEE_DISCORD_ID"))
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID"))

DB_PATH = os.getenv("DB_PATH", "/data/summer-bummer.sqlite3")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

CHECK_EMOJI = "✅"


anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

gc = gspread.service_account(filename=GOOGLE_CREDS_PATH)

if GOOGLE_SHEET_ID:
    spreadsheet = gc.open_by_key(GOOGLE_SHEET_ID)
else:
    spreadsheet = gc.open(SHEET_NAME)


intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.guilds = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_messages (
            message_id TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL,
            guild_id TEXT NOT NULL,
            author_name TEXT,
            content TEXT,
            jump_url TEXT,
            created_at TEXT,
            processed INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def save_pending_message(message: discord.Message):
    conn = db()
    conn.execute("""
        INSERT OR IGNORE INTO pending_messages (
            message_id,
            channel_id,
            guild_id,
            author_name,
            content,
            jump_url,
            created_at,
            processed
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        str(message.id),
        str(message.channel.id),
        str(message.guild.id),
        message.author.display_name,
        message.content or "",
        message.jump_url,
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()


def get_pending_messages():
    conn = db()
    rows = conn.execute("""
        SELECT message_id, channel_id, guild_id, author_name, content, jump_url
        FROM pending_messages
        WHERE processed = 0
        ORDER BY created_at ASC
    """).fetchall()
    conn.close()

    return [
        {
            "message_id": row[0],
            "channel_id": row[1],
            "guild_id": row[2],
            "author_name": row[3],
            "content": row[4],
            "jump_url": row[5],
        }
        for row in rows
    ]


def mark_processed(message_id: str):
    conn = db()
    conn.execute("""
        UPDATE pending_messages
        SET processed = 1
        WHERE message_id = ?
    """, (message_id,))
    conn.commit()
    conn.close()


def get_all_challenge_tasks():
    """
    Reads all person tabs from the Google Sheet.

    Expected layout:
    Column A = Task Code
    Column B = Task Description
    Column D = Completed checkbox
    """

    ignored_tabs = {
        "leaderboard",
        "points",
        "summary",
        "instructions",
        "rules",
        "data",
        "sheet1",
    }

    all_tasks = []

    for ws in spreadsheet.worksheets():
        sheet_name = ws.title.strip()

        if sheet_name.lower() in ignored_tabs:
            continue

        values = ws.get_all_values()

        for row_index, row in enumerate(values, start=1):
            task_code = row[0].strip() if len(row) > 0 else ""
            task_name = row[1].strip() if len(row) > 1 else ""
            completed = row[3].strip() if len(row) > 3 else ""

            if not task_code or not task_name:
                continue

            # Skip obvious headers
            if task_code.lower() in {"task", "code", "task code"}:
                continue

            all_tasks.append({
                "person_sheet": sheet_name,
                "row_number": row_index,
                "task_code": task_code,
                "task_name": task_name,
                "completed": completed,
            })

    return all_tasks


def extract_json(text: str):
    """
    Claude should return JSON only, but this protects against accidental markdown.
    """

    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```json", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"^```", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in Claude response: {text}")

    return json.loads(match.group(0))


def ask_claude_to_match_task(message, all_tasks):
    prompt = f"""
You are helping update a Google Sheet for a summer challenge.

A Discord message was approved by Amanda with a ✅ reaction.

Discord message author:
{message["author_name"]}

Discord message text:
{message["content"]}

Discord message link:
{message["jump_url"]}

Here are all available challenge tasks from the Google Sheet:
{json.dumps(all_tasks, indent=2)}

Return ONLY valid JSON in this exact format:

{{
  "person_sheet": "Exact Google Sheet tab name",
  "task_code": "Exact task code from Column A",
  "task_name": "Exact task name from Column B",
  "confidence": 0.0,
  "reason": "Short explanation"
}}

Rules:
- Match the Discord message to the most likely task.
- Use the exact person_sheet from the available tasks.
- Use the exact task_code from the available tasks.
- Use the exact task_name from the available tasks.
- If the message says who completed it, use that person's sheet.
- If the message author is the person who completed it, infer from the author.
- If unsure, return confidence below 0.75.
- Do not invent tasks.
"""

    response = anthropic.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=700,
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )

    text = response.content[0].text
    return extract_json(text)


def update_google_sheet(match):
    person_sheet = match["person_sheet"]
    task_code = match["task_code"]

    ws = spreadsheet.worksheet(person_sheet)
    values = ws.get_all_values()

    for row_index, row in enumerate(values, start=1):
        code = row[0].strip() if len(row) > 0 else ""

        if code.lower() == task_code.lower():
            # Column D = completed checkbox
            ws.update_cell(row_index, 4, True)
            return row_index

    raise ValueError(f"Could not find task code {task_code} on sheet {person_sheet}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    guild = discord.Object(id=DISCORD_GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    print("Slash commands synced")


@bot.event
async def on_raw_reaction_add(payload):
    if str(payload.emoji) != CHECK_EMOJI:
        return

    if payload.user_id != FIANCEE_DISCORD_ID:
        return

    if payload.guild_id != DISCORD_GUILD_ID:
        return

    channel = bot.get_channel(payload.channel_id)

    if channel is None:
        channel = await bot.fetch_channel(payload.channel_id)

    message = await channel.fetch_message(payload.message_id)

    if message.author.bot:
        return

    save_pending_message(message)

    try:
        await message.add_reaction("📌")
    except Exception:
        pass

    print(f"Saved pending message: {message.jump_url}")


@bot.tree.command(
    name="process_marked",
    description="Process all pending ✅ Summer Bummer messages"
)
async def process_marked(interaction: discord.Interaction):
    if interaction.user.id != FIANCEE_DISCORD_ID:
        await interaction.response.send_message(
            "Only Amanda can run this command.",
            ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    pending = get_pending_messages()

    if not pending:
        await interaction.followup.send("No pending ✅ messages to process.")
        return

    all_tasks = get_all_challenge_tasks()

    processed = []
    low_confidence = []
    failed = []

    for message in pending:
        try:
            match = ask_claude_to_match_task(message, all_tasks)

            confidence = float(match.get("confidence", 0))

            if confidence < 0.75:
                low_confidence.append({
                    "message": message,
                    "match": match,
                })
                continue

            updated_row = update_google_sheet(match)
            mark_processed(message["message_id"])

            processed.append({
                "message": message,
                "match": match,
                "row": updated_row,
            })

        except Exception as e:
            failed.append({
                "message": message,
                "error": str(e),
            })

    response_lines = []

    response_lines.append(f"Processed {len(processed)} message(s).")

    if processed:
        response_lines.append("")
        response_lines.append("Updated:")
        for item in processed:
            match = item["match"]
            response_lines.append(
                f"- {match['person_sheet']}: {match['task_code']} — {match['task_name']}"
            )

    if low_confidence:
        response_lines.append("")
        response_lines.append("Needs manual review:")
        for item in low_confidence:
            match = item["match"]
            msg = item["message"]
            response_lines.append(
                f"- Low confidence `{match.get('confidence')}` for: {msg['jump_url']}"
            )

    if failed:
        response_lines.append("")
        response_lines.append("Failed:")
        for item in failed:
            response_lines.append(
                f"- {item['message']['jump_url']} — {item['error']}"
            )

    await interaction.followup.send("\n".join(response_lines))


bot.run(DISCORD_TOKEN)