import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from pymongo import AsyncMongoClient

# Security: Pulling credentials from Environment Variables
TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

if not TOKEN or not MONGO_URI:
    logging.error("CRITICAL ERROR: BOT_TOKEN or MONGO_URI is missing from environment variables!")
    exit(1)

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

client = AsyncMongoClient(MONGO_URI)
db = client.anon_chat_db
queue_col = db.queue
chats_col = db.active_chats

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Welcome to the Anonymous Chat Bot! 🕵‍♂\n\n"
        "Type /search to find a random partner.\n"
        "Type /stop to end a chat."
    )

@router.message(Command("search"))
async def cmd_search(message: types.Message):
    user_id = message.from_user.id
    
    if await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]}):
        await message.answer("You are already in a chat! Type /stop to end it.")
        return
        
    if await queue_col.find_one({"user_id": user_id}):
        await message.answer("You are already in the waiting queue. Please wait...")
        return

    waiting_partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}})
    
    if waiting_partner:
        partner_id = waiting_partner["user_id"]
        await chats_col.insert_one({"user_1": user_id, "user_2": partner_id})
        await bot.send_message(user_id, "Partner found! Say Hi! 👋\n(Type /stop to disconnect)")
        await bot.send_message(partner_id, "Partner found! Say Hi! 👋\n(Type /stop to disconnect)")
    else:
        await queue_col.insert_one({"user_id": user_id})
        await message.answer("Waiting for a partner... ⏳\n(Type /stop to cancel)")

@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    user_id = message.from_user.id
    
    deleted = await queue_col.find_one_and_delete({"user_id": user_id})
    if deleted:
        await message.answer("You left the waiting queue.")
        return
        
    chat = await chats_col.find_one_and_delete({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    
    if chat:
        partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]
        await message.answer("You disconnected from the chat. Type /search to find a new one.")
        try:
            await bot.send_message(partner_id, "Your partner disconnected. Type /search to find a new one.")
        except Exception:
            pass 
    else:
        await message.answer("You are not in a chat. Type /search to find a partner.")

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
            await message.answer("Failed to send message. Your partner might have blocked the bot.")
            await chats_col.delete_one({"_id": chat["_id"]})
            try:
                await bot.send_message(partner_id, "Your partner was disconnected due to an error.")
            except Exception:
                pass
    else:
        await message.answer("You are not connected to anyone. Type /search to find a partner.")

# Render Free Tier Requirement: Fake Web Server
async def ping(request):
    return web.Response(text="Bot is running!")

async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True) 
    
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
