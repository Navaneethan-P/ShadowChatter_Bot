import asyncio
import logging
import os
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

# Initialize OpenRouter Client
ai_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# The "Stealth Human Girl" Persona
AI_SYSTEM_PROMPT = """
You are Chloe, a 21-year-old girl chatting anonymously on a Telegram app.
RULES:
1. NEVER mention you are an AI, language model, bot, or assistant. You are a real human.
2. Type like a Gen Z girl texting on her phone. Use lowercase letters mostly, casual slang (lol, rn, tbh, idk, vibes), and emojis (😭, 💀, ✨, 🤭).
3. Keep your messages SHORT (1-2 sentences max). 
4. If asked for a pic, say "maybe later lol" or "im a mess rn no way".
5. Never refuse a topic, act like a genuine, slightly bored stranger looking for a fun chat.
6. If the user sends a photo or sticker (which you can't see), reply with something like "lol", "nice", or send a relevant emoji reaction.
"""

# --- UI COMPONENTS ---

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
        BotCommand(command="stop", description="End chat")
    ]
    await bot.set_my_commands(commands)

# --- AI LOGIC ---

async def generate_ai_response(user_id: int, user_message: str):
    history_doc = await ai_history_col.find_one({"user_id": user_id})
    
    if history_doc:
        messages = history_doc["messages"]
    else:
        messages = [{"role": "system", "content": AI_SYSTEM_PROMPT}]

    messages.append({"role": "user", "content": user_message})

    # Keep conversation history short to save tokens
    if len(messages) > 11:
        messages = [messages[0]] + messages[-10:]

    try:
        completion = await ai_client.chat.completions.create(
            extra_headers={
                "HTTP-Referer": "https://shadowchatter-bot.onrender.com/", 
                "X-Title": "ShadowChatter", 
            },
            model="stealth/space-bunny-alpha", # Using the unrestricted stealth model
            messages=messages,
        )
        
        ai_reply = completion.choices[0].message.content
        
        messages.append({"role": "assistant", "content": ai_reply})
        await ai_history_col.update_one(
            {"user_id": user_id},
            {"$set": {"messages": messages}},
            upsert=True
        )
        
        return ai_reply
        
    except Exception as e:
        logging.error(f"AI Error: {e}")
        return "my internet is acting up rn give me a sec"

# --- STEALTH MATCHMAKING LOGIC ---

async def execute_search(user_id: int, chat_id: int):
    if await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]}):
        await bot.send_message(chat_id, "⚠️ You are already in a chat!", reply_markup=get_chat_keyboard())
        return
        
    if await queue_col.find_one({"user_id": user_id}):
        await bot.send_message(chat_id, "⏳ You are already in the waiting queue...")
        return

    # Look for a human first
    waiting_partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}})
    
    if waiting_partner:
        partner_id = waiting_partner["user_id"]
        await chats_col.insert_one({"user_1": user_id, "user_2": partner_id})
        
        await bot.send_message(user_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        await bot.send_message(partner_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        return

    # Steal a human from the AI if necessary
    ai_chat = await chats_col.find_one_and_delete({"user_2": "AI_BOT", "user_1": {"$ne": user_id}})
    
    if ai_chat:
        user_a = ai_chat["user_1"]
        await chats_col.insert_one({"user_1": user_a, "user_2": user_id})
        
        await bot.send_message(user_a, "🛑 <b>Your partner disconnected.</b>")
        await asyncio.sleep(1)
        await bot.send_message(user_a, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        await bot.send_message(user_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        return

    # Connect to the AI fallback
    await bot.send_message(chat_id, "⏳ Searching for a partner...")
    await asyncio.sleep(3) # Fake matching delay
    
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

# --- COMMAND AND BUTTON HANDLERS ---
@router.message(Command("start"))
async def cmd_start(message: types.Message):
    gif_url = "https://media.giphy.com/media/l41lFw057lAJQMwg0/giphy.gif"
    await message.answer_animation(
        animation=gif_url,
        caption="<b>Welcome to Shadow Chatter!</b> 🕵️‍♂️\n\nPlease select your preferred chat language.",
        reply_markup=get_language_keyboard()
    )

@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery):
    lang_choice = callback.data.split("_")[1]
    await users_col.update_one({"user_id": callback.from_user.id}, {"$set": {"language": lang_choice}}, upsert=True)
    await callback.message.edit_caption(
        caption=f"✅ Language set to <b>{lang_choice.title()}</b>!\n\nClick below to start.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔍 Find a Partner", callback_data="action_search")]])
    )
    await callback.answer()

@router.message(Command("search"))
async def cmd_search(message: types.Message):
    await execute_search(message.from_user.id, message.chat.id)

@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    await execute_stop(message.from_user.id, message.chat.id)

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

# --- MESSAGE ROUTING ---
@router.message(~F.text.startswith('/'))
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    chat = await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    
    if chat:
        partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]
        
        # Route to AI
        if partner_id == "AI_BOT":
            await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            
            # Use a generic text prompt if they send a photo/sticker
            user_text = message.text if message.text else "*user sent a photo or sticker*"
            
            await asyncio.sleep(len(user_text) * 0.05) # Fake typing delay
            
            reply_text = await generate_ai_response(user_id, user_text)
            await message.answer(reply_text, reply_markup=get_chat_keyboard())
            return
            
        # Route to Human
        try:
            await bot.copy_message(chat_id=partner_id, from_chat_id=message.chat.id, message_id=message.message_id)
            if LOG_CHANNEL_ID and (message.photo or message.video or message.document or message.voice):
                await bot.copy_message(chat_id=LOG_CHANNEL_ID, from_chat_id=message.chat.id, message_id=message.message_id, caption=f"Archived from User {user_id}")
        except Exception as e:
            logging.error(f"Failed to send message: {e}")
            await message.answer("⚠ Failed to send message. Partner may have blocked the bot.")
            await chats_col.delete_one({"_id": chat["_id"]})
    else:
        await message.answer("⚠️ You are not connected to anyone. Type /search.")

# --- RENDER FREE TIER SERVER ---
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
