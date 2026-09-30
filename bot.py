import asyncio
import logging
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
from pymongo import AsyncMongoClient

# Your Telegram Bot Token
TOKEN = "8688427246:AAGXVbX7lo8IyjZ1PY25F_KDUgaNKyFHjgQ"

# Your MongoDB Connection String
MONGO_URI = "mongodb://pnavaneethan2005_db_user:4todKATcKJANHoQl@ac-iwa75hv-shard-00-00.jg76bo8.mongodb.net:27017,ac-iwa75hv-shard-00-01.jg76bo8.mongodb.net:27017,ac-iwa75hv-shard-00-02.jg76bo8.mongodb.net:27017/?ssl=true&replicaSet=atlas-vye1bf-shard-0&authSource=admin&appName=ShadowChatterDB"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TOKEN)
dp = Dispatcher()
router = Router()

# Connect to MongoDB asynchronously
client = AsyncMongoClient(MONGO_URI)
db = client.anon_chat_db
queue_col = db.queue
chats_col = db.active_chats

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Welcome to the Anonymous Chat Bot! 🕵‍♂️️\n\n"
        "Type /search to find a random partner.\n"
        "Type /stop to end a chat."
    )

@router.message(Command("search"))
async def cmd_search(message: types.Message):
    user_id = message.from_user.id
    
    # 1. Check if user is already chatting
    if await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]}):
        await message.answer("You are already in a chat! Type /stop to end it.")
        return
        
    # 2. Check if user is already in the waiting pool
    if await queue_col.find_one({"user_id": user_id}):
        await message.answer("You are already in the waiting queue. Please wait...")
        return

    # 3. Look for someone else waiting
    waiting_partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}})
    
    if waiting_partner:
        partner_id = waiting_partner["user_id"]
        
        # Link both users in the active chats collection
        await chats_col.insert_one({"user_1": user_id, "user_2": partner_id})
        
        # Notify both users
        await bot.send_message(user_id, "Partner found! Say Hi! 👋\n(Type /stop to disconnect)")
        await bot.send_message(partner_id, "Partner found! Say Hi! 👋\n(Type /stop to disconnect)")
    else:
        # Nobody is waiting, add the current user to the queue
        await queue_col.insert_one({"user_id": user_id})
        await message.answer("Waiting for a partner... ⏳\n(Type /stop to cancel)")

@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    user_id = message.from_user.id
    
    # Try removing the user from the waiting queue first
    deleted = await queue_col.find_one_and_delete({"user_id": user_id})
    if deleted:
        await message.answer("You left the waiting queue.")
        return
        
    # Try finding and deleting an active chat involving this user
    chat = await chats_col.find_one_and_delete({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    
    if chat:
        # Identify who the partner was
        partner_id = chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]
        
        await message.answer("You disconnected from the chat. Type /search to find a new one.")
        try:
            await bot.send_message(partner_id, "Your partner disconnected. Type /search to find a new one.")
        except Exception:
            pass # Fails silently if the partner blocked the bot
    else:
        await message.answer("You are not in a chat. Type /search to find a partner.")

@router.message(~F.text.startswith('/'))
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    
    # Query the database to see if the sender is currently matched
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
            
            # Emergency cleanup if the connection is dead
            await chats_col.delete_one({"_id": chat["_id"]})
            try:
                await bot.send_message(partner_id, "Your partner was disconnected due to an error.")
            except Exception:
                pass
    else:
        await message.answer("You are not connected to anyone. Type /search to find a partner.")

async def main():
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True) 
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())