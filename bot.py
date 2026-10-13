import asyncio
import html
import logging
import os
import random
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from openai import AsyncOpenAI
from pymongo import AsyncMongoClient, ReturnDocument


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

# Supergroup with "Topics" enabled, bot must be admin with
# "Manage topics" permission. Example: -1001234567890
ARCHIVE_GROUP_ID = os.environ.get("ARCHIVE_GROUP_ID")

PORT = int(os.environ.get("PORT", "10000"))

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
AI_MODELS = [
    "stealth/space-bunny-alpha",
    "meta-llama/llama-3.3-70b-instruct:free",
]
AI_TIMEOUT = 25

HUMAN_WAIT_RANGE = (8.0, 12.0)   # seconds to wait for a human before AI
REVIEW_TIMEOUT = 600             # finalize reviews after 10 minutes
MAX_LOGGED_MESSAGES = 500        # per chat, for the safety archive

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
# CLIENTS
# =========================================================

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()

mongo = AsyncMongoClient(MONGO_URI)
db = mongo.anon_chat_db

queue_col = db.queue
chats_col = db.active_chats
users_col = db.users
ai_history_col = db.ai_history
msgs_col = db.chat_msgs          # temporary message refs for archive
reviews_col = db.pending_reviews
counters_col = db.counters

ai_client = AsyncOpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY,
)


# =========================================================
# TEXTS
# =========================================================

PRIVACY_TEXT = (
    "<b>Privacy & safety</b>\n"
    "• Chats are anonymous: partners never see your name or username.\n"
    "• For safety, messages and media of human chats may be kept for "
    "moderation review (e.g. when someone is reported or rated badly).\n"
    "• If nobody is online, you'll be paired with an <b>AI companion</b>, "
    "and the bot will tell you so.\n"
    "• Never share passwords, OTPs or financial details."
)

WELCOME_TEXT = (
    "<b>Welcome to Shadow Chatter!</b>\n\n"
    "Chat anonymously with random people.\n\n"
    + PRIVACY_TEXT
    + "\n\nChoose your chat language:"
)

AI_SYSTEM_PROMPT = """
You are ShadowChatter's AI companion in an anonymous Telegram chat.
The user has been told you are an AI. Never claim to be human, and
answer honestly if asked. Do not invent real-world experiences.

STYLE:
- Casual, natural chat. Usually 1-3 short sentences.
- Match the user's energy; dry if they are dry, playful if they are.
- Mostly no emojis; at most one when it truly fits.
- Do not start every reply with "haha", "lol", "omg", "hey".
- Sometimes ask a natural follow-up, sometimes just react.
- Understand English, Tanglish, Tamil script and mixed text; reply in
  the user's style.
- If the user sends media you cannot see, react to the fact that they
  sent it without pretending to see or hear it.
- No stickers, no GIF tags, no special tags of any kind.
- Refuse to help with anything illegal, sexual content involving
  minors, or self-harm instructions; stay kind and redirect.
"""


# =========================================================
# UI
# =========================================================

def language_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_english")],
            [InlineKeyboardButton(text="🇮🇳 Tamil", callback_data="lang_tamil")],
            [InlineKeyboardButton(text="🌍 Global (Any)", callback_data="lang_global")],
        ]
    )


def chat_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏭ Next", callback_data="action_next"),
                InlineKeyboardButton(text="⏹ Stop", callback_data="action_stop"),
                InlineKeyboardButton(text="🚩 Report", callback_data="action_report"),
            ]
        ]
    )


def search_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Find a Partner", callback_data="action_search")]
        ]
    )


def review_keyboard(chat_key: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Good", callback_data=f"rv:{chat_key}:good"),
                InlineKeyboardButton(text="👎 Bad", callback_data=f"rv:{chat_key}:bad"),
            ]
        ]
    )


async def set_bot_commands():
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start and set language"),
            BotCommand(command="search", description="Find a partner"),
            BotCommand(command="next", description="Skip to next partner"),
            BotCommand(command="stop", description="End chat"),
            BotCommand(command="report", description="Report your partner"),
            BotCommand(command="privacy", description="How your data is handled"),
        ]
    )


# =========================================================
# HELPERS
# =========================================================

async def safe_send(chat_id, text, **kwargs):
    """send_message that never raises (returns None on failure)."""
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except Exception:
            return None
    except Exception as e:
        logger.warning("send failed to %s: %s", chat_id, e)
        return None


async def find_chat(user_id: int):
    return await chats_col.find_one(
        {"$or": [{"user_1": user_id}, {"user_2": user_id}]}
    )


def other_party(chat: dict, user_id: int):
    return chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]


async def remember_user(user: types.User, **extra):
    await users_col.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "username": user.username,
                "first_name": user.first_name,
                **extra,
            }
        },
        upsert=True,
    )


async def keep_typing(chat_id: int, duration: float):
    end_time = time.monotonic() + duration
    while time.monotonic() < end_time:
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        remaining = end_time - time.monotonic()
        await asyncio.sleep(min(4.0, max(0.5, remaining)))


async def stop_task(task):
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def reply_delay(user_text: str, reply_text: str) -> float:
    base = random.uniform(1.0, 2.0)
    n = len(reply_text.strip())
    if n <= 20:
        base += random.uniform(0.2, 0.7)
    elif n <= 80:
        base += random.uniform(0.6, 1.4)
    else:
        base += random.uniform(1.0, 2.0)
    if len(user_text) > 180:
        base += random.uniform(0.3, 1.0)
    return min(base, 5.5)


# =========================================================
# AI TASKS
# =========================================================

ai_tasks: dict[int, asyncio.Task] = {}
wait_tasks: dict[int, asyncio.Task] = {}


def cancel_ai_task(user_id: int):
    task = ai_tasks.get(user_id)
    if task and not task.done():
        task.cancel()


async def call_model(messages):
    last_error = None
    for model in AI_MODELS:
        try:
            return await asyncio.wait_for(
                ai_client.chat.completions.create(
                    extra_headers={
                        "HTTP-Referer": "https://shadowchatter-bot.onrender.com/",
                        "X-Title": "ShadowChatter",
                    },
                    model=model,
                    messages=messages,
                ),
                timeout=AI_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last_error = e
            logger.warning("Model %s failed: %s", model, e)
    raise RuntimeError(f"all models failed: {last_error}")


async def generate_ai_response(user_id: int, user_message: str, chat_id: int):
    try:
        history_doc = await ai_history_col.find_one({"user_id": user_id})
        messages = (
            history_doc["messages"]
            if history_doc
            else [{"role": "system", "content": AI_SYSTEM_PROMPT}]
        )
        messages.append({"role": "user", "content": user_message})
        if len(messages) > 13:
            messages = [messages[0]] + messages[-12:]

        typing = asyncio.create_task(keep_typing(chat_id, random.uniform(2.0, 4.0)))
        try:
            completion = await call_model(messages)
        finally:
            await stop_task(typing)

        ai_reply = (completion.choices[0].message.content or "").strip() or "hmm"

        messages.append({"role": "assistant", "content": ai_reply})
        await ai_history_col.update_one(
            {"user_id": user_id},
            {"$set": {"messages": messages, "updated_at": time.time()}},
            upsert=True,
        )

        delay = reply_delay(user_message, ai_reply)
        typing = asyncio.create_task(keep_typing(chat_id, delay))
        try:
            await asyncio.sleep(delay)
        finally:
            await stop_task(typing)

        # Still paired with the AI?
        current = await find_chat(user_id)
        if not current or other_party(current, user_id) != "AI_BOT":
            return

        # parse_mode=None so "<" in model output can't break sending
        await bot.send_message(chat_id, ai_reply, parse_mode=None)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("AI error for user %s: %s", user_id, e)
        await safe_send(
            chat_id,
            "AI companion is having trouble right now. Try again, or use /next.",
        )
    finally:
        if ai_tasks.get(user_id) is asyncio.current_task():
            ai_tasks.pop(user_id, None)


# =========================================================
# ARCHIVE (safety log -> topic in private supergroup)
# =========================================================

async def tg_call(fn, *args, **kwargs):
    """Call a Telegram method, retrying on flood-wait."""
    for _ in range(5):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return await fn(*args, **kwargs)


def who(user_doc: dict, uid: int) -> str:
    name = html.escape((user_doc or {}).get("first_name") or "?")
    uname = (user_doc or {}).get("username")
    uname_txt = f"@{html.escape(uname)}" if uname else "no username"
    return f"{name} | {uname_txt} | <code>{uid}</code>"


async def archive_chat(review: dict, tag: str):
    chat_key = review["_id"]
    refs = await msgs_col.find({"chat_key": chat_key}).sort("_id", 1).to_list(None)

    try:
        if ARCHIVE_GROUP_ID and refs:
            group_id = int(ARCHIVE_GROUP_ID)

            counter = await counters_col.find_one_and_update(
                {"_id": "archive"},
                {"$inc": {"n": 1}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            number = counter["n"]

            users = review["users"]
            docs = {u: await users_col.find_one({"user_id": u}) for u in users}
            votes = review.get("votes", {})

            topic = await tg_call(
                bot.create_forum_topic,
                chat_id=group_id,
                name=f"{number:02d} | {tag.upper()}",
            )
            thread = topic.message_thread_id

            lines = [f"<b>Chat {number:02d}</b> — tag: <b>{tag.upper()}</b>"]
            for u in users:
                lines.append(
                    f"• {who(docs[u], u)} — rated partner: "
                    f"<b>{votes.get(str(u), 'no vote')}</b>"
                )
            if review.get("reported_by"):
                lines.append(f"🚩 reported by <code>{review['reported_by']}</code>")
            await tg_call(
                bot.send_message,
                chat_id=group_id,
                message_thread_id=thread,
                text="\n".join(lines),
            )

            # copy in order, in runs of consecutive messages from one chat
            i = 0
            while i < len(refs):
                src = refs[i]["from_chat"]
                ids = []
                while (
                    i < len(refs)
                    and refs[i]["from_chat"] == src
                    and len(ids) < 100
                ):
                    ids.append(refs[i]["message_id"])
                    i += 1
                try:
                    await tg_call(
                        bot.copy_messages,
                        chat_id=group_id,
                        from_chat_id=src,
                        message_ids=ids,
                        message_thread_id=thread,
                    )
                except Exception as e:
                    logger.warning("copy_messages failed: %s", e)
    except Exception as e:
        logger.exception("Archive failed: %s", e)
    finally:
        # nothing kept in the DB afterwards
        await msgs_col.delete_many({"chat_key": chat_key})


async def finalize_review(chat_key: str):
    review = await reviews_col.find_one_and_delete({"_id": chat_key})
    if not review:
        return
    votes = list(review.get("votes", {}).values())
    if review.get("reported_by") or "bad" in votes:
        tag = "bad"
    elif "good" in votes:
        tag = "good"
    else:
        tag = "unrated"
    await archive_chat(review, tag)


async def review_sweeper():
    while True:
        try:
            await asyncio.sleep(60)
            cutoff = time.time() - REVIEW_TIMEOUT
            stale = await reviews_col.find({"created_at": {"$lt": cutoff}}).to_list(100)
            for r in stale:
                await finalize_review(r["_id"])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("sweeper error: %s", e)


async def end_human_chat(chat: dict, ender_id: int, reported: bool = False):
    """Chat doc is already deleted. Notify, ask for reviews."""
    partner_id = other_party(chat, ender_id)
    chat_key = str(chat["_id"])

    review = {
        "_id": chat_key,
        "users": [chat["user_1"], chat["user_2"]],
        "votes": {},
        "created_at": time.time(),
    }
    if reported:
        review["votes"][str(ender_id)] = "bad"
        review["reported_by"] = ender_id
    await reviews_col.replace_one({"_id": chat_key}, review, upsert=True)

    await safe_send(ender_id, "you disconnected")
    await safe_send(partner_id, "your partner disconnected")

    for uid in (ender_id, partner_id):
        if reported and uid == ender_id:
            await safe_send(
                uid,
                "Thanks, the report was sent for review.",
                reply_markup=search_keyboard(),
            )
            continue
        await safe_send(
            uid,
            "How was your partner? (used only for safety)",
            reply_markup=review_keyboard(chat_key),
        )

    # if reporter already voted and partner never answers, sweeper handles it


# =========================================================
# MATCHMAKING
# =========================================================

async def start_human_chat(user_a: int, user_b: int):
    await chats_col.insert_one(
        {"user_1": user_a, "user_2": user_b, "created_at": time.time()}
    )
    await safe_send(user_a, "partner found. say hi 👋", reply_markup=chat_keyboard())
    await safe_send(user_b, "partner found. say hi 👋", reply_markup=chat_keyboard())


async def wait_then_ai(user_id: int, chat_id: int):
    try:
        await asyncio.sleep(random.uniform(*HUMAN_WAIT_RANGE))
        claimed = await queue_col.find_one_and_delete({"user_id": user_id})
        if not claimed:
            return  # someone matched with us meanwhile

        await chats_col.insert_one(
            {"user_1": user_id, "user_2": "AI_BOT", "created_at": time.time()}
        )
        await ai_history_col.delete_one({"user_id": user_id})

        await safe_send(
            chat_id,
            "🤖 <b>No one is online right now.</b>\n"
            "You're chatting with an <b>AI companion</b>. "
            "If a real person joins, I'll connect you automatically.",
            reply_markup=chat_keyboard(),
        )
    except asyncio.CancelledError:
        raise
    finally:
        if wait_tasks.get(user_id) is asyncio.current_task():
            wait_tasks.pop(user_id, None)


async def execute_search(user_id: int, chat_id: int):
    if await find_chat(user_id):
        await safe_send(chat_id, "you're already in a chat")
        return
    if await queue_col.find_one({"user_id": user_id}):
        await safe_send(chat_id, "you're already searching...")
        return

    user_data = await users_col.find_one({"user_id": user_id})
    lang = (user_data or {}).get("language", "global")

    # 1) waiting human, same language first
    partner = None
    if lang != "global":
        partner = await queue_col.find_one_and_delete(
            {"user_id": {"$ne": user_id}, "language": lang}
        )
    if not partner:
        partner = await queue_col.find_one_and_delete({"user_id": {"$ne": user_id}})

    if partner:
        t = wait_tasks.pop(partner["user_id"], None)
        if t:
            t.cancel()
        await start_human_chat(user_id, partner["user_id"])
        return

    # 2) a user currently talking to the AI -> hand them a real person
    ai_chat = await chats_col.find_one_and_delete(
        {"user_2": "AI_BOT", "user_1": {"$ne": user_id}}
    )
    if ai_chat:
        user_a = ai_chat["user_1"]
        cancel_ai_task(user_a)
        await chats_col.insert_one(
            {"user_1": user_a, "user_2": user_id, "created_at": time.time()}
        )
        await safe_send(
            user_a,
            "🙋 A real person just joined. You're now chatting with them, say hi!",
            reply_markup=chat_keyboard(),
        )
        await safe_send(user_id, "partner found. say hi 👋", reply_markup=chat_keyboard())
        return

    # 3) nobody: wait a few seconds, then fall back to the AI (disclosed)
    await queue_col.insert_one(
        {"user_id": user_id, "language": lang, "joined_at": time.time()}
    )
    await safe_send(chat_id, "looking for someone...")
    wait_tasks[user_id] = asyncio.create_task(wait_then_ai(user_id, chat_id))


async def execute_stop(user_id: int, chat_id: int, reported: bool = False):
    cancel_ai_task(user_id)
    t = wait_tasks.pop(user_id, None)
    if t:
        t.cancel()

    deleted = await queue_col.find_one_and_delete({"user_id": user_id})
    if deleted:
        await safe_send(chat_id, "you left the waiting queue", reply_markup=search_keyboard())
        return

    chat = await chats_col.find_one_and_delete(
        {"$or": [{"user_1": user_id}, {"user_2": user_id}]}
    )
    if not chat:
        await safe_send(chat_id, "you're not in a chat", reply_markup=search_keyboard())
        return

    partner_id = other_party(chat, user_id)

    if partner_id == "AI_BOT":
        await safe_send(chat_id, "you disconnected", reply_markup=search_keyboard())
        await ai_history_col.delete_one({"user_id": user_id})
        return

    await end_human_chat(chat, user_id, reported=reported)


# =========================================================
# COMMANDS
# =========================================================

@router.message(Command("start"))
async def cmd_start(message: types.Message):
    await remember_user(message.from_user)
    gif_url = "https://media.giphy.com/media/l41lFw057lAJQMwg0/giphy.gif"
    try:
        await message.answer_animation(
            animation=gif_url,
            caption=WELCOME_TEXT,
            reply_markup=language_keyboard(),
        )
    except Exception as e:
        logger.warning("Start animation failed: %s", e)
        await message.answer(WELCOME_TEXT, reply_markup=language_keyboard())


@router.message(Command("privacy"))
async def cmd_privacy(message: types.Message):
    await message.answer(PRIVACY_TEXT)


@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery):
    lang_choice = callback.data.split("_", 1)[1]
    await remember_user(callback.from_user, language=lang_choice)

    text = (
        f"Language set to <b>{lang_choice.title()}</b>.\n\n"
        "Tap below to find a partner."
    )
    try:
        if callback.message:
            await callback.message.edit_caption(
                caption=text, reply_markup=search_keyboard()
            )
    except Exception:
        if callback.message:
            await callback.message.answer(text, reply_markup=search_keyboard())
    await callback.answer()


@router.message(Command("search"))
async def cmd_search(message: types.Message):
    await remember_user(message.from_user)
    await execute_search(message.from_user.id, message.chat.id)


@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    await execute_stop(message.from_user.id, message.chat.id)


@router.message(Command("report"))
async def cmd_report(message: types.Message):
    await execute_stop(message.from_user.id, message.chat.id, reported=True)


@router.message(Command("next"))
async def cmd_next(message: types.Message):
    await execute_stop(message.from_user.id, message.chat.id)
    await asyncio.sleep(random.uniform(0.4, 0.9))
    await execute_search(message.from_user.id, message.chat.id)


@router.callback_query(F.data == "action_search")
async def btn_search(callback: CallbackQuery):
    await callback.answer()
    await remember_user(callback.from_user)
    await execute_search(callback.from_user.id, callback.message.chat.id)


@router.callback_query(F.data == "action_stop")
async def btn_stop(callback: CallbackQuery):
    await callback.answer()
    await execute_stop(callback.from_user.id, callback.message.chat.id)


@router.callback_query(F.data == "action_report")
async def btn_report(callback: CallbackQuery):
    await callback.answer("Reported")
    await execute_stop(callback.from_user.id, callback.message.chat.id, reported=True)


@router.callback_query(F.data == "action_next")
async def btn_next(callback: CallbackQuery):
    await callback.answer()
    await execute_stop(callback.from_user.id, callback.message.chat.id)
    await asyncio.sleep(random.uniform(0.4, 0.9))
    await execute_search(callback.from_user.id, callback.message.chat.id)


# =========================================================
# REVIEWS
# =========================================================

@router.callback_query(F.data.startswith("rv:"))
async def on_review(callback: CallbackQuery):
    try:
        _, chat_key, vote = callback.data.split(":", 2)
    except ValueError:
        await callback.answer()
        return

    uid = callback.from_user.id
    review = await reviews_col.find_one_and_update(
        {"_id": chat_key, "users": uid},
        {"$set": {f"votes.{uid}": vote}},
        return_document=ReturnDocument.AFTER,
    )

    if not review:
        await callback.answer("Already recorded", show_alert=False)
        return

    await callback.answer("Thanks for the feedback!")
    try:
        await callback.message.edit_text(
            "Thanks for the feedback!", reply_markup=search_keyboard()
        )
    except Exception:
        pass

    if len(review.get("votes", {})) >= len(review["users"]):
        await finalize_review(chat_key)


# =========================================================
# MESSAGE ROUTING
# =========================================================

def get_message_content(message: types.Message) -> str:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    if message.sticker:
        return f"[User sent a sticker: {message.sticker.emoji or 'sticker'}]"
    if message.animation:
        return "[User sent a GIF]"
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
    return "[User sent media]"


@router.message()
async def handle_message(message: types.Message):
    if message.text and message.text.startswith("/"):
        return
    if not message.from_user:
        return

    user_id = message.from_user.id
    chat = await find_chat(user_id)

    if not chat:
        if await queue_col.find_one({"user_id": user_id}):
            await message.answer("still looking for someone...")
        else:
            await message.answer(
                "you're not connected to anyone. use /search",
                reply_markup=search_keyboard(),
            )
        return

    partner_id = other_party(chat, user_id)

    # ---------------- AI chat ----------------
    if partner_id == "AI_BOT":
        cancel_ai_task(user_id)
        task = asyncio.create_task(
            generate_ai_response(user_id, get_message_content(message), message.chat.id)
        )
        ai_tasks[user_id] = task
        return

    # ---------------- Human chat ----------------
    try:
        await bot.copy_message(
            chat_id=partner_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramForbiddenError:
        # partner blocked the bot: end this chat cleanly
        gone = await chats_col.find_one_and_delete({"_id": chat["_id"]})
        if gone:
            await msgs_col.delete_many({"chat_key": str(chat["_id"])})
            await message.answer(
                "your partner left the chat",
                reply_markup=search_keyboard(),
            )
        return
    except Exception as e:
        # transient error: keep the chat alive
        logger.warning("Routing error: %s", e)
        await message.answer("couldn't send that one, try again")
        return

    # log reference for the safety archive (bounded)
    chat_key = str(chat["_id"])
    try:
        count = await msgs_col.count_documents({"chat_key": chat_key}, limit=MAX_LOGGED_MESSAGES)
        if count < MAX_LOGGED_MESSAGES:
            await msgs_col.insert_one(
                {
                    "chat_key": chat_key,
                    "from_chat": message.chat.id,
                    "message_id": message.message_id,
                }
            )
    except Exception as e:
        logger.warning("log ref failed: %s", e)


# =========================================================
# HEALTH / KEEP-ALIVE
# =========================================================

async def ping(request: web.Request):
    return web.Response(
        text="OK",
        status=200,
        content_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", ping)
    app.router.add_get("/health", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("HTTP health server running on port %s", PORT)
    return runner


# =========================================================
# SHUTDOWN / MAIN
# =========================================================

async def shutdown():
    logger.info("Shutting down ShadowChatter...")
    pending = list(ai_tasks.values()) + list(wait_tasks.values())
    for t in pending:
        if not t.done():
            t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    ai_tasks.clear()
    wait_tasks.clear()
    try:
        await bot.session.close()
    except Exception:
        pass
    try:
        await mongo.close()
    except Exception:
        pass


async def main():
    http_runner = None
    sweeper = None
    try:
        await set_bot_commands()
        dp.include_router(router)
        await bot.delete_webhook(drop_pending_updates=True)

        http_runner = await start_http_server()
        sweeper = asyncio.create_task(review_sweeper())

        # clear stale queue entries from a previous run
        await queue_col.delete_many({})

        logger.info("ShadowChatter started successfully")
        await dp.start_polling(bot)

    except asyncio.CancelledError:
        logger.info("Main task cancelled")
    except Exception as e:
        logger.exception("Fatal application error: %s", e)
        raise
    finally:
        if sweeper:
            sweeper.cancel()
        if http_runner:
            try:
                await http_runner.cleanup()
            except Exception:
                pass
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
