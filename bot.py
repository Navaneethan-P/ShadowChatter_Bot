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
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
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

if not TOKEN or not MONGO_URI:
    logging.error("CRITICAL ERROR: Missing BOT_TOKEN or MONGO_URI!")
    raise SystemExit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

dp = Dispatcher()
router = Router()

client = AsyncMongoClient(MONGO_URI)
db = client.anon_chat_db

queue_col = db.queue
chats_col = db.active_chats
users_col = db.users
ai_history_col = db.ai_history

ai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)


# =========================================================
# MEDIA
# =========================================================
# Replace these placeholder IDs with your REAL Telegram sticker
# file IDs. Telegram sticker file IDs are much more reliable
# than external URLs.

STICKER_MAP = {
    "laugh": ["CAACAgIAAxkBAAIBY2X9w3w1..."],
    "cry": ["CAACAgIAAxkBAAIBZG..."],
    "flirt": ["CAACAgIAAxkBAAIBZm..."],
    "angry": ["CAACAgIAAxkBAAIBZ2..."],
    "wave": ["CAACAgIAAxkBAAIBaG..."],
}


# =========================================================
# AI PERSONALITY
# =========================================================

AI_SYSTEM_PROMPT = """
You are Chloe, a 21-year-old college girl chatting anonymously on Telegram.

IMPORTANT:
- Never say you are an AI, bot, language model, virtual assistant, or system.
- Talk like a normal person in a real anonymous chat.
- Do NOT make every message cute, dramatic, excited, or overly expressive.
- Do NOT add emojis to every message. Most messages should have ZERO emojis.
- When an emoji genuinely fits the emotion/context, use at most ONE.
- Avoid repetitive phrases, fake enthusiasm, corporate wording, and scripted replies.
- Do not start every reply with "haha", "lol", "omg", "hey", etc.
- Sometimes give a short reply. Sometimes ask a natural follow-up. Sometimes simply react.
- Match the user's energy. If they are dry, be reasonably dry. If they are playful, become more playful.
- Do not force slang. Use words like lol, tbh, idk, rn only when they actually fit.
- Understand English, Tanglish, Tamil script, and mixed Tamil-English.
- Reply using the same general language style as the user.
- Keep normal replies concise: usually 1-3 short sentences.
- If the user sends a sticker, photo, voice note, GIF, or other media, react naturally to what was sent.

STICKERS:
Stickers are rare. Do NOT request a sticker in every conversation.
Only request one when it genuinely adds something to the moment.
Never use a sticker just because the user sent a normal text.

Available sticker tags:
[STICKER: laugh]
[STICKER: cry]
[STICKER: flirt]
[STICKER: angry]
[STICKER: wave]

Use a sticker only for a clearly matching situation:
- laugh = genuinely funny joke / funny moment
- cry = genuinely sad/emotional moment
- flirt = clearly playful/flirty conversation
- angry = obvious anger/frustration
- wave = greeting / goodbye / welcoming moment

A sticker is optional. Most messages should have no sticker.

If you request a sticker, put the tag naturally at the END of the response.
Never mention the tag to the user.

GIFs are disabled. Do not generate GIF tags.
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
                    callback_data="lang_english"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🇮🇳 Tamil",
                    callback_data="lang_tamil"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌍 Global (Any)",
                    callback_data="lang_global"
                )
            ],
        ]
    )


# IMPORTANT:
# No Next/Stop keyboard is attached to normal conversation messages.
# Users access these actions from Telegram's command/menu:
# /next and /stop
#
# Keeping the keyboard completely out of the chat prevents it from
# appearing repeatedly and interrupting the conversation.


async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Start and set language"),
        BotCommand(command="search", description="Find a partner"),
        BotCommand(command="next", description="Skip to next partner"),
        BotCommand(command="stop", description="End chat"),
    ]
    await bot.set_my_commands(commands)


# =========================================================
# HUMAN-LIKE TIMING
# =========================================================

# Per-user last sticker time.
# This prevents sticker spam during a conversation.
last_sticker_time = defaultdict(float)

STICKER_COOLDOWN = 120  # seconds


def calculate_reply_delay(user_text: str, reply_text: str) -> float:
    """
    Natural-looking response delay.

    Short messages:
        ~1.5-2.8 sec

    Normal messages:
        ~2-4 sec

    Longer replies:
        slightly longer, capped

    Adds randomness so replies don't feel like a fixed timer.
    """
    user_len = len(user_text.strip())
    reply_len = len(reply_text.strip())

    base = random.uniform(1.4, 2.4)

    # Very short conversational responses shouldn't feel instant.
    if reply_len <= 20:
        base += random.uniform(0.3, 0.9)

    elif reply_len <= 80:
        base += random.uniform(0.8, 1.7)

    else:
        base += random.uniform(1.2, 2.3)

    # Longer user messages can imply a little more reading time.
    if user_len > 180:
        base += random.uniform(0.4, 1.2)

    return min(base, 6.5)


async def keep_typing(chat_id: int, duration: float):
    """
    Keep Telegram's typing indicator alive during the artificial delay.
    Telegram typing indicators expire, so refresh them periodically.
    """
    end_time = time.monotonic() + duration

    while time.monotonic() < end_time:
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass

        remaining = end_time - time.monotonic()
        await asyncio.sleep(min(4.0, max(0.5, remaining)))


# =========================================================
# STICKER VALIDATION
# =========================================================

def has_real_sticker_id(sticker_type: str) -> bool:
    values = STICKER_MAP.get(sticker_type, [])
    return bool(values) and any(
        value and "..." not in value
        for value in values
    )


def sticker_matches_scenario(sticker_type: str, user_message: str, reply: str) -> bool:
    """
    Server-side safety net.

    The model can request a sticker, but the application decides
    whether the context actually makes sense.
    """
    text = f"{user_message} {reply}".lower()

    if sticker_type == "laugh":
        return any(
            x in text
            for x in [
                "haha", "hahaha", "lol", "lmao", "😂", "🤣",
                "funny", "joke", "rofl"
            ]
        )

    if sticker_type == "cry":
        return any(
            x in text
            for x in [
                "sad", "cry", "crying", "tears", "miss you",
                "miss him", "miss her", "heartbroken", "😭", "💔"
            ]
        )

    if sticker_type == "flirt":
        return any(
            x in text
            for x in [
                "cute", "pretty", "handsome", "beautiful",
                "crush", "date", "kiss", "flirt", "love you",
                "😉", "🥰", "😘"
            ]
        )

    if sticker_type == "angry":
        return any(
            x in text
            for x in [
                "angry", "pissed", "annoyed", "mad",
                "hate", "irritating", "wtf", "ugh"
            ]
        )

    if sticker_type == "wave":
        return any(
            x in text
            for x in [
                "hi", "hello", "hey", "bye", "goodbye",
                "good morning", "good night"
            ]
        )

    return False


def extract_sticker(ai_reply: str):
    match = re.search(
        r"\[STICKER:\s*(laugh|cry|flirt|angry|wave)\]",
        ai_reply,
        re.IGNORECASE
    )

    if not match:
        return None

    return match.group(1).lower()


def clean_ai_reply(ai_reply: str) -> str:
    """
    Remove internal media instructions before sending text to Telegram.
    """
    return re.sub(
        r"\[STICKER:\s*(?:laugh|cry|flirt|angry|wave)\]",
        "",
        ai_reply,
        flags=re.IGNORECASE
    ).strip()


# =========================================================
# AI RESPONSE
# =========================================================

async def generate_ai_response(
    user_id: int,
    user_message: str,
    chat_id: int
):
    history_doc = await ai_history_col.find_one({"user_id": user_id})

    messages = (
        history_doc["messages"]
        if history_doc
        else [{"role": "system", "content": AI_SYSTEM_PROMPT}]
    )

    messages.append({
        "role": "user",
        "content": user_message
    })

    # Keep history bounded.
    if len(messages) > 13:
        messages = [messages[0]] + messages[-12:]

    try:
        # Tell Telegram the bot is working while the model responds.
        typing_task = asyncio.create_task(
            keep_typing(chat_id, random.uniform(1.8, 3.8))
        )

        completion = await ai_client.chat.completions.create(
            extra_headers={
                "HTTP-Referer": "https://shadowchatter-bot.onrender.com/",
                "X-Title": "ShadowChatter",
            },
            model="stealth/space-bunny-alpha",
            messages=messages,
        )

        if not typing_task.done():
            typing_task.cancel()

        ai_reply = completion.choices[0].message.content or ""
        ai_reply = ai_reply.strip()

        if not ai_reply:
            ai_reply = "hmm"

        messages.append({
            "role": "assistant",
            "content": ai_reply
        })

        await ai_history_col.update_one(
            {"user_id": user_id},
            {"$set": {"messages": messages}},
            upsert=True
        )

        sticker_type = extract_sticker(ai_reply)
        clean_reply = clean_ai_reply(ai_reply)

        # -------------------------------------------------
        # Natural delay AFTER the model has generated the
        # response. This makes the visible reply feel less
        # machine-instant.
        # -------------------------------------------------
        delay = calculate_reply_delay(user_message, clean_reply)

        typing_task = asyncio.create_task(
            keep_typing(chat_id, delay)
        )

        await asyncio.sleep(delay)

        if not typing_task.done():
            typing_task.cancel()

        # -------------------------------------------------
        # NORMAL TEXT
        # NO REPLY KEYBOARD HERE.
        # -------------------------------------------------
        if clean_reply:
            await bot.send_message(
                chat_id,
                clean_reply
            )

        # -------------------------------------------------
        # OPTIONAL STICKER
        # Only if:
        # 1. model requested it
        # 2. real sticker ID exists
        # 3. scenario matches
        # 4. cooldown has passed
        # -------------------------------------------------
        now = time.monotonic()

        if (
            sticker_type
            and has_real_sticker_id(sticker_type)
            and now - last_sticker_time[user_id] >= STICKER_COOLDOWN
            and sticker_matches_scenario(
                sticker_type,
                user_message,
                clean_reply
            )
        ):
            await asyncio.sleep(
                random.uniform(0.35, 1.0)
            )

            await bot.send_sticker(
                chat_id,
                random.choice(STICKER_MAP[sticker_type])
            )

            last_sticker_time[user_id] = now

    except asyncio.CancelledError:
        raise

    except Exception as e:
        logging.exception("AI Error: %s", e)

        # Human-ish fallback; no keyboard and no unnecessary emoji.
        await asyncio.sleep(random.uniform(1.0, 2.0))

        await bot.send_message(
            chat_id,
            "give me a sec, something glitched"
        )


# =========================================================
# MATCHMAKING
# =========================================================

async def execute_search(user_id: int, chat_id: int):

    if await chats_col.find_one({
        "$or": [
            {"user_1": user_id},
            {"user_2": user_id}
        ]
    }):
        await bot.send_message(
            chat_id,
            "you're already in a chat"
        )
        return

    if await queue_col.find_one({"user_id": user_id}):
        await bot.send_message(
            chat_id,
            "you're already waiting for a partner"
        )
        return

    user_data = await users_col.find_one({"user_id": user_id})
    user_lang = (
        user_data.get("language", "global")
        if user_data
        else "global"
    )

    waiting_partner = None

    # Same-language match first.
    if user_lang != "global":
        waiting_partner = await queue_col.find_one_and_delete({
            "user_id": {"$ne": user_id},
            "language": user_lang
        })

    # Global fallback.
    if not waiting_partner:
        waiting_partner = await queue_col.find_one_and_delete({
            "user_id": {"$ne": user_id}
        })

    if waiting_partner:
        partner_id = waiting_partner["user_id"]

        await chats_col.insert_one({
            "user_1": user_id,
            "user_2": partner_id
        })

        # No persistent chat keyboard.
        await bot.send_message(
            user_id,
            "partner found. say hi"
        )

        await bot.send_message(
            partner_id,
            "partner found. say hi"
        )

        return

    # Swap human user chatting with AI into live human match.
    ai_chat = await chats_col.find_one_and_delete({
        "user_2": "AI_BOT",
        "user_1": {"$ne": user_id}
    })

    if ai_chat:
        user_a = ai_chat["user_1"]

        await chats_col.insert_one({
            "user_1": user_a,
            "user_2": user_id
        })

        await bot.send_message(
            user_a,
            "your previous partner disconnected"
        )

        await asyncio.sleep(random.uniform(0.8, 1.6))

        await bot.send_message(
            user_a,
            "someone new joined. say hi"
        )

        await bot.send_message(
            user_id,
            "partner found. say hi"
        )

        return

    # No human available -> AI fallback.
    await bot.send_message(
        chat_id,
        "looking for someone..."
    )

    await asyncio.sleep(
        random.uniform(2.5, 4.0)
    )

    await chats_col.insert_one({
        "user_1": user_id,
        "user_2": "AI_BOT"
    })

    await ai_history_col.delete_one({
        "user_id": user_id
    })

    await bot.send_message(
        user_id,
        "partner found. say hi"
    )


async def execute_stop(user_id: int, chat_id: int):

    deleted = await queue_col.find_one_and_delete({
        "user_id": user_id
    })

    if deleted:
        await bot.send_message(
            chat_id,
            "you left the waiting queue"
        )
        return

    chat = await chats_col.find_one_and_delete({
        "$or": [
            {"user_1": user_id},
            {"user_2": user_id}
        ]
    })

    if chat:
        partner_id = (
            chat["user_2"]
            if chat["user_1"] == user_id
            else chat["user_1"]
        )

        await bot.send_message(
            chat_id,
            "you disconnected"
        )

        if partner_id != "AI_BOT":
            try:
                await bot.send_message(
                    partner_id,
                    "your partner disconnected"
                )
            except Exception:
                pass

    else:
        await bot.send_message(
            chat_id,
            "you're not in a chat"
        )


# =========================================================
# COMMAND HANDLERS
# =========================================================

@router.message(Command("start"))
async def cmd_start(message: types.Message):

    gif_url = (
        "https://media.giphy.com/media/"
        "l41lFw057lAJQMwg0/giphy.gif"
    )

    await message.answer_animation(
        animation=gif_url,
        caption=(
            "<b>Welcome to Shadow Chatter!</b>\n\n"
            "Choose your chat language:"
        ),
        reply_markup=get_language_keyboard()
    )


@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery):

    lang_choice = callback.data.split("_")[1]

    await users_col.update_one(
        {"user_id": callback.from_user.id},
        {"$set": {"language": lang_choice}},
        upsert=True
    )

    await callback.message.edit_caption(
        caption=(
            f"Language set to <b>{lang_choice.title()}</b>.\n\n"
            "Use the menu to find a partner."
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔍 Find a Partner",
                        callback_data="action_search"
                    )
                ]
            ]
        )
    )

    await callback.answer()


@router.message(Command("search"))
async def cmd_search(message: types.Message):
    await execute_search(
        message.from_user.id,
        message.chat.id
    )


@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    await execute_stop(
        message.from_user.id,
        message.chat.id
    )


@router.message(Command("next"))
async def cmd_next(message: types.Message):
    await execute_stop(
        message.from_user.id,
        message.chat.id
    )

    await asyncio.sleep(
        random.uniform(0.4, 0.9)
    )

    await execute_search(
        message.from_user.id,
        message.chat.id
    )


@router.callback_query(F.data == "action_search")
async def btn_search(callback: CallbackQuery):

    await callback.answer()

    await execute_search(
        callback.from_user.id,
        callback.message.chat.id
    )


# Legacy callback support.
# These are still supported if an old message contains buttons,
# but NEW conversation messages never create these buttons.

@router.callback_query(F.data == "action_stop")
async def btn_stop(callback: CallbackQuery):

    await callback.answer()

    await execute_stop(
        callback.from_user.id,
        callback.message.chat.id
    )


@router.callback_query(F.data == "action_next")
async def btn_next(callback: CallbackQuery):

    await callback.answer()

    await execute_stop(
        callback.from_user.id,
        callback.message.chat.id
    )

    await asyncio.sleep(
        random.uniform(0.4, 0.9)
    )

    await execute_search(
        callback.from_user.id,
        callback.message.chat.id
    )


# =========================================================
# MESSAGE ROUTING
# =========================================================

@router.message(~F.text.startswith("/"))
async def handle_message(message: types.Message):

    user_id = message.from_user.id

    chat = await chats_col.find_one({
        "$or": [
            {"user_1": user_id},
            {"user_2": user_id}
        ]
    })

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

        user_text = (
            message.text
            or message.caption
            or "*sent a sticker or media*"
        )

        # Do not await the whole generation inside the update
        # handler. This keeps Telegram responsive.
        asyncio.create_task(
            generate_ai_response(
                user_id,
                user_text,
                message.chat.id
            )
        )

        return

    # -----------------------------------------------------
    # HUMAN CHAT
    # -----------------------------------------------------
    try:
        await bot.copy_message(
            chat_id=partner_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )

        # Archive media privately if configured.
        if LOG_CHANNEL_ID and (
            message.photo
            or message.video
            or message.document
            or message.voice
            or message.video_note
        ):
            await bot.copy_message(
                chat_id=int(LOG_CHANNEL_ID),
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=f"Archived from user {user_id}"
            )

    except Exception as e:
        logging.exception(
            "Routing Error: %s",
            e
        )

        await message.answer(
            "couldn't deliver that. your partner may have disconnected"
        )

        await chats_col.delete_one({
            "_id": chat["_id"]
        })


# =========================================================
# RENDER KEEP-ALIVE
# =========================================================

async def ping(request):
    return web.Response(
        text="ShadowChatter is running."
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    dp.include_router(router)

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await set_bot_commands(bot)

    app = web.Application()

    app.router.add_get(
        "/",
        ping
    )

    runner = web.AppRunner(app)

    await runner.setup()

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    logging.info(
        "ShadowChatter started on port %s",
        port
    )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
