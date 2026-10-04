import asyncio
import logging
import os
import random
import re
import time
from collections import defaultdict

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from pymongo import AsyncMongoClient
from openai import AsyncOpenAI


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

PORT = int(os.environ.get("PORT", "10000"))

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
AI_MODEL = "stealth/space-bunny-alpha"

HEALTH_RESPONSE = "OK"

if not TOKEN:
    raise SystemExit("CRITICAL ERROR: BOT_TOKEN is missing")

if not MONGO_URI:
    raise SystemExit("CRITICAL ERROR: MONGO_URI is missing")

if not OPENROUTER_API_KEY:
    raise SystemExit("CRITICAL ERROR: OPENROUTER_API_KEY is missing")


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("shadowchatter")


# =========================================================
# TELEGRAM
# =========================================================

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    ),
)

dp = Dispatcher()
router = Router()


# =========================================================
# DATABASE
# =========================================================

client = AsyncMongoClient(MONGO_URI)

db = client.anon_chat_db

queue_col = db.queue
chats_col = db.active_chats
users_col = db.users
ai_history_col = db.ai_history


# =========================================================
# AI CLIENT
# =========================================================

ai_client = AsyncOpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY,
)


# =========================================================
# MEDIA
# =========================================================

# Replace these placeholder values with REAL Telegram
# sticker file IDs.
#
# Placeholder IDs are automatically ignored.

STICKER_MAP = {
    "laugh": [
        "CAACAgIAAxkBAAIBY2X9w3w1..."
    ],
    "cry": [
        "CAACAgIAAxkBAAIBZG..."
    ],
    "flirt": [
        "CAACAgIAAxkBAAIBZm..."
    ],
    "angry": [
        "CAACAgIAAxkBAAIBZ2..."
    ],
    "wave": [
        "CAACAgIAAxkBAAIBaG..."
    ],
}


# =========================================================
# AI PERSONALITY
# =========================================================

AI_SYSTEM_PROMPT = """
You are ShadowChatter's AI conversation partner.

You are an AI companion inside an anonymous Telegram chat.

IMPORTANT:
- Do not claim to be a real human.
- If directly asked whether you are an AI, answer honestly.
- Do not pretend to have real-world experiences you do not have.
- Otherwise, keep the conversation natural and relaxed.

STYLE:
- Talk naturally, like a casual anonymous chat.
- Do NOT make every message cute, dramatic, excited, or overly expressive.
- Do NOT add emojis to every message.
- Most messages should have zero emojis.
- When an emoji genuinely fits, use at most one.
- Avoid repetitive phrases and scripted replies.
- Do not start every reply with "haha", "lol", "omg", "hey", etc.
- Sometimes give a short reply.
- Sometimes ask a natural follow-up.
- Sometimes simply react.
- Match the user's energy.
- If the user is dry, be reasonably dry.
- If the user is playful, become more playful.
- Do not force slang.
- Use words like lol, tbh, idk, rn only when they actually fit.
- Understand English, Tanglish, Tamil script, and mixed Tamil-English.
- Reply using the same general language style as the user.
- Keep normal replies concise: usually 1-3 short sentences.

MEDIA:
- If the user sends a sticker, photo, voice note, GIF, or other media,
  react naturally when possible.
- Do not claim to have seen/heard content that is unavailable to you.
- Stickers are rare.
- Do not request stickers in every conversation.

STICKERS:

Available tags:

[STICKER: laugh]
[STICKER: cry]
[STICKER: flirt]
[STICKER: angry]
[STICKER: wave]

Only use a sticker tag when genuinely appropriate.

laugh:
- genuinely funny moment

cry:
- genuinely sad/emotional moment

flirt:
- clearly playful/flirty conversation

angry:
- obvious anger/frustration

wave:
- greeting/goodbye/welcoming moment

A sticker is optional.

If you use a sticker tag, put it at the END of your response.

Never explain the tag to the user.

GIFs are disabled.
Do not generate GIF tags.
"""


# =========================================================
# UI
# =========================================================

def get_language_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🇬🇧 English",
                    callback_data="lang_english",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🇮🇳 Tamil",
                    callback_data="lang_tamil",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌍 Global (Any)",
                    callback_data="lang_global",
                )
            ],
        ]
    )


async def set_bot_commands(bot_instance: Bot):
    commands = [
        BotCommand(
            command="start",
            description="Start and set language",
        ),
        BotCommand(
            command="search",
            description="Find a partner",
        ),
        BotCommand(
            command="next",
            description="Skip to next partner",
        ),
        BotCommand(
            command="stop",
            description="End chat",
        ),
    ]

    await bot_instance.set_my_commands(commands)


# =========================================================
# TIMING
# =========================================================

last_sticker_time = defaultdict(float)

STICKER_COOLDOWN = 120


def calculate_reply_delay(
    user_text: str,
    reply_text: str,
) -> float:

    user_len = len(user_text.strip())
    reply_len = len(reply_text.strip())

    base = random.uniform(1.4, 2.4)

    if reply_len <= 20:
        base += random.uniform(0.3, 0.9)

    elif reply_len <= 80:
        base += random.uniform(0.8, 1.7)

    else:
        base += random.uniform(1.2, 2.3)

    if user_len > 180:
        base += random.uniform(0.4, 1.2)

    return min(base, 6.5)


async def keep_typing(
    chat_id: int,
    duration: float,
):
    end_time = time.monotonic() + duration

    while time.monotonic() < end_time:

        try:
            await bot.send_chat_action(
                chat_id=chat_id,
                action="typing",
            )

        except Exception:
            pass

        remaining = end_time - time.monotonic()

        await asyncio.sleep(
            min(
                4.0,
                max(
                    0.5,
                    remaining,
                ),
            )
        )


# =========================================================
# STICKERS
# =========================================================

def has_real_sticker_id(
    sticker_type: str,
) -> bool:

    values = STICKER_MAP.get(
        sticker_type,
        [],
    )

    return bool(values) and any(
        value
        and "..." not in value
        for value in values
    )


def sticker_matches_scenario(
    sticker_type: str,
    user_message: str,
    reply: str,
) -> bool:

    text = (
        f"{user_message} {reply}"
    ).lower()

    if sticker_type == "laugh":
        return any(
            x in text
            for x in [
                "haha",
                "hahaha",
                "lol",
                "lmao",
                "😂",
                "🤣",
                "funny",
                "joke",
                "rofl",
            ]
        )

    if sticker_type == "cry":
        return any(
            x in text
            for x in [
                "sad",
                "cry",
                "crying",
                "tears",
                "miss you",
                "miss him",
                "miss her",
                "heartbroken",
                "😭",
                "💔",
            ]
        )

    if sticker_type == "flirt":
        return any(
            x in text
            for x in [
                "cute",
                "pretty",
                "handsome",
                "beautiful",
                "crush",
                "date",
                "kiss",
                "flirt",
                "love you",
                "😉",
                "🥰",
                "😘",
            ]
        )

    if sticker_type == "angry":
        return any(
            x in text
            for x in [
                "angry",
                "pissed",
                "annoyed",
                "mad",
                "hate",
                "irritating",
                "wtf",
                "ugh",
            ]
        )

    if sticker_type == "wave":
        return any(
            x in text
            for x in [
                "hi",
                "hello",
                "hey",
                "bye",
                "goodbye",
                "good morning",
                "good night",
            ]
        )

    return False


def extract_sticker(ai_reply: str):
    match = re.search(
        r"\[STICKER:\s*(laugh|cry|flirt|angry|wave)\]",
        ai_reply,
        re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1).lower()


def clean_ai_reply(ai_reply: str) -> str:
    return re.sub(
        r"\[STICKER:\s*(?:laugh|cry|flirt|angry|wave)\]",
        "",
        ai_reply,
        flags=re.IGNORECASE,
    ).strip()


# =========================================================
# AI TASK MANAGEMENT
# =========================================================

ai_tasks = {}


def cancel_ai_task(user_id: int):
    task = ai_tasks.get(user_id)

    if task and not task.done():
        task.cancel()


# =========================================================
# AI RESPONSE
# =========================================================

async def generate_ai_response(
    user_id: int,
    user_message: str,
    chat_id: int,
):

    try:

        history_doc = await ai_history_col.find_one(
            {"user_id": user_id}
        )

        messages = (
            history_doc["messages"]
            if history_doc
            else [
                {
                    "role": "system",
                    "content": AI_SYSTEM_PROMPT,
                }
            ]
        )

        messages.append(
            {
                "role": "user",
                "content": user_message,
            }
        )

        # Keep history bounded.
        if len(messages) > 13:
            messages = [
                messages[0]
            ] + messages[-12:]

        # -------------------------------------------------
        # MODEL REQUEST
        # -------------------------------------------------

        typing_duration = random.uniform(
            1.8,
            3.8,
        )

        typing_task = asyncio.create_task(
            keep_typing(
                chat_id,
                typing_duration,
            )
        )

        try:

            completion = (
                await ai_client.chat.completions.create(
                    extra_headers={
                        "HTTP-Referer": (
                            "https://shadowchatter-bot.onrender.com/"
                        ),
                        "X-Title": "ShadowChatter",
                    },
                    model=AI_MODEL,
                    messages=messages,
                )
            )

        finally:

            if not typing_task.done():
                typing_task.cancel()

            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        ai_reply = (
            completion.choices[0].message.content
            or ""
        ).strip()

        if not ai_reply:
            ai_reply = "hmm"

        messages.append(
            {
                "role": "assistant",
                "content": ai_reply,
            }
        )

        await ai_history_col.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "messages": messages,
                    "updated_at": time.time(),
                }
            },
            upsert=True,
        )

        sticker_type = extract_sticker(
            ai_reply
        )

        clean_reply = clean_ai_reply(
            ai_reply
        )

        # -------------------------------------------------
        # NATURAL RESPONSE DELAY
        # -------------------------------------------------

        delay = calculate_reply_delay(
            user_message,
            clean_reply,
        )

        typing_task = asyncio.create_task(
            keep_typing(
                chat_id,
                delay,
            )
        )

        try:
            await asyncio.sleep(delay)

        finally:

            if not typing_task.done():
                typing_task.cancel()

            try:
                await typing_task
            except asyncio.CancelledError:
                pass

        # -------------------------------------------------
        # VERIFY CHAT STILL EXISTS
        # -------------------------------------------------

        current_chat = await chats_col.find_one(
            {
                "$or": [
                    {"user_1": user_id},
                    {"user_2": user_id},
                ]
            }
        )

        if not current_chat:
            return

        partner_id = (
            current_chat["user_2"]
            if current_chat["user_1"] == user_id
            else current_chat["user_1"]
        )

        if partner_id != "AI_BOT":
            return

        # -------------------------------------------------
        # SEND TEXT
        # -------------------------------------------------

        if clean_reply:

            await bot.send_message(
                chat_id,
                clean_reply,
            )

        # -------------------------------------------------
        # OPTIONAL STICKER
        # -------------------------------------------------

        now = time.monotonic()

        if (
            sticker_type
            and has_real_sticker_id(
                sticker_type
            )
            and (
                now
                - last_sticker_time[user_id]
                >= STICKER_COOLDOWN
            )
            and sticker_matches_scenario(
                sticker_type,
                user_message,
                clean_reply,
            )
        ):

            await asyncio.sleep(
                random.uniform(
                    0.35,
                    1.0,
                )
            )

            await bot.send_sticker(
                chat_id,
                random.choice(
                    STICKER_MAP[
                        sticker_type
                    ]
                ),
            )

            last_sticker_time[user_id] = (
                time.monotonic()
            )

    except asyncio.CancelledError:
        raise

    except Exception as e:

        logger.exception(
            "AI Error for user %s: %s",
            user_id,
            e,
        )

        try:

            await asyncio.sleep(
                random.uniform(
                    1.0,
                    2.0,
                )
            )

            await bot.send_message(
                chat_id,
                "give me a sec, something glitched",
            )

        except Exception:
            pass

    finally:

        current_task = asyncio.current_task()

        if (
            ai_tasks.get(user_id)
            is current_task
        ):
            ai_tasks.pop(
                user_id,
                None,
            )


# =========================================================
# MATCHMAKING
# =========================================================

async def execute_search(
    user_id: int,
    chat_id: int,
):

    existing_chat = await chats_col.find_one(
        {
            "$or": [
                {"user_1": user_id},
                {"user_2": user_id},
            ]
        }
    )

    if existing_chat:

        await bot.send_message(
            chat_id,
            "you're already in a chat",
        )

        return

    waiting = await queue_col.find_one(
        {"user_id": user_id}
    )

    if waiting:

        await bot.send_message(
            chat_id,
            "you're already waiting for a partner",
        )

        return

    user_data = await users_col.find_one(
        {"user_id": user_id}
    )

    user_lang = (
        user_data.get(
            "language",
            "global",
        )
        if user_data
        else "global"
    )

    waiting_partner = None

    # -----------------------------------------------------
    # SAME LANGUAGE
    # -----------------------------------------------------

    if user_lang != "global":

        waiting_partner = (
            await queue_col.find_one_and_delete(
                {
                    "user_id": {
                        "$ne": user_id
                    },
                    "language": user_lang,
                }
            )
        )

    # -----------------------------------------------------
    # GLOBAL FALLBACK
    # -----------------------------------------------------

    if not waiting_partner:

        waiting_partner = (
            await queue_col.find_one_and_delete(
                {
                    "user_id": {
                        "$ne": user_id
                    }
                }
            )
        )

    # -----------------------------------------------------
    # HUMAN MATCH
    # -----------------------------------------------------

    if waiting_partner:

        partner_id = waiting_partner[
            "user_id"
        ]

        await chats_col.insert_one(
            {
                "user_1": user_id,
                "user_2": partner_id,
                "created_at": time.time(),
            }
        )

        await bot.send_message(
            user_id,
            "partner found. say hi",
        )

        try:

            await bot.send_message(
                partner_id,
                "partner found. say hi",
            )

        except Exception as e:

            logger.warning(
                "Could not notify partner %s: %s",
                partner_id,
                e,
            )

        return

    # -----------------------------------------------------
    # AI CHAT -> HUMAN MATCH
    # -----------------------------------------------------

    ai_chat = (
        await chats_col.find_one_and_delete(
            {
                "user_2": "AI_BOT",
                "user_1": {
                    "$ne": user_id
                },
            }
        )
    )

    if ai_chat:

        user_a = ai_chat["user_1"]

        cancel_ai_task(user_a)

        await chats_col.insert_one(
            {
                "user_1": user_a,
                "user_2": user_id,
                "created_at": time.time(),
            }
        )

        await bot.send_message(
            user_a,
            "your previous partner disconnected",
        )

        await asyncio.sleep(
            random.uniform(
                0.8,
                1.6,
            )
        )

        await bot.send_message(
            user_a,
            "someone new joined. say hi",
        )

        await bot.send_message(
            user_id,
            "partner found. say hi",
        )

        return

    # -----------------------------------------------------
    # AI FALLBACK
    # -----------------------------------------------------

    await bot.send_message(
        chat_id,
        "looking for someone...",
    )

    await asyncio.sleep(
        random.uniform(
            2.5,
            4.0,
        )
    )

    await chats_col.insert_one(
        {
            "user_1": user_id,
            "user_2": "AI_BOT",
            "created_at": time.time(),
        }
    )

    await ai_history_col.delete_one(
        {
            "user_id": user_id
        }
    )

    await bot.send_message(
        user_id,
        "partner found. say hi",
    )


async def execute_stop(
    user_id: int,
    chat_id: int,
):

    # Cancel any running AI response.
    cancel_ai_task(user_id)

    # -----------------------------------------------------
    # REMOVE FROM QUEUE
    # -----------------------------------------------------

    deleted = (
        await queue_col.find_one_and_delete(
            {"user_id": user_id}
        )
    )

    if deleted:

        await bot.send_message(
            chat_id,
            "you left the waiting queue",
        )

        return

    # -----------------------------------------------------
    # REMOVE ACTIVE CHAT
    # -----------------------------------------------------

    chat = (
        await chats_col.find_one_and_delete(
            {
                "$or": [
                    {"user_1": user_id},
                    {"user_2": user_id},
                ]
            }
        )
    )

    if not chat:

        await bot.send_message(
            chat_id,
            "you're not in a chat",
        )

        return

    partner_id = (
        chat["user_2"]
        if chat["user_1"] == user_id
        else chat["user_1"]
    )

    await bot.send_message(
        chat_id,
        "you disconnected",
    )

    if partner_id != "AI_BOT":

        try:

            await bot.send_message(
                partner_id,
                "your partner disconnected",
            )

        except Exception:
            pass


# =========================================================
# /START
# =========================================================

@router.message(Command("start"))
async def cmd_start(
    message: types.Message,
):

    gif_url = (
        "https://media.giphy.com/media/"
        "l41lFw057lAJQMwg0/giphy.gif"
    )

    try:

        await message.answer_animation(
            animation=gif_url,
            caption=(
                "<b>Welcome to Shadow Chatter!</b>\n\n"
                "Choose your chat language:"
            ),
            reply_markup=get_language_keyboard(),
        )

    except Exception as e:

        logger.warning(
            "Start animation failed: %s",
            e,
        )

        await message.answer(
            (
                "<b>Welcome to Shadow Chatter!</b>\n\n"
                "Choose your chat language:"
            ),
            reply_markup=get_language_keyboard(),
        )


# =========================================================
# LANGUAGE
# =========================================================

@router.callback_query(
    F.data.startswith("lang_")
)
async def set_language(
    callback: CallbackQuery,
):

    lang_choice = callback.data.split(
        "_",
        1,
    )[1]

    await users_col.update_one(
        {
            "user_id": callback.from_user.id
        },
        {
            "$set": {
                "language": lang_choice
            }
        },
        upsert=True,
    )

    text = (
        f"Language set to "
        f"<b>{lang_choice.title()}</b>.\n\n"
        "Use the button below to find a partner."
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Find a Partner",
                    callback_data="action_search",
                )
            ]
        ]
    )

    try:

        if callback.message:

            await callback.message.edit_caption(
                caption=text,
                reply_markup=keyboard,
            )

    except Exception as e:

        logger.warning(
            "Could not edit language message: %s",
            e,
        )

        if callback.message:

            await callback.message.answer(
                text,
                reply_markup=keyboard,
            )

    await callback.answer()


# =========================================================
# SEARCH
# =========================================================

@router.message(Command("search"))
async def cmd_search(
    message: types.Message,
):

    await execute_search(
        message.from_user.id,
        message.chat.id,
    )


# =========================================================
# STOP
# =========================================================

@router.message(Command("stop"))
async def cmd_stop(
    message: types.Message,
):

    await execute_stop(
        message.from_user.id,
        message.chat.id,
    )


# =========================================================
# NEXT
# =========================================================

@router.message(Command("next"))
async def cmd_next(
    message: types.Message,
):

    await execute_stop(
        message.from_user.id,
        message.chat.id,
    )

    await asyncio.sleep(
        random.uniform(
            0.4,
            0.9,
        )
    )

    await execute_search(
        message.from_user.id,
        message.chat.id,
    )


# =========================================================
# SEARCH BUTTON
# =========================================================

@router.callback_query(
    F.data == "action_search"
)
async def btn_search(
    callback: CallbackQuery,
):

    await callback.answer()

    await execute_search(
        callback.from_user.id,
        callback.message.chat.id,
    )


# =========================================================
# LEGACY STOP BUTTON
# =========================================================

@router.callback_query(
    F.data == "action_stop"
)
async def btn_stop(
    callback: CallbackQuery,
):

    await callback.answer()

    await execute_stop(
        callback.from_user.id,
        callback.message.chat.id,
    )


# =========================================================
# LEGACY NEXT BUTTON
# =========================================================

@router.callback_query(
    F.data == "action_next"
)
async def btn_next(
    callback: CallbackQuery,
):

    await callback.answer()

    await execute_stop(
        callback.from_user.id,
        callback.message.chat.id,
    )

    await asyncio.sleep(
        random.uniform(
            0.4,
            0.9,
        )
    )

    await execute_search(
        callback.from_user.id,
        callback.message.chat.id,
    )


# =========================================================
# MESSAGE TYPE DESCRIPTION
# =========================================================

def get_message_content(
    message: types.Message,
) -> str:

    if message.text:
        return message.text

    if message.caption:
        return message.caption

    if message.sticker:
        return (
            f"[User sent a sticker: "
            f"{message.sticker.emoji or 'sticker'}]"
        )

    if message.photo:
        return "[User sent a photo]"

    if message.video:
        return "[User sent a video]"

    if message.voice:
        return "[User sent a voice message]"

    if message.video_note:
        return "[User sent a video note]"

    if message.document:
        return "[User sent a document]"

    if message.animation:
        return "[User sent a GIF/animation]"

    return "[User sent media]"


# =========================================================
# GENERAL MESSAGE ROUTING
# =========================================================
#
# IMPORTANT:
# This intentionally does NOT use:
#
#     @router.message(~F.text.startswith("/"))
#
# because that can miss non-text messages.
#
# Commands are already handled above.
# Everything else reaches this handler.
# =========================================================

@router.message()
async def handle_message(
    message: types.Message,
):

    # Commands are handled by dedicated handlers.
    if message.text and message.text.startswith("/"):
        return

    if not message.from_user:
        return

    user_id = message.from_user.id

    # -----------------------------------------------------
    # FIND CHAT
    # -----------------------------------------------------

    chat = await chats_col.find_one(
        {
            "$or": [
                {"user_1": user_id},
                {"user_2": user_id},
            ]
        }
    )

    if not chat:

        await message.answer(
            "you're not connected to anyone. use /search"
        )

        return

    partner_id = (
        chat["user_2"]
        if chat["user_1"] == user_id
        else chat["user_1"]
    )

    # -----------------------------------------------------
    # AI CHAT
    # -----------------------------------------------------

    if partner_id == "AI_BOT":

        user_text = get_message_content(
            message
        )

        # Cancel previous AI response for this user.
        #
        # This prevents:
        #
        # user: hello
        # user: are you there?
        #
        # from producing two overlapping AI responses.
        cancel_ai_task(user_id)

        task = asyncio.create_task(
            generate_ai_response(
                user_id,
                user_text,
                message.chat.id,
            )
        )

        ai_tasks[user_id] = task

        return

    # -----------------------------------------------------
    # HUMAN CHAT
    # -----------------------------------------------------

    try:

        await bot.copy_message(
            chat_id=partner_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )

        # -------------------------------------------------
        # PRIVATE ARCHIVE
        # -------------------------------------------------

        if LOG_CHANNEL_ID and (
            message.photo
            or message.video
            or message.document
            or message.voice
            or message.video_note
            or message.animation
            or message.sticker
        ):

            try:

                await bot.copy_message(
                    chat_id=int(
                        LOG_CHANNEL_ID
                    ),
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                )

            except Exception as archive_error:

                logger.warning(
                    "Archive failed: %s",
                    archive_error,
                )

    except Exception as e:

        logger.exception(
            "Routing Error: %s",
            e,
        )

        await message.answer(
            "couldn't deliver that. your partner may have disconnected"
        )

        try:

            await chats_col.delete_one(
                {
                    "_id": chat["_id"]
                }
            )

        except Exception:
            pass


# =========================================================
# HEALTH / KEEP-ALIVE
# =========================================================

async def ping(
    request: web.Request,
):

    # IMPORTANT:
    # Keep this response extremely small.
    #
    # cron-job.org should receive only:
    #
    # OK
    #
    # HTTP 200.
    return web.Response(
        text=HEALTH_RESPONSE,
        status=200,
        content_type="text/plain",
        headers={
            "Cache-Control": "no-store",
        },
    )


async def health(
    request: web.Request,
):

    return web.Response(
        text=HEALTH_RESPONSE,
        status=200,
        content_type="text/plain",
        headers={
            "Cache-Control": "no-store",
        },
    )


# =========================================================
# START HTTP SERVER
# =========================================================

async def start_http_server():

    app = web.Application(
        client_max_size=10 * 1024 * 1024
    )

    app.router.add_get(
        "/",
        ping,
    )

    app.router.add_get(
        "/health",
        health,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    logger.info(
        "HTTP health server running on port %s",
        PORT,
    )

    return runner


# =========================================================
# SHUTDOWN
# =========================================================

async def shutdown():

    logger.info(
        "Shutting down ShadowChatter..."
    )

    # Cancel AI tasks.
    for task in list(
        ai_tasks.values()
    ):

        if not task.done():
            task.cancel()

    if ai_tasks:

        await asyncio.gather(
            *ai_tasks.values(),
            return_exceptions=True,
        )

    ai_tasks.clear()

    try:
        await bot.session.close()
    except Exception:
        pass

    try:
        await client.close()
    except Exception:
        pass


# =========================================================
# MAIN
# =========================================================

async def main():

    http_runner = None

    try:

        # -------------------------------------------------
        # TELEGRAM COMMAND MENU
        # -------------------------------------------------

        await set_bot_commands(
            bot
        )

        # -------------------------------------------------
        # REGISTER ROUTER
        # -------------------------------------------------

        dp.include_router(
            router
        )

        # -------------------------------------------------
        # REMOVE OLD WEBHOOK
        # -------------------------------------------------

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        # -------------------------------------------------
        # START RENDER HEALTH SERVER
        # -------------------------------------------------

        http_runner = (
            await start_http_server()
        )

        logger.info(
            "ShadowChatter started successfully"
        )

        logger.info(
            "Health endpoint: /health"
        )

        # -------------------------------------------------
        # TELEGRAM POLLING
        # -------------------------------------------------

        await dp.start_polling(
            bot
        )

    except asyncio.CancelledError:

        logger.info(
            "Main task cancelled"
        )

    except Exception as e:

        logger.exception(
            "Fatal application error: %s",
            e,
        )

        raise

    finally:

        if http_runner:

            try:
                await http_runner.cleanup()
            except Exception:
                pass

        await shutdown()


# =========================================================
# ENTRY POINT
# =========================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Stopped by user"
        )
