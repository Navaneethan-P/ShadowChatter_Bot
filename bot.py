import asyncio
import html
import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from pymongo import AsyncMongoClient, ReturnDocument


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

# Your own Telegram numeric user ID (get it from @userinfobot).
# Finished chats are sent ONLY to this ID, never to any group.
OWNER_ID = int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID") else None

PORT = int(os.environ.get("PORT", "10000"))

MAX_LOGGED_MESSAGES = 1000   # per chat
IST = timezone(timedelta(hours=5, minutes=30))

if not TOKEN:
    raise SystemExit("CRITICAL ERROR: BOT_TOKEN is missing")
if not MONGO_URI:
    raise SystemExit("CRITICAL ERROR: MONGO_URI is missing")
if not OWNER_ID:
    logging.warning("OWNER_ID is not set: chat archive is disabled")


# =========================================================
# LOGGING / CLIENTS
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("shadowchatter")

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
router.message.filter(F.chat.type == "private")   # ignore groups completely

mongo = AsyncMongoClient(MONGO_URI)
db = mongo.anon_chat_db

queue_col = db.queue
chats_col = db.active_chats
users_col = db.users
msgs_col = db.chat_msgs        # temporary message refs, deleted after archive
reviews_col = db.pending_reviews
counters_col = db.counters

_bot_username = None


# =========================================================
# TEXT
# =========================================================

PRIVACY_TEXT = (
    "<b>Privacy & safety</b>\n"
    "• Chats are anonymous: partners never see your name or username "
    "(unless you both choose to /reveal).\n"
    "• For safety, finished chats (text and media) are saved by the "
    "bot admin for moderation review.\n"
    "• Never share passwords, OTPs or financial details.\n"
    "• Be kind. Reported or badly rated users can be banned."
)

WELCOME_TEXT = (
    "<b>Welcome to Shadow Chatter 🕶</b>\n\n"
    "Meet random people anonymously: text, photos, GIFs, voice, all supported.\n\n"
    + PRIVACY_TEXT
    + "\n\nFirst, choose your language:"
)

INTERESTS = {
    "music": "🎵 Music",
    "movies": "🎬 Movies",
    "gaming": "🎮 Gaming",
    "tech": "💻 Tech",
    "any": "😎 Just chat",
}

ICEBREAKERS = [
    "What's the last thing that made you genuinely laugh?",
    "If you could instantly learn any skill, what would it be?",
    "What's a small thing that always makes your day better?",
    "Best movie you watched this year?",
    "What song is stuck in your head lately?",
    "Morning person or night owl?",
    "What's your most useless talent?",
    "If you could travel anywhere tomorrow, where would you go?",
]
TRUTHS = [
    "What's the most embarrassing thing you've done in public?",
    "What's a secret talent nobody knows you have?",
    "What's your biggest irrational fear?",
    "What's the last lie you told?",
    "Who was your first celebrity crush?",
    "What's a habit you're trying to quit?",
]
DARES = [
    "Send the 5th photo in your gallery (if you're comfortable).",
    "Describe your day using only emojis.",
    "Type a message with your eyes closed and send it.",
    "Say something nice about your partner.",
    "Share your most-played song right now.",
    "Make up a 2-line poem about this chat.",
]
WYR = [
    "Would you rather lose your phone or your wallet for a week?",
    "Would you rather read minds or see the future?",
    "Would you rather always be 10 mins late or 20 mins early?",
    "Would you rather have unlimited food or unlimited travel?",
    "Would you rather never use social media or never watch movies again?",
]
FUN_BANK = {
    "ice": ("🧊 Icebreaker", ICEBREAKERS),
    "truth": ("🤔 Truth", TRUTHS),
    "dare": ("😈 Dare", DARES),
    "wyr": ("⚖️ Would you rather", WYR),
}


# =========================================================
# KEYBOARDS
# =========================================================

def language_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_english")],
            [InlineKeyboardButton(text="🇮🇳 Tamil", callback_data="lang_tamil")],
            [InlineKeyboardButton(text="🌍 Global (Any)", callback_data="lang_global")],
        ]
    )


def interest_keyboard():
    rows, row = [], []
    for key, label in INTERESTS.items():
        row.append(InlineKeyboardButton(text=label, callback_data=f"int_{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Find a Partner", callback_data="action_search")]
        ]
    )


def chat_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏭ Next", callback_data="action_next"),
                InlineKeyboardButton(text="⏹ Stop", callback_data="action_stop"),
            ],
            [
                InlineKeyboardButton(text="🎲 Fun", callback_data="fun_menu"),
                InlineKeyboardButton(text="🤝 Reveal", callback_data="rev_ask"),
                InlineKeyboardButton(text="🚩 Report", callback_data="action_report"),
            ],
        ]
    )


def fun_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧊 Icebreaker", callback_data="fun_ice"),
                InlineKeyboardButton(text="🤔 Truth", callback_data="fun_truth"),
            ],
            [
                InlineKeyboardButton(text="😈 Dare", callback_data="fun_dare"),
                InlineKeyboardButton(text="⚖️ Would you rather", callback_data="fun_wyr"),
            ],
        ]
    )


def review_keyboard(chat_no: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Good", callback_data=f"rv:{chat_no}:good"),
                InlineKeyboardButton(text="👎 Bad", callback_data=f"rv:{chat_no}:bad"),
            ]
        ]
    )


async def set_bot_commands():
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start / change language"),
            BotCommand(command="search", description="Find a partner"),
            BotCommand(command="next", description="Skip to next partner"),
            BotCommand(command="stop", description="End chat"),
            BotCommand(command="fun", description="Icebreakers, truth or dare"),
            BotCommand(command="reveal", description="Share usernames (both must agree)"),
            BotCommand(command="interest", description="Pick your interest"),
            BotCommand(command="stats", description="Your stats"),
            BotCommand(command="invite", description="Invite friends"),
            BotCommand(command="report", description="Report your partner"),
            BotCommand(command="privacy", description="How data is handled"),
        ]
    )


# =========================================================
# HELPERS
# =========================================================

async def safe_send(chat_id, text, **kwargs):
    for _ in range(2):
        try:
            return await bot.send_message(chat_id, text, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            logger.warning("send failed to %s: %s", chat_id, e)
            return None
    return None


async def tg_call(fn, *args, **kwargs):
    for _ in range(6):
        try:
            return await fn(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return await fn(*args, **kwargs)


async def get_username() -> str:
    global _bot_username
    if not _bot_username:
        _bot_username = (await bot.get_me()).username
    return _bot_username


async def find_chat(user_id: int):
    return await chats_col.find_one(
        {"$or": [{"user_1": user_id}, {"user_2": user_id}]}
    )


def other_party(chat: dict, user_id: int) -> int:
    return chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]


async def touch_user(user: types.User, **extra):
    await users_col.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "username": user.username,
                "first_name": user.first_name,
                "last_seen": time.time(),
                **extra,
            }
        },
        upsert=True,
    )


def badge(doc: dict) -> str:
    good, bad = doc.get("good", 0), doc.get("bad", 0)
    total = good + bad
    if total < 3:
        return "🌱 New"
    if good / total >= 0.8:
        return "⭐ Trusted"
    return "⚠️ Mixed reviews"


def who(doc: dict, uid: int) -> str:
    doc = doc or {}
    name = html.escape(doc.get("first_name") or "?")
    uname = doc.get("username")
    uname_txt = f"@{html.escape(uname)}" if uname else "no username"
    return f"{name} | {uname_txt} | <code>{uid}</code>"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


async def edit_or_answer(callback: CallbackQuery, text: str, kb=None):
    try:
        if callback.message.caption is not None or callback.message.animation:
            await callback.message.edit_caption(caption=text, reply_markup=kb)
        else:
            await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)


bg_tasks: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)
    return t


wait_tasks: dict[int, asyncio.Task] = {}


# =========================================================
# ARCHIVE: finished chat -> OWNER'S private DM only
# =========================================================

async def archive_chat(chat: dict) -> int | None:
    """Copy the whole chat to the owner. Returns the chat number."""
    chat_key = str(chat["_id"])
    try:
        refs = await msgs_col.find({"chat_key": chat_key}).sort("_id", 1).to_list(None)
        if not OWNER_ID or not refs:
            return None

        counter = await counters_col.find_one_and_update(
            {"_id": "archive"},
            {"$inc": {"n": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        number = counter["n"]

        a, b = chat["user_1"], chat["user_2"]
        doc_a = await users_col.find_one({"user_id": a})
        doc_b = await users_col.find_one({"user_id": b})
        now = datetime.now(IST)
        started = chat.get("created_at", time.time())

        header = (
            f"📁 <b>Chat-{number:02d}</b>\n"
            f"🗓 {now.strftime('%d %b %Y, %I:%M %p')} IST\n"
            f"⏱ {fmt_duration(time.time() - started)} · {len(refs)} messages\n\n"
            f"🅰️ {who(doc_a, a)}\n"
            f"🅱️ {who(doc_b, b)}\n\n"
            "Messages follow, in order:"
        )
        await tg_call(bot.send_message, OWNER_ID, header)

        i = 0
        while i < len(refs):
            sender = refs[i]["sender"]
            src = refs[i]["from_chat"]
            ids = []
            while (
                i < len(refs)
                and refs[i]["sender"] == sender
                and len(ids) < 100
            ):
                ids.append(refs[i]["message_id"])
                i += 1
            label = "🅰️" if sender == a else "🅱️"
            try:
                await tg_call(bot.send_message, OWNER_ID, f"{label} ▼")
                await tg_call(
                    bot.copy_messages,
                    chat_id=OWNER_ID,
                    from_chat_id=src,
                    message_ids=ids,
                )
            except Exception as e:
                logger.warning("archive copy failed: %s", e)

        await tg_call(bot.send_message, OWNER_ID, f"🏁 End of Chat-{number:02d}")
        return number
    except Exception as e:
        logger.exception("Archive failed: %s", e)
        return None
    finally:
        await msgs_col.delete_many({"chat_key": chat_key})


async def notify_owner(text: str):
    if OWNER_ID:
        await safe_send(OWNER_ID, text)


# =========================================================
# REVIEWS
# =========================================================

async def record_vote(chat_no: int, voter: int, vote: str, reported: bool = False):
    review = await reviews_col.find_one_and_update(
        {
            "_id": str(chat_no),
            "users": voter,
            f"votes.{voter}": {"$exists": False},
        },
        {"$set": {f"votes.{voter}": vote}},
        return_document=ReturnDocument.AFTER,
    )
    if not review:
        return False

    target = [u for u in review["users"] if u != voter][0]
    field = "good" if vote == "good" else "bad"
    updated = await users_col.find_one_and_update(
        {"user_id": target},
        {"$inc": {field: 1}},
        return_document=ReturnDocument.AFTER,
    )

    icon = "🚩" if reported else ("👍" if vote == "good" else "👎")
    voter_doc = await users_col.find_one({"user_id": voter})
    target_doc = updated or {}
    await notify_owner(
        f"{icon} <b>Chat-{chat_no:02d}</b>: {who(voter_doc, voter)}\n"
        f"{'reported' if reported else 'rated'} partner {who(target_doc, target)} "
        f"as <b>{vote.upper()}</b>"
    )

    # automatic ban for repeat offenders
    bad, good = target_doc.get("bad", 0), target_doc.get("good", 0)
    if bad >= 5 and bad > 2 * good and not target_doc.get("banned"):
        await users_col.update_one({"user_id": target}, {"$set": {"banned": True}})
        await notify_owner(f"⛔ Auto-banned <code>{target}</code> (bad={bad}, good={good})")
    return True


async def finish_chat(chat: dict, ender_id: int, reported: bool = False):
    """Chat doc is already deleted. Archive, notify and ask for reviews."""
    partner_id = other_party(chat, ender_id)

    await safe_send(ender_id, "you disconnected")
    await safe_send(partner_id, "your partner disconnected")

    number = await archive_chat(chat)

    if number is None:
        await safe_send(ender_id, "Ready for another?", reply_markup=search_keyboard())
        await safe_send(partner_id, "Ready for another?", reply_markup=search_keyboard())
        return

    await reviews_col.replace_one(
        {"_id": str(number)},
        {
            "_id": str(number),
            "users": [chat["user_1"], chat["user_2"]],
            "votes": {},
            "created_at": datetime.now(timezone.utc),   # TTL index cleans up
        },
        upsert=True,
    )

    if reported:
        await record_vote(number, ender_id, "bad", reported=True)
        await safe_send(
            ender_id,
            "Thanks, your report was sent for review.",
            reply_markup=search_keyboard(),
        )
    else:
        await safe_send(
            ender_id,
            "How was your partner?",
            reply_markup=review_keyboard(number),
        )
    await safe_send(
        partner_id,
        "How was your partner?",
        reply_markup=review_keyboard(number),
    )


# =========================================================
# MATCHMAKING
# =========================================================

async def count_online() -> int:
    return await users_col.count_documents({"last_seen": {"$gt": time.time() - 600}})


async def start_human_chat(user_a: int, user_b: int):
    await chats_col.insert_one(
        {"user_1": user_a, "user_2": user_b, "created_at": time.time(), "msg_count": 0}
    )
    doc_a = await users_col.find_one({"user_id": user_a}) or {}
    doc_b = await users_col.find_one({"user_id": user_b}) or {}
    await users_col.update_many(
        {"user_id": {"$in": [user_a, user_b]}}, {"$inc": {"chats": 1}}
    )

    def intro(partner_doc):
        lang = partner_doc.get("language", "global").title()
        interest = INTERESTS.get(partner_doc.get("interest", "any"), "😎 Just chat")
        return (
            "✅ <b>Partner found!</b> Say hi 👋\n"
            f"{badge(partner_doc)} · 🗣 {lang} · {interest}\n"
            "Tap 🎲 Fun if you need something to talk about."
        )

    await safe_send(user_a, intro(doc_b), reply_markup=chat_keyboard())
    await safe_send(user_b, intro(doc_a), reply_markup=chat_keyboard())


async def nudge_waiting(user_id: int):
    """After 90s of waiting, suggest inviting friends (no fake partners)."""
    try:
        await asyncio.sleep(90)
        if not await queue_col.find_one({"user_id": user_id}):
            return
        link = f"https://t.me/{await get_username()}?start=ref_{user_id}"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📤 Invite a friend",
                        url=f"https://t.me/share/url?url={link}&text=Chat%20anonymously%20with%20random%20people",
                    )
                ]
            ]
        )
        await safe_send(
            user_id,
            "Still searching, it's quiet right now. More users = faster matches. "
            "Invite a friend, or stay in the queue and I'll connect you as soon as someone joins.",
            reply_markup=kb,
        )
    except asyncio.CancelledError:
        raise
    finally:
        if wait_tasks.get(user_id) is asyncio.current_task():
            wait_tasks.pop(user_id, None)


async def execute_search(user_id: int, chat_id: int):
    user = await users_col.find_one({"user_id": user_id}) or {}
    if user.get("banned"):
        await safe_send(chat_id, "your account is restricted")
        return
    if await find_chat(user_id):
        await safe_send(chat_id, "you're already in a chat")
        return
    if await queue_col.find_one({"user_id": user_id}):
        await safe_send(chat_id, "you're already searching...")
        return

    lang = user.get("language", "global")
    interest = user.get("interest", "any")

    # tier 1: language + interest, tier 2: language, tier 3: anyone
    tiers = []
    q1 = {"user_id": {"$ne": user_id}}
    if lang != "global":
        q1["language"] = {"$in": [lang, "global"]}
    if interest != "any":
        q1["interest"] = {"$in": [interest, "any"]}
    tiers.append(q1)
    if lang != "global":
        tiers.append({"user_id": {"$ne": user_id}, "language": {"$in": [lang, "global"]}})
    tiers.append({"user_id": {"$ne": user_id}})

    partner = None
    for q in tiers:
        partner = await queue_col.find_one_and_delete(q)
        if partner:
            break

    if partner:
        t = wait_tasks.pop(partner["user_id"], None)
        if t:
            t.cancel()
        await start_human_chat(user_id, partner["user_id"])
        return

    await queue_col.insert_one(
        {
            "user_id": user_id,
            "language": lang,
            "interest": interest,
            "joined_at": time.time(),
        }
    )
    online = await count_online()
    await safe_send(
        chat_id,
        f"🔍 Looking for someone...\n👥 {online} people active recently\n"
        "Use /stop to cancel.",
    )
    wait_tasks[user_id] = asyncio.create_task(nudge_waiting(user_id))


async def execute_stop(user_id: int, chat_id: int, reported: bool = False):
    t = wait_tasks.pop(user_id, None)
    if t:
        t.cancel()

    if await queue_col.find_one_and_delete({"user_id": user_id}):
        await safe_send(chat_id, "you left the waiting queue", reply_markup=search_keyboard())
        return

    chat = await chats_col.find_one_and_delete(
        {"$or": [{"user_1": user_id}, {"user_2": user_id}]}
    )
    if not chat:
        await safe_send(chat_id, "you're not in a chat", reply_markup=search_keyboard())
        return

    spawn(finish_chat(chat, user_id, reported=reported))


# =========================================================
# COMMANDS
# =========================================================

@router.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    existing = await users_col.find_one({"user_id": uid})
    await touch_user(message.from_user)

    # referral: /start ref_<id>
    arg = (command.args or "").strip()
    if not existing and arg.startswith("ref_"):
        try:
            ref = int(arg[4:])
            if ref != uid:
                await users_col.update_one({"user_id": uid}, {"$set": {"referred_by": ref}})
                r = await users_col.find_one_and_update(
                    {"user_id": ref}, {"$inc": {"referrals": 1}},
                    return_document=ReturnDocument.AFTER,
                )
                if r:
                    await safe_send(ref, f"🎉 A friend joined using your link! Total invites: {r.get('referrals', 1)}")
        except ValueError:
            pass

    gif_url = "https://media.giphy.com/media/l41lFw057lAJQMwg0/giphy.gif"
    try:
        await message.answer_animation(
            animation=gif_url, caption=WELCOME_TEXT, reply_markup=language_keyboard()
        )
    except Exception as e:
        logger.warning("Start animation failed: %s", e)
        await message.answer(WELCOME_TEXT, reply_markup=language_keyboard())


@router.message(Command("privacy"))
async def cmd_privacy(message: types.Message):
    await message.answer(PRIVACY_TEXT)


@router.message(Command("interest"))
async def cmd_interest(message: types.Message):
    await message.answer("Pick what you like to talk about:", reply_markup=interest_keyboard())


@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery):
    lang = callback.data.split("_", 1)[1]
    await touch_user(callback.from_user, language=lang)
    await callback.answer()
    await edit_or_answer(
        callback,
        f"Language: <b>{lang.title()}</b> ✅\n\nNow pick your interest:",
        interest_keyboard(),
    )


@router.callback_query(F.data.startswith("int_"))
async def set_interest(callback: CallbackQuery):
    key = callback.data.split("_", 1)[1]
    if key not in INTERESTS:
        await callback.answer()
        return
    await touch_user(callback.from_user, interest=key)
    await callback.answer()
    await edit_or_answer(
        callback,
        f"All set! Interest: <b>{INTERESTS[key]}</b>\n\nTap below to meet someone.",
        search_keyboard(),
    )


@router.message(Command("search"))
async def cmd_search(message: types.Message):
    await touch_user(message.from_user)
    await execute_search(message.from_user.id, message.chat.id)


@router.message(Command("stop"))
async def cmd_stop(message: types.Message):
    await execute_stop(message.from_user.id, message.chat.id)


@router.message(Command("report"))
async def cmd_report(message: types.Message):
    await execute_stop(message.from_user.id, message.chat.id, reported=True)


@router.message(Command("next"))
async def cmd_next(message: types.Message):
    await touch_user(message.from_user)
    await execute_stop(message.from_user.id, message.chat.id)
    await asyncio.sleep(random.uniform(0.6, 1.2))
    await execute_search(message.from_user.id, message.chat.id)


@router.callback_query(F.data == "action_search")
async def btn_search(callback: CallbackQuery):
    await callback.answer()
    await touch_user(callback.from_user)
    await execute_search(callback.from_user.id, callback.from_user.id)


@router.callback_query(F.data == "action_stop")
async def btn_stop(callback: CallbackQuery):
    await callback.answer()
    await execute_stop(callback.from_user.id, callback.from_user.id)


@router.callback_query(F.data == "action_report")
async def btn_report(callback: CallbackQuery):
    await callback.answer("Reported")
    await execute_stop(callback.from_user.id, callback.from_user.id, reported=True)


@router.callback_query(F.data == "action_next")
async def btn_next(callback: CallbackQuery):
    await callback.answer()
    await touch_user(callback.from_user)
    await execute_stop(callback.from_user.id, callback.from_user.id)
    await asyncio.sleep(random.uniform(0.6, 1.2))
    await execute_search(callback.from_user.id, callback.from_user.id)


# ---------------- Fun ----------------

@router.message(Command("fun"))
async def cmd_fun(message: types.Message):
    if not await find_chat(message.from_user.id):
        await message.answer("Start a chat first: /search")
        return
    await message.answer("Pick one, I'll send it to both of you:", reply_markup=fun_keyboard())


@router.callback_query(F.data == "fun_menu")
async def fun_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Pick one, I'll send it to both of you:", reply_markup=fun_keyboard())


@router.callback_query(F.data.startswith("fun_") & (F.data != "fun_menu"))
async def fun_pick(callback: CallbackQuery):
    kind = callback.data.split("_", 1)[1]
    if kind not in FUN_BANK:
        await callback.answer()
        return
    chat = await find_chat(callback.from_user.id)
    if not chat:
        await callback.answer("You're not in a chat", show_alert=True)
        return
    await callback.answer()
    title, bank = FUN_BANK[kind]
    text = f"{title}\n<b>{random.choice(bank)}</b>"
    for uid in (chat["user_1"], chat["user_2"]):
        await safe_send(uid, text)


# ---------------- Reveal (mutual, opt-in) ----------------

async def ask_reveal(user_id: int):
    chat = await find_chat(user_id)
    if not chat:
        await safe_send(user_id, "Start a chat first: /search")
        return
    partner = other_party(chat, user_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Share", callback_data=f"rev_yes:{user_id}"),
                InlineKeyboardButton(text="❌ No thanks", callback_data=f"rev_no:{user_id}"),
            ]
        ]
    )
    await safe_send(partner, "🤝 Your partner wants to swap Telegram usernames. Share?", reply_markup=kb)
    await safe_send(user_id, "Request sent. Usernames are shared only if your partner agrees.")


@router.message(Command("reveal"))
async def cmd_reveal(message: types.Message):
    await ask_reveal(message.from_user.id)


@router.callback_query(F.data == "rev_ask")
async def btn_reveal(callback: CallbackQuery):
    await callback.answer()
    await ask_reveal(callback.from_user.id)


@router.callback_query(F.data.startswith("rev_yes:") | F.data.startswith("rev_no:"))
async def reveal_answer(callback: CallbackQuery):
    action, requester = callback.data.split(":", 1)
    requester = int(requester)
    me = callback.from_user.id
    chat = await find_chat(me)
    await callback.answer()

    if not chat or other_party(chat, me) != requester:
        await edit_or_answer(callback, "That chat has ended.")
        return

    if action == "rev_no":
        await edit_or_answer(callback, "You declined.")
        await safe_send(requester, "Your partner prefers to stay anonymous.")
        return

    me_doc = await users_col.find_one({"user_id": me}) or {}
    req_doc = await users_col.find_one({"user_id": requester}) or {}

    def handle(doc):
        return f"@{html.escape(doc['username'])}" if doc.get("username") else "(no username set)"

    await edit_or_answer(callback, f"Shared! Their username: {handle(req_doc)}")
    await safe_send(requester, f"🤝 Your partner agreed. Their username: {handle(me_doc)}")


# ---------------- Reviews ----------------

@router.callback_query(F.data.startswith("rv:"))
async def on_review(callback: CallbackQuery):
    try:
        _, no, vote = callback.data.split(":", 2)
        no = int(no)
    except ValueError:
        await callback.answer()
        return
    if vote not in ("good", "bad"):
        await callback.answer()
        return

    ok = await record_vote(no, callback.from_user.id, vote)
    await callback.answer("Thanks!" if ok else "Already recorded")
    await edit_or_answer(callback, "Thanks for the feedback! 🙌", search_keyboard())


# ---------------- Stats / invite ----------------

@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    uid = message.from_user.id

    if uid == OWNER_ID:
        users = await users_col.count_documents({})
        online = await count_online()
        active = await chats_col.count_documents({})
        waiting = await queue_col.count_documents({})
        counter = await counters_col.find_one({"_id": "archive"}) or {}
        await message.answer(
            "📊 <b>Admin stats</b>\n"
            f"👤 Users: {users}\n🟢 Active (10 min): {online}\n"
            f"💬 Live chats: {active}\n⏳ Waiting: {waiting}\n"
            f"📁 Archived chats: {counter.get('n', 0)}"
        )
        return

    d = await users_col.find_one({"user_id": uid}) or {}
    await message.answer(
        "📊 <b>Your stats</b>\n"
        f"Chats: {d.get('chats', 0)}\n"
        f"Reputation: {badge(d)} (👍 {d.get('good', 0)} / 👎 {d.get('bad', 0)})\n"
        f"Friends invited: {d.get('referrals', 0)}"
    )


@router.message(Command("invite"))
async def cmd_invite(message: types.Message):
    link = f"https://t.me/{await get_username()}?start=ref_{message.from_user.id}"
    await message.answer(f"Invite friends with your link:\n{link}")


# ---------------- Owner tools ----------------

@router.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if message.from_user.id != OWNER_ID or not command.args:
        return
    try:
        target = int(command.args.strip())
    except ValueError:
        return
    await users_col.update_one({"user_id": target}, {"$set": {"banned": True}}, upsert=True)
    await message.answer(f"Banned {target}")


@router.message(Command("unban"))
async def cmd_unban(message: types.Message, command: CommandObject):
    if message.from_user.id != OWNER_ID or not command.args:
        return
    try:
        target = int(command.args.strip())
    except ValueError:
        return
    await users_col.update_one({"user_id": target}, {"$set": {"banned": False}})
    await message.answer(f"Unbanned {target}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if message.from_user.id != OWNER_ID or not command.args:
        return
    sent = 0
    async for u in users_col.find({"banned": {"$ne": True}}, {"user_id": 1}):
        if await safe_send(u["user_id"], command.args, parse_mode=None):
            sent += 1
        await asyncio.sleep(0.06)
    await message.answer(f"Broadcast delivered to {sent} users")


# =========================================================
# MESSAGE ROUTING
# =========================================================

@router.message()
async def handle_message(message: types.Message):
    if message.text and message.text.startswith("/"):
        return
    if not message.from_user:
        return

    user_id = message.from_user.id
    await users_col.update_one({"user_id": user_id}, {"$set": {"last_seen": time.time()}})

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

    try:
        await bot.copy_message(
            chat_id=partner_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except TelegramForbiddenError:
        gone = await chats_col.find_one_and_delete({"_id": chat["_id"]})
        if gone:
            await message.answer("your partner left the chat")
            spawn(finish_chat(gone, partner_id))   # archive what exists
        return
    except Exception as e:
        logger.warning("Routing error: %s", e)
        await message.answer("couldn't send that one, try again")
        return

    # log reference for the archive (bounded per chat)
    try:
        updated = await chats_col.find_one_and_update(
            {"_id": chat["_id"]},
            {"$inc": {"msg_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if updated and updated.get("msg_count", 0) <= MAX_LOGGED_MESSAGES:
            await msgs_col.insert_one(
                {
                    "chat_key": str(chat["_id"]),
                    "from_chat": message.chat.id,
                    "message_id": message.message_id,
                    "sender": user_id,
                }
            )
    except Exception as e:
        logger.warning("log ref failed: %s", e)


# =========================================================
# GROUP / CHANNEL PROTECTION
# =========================================================

@router.my_chat_member()
async def on_added_somewhere(event: types.ChatMemberUpdated):
    """If anyone adds the bot to a group or channel, leave immediately."""
    if event.chat.type != "private" and event.new_chat_member.status in (
        "member",
        "administrator",
    ):
        try:
            await bot.leave_chat(event.chat.id)
            logger.info("Left unauthorized chat %s", event.chat.id)
        except Exception as e:
            logger.warning("leave_chat failed: %s", e)


# =========================================================
# HEALTH / KEEP-ALIVE
# =========================================================

async def ping(request: web.Request):
    return web.Response(
        text="OK", status=200, content_type="text/plain",
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
# MAIN
# =========================================================

async def shutdown():
    logger.info("Shutting down ShadowChatter...")
    pending = list(wait_tasks.values()) + list(bg_tasks)
    for t in pending:
        if not t.done():
            t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
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
    try:
        await set_bot_commands()
        dp.include_router(router)
        await bot.delete_webhook(drop_pending_updates=True)

        http_runner = await start_http_server()

        # clean state from a previous run
        await queue_col.delete_many({})
        await reviews_col.create_index("created_at", expireAfterSeconds=86400)

        logger.info("ShadowChatter started successfully")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    except asyncio.CancelledError:
        logger.info("Main task cancelled")
    except Exception as e:
        logger.exception("Fatal application error: %s", e)
        raise
    finally:
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
