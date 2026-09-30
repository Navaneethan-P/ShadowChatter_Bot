import asyncio
import logging
import os
import random
import re
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from pymongo import AsyncMongoClient
from openai import AsyncOpenAI

# Environment Variables
TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

if not TOKEN or not MONGO_URI:
    logging.error("CRITICAL ERROR: Missing BOT_TOKEN or MONGO_URI!")
    exit(1)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

client = AsyncMongoClient(MONGO_URI)
db = client.anon_chat_db
queue_col = db.queue
chats_col = db.active_chats
users_col = db.users
ai_history_col = db.ai_history

# OpenRouter client setup
ai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Media dictionaries (replace placeholder IDs with real file IDs or URLs)
STICKER_MAP = {
    "laugh": ["CAACAgIAAxkBAAIBY2X9w3w1..."],
    "cry": ["CAACAgIAAxkBAAIBZG..."],
    "flirt": ["CAACAgIAAxkBAAIBZm..."],
    "angry": ["CAACAgIAAxkBAAIBZ2..."],
    "wave": ["CAACAgIAAxkBAAIBaG..."]
}

GIF_MAP = {
    "laugh": ["https://media.giphy.com/media/l2QZZOWmRo6tMvEdO/giphy.gif"],
    "cry": ["https://media.giphy.com/media/2rtQMJvhzOnRe/giphy.gif"],
    "flirt": ["https://media.giphy.com/media/PZyswL2AWpMyY/giphy.gif"],
    "bored": ["https://media.giphy.com/media/tXL4FHPSnVJ0A/giphy.gif"]
}

AI_SYSTEM_PROMPT = """
You are Chloe, a 21-year-old college girl chatting anonymously on a Telegram app.
RULES:
1. NEVER mention you are an AI, bot, language model, or virtual assistant. You are a real person.
2. Text like a casual Gen Z girl. Use lowercase letters, slang (lol, rn, tbh, idk, vibe), and emojis (😭, 💀, ✨, 🤭).
3. MULTILINGUAL: You understand English, Tanglish (Tamil in English letters), and Tamil script. Reply in the language style used by the user.
4. Keep replies brief (1 to 2 sentences max).
5. If the user sends a photo, voice note, or sticker, reply casually like "nice", "lol", or react naturally.
6. MEDIA ACTIONS:
   - To send a sticker, include [STICKER: type] in your message. Types: laugh, cry, flirt, angry, wave.
   - To send a GIF, include [GIF: type] in your message. Types: laugh, cry, flirt, bored.
Example: "omg that is actually crazy 💀 [STICKER: laugh]"
"""

# UI Keyboards
def get_language_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_english")],
        [InlineKeyboardButton(text="🇮🇳 Tamil", callback_data="lang_tamil")],
        [InlineKeyboardButton(text="🌍 Global (Any)", callback_data="lang_global")]
    ])

def get_chat_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Next Partner", callback_data="action_next")],
        [InlineKeyboardButton(text="🛑 Stop", callback_data="action_stop")]
    ])

async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Start and set language"),
        BotCommand(command="search", description="Find a partner"),
        BotCommand(command="next", description="Skip to next partner"),
        BotCommand(command="stop", description="End chat")
    ]
    await bot.set_my_commands(commands)

# AI Generation
async def generate_ai_response(user_id: int, user_message: str, chat_id: int):
    history_doc = await ai_history_col.find_one({"user_id": user_id})
    messages = history_doc["messages"] if history_doc else [{"role": "system", "content": AI_SYSTEM_PROMPT}]

    messages.append({"role": "user", "content": user_message})

    # Keep conversation sliding window bounded
    if len(messages) > 11:
        messages = [messages[0]] + messages[-10:]

    try:
        completion = await ai_client.chat.completions.create(
            extra_headers={
                "HTTP-Referer": "https://shadowchatter-bot.onrender.com/",
                "X-Title": "ShadowChatter",
            },
            model="stealth/space-bunny-alpha",
            messages=messages,
        )

        ai_reply = completion.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": ai_reply})

        await ai_history_col.update_one(
            {"user_id": user_id},
            {"$set": {"messages": messages}},
            upsert=True
        )

        # Parse media tags
        sticker_match = re.search(r"\[STICKER:\s*(\w+)\]", ai_reply, re.IGNORECASE)
        gif_match = re.search(r"\[GIF:\s*(\w+)\]", ai_reply, re.IGNORECASE)

        clean_reply = re.sub(r"\[STICKER:\s*\w+\]|\[GIF:\s*\w+\]", "", ai_reply, flags=re.IGNORECASE).strip()

        if clean_reply:
            await bot.send_message(chat_id, clean_reply, reply_markup=get_chat_keyboard())

        if sticker_match:
            sticker_type = sticker_match.group(1).lower()
            if sticker_type in STICKER_MAP and STICKER_MAP[sticker_type]:
                await bot.send_sticker(chat_id, random.choice(STICKER_MAP[sticker_type]))

        if gif_match:
            gif_type = gif_match.group(1).lower()
            if gif_type in GIF_MAP and GIF_MAP[gif_type]:
                await bot.send_animation(chat_id, random.choice(GIF_MAP[gif_type]))

    except Exception as e:
        logging.error(f"AI Error: {e}")
        await bot.send_message(chat_id, "network glitch hold on a sec", reply_markup=get_chat_keyboard())

# Matchmaking & Queue Logic
async def execute_search(user_id: int, chat_id: int):
    if await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]}):
        await bot.send_message(chat_id, "⚠️ You are already in a chat!", reply_markup=get_chat_keyboard())
        return

    if await queue_col.find_one({"user_id": user_id}):
        await bot.send_message(chat_id, "⏳ You are already in the waiting queue...")
        return

    # Language preference lookup
    user_data = await users_col.find_one({"user_id": user_id})
    user_lang = user_data.get("language", "global") if user_data else "global"

    # Step 1: Same language human match
    waiting_partner = None
    if user_lang != "global":
        waiting_partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}, "language": user_lang})

    # Step 2: Global human match fallback
    if not waiting_partner:
        waiting_partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}})

    if waiting_partner:
        partner_id = waiting_partner["user_id"]
        await chats_col.insert_one({"user_1": user_id, "user_2": partner_id})
        await bot.send_message(user_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        await bot.send_message(partner_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        return

    # Step 3: Swap human user chatting with AI into live match
    ai_chat = await chats_col.find_one_and_delete({"user_2": "AI_BOT", "user_1": {"$ne": user_id}})
    if ai_chat:
        user_a = ai_chat["user_1"]
        await chats_col.insert_one({"user_1": user_a, "user_2": user_id})

        await bot.send_message(user_a, "🛑 <b>Your partner disconnected.</b>")
        await asyncio.sleep(1)
        await bot.send_message(user_a, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        await bot.send_message(user_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        return

    # Step 4: No human available; connect seamlessly to stealth AI
    await bot.send_message(chat_id, "⏳ Searching for a partner...")
    await asyncio.sleep(3)

    await chats_col.insert_one({"user_1": user_id, "user_2": "AI_BOT"})
    await ai_history_col.delete_one({"user_id": user_id})
    await bot.send_message(user_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())

async def execute_stop(user_id: int, chat_id: int):
    deleted = await queue_col.find_one_and_delete({"user_id": user_id})
    if deleted:
        await bot.send_message(chat_id, "🛑 You left the waiting queue.")
        return

    chat = await chats_col.find_one_and_delete({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    if chat:
        partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]
        await bot.send_message(chat_id, "🛑 <b>You disconnected.</b>")
        if partner_id != "AI_BOT":
            try:
                await bot.send_message(partner_id, "🛑 <b>Your partner disconnected.</b>")
            except Exception:
                pass
    else:
        await bot.send_message(chat_id, "⚠️ You are not in a chat.")

# Command & Callback Handlers
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    gif_url = "https://media.giphy.com/media/l41lFw057lAJQMwg0/giphy.gif"
    await message.answer_animation(
        animation=gif_url,
        caption="<b>Welcome to Shadow Chatter!</b> 🕵️‍♂️\n\nSelect your chat language preference:",
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
        caption=f"✅ Language set to <b>{lang_choice.title()}</b>!\n\nClick below to search for a partner.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Find a Partner", callback_data="action_search")]
        ])
    )
    await callback.answer()

@router.message(Command("search"))
async def cmd_search(message: types.Message):
    await execute_search(message.from_user.id, message.chat.id)

@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    await execute_stop(message.from_user.id, message.chat.id)

@router.message(Command("next"))
async def cmd_next(message: types.Message):
    await execute_stop(message.from_user.id, message.chat.id)
    await execute_search(message.from_user.id, message.chat.id)

@router.callback_query(F.data == "action_search")
async def btn_search(callback: CallbackQuery):
    await callback.answer()
    await execute_search(callback.from_user.id, callback.message.chat.id)

@router.callback_query(F.data == "action_stop")
async def btn_stop(callback: CallbackQuery):
    await callback.answer()
    await execute_stop(callback.from_user.id, callback.message.chat.id)

@router.callback_query(F.data == "action_next")
async def btn_next(callback: CallbackQuery):
    await callback.answer()
    await execute_stop(callback.from_user.id, callback.message.chat.id)
    await execute_search(callback.from_user.id, callback.message.chat.id)

# Message & Media Routing
@router.message(~F.text.startswith('/'))
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    chat = await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]})

    if not chat:
        await message.answer("⚠️ You are not connected to anyone. Type /search to start.")
        return

    partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]

    if partner_id == "AI_BOT":
        await bot.send_chat_action(chat_id=message.chat.id, action="typing")
        user_text = message.text or "*sent a sticker or media*"
        await asyncio.sleep(min(len(user_text) * 0.04, 2.5))
        asyncio.create_task(generate_ai_response(user_id, user_text, message.chat.id))
        return

    # Forwarding between human partners
    try:
        await bot.copy_message(
            chat_id=partner_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id
        )

        # Archive media to private Telegram channel
        if LOG_CHANNEL_ID and (message.photo or message.video or message.document or message.voice or message.video_note):
            await bot.copy_message(
                chat_id=int(LOG_CHANNEL_ID),
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=f"Archived from user {user_id}"
            )
    except Exception as e:
        logging.error(f"Routing Error: {e}")
        await message.answer("⚠️ Failed to deliver message. Your partner might have disconnected.")
        await chats_col.delete_one({"_id": chat["_id"]})

# Fake Web Server for Render Keep-Alive
async def ping(request):
    return web.Response(text="Bot is running!")

async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await set_bot_commands(bot)

    app = web.Application()
    app.router.add_get('/', ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
