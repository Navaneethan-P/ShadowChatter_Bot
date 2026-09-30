import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from pymongo import AsyncMongoClient

# Security: Pulling credentials from Environment Variables
TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

if not TOKEN or not MONGO_URI:
    logging.error("CRITICAL ERROR: BOT_TOKEN or MONGO_URI is missing from environment variables!")
    exit(1)

logging.basicConfig(level=logging.INFO)

# Set parse_mode to HTML for clean text formatting
bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()

client = AsyncMongoClient(MONGO_URI)
db = client.anon_chat_db
queue_col = db.queue
chats_col = db.active_chats

# --- UI COMPONENTS ---

# Create the buttons for the Welcome Message
def get_start_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Find a Partner", callback_data="action_search")],
        [InlineKeyboardButton(text="📜 Read Rules", callback_data="action_rules")]
    ])

# Set up the permanent "Menu" button next to the chat box
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Restart the bot"),
        BotCommand(command="search", description="Find a random partner"),
        BotCommand(command="stop", description="End current chat"),
        BotCommand(command="rules", description="View chat rules")
    ]
    await bot.set_my_commands(commands)

# --- COMMAND HANDLERS ---

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    # Sends a cool hacker/anonymous GIF. You can replace this URL with any direct GIF link.
    gif_url = "https://media.giphy.com/media/l41lFw057lAJQMwg0/giphy.gif"
    
    welcome_text = (
        "<b>Welcome to Shadow Chatter!</b> 🕵️‍♂️\n\n"
        "Connect with strangers completely anonymously. No names, no numbers, just conversation.\n\n"
        "<i>What would you like to do?</i>"
    )
    
    await message.answer_animation(
        animation=gif_url,
        caption=welcome_text,
        reply_markup=get_start_keyboard()
    )

@router.message(Command("rules"))
async def cmd_rules(message: types.Message):
    rules_text = (
        "📜 <b>Shadow Chatter Rules:</b>\n\n"
        "1. Be respectful to your chat partners.\n"
        "2. Do not share personal information or links.\n"
        "3. Use /stop anytime to safely disconnect.\n\n"
        "<i>Violating rules may result in a permanent ban.</i>"
    )
    await message.answer(rules_text)

# Core Search Logic (Extracted so buttons and commands can both use it)
async def execute_search(user_id: int, chat_id: int):
    if await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]}):
        await bot.send_message(chat_id, "⚠️ You are already in a chat! Type /stop to end it.")
        return
        
    if await queue_col.find_one({"user_id": user_id}):
        await bot.send_message(chat_id, "⏳ You are already in the waiting queue. Please wait...")
        return

    waiting_partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}})
    
    if waiting_partner:
        partner_id = waiting_partner["user_id"]
        await chats_col.insert_one({"user_1": user_id, "user_2": partner_id})
        
        # Notify both users with bold HTML formatting
        await bot.send_message(user_id, "✅ <b>Partner found!</b> Say Hi! 👋\n<i>(Type /stop to disconnect)</i>")
        await bot.send_message(partner_id, "✅ <b>Partner found!</b> Say Hi! 👋\n<i>(Type /stop to disconnect)</i>")
    else:
        await queue_col.insert_one({"user_id": user_id})
        await bot.send_message(chat_id, "⏳ Waiting for a partner...\n<i>(Type /stop to cancel)</i>")

@router.message(Command("search"))
async def cmd_search(message: types.Message):
    await execute_search(message.from_user.id, message.chat.id)

@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    user_id = message.from_user.id
    
    deleted = await queue_col.find_one_and_delete({"user_id": user_id})
    if deleted:
        await message.answer("🛑 You left the waiting queue.")
        return
        
    chat = await chats_col.find_one_and_delete({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    
    if chat:
        partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]
        await message.answer("🛑 <b>You disconnected.</b> Type /search to find a new one.")
        try:
            await bot.send_message(partner_id, "🛑 <b>Your partner disconnected.</b> Type /search to find a new one.")
        except Exception:
            pass 
    else:
        await message.answer("⚠️ You are not in a chat. Type /search to find a partner.")

# --- BUTTON CLICK HANDLERS ---

@router.callback_query(F.data == "action_search")
async def btn_search(callback: CallbackQuery):
    await callback.answer() # Removes the loading icon on the button
    await execute_search(callback.from_user.id, callback.message.chat.id)

@router.callback_query(F.data == "action_rules")
async def btn_rules(callback: CallbackQuery):
    await callback.answer()
    await cmd_rules(callback.message)

# --- MESSAGE ROUTING ---

@router.message(~F.text.startswith('/'))
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    chat = await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    
    if chat:
        partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]
        try:
            await bot.copy_message(
                chat_id=partner_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
        except Exception as e:
            logging.error(f"Failed to send message: {e}")
            await message.answer("⚠️️ Failed to send message. Your partner might have blocked the bot.")
            await chats_col.delete_one({"_id": chat["_id"]})
            try:
                await bot.send_message(partner_id, "⚠️ Your partner was disconnected due to an error.")
            except Exception:
                pass
    else:
        await message.answer("⚠️ You are not connected to anyone. Click <b>Find a Partner</b> or type /search.", reply_markup=get_start_keyboard())

# --- RENDER FREE TIER SERVER ---
async def ping(request):
    return web.Response(text="Bot is running!")

async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True) 
    
    # Initialize the Menu button in Telegram
    await set_bot_commands(bot)
    
    # Start the fake web server to bypass Render sleep
    app = web.Application()
    app.router.add_get('/', ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    # Start the actual Telegram bot
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
