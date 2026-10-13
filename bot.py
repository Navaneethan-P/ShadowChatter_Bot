import asyncio
import html
import logging
import os
import random
import re
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
    BotCommandScopeChat,
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

# Your own numeric Telegram ID (from @userinfobot). Only this account
# can use the admin commands and receive chat archives.
OWNER_ID = int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID") else None

# 1 = also push the full replay to your DM the moment a chat ends
AUTO_REPLAY = os.environ.get("AUTO_REPLAY", "0") == "1"

PORT = int(os.environ.get("PORT", "10000"))

MAX_LOGGED_MESSAGES = 1000
IST = timezone(timedelta(hours=5, minutes=30))

if not TOKEN:
    raise SystemExit("CRITICAL ERROR: BOT_TOKEN is missing")
if not MONGO_URI:
    raise SystemExit("CRITICAL ERROR: MONGO_URI is missing")


# =========================================================
# LOGGING / CLIENTS
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("shadowchatter")
if not OWNER_ID:
    logger.warning("OWNER_ID is not set: admin tools are disabled")

bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
router.message.filter(F.chat.type == "private")   # groups are ignored

mongo = AsyncMongoClient(MONGO_URI)
db = mongo.anon_chat_db

queue_col = db.queue
chats_col = db.active_chats
users_col = db.users
msgs_col = db.chat_msgs        # temporary log of the live chat
reviews_col = db.pending_reviews
counters_col = db.counters
archives_col = db.archives     # finished chats, opened with /openchat_N

_bot_username = None


def is_owner(uid: int) -> bool:
    return OWNER_ID is not None and uid == OWNER_ID


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
    "• Be kind. Reported or badly rated users are blocked."
)

SAFETY_TEXT = (
    "🛡 <b>Staying safe here</b>\n"
    "• Your name, number and username are never shown to partners.\n"
    "• Don't share your phone number, address, OTPs or money details.\n"
    "• Tap 🚩 Report (or /report) if anyone makes you uncomfortable: "
    "the chat is reviewed and the user can be blocked.\n"
    "• /next skips instantly, /stop ends the chat.\n"
    "• Share your username only with 🤝 Reveal, which needs both of you to agree."
)

WELCOME_TEXT = (
    "<b>Welcome to Shadow Chatter 🕶</b>\n\n"
    "Meet random people anonymously: text, photos, GIFs, voice, plus games "
    "and music to play together.\n\n"
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
    "Describe your day using only emojis.",
    "Type a message with your eyes closed and send it.",
    "Say something genuinely nice about your partner.",
    "Share your most-played song right now.",
    "Make up a 2-line poem about this chat.",
    "Send a voice note saying 'hello' in your funniest voice.",
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

MUSIC_MOODS = [
    ("🎶 Tamil Melody", "tamil melody songs"),
    ("🔥 Tamil Kuthu", "tamil kuthu hits"),
    ("🎧 Lo-fi Chill", "lofi chill beats"),
    ("🎤 English Pop", "english pop hits"),
    ("📻 90s Tamil", "90s tamil songs"),
    ("🎸 Indie / Rock", "indie rock songs"),
]


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
                InlineKeyboardButton(text="🎮 Games", callback_data="game_menu"),
                InlineKeyboardButton(text="🎵 Music", callback_data="music_menu"),
            ],
            [
                InlineKeyboardButton(text="🤝 Reveal", callback_data="rev_ask"),
                InlineKeyboardButton(text="🚩 Report", callback_data="action_report"),
            ],
        ]
    )


def games_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⭕ Tic-Tac-Toe", callback_data="ttt_start"),
                InlineKeyboardButton(text="✊ Rock-Paper-Scissors", callback_data="rps_start"),
            ],
            [
                InlineKeyboardButton(text="🎲 Dice duel", callback_data="dice_d"),
                InlineKeyboardButton(text="🎯 Darts", callback_data="dice_t"),
                InlineKeyboardButton(text="🎳 Bowling", callback_data="dice_b"),
            ],
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


PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Start / change language"),
    BotCommand(command="search", description="Find a partner"),
    BotCommand(command="next", description="Skip to next partner"),
    BotCommand(command="stop", description="End chat"),
    BotCommand(command="games", description="Play games with your partner"),
    BotCommand(command="music", description="Listen to music together"),
    BotCommand(command="reveal", description="Share usernames (both must agree)"),
    BotCommand(command="interest", description="Pick your interest"),
    BotCommand(command="stats", description="Your stats"),
    BotCommand(command="invite", description="Invite friends"),
    BotCommand(command="safety", description="Safety tips"),
    BotCommand(command="report", description="Report your partner"),
    BotCommand(command="privacy", description="How data is handled"),
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand(command="admin", description="Admin help"),
    BotCommand(command="chats", description="List saved chats"),
    BotCommand(command="block", description="Block a user (@username or id)"),
    BotCommand(command="unblock", description="Unblock a user"),
    BotCommand(command="whois", description="Look up a user"),
    BotCommand(command="broadcast", description="Message all users"),
]

ADMIN_HELP = (
    "🛠 <b>Admin commands</b>\n"
    "/chats : last 15 saved chats (add <code>bad</code>, <code>good</code> or "
    "<code>unrated</code> to filter)\n"
    "/openchat_07 (or <code>/openchat 7</code>) : replay the full chat\n"
    "/deletechat_07 : delete a saved chat\n"
    "/block @username or id : block a user (ends their chat)\n"
    "/unblock @username or id\n"
    "/whois @username or id : profile, rating, status\n"
    "/stats : bot numbers\n"
    "/broadcast text : message every user"
)


async def set_bot_commands():
    await bot.set_my_commands(PUBLIC_COMMANDS)
    if OWNER_ID:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=OWNER_ID)
            )
        except Exception as e:
            logger.warning("owner commands not set (open the bot and /start first): %s", e)


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
                "username_lower": (user.username or "").lower(),
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
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


async def edit_or_answer(callback: CallbackQuery, text: str, kb=None):
    try:
        if callback.message.caption is not None or callback.message.animation:
            await callback.message.edit_caption(caption=text, reply_markup=kb)
        else:
            await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)


async def resolve_user(arg: str):
    """'@name', 'name' or numeric id -> user_id (or None)."""
    arg = (arg or "").strip()
    if not arg:
        return None
    if arg.lstrip("-").isdigit():
        return int(arg)
    doc = await users_col.find_one({"username_lower": arg.lstrip("@").lower()})
    return doc["user_id"] if doc else None


bg_tasks: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)
    return t


wait_tasks: dict[int, asyncio.Task] = {}


# =========================================================
# MESSAGE LOG  (what we keep so /openchat can replay it)
# =========================================================

def extract_content(m: types.Message):
    """-> (type, text, file_id). file_ids let the bot re-send media later."""
    text = m.text or m.caption or ""
    if m.text:
        return "text", text, None
    if m.animation:
        return "animation", text, m.animation.file_id
    if m.photo:
        return "photo", text, m.photo[-1].file_id
    if m.video:
        return "video", text, m.video.file_id
    if m.voice:
        return "voice", text, m.voice.file_id
    if m.audio:
        return "audio", text, m.audio.file_id
    if m.video_note:
        return "video_note", text, m.video_note.file_id
    if m.sticker:
        return "sticker", m.sticker.emoji or "", m.sticker.file_id
    if m.document:
        return "document", text, m.document.file_id
    return "other", text, None


CAPTION_TYPES = {"photo", "video", "animation", "voice", "audio", "document"}


async def send_logged(chat_id: int, m: dict, label: str):
    t = m["t"]
    text = m.get("x") or ""
    try:
        if t == "text":
            await tg_call(bot.send_message, chat_id, f"{label} {html.escape(text)}")
        elif t in CAPTION_TYPES and m.get("f"):
            cap = html.escape(f"{label} {text}".strip())
            await tg_call(getattr(bot, f"send_{t}"), chat_id, **{t: m["f"]}, caption=cap)
        elif t in ("sticker", "video_note") and m.get("f"):
            await tg_call(bot.send_message, chat_id, f"{label} [{t}]")
            await tg_call(getattr(bot, f"send_{t}"), chat_id, **{t: m["f"]})
        else:
            await tg_call(bot.send_message, chat_id, f"{label} [unsupported content]")
    except Exception as e:
        logger.warning("replay item failed: %s", e)
        await safe_send(chat_id, f"{label} [could not load {t}]")


TAG_LABEL = {"good": "🟢 GOOD", "bad": "🔴 BAD", "unrated": "⚪ UNRATED"}


def archive_header(a: dict) -> str:
    users = a["users"]
    info = a["info"]
    return (
        f"📁 <b>Chat-{a['_id']:02d}</b> · {TAG_LABEL.get(a.get('tag', 'unrated'))}\n"
        f"🗓 {a['date_text']} IST\n"
        f"⏱ {fmt_duration(a['duration'])} · {a['count']} messages\n\n"
        f"🅰️ {who(info['a'], users[0])}\n"
        f"🅱️ {who(info['b'], users[1])}"
        + (f"\n🚩 reported by <code>{a['reported_by']}</code>" if a.get("reported_by") else "")
    )


def archive_admin_keyboard(a: dict):
    u = a["users"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⛔ Block 🅰️", callback_data=f"adm_block:{u[0]}"),
                InlineKeyboardButton(text="⛔ Block 🅱️", callback_data=f"adm_block:{u[1]}"),
            ]
        ]
    )


async def replay_archive(chat_id: int, a: dict):
    await safe_send(chat_id, archive_header(a), reply_markup=archive_admin_keyboard(a))
    for m in a["messages"]:
        label = "🅰️" if m["s"] == "a" else "🅱️"
        await send_logged(chat_id, m, label)
    await safe_send(chat_id, f"🏁 End of Chat-{a['_id']:02d}")


# =========================================================
# ARCHIVE
# =========================================================

async def archive_chat(chat: dict) -> int | None:
    chat_key = str(chat["_id"])
    try:
        refs = await msgs_col.find({"chat_key": chat_key}).sort("_id", 1).to_list(None)
        if not refs:
            return None

        counter = await counters_col.find_one_and_update(
            {"_id": "archive"},
            {"$inc": {"n": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        number = counter["n"]

        a, b = chat["user_1"], chat["user_2"]
        doc_a = await users_col.find_one({"user_id": a}) or {}
        doc_b = await users_col.find_one({"user_id": b}) or {}
        now = datetime.now(IST)

        archive = {
            "_id": number,
            "users": [a, b],
            "info": {
                "a": {"first_name": doc_a.get("first_name"), "username": doc_a.get("username")},
                "b": {"first_name": doc_b.get("first_name"), "username": doc_b.get("username")},
            },
            "date_text": now.strftime("%d %b %Y, %I:%M %p"),
            "created_at": time.time(),
            "duration": time.time() - chat.get("created_at", time.time()),
            "count": len(refs),
            "tag": "unrated",
            "messages": [
                {
                    "s": "a" if r["sender"] == a else "b",
                    "t": r["type"],
                    "x": r.get("text", ""),
                    "f": r.get("file_id"),
                }
                for r in refs
            ],
        }
        await archives_col.insert_one(archive)

        if OWNER_ID:
            await safe_send(
                OWNER_ID,
                archive_header(archive) + f"\n\n▶️ Open: /openchat_{number:02d}",
                reply_markup=archive_admin_keyboard(archive),
            )
            if AUTO_REPLAY:
                for m in archive["messages"]:
                    await send_logged(OWNER_ID, m, "🅰️" if m["s"] == "a" else "🅱️")
                await safe_send(OWNER_ID, f"🏁 End of Chat-{number:02d}")
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

    votes = list(review.get("votes", {}).values())
    tag = "bad" if "bad" in votes else ("good" if "good" in votes else "unrated")
    update = {"tag": tag}
    if reported:
        update["reported_by"] = voter
    await archives_col.update_one({"_id": chat_no}, {"$set": update})

    target = [u for u in review["users"] if u != voter][0]
    updated = await users_col.find_one_and_update(
        {"user_id": target},
        {"$inc": {"good" if vote == "good" else "bad": 1}},
        return_document=ReturnDocument.AFTER,
    )

    icon = "🚩" if reported else ("👍" if vote == "good" else "👎")
    voter_doc = await users_col.find_one({"user_id": voter})
    target_doc = updated or {}
    await notify_owner(
        f"{icon} <b>Chat-{chat_no:02d}</b>: {who(voter_doc, voter)}\n"
        f"{'reported' if reported else 'rated'} partner {who(target_doc, target)} "
        f"as <b>{vote.upper()}</b>\n▶️ /openchat_{chat_no:02d}"
    )

    bad, good = target_doc.get("bad", 0), target_doc.get("good", 0)
    if bad >= 5 and bad > 2 * good and not target_doc.get("banned"):
        await block_user(target, auto=True)
    return True


async def finish_chat(chat: dict, ender_id: int, reported: bool = False):
    """Chat doc already deleted. Archive, notify, ask for reviews."""
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
        await safe_send(ender_id, "How was your partner?", reply_markup=review_keyboard(number))
    await safe_send(partner_id, "How was your partner?", reply_markup=review_keyboard(number))


# =========================================================
# BLOCKING
# =========================================================

async def block_user(target: int, auto: bool = False) -> str:
    await users_col.update_one({"user_id": target}, {"$set": {"banned": True}}, upsert=True)

    t = wait_tasks.pop(target, None)
    if t:
        t.cancel()
    await queue_col.delete_many({"user_id": target})

    chat = await chats_col.find_one_and_delete(
        {"$or": [{"user_1": target}, {"user_2": target}]}
    )
    if chat:
        spawn(finish_chat(chat, target))

    await safe_send(target, "⛔ Your account has been restricted for breaking the rules.")
    doc = await users_col.find_one({"user_id": target})
    msg = f"⛔ {'Auto-blocked' if auto else 'Blocked'} {who(doc, target)}"
    if auto:
        await notify_owner(msg)
    return msg


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
            "Try 🎮 Games or 🎵 Music if you need something to do together."
        )

    await safe_send(user_a, intro(doc_b), reply_markup=chat_keyboard())
    await safe_send(user_b, intro(doc_a), reply_markup=chat_keyboard())


async def nudge_waiting(user_id: int):
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
        {"user_id": user_id, "language": lang, "interest": interest, "joined_at": time.time()}
    )
    online = await count_online()
    await safe_send(
        chat_id,
        f"🔍 Looking for someone...\n👥 {online} people active recently\nUse /stop to cancel.",
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
# USER COMMANDS
# =========================================================

@router.message(Command("start"))
async def cmd_start(message: types.Message, command: CommandObject):
    uid = message.from_user.id
    existing = await users_col.find_one({"user_id": uid})
    await touch_user(message.from_user)

    arg = (command.args or "").strip()
    if not existing and arg.startswith("ref_"):
        try:
            ref = int(arg[4:])
            if ref != uid:
                await users_col.update_one({"user_id": uid}, {"$set": {"referred_by": ref}})
                r = await users_col.find_one_and_update(
                    {"user_id": ref},
                    {"$inc": {"referrals": 1}},
                    return_document=ReturnDocument.AFTER,
                )
                if r:
                    await safe_send(
                        ref,
                        f"🎉 A friend joined using your link! Total invites: {r.get('referrals', 1)}",
                    )
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


@router.message(Command("safety"))
async def cmd_safety(message: types.Message):
    await message.answer(SAFETY_TEXT)


@router.message(Command("interest"))
async def cmd_interest(message: types.Message):
    await message.answer("Pick what you like to talk about:", reply_markup=interest_keyboard())


@router.callback_query(F.data.startswith("lang_"))
async def set_language(callback: CallbackQuery):
    lang = callback.data.split("_", 1)[1]
    await touch_user(callback.from_user, language=lang)
    await callback.answer()
    await edit_or_answer(
        callback, f"Language: <b>{lang.title()}</b> ✅\n\nNow pick your interest:", interest_keyboard()
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


# =========================================================
# GAMES, MUSIC, FUN
# =========================================================

GAMES_PROMPT = "🎮 Pick something to do together:"


@router.message(Command("games"))
@router.message(Command("fun"))
async def cmd_games(message: types.Message):
    if not await find_chat(message.from_user.id):
        await message.answer("Start a chat first: /search")
        return
    await message.answer(GAMES_PROMPT, reply_markup=games_keyboard())


@router.callback_query(F.data == "game_menu")
async def games_menu(callback: CallbackQuery):
    await callback.answer()
    if not await find_chat(callback.from_user.id):
        return
    await callback.message.answer(GAMES_PROMPT, reply_markup=games_keyboard())


async def get_pair(callback: CallbackQuery):
    """Return (chat, partner_id) or answer an alert and return (None, None)."""
    chat = await find_chat(callback.from_user.id)
    if not chat:
        await callback.answer("You're not in a chat", show_alert=True)
        return None, None
    return chat, other_party(chat, callback.from_user.id)


# ---------- prompts ----------

@router.callback_query(F.data.in_({"fun_ice", "fun_truth", "fun_dare", "fun_wyr"}))
async def fun_pick(callback: CallbackQuery):
    chat, _ = await get_pair(callback)
    if not chat:
        return
    await callback.answer()
    title, bank = FUN_BANK[callback.data.split("_", 1)[1]]
    text = f"{title}\n<b>{random.choice(bank)}</b>"
    for uid in (chat["user_1"], chat["user_2"]):
        await safe_send(uid, text)


# ---------- music ----------

async def send_music_menu(user_id: int):
    chat = await find_chat(user_id)
    if not chat:
        await safe_send(user_id, "Start a chat first: /search")
        return
    rows = []
    for i in range(0, len(MUSIC_MOODS), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    url="https://www.youtube.com/results?search_query=" + q.replace(" ", "+"),
                )
                for label, q in MUSIC_MOODS[i : i + 2]
            ]
        )
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    text = (
        "🎵 <b>Listen together</b>\n"
        "Pick a vibe, press play at the same time, then tell each other what you think."
    )
    for uid in (chat["user_1"], chat["user_2"]):
        await safe_send(uid, text, reply_markup=kb)


@router.message(Command("music"))
async def cmd_music(message: types.Message):
    await send_music_menu(message.from_user.id)


@router.callback_query(F.data == "music_menu")
async def btn_music(callback: CallbackQuery):
    await callback.answer()
    await send_music_menu(callback.from_user.id)


# ---------- dice duel ----------

DICE_KINDS = {"dice_d": "🎲", "dice_t": "🎯", "dice_b": "🎳"}
dice_busy: set = set()


@router.callback_query(F.data.in_(set(DICE_KINDS)))
async def dice_game(callback: CallbackQuery):
    chat, _ = await get_pair(callback)
    if not chat:
        return
    key = str(chat["_id"])
    if key in dice_busy:
        await callback.answer("Round in progress", show_alert=False)
        return
    await callback.answer()
    dice_busy.add(key)
    emoji = DICE_KINDS[callback.data]
    try:
        a, b = chat["user_1"], chat["user_2"]
        ma = await tg_call(bot.send_dice, a, emoji=emoji)
        mb = await tg_call(bot.send_dice, b, emoji=emoji)
        va, vb = ma.dice.value, mb.dice.value
        await asyncio.sleep(4)

        if va == vb:
            ra = rb = f"🤝 Tie! Both scored {va}."
        else:
            ra = f"{'🏆 You win!' if va > vb else '😅 You lost.'} You {va} · Partner {vb}"
            rb = f"{'🏆 You win!' if vb > va else '😅 You lost.'} You {vb} · Partner {va}"
        again = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔁 Again", callback_data=callback.data)]]
        )
        await safe_send(a, ra, reply_markup=again)
        await safe_send(b, rb, reply_markup=again)
    except Exception as e:
        logger.warning("dice failed: %s", e)
    finally:
        dice_busy.discard(key)


# ---------- rock paper scissors ----------

RPS_NAME = {"r": "✊ Rock", "p": "✋ Paper", "s": "✌️ Scissors"}
RPS_BEATS = {"r": "s", "s": "p", "p": "r"}


@router.callback_query(F.data == "rps_start")
async def rps_start(callback: CallbackQuery):
    chat, _ = await get_pair(callback)
    if not chat:
        return
    if chat.get("rps"):
        await callback.answer("Round in progress", show_alert=False)
        return
    await callback.answer()
    await chats_col.update_one({"_id": chat["_id"]}, {"$set": {"rps": {"on": True}}})
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=RPS_NAME["r"], callback_data="rps_r"),
                InlineKeyboardButton(text=RPS_NAME["p"], callback_data="rps_p"),
                InlineKeyboardButton(text=RPS_NAME["s"], callback_data="rps_s"),
            ]
        ]
    )
    for uid in (chat["user_1"], chat["user_2"]):
        await safe_send(
            uid,
            "✊✋✌️ <b>Rock Paper Scissors</b>\nChoose, picks stay hidden until you both lock in:",
            reply_markup=kb,
        )


@router.callback_query(F.data.in_({"rps_r", "rps_p", "rps_s"}))
async def rps_pick(callback: CallbackQuery):
    chat, _ = await get_pair(callback)
    if not chat:
        return
    uid = callback.from_user.id
    rps = chat.get("rps")
    if not rps:
        await callback.answer("This round is over", show_alert=False)
        return
    if f"c{uid}" in rps:
        await callback.answer("Already locked in", show_alert=False)
        return

    choice = callback.data[-1]
    updated = await chats_col.find_one_and_update(
        {"_id": chat["_id"]},
        {"$set": {f"rps.c{uid}": choice}},
        return_document=ReturnDocument.AFTER,
    )
    await callback.answer("Locked in ✅")
    picks = {k: v for k, v in (updated.get("rps") or {}).items() if k.startswith("c")}

    if len(picks) < 2:
        await edit_or_answer(callback, "Locked in ✅ Waiting for your partner...")
        return

    await chats_col.update_one({"_id": chat["_id"]}, {"$unset": {"rps": ""}})
    a, b = chat["user_1"], chat["user_2"]
    ca, cb = picks[f"c{a}"], picks[f"c{b}"]

    def result(mine, theirs):
        if mine == theirs:
            return "🤝 Draw!"
        return "🏆 You win!" if RPS_BEATS[mine] == theirs else "😅 You lost."

    again = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔁 Play again", callback_data="rps_start")]]
    )
    await safe_send(a, f"You: {RPS_NAME[ca]}\nPartner: {RPS_NAME[cb]}\n<b>{result(ca, cb)}</b>", reply_markup=again)
    await safe_send(b, f"You: {RPS_NAME[cb]}\nPartner: {RPS_NAME[ca]}\n<b>{result(cb, ca)}</b>", reply_markup=again)


# ---------- tic-tac-toe ----------

TTT_SYM = {"x": "❌", "o": "⭕", "-": "▫️"}
TTT_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
]


def ttt_winner(board: str):
    for a, b, c in TTT_LINES:
        if board[a] != "-" and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if "-" not in board else None


def ttt_render(state: dict, viewer: int):
    board = state["board"]
    players = state["players"]               # {"x": uid, "o": uid}
    mine = "x" if players["x"] == viewer else "o"
    result = ttt_winner(board)

    if result == "draw":
        status = "🤝 Draw!"
    elif result:
        status = "🏆 You win!" if result == mine else "😅 You lost."
    else:
        status = "Your turn" if state["turn"] == viewer else "Partner's turn..."

    text = f"⭕❌ <b>Tic-Tac-Toe</b>\nYou are {TTT_SYM[mine]}\n<b>{status}</b>"
    rows = [
        [
            InlineKeyboardButton(text=TTT_SYM[board[r * 3 + c]], callback_data=f"tm:{r * 3 + c}")
            for c in range(3)
        ]
        for r in range(3)
    ]
    if result:
        rows.append([InlineKeyboardButton(text="🔁 Play again", callback_data="ttt_start")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "ttt_start")
async def ttt_start(callback: CallbackQuery):
    chat, partner = await get_pair(callback)
    if not chat:
        return
    if chat.get("ttt"):
        await callback.answer("A game is already running", show_alert=False)
        return
    await callback.answer("Invite sent")
    me = callback.from_user.id
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Play", callback_data=f"ttt_yes:{me}"),
                InlineKeyboardButton(text="❌ No", callback_data=f"ttt_no:{me}"),
            ]
        ]
    )
    await safe_send(partner, "⭕❌ Your partner wants to play <b>Tic-Tac-Toe</b>!", reply_markup=kb)
    await safe_send(me, "Invite sent, waiting for your partner...")


@router.callback_query(F.data.startswith("ttt_no:"))
async def ttt_decline(callback: CallbackQuery):
    starter = int(callback.data.split(":", 1)[1])
    await callback.answer()
    await edit_or_answer(callback, "You declined the game.")
    await safe_send(starter, "Your partner skipped the game. Try another one 🎮")


@router.callback_query(F.data.startswith("ttt_yes:"))
async def ttt_accept(callback: CallbackQuery):
    starter = int(callback.data.split(":", 1)[1])
    me = callback.from_user.id
    chat = await find_chat(me)
    if not chat or other_party(chat, me) != starter or chat.get("ttt"):
        await callback.answer("Not available anymore", show_alert=True)
        return
    await callback.answer()
    await edit_or_answer(callback, "Game on! 🎮")

    state = {
        "board": "-" * 9,
        "turn": starter,
        "players": {"x": starter, "o": me},
        "msgs": {},
    }
    for uid in (starter, me):
        text, kb = ttt_render(state, uid)
        sent = await safe_send(uid, text, reply_markup=kb)
        if sent:
            state["msgs"][str(uid)] = sent.message_id
    await chats_col.update_one({"_id": chat["_id"]}, {"$set": {"ttt": state}})


@router.callback_query(F.data.startswith("tm:"))
async def ttt_move(callback: CallbackQuery):
    chat, _ = await get_pair(callback)
    if not chat:
        return
    state = chat.get("ttt")
    uid = callback.from_user.id
    if not state:
        await callback.answer("Game over", show_alert=False)
        return
    if state["turn"] != uid:
        await callback.answer("Wait for your turn", show_alert=False)
        return

    idx = int(callback.data.split(":", 1)[1])
    board = state["board"]
    if board[idx] != "-":
        await callback.answer("Taken!", show_alert=False)
        return

    mark = "x" if state["players"]["x"] == uid else "o"
    board = board[:idx] + mark + board[idx + 1 :]
    state["board"] = board
    state["turn"] = other_party(chat, uid)

    if ttt_winner(board):
        await chats_col.update_one({"_id": chat["_id"]}, {"$unset": {"ttt": ""}})
    else:
        await chats_col.update_one({"_id": chat["_id"]}, {"$set": {"ttt": state}})
    await callback.answer()

    for pid_str, mid in state["msgs"].items():
        text, kb = ttt_render(state, int(pid_str))
        try:
            await bot.edit_message_text(
                text, chat_id=int(pid_str), message_id=mid, reply_markup=kb
            )
        except Exception as e:
            logger.debug("ttt edit failed: %s", e)


# =========================================================
# REVEAL (mutual, opt-in)
# =========================================================

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


# =========================================================
# REVIEWS / STATS / INVITE
# =========================================================

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


@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    uid = message.from_user.id

    if is_owner(uid):
        users = await users_col.count_documents({})
        online = await count_online()
        active = await chats_col.count_documents({})
        waiting = await queue_col.count_documents({})
        banned = await users_col.count_documents({"banned": True})
        counter = await counters_col.find_one({"_id": "archive"}) or {}
        await message.answer(
            "📊 <b>Admin stats</b>\n"
            f"👤 Users: {users}\n🟢 Active (10 min): {online}\n"
            f"💬 Live chats: {active}\n⏳ Waiting: {waiting}\n"
            f"⛔ Blocked: {banned}\n📁 Saved chats: {counter.get('n', 0)}"
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


# =========================================================
# ADMIN TOOLS (owner only)
# =========================================================

@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if is_owner(message.from_user.id):
        await message.answer(ADMIN_HELP)


@router.message(Command("chats"))
async def cmd_chats(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        return
    flt = (command.args or "").strip().lower()
    query = {"tag": flt} if flt in ("good", "bad", "unrated") else {}
    docs = await archives_col.find(query, {"messages": 0}).sort("_id", -1).to_list(15)
    if not docs:
        await message.answer("No saved chats yet.")
        return

    lines = []
    for a in docs:
        n = a["_id"]
        ia, ib = a["info"]["a"], a["info"]["b"]

        def nm(i):
            return f"@{html.escape(i['username'])}" if i.get("username") else html.escape(i.get("first_name") or "?")

        lines.append(
            f"{TAG_LABEL.get(a.get('tag', 'unrated'))} · <b>Chat-{n:02d}</b> · {a['date_text']}\n"
            f"   {nm(ia)} ↔ {nm(ib)} · {a['count']} msgs\n"
            f"   ▶️ /openchat_{n:02d}"
        )
    await message.answer("\n\n".join(lines))


@router.message(F.text.regexp(r"^/openchat"))
async def cmd_openchat(message: types.Message):
    if not is_owner(message.from_user.id):
        return
    m = re.match(r"^/openchat[_\s\-]*(\d+)", message.text)
    if not m:
        await message.answer("Usage: /openchat_07  or  /openchat 7")
        return
    n = int(m.group(1))
    a = await archives_col.find_one({"_id": n})
    if not a:
        await message.answer(f"Chat-{n:02d} not found. Use /chats to list saved chats.")
        return
    await replay_archive(message.chat.id, a)


@router.message(F.text.regexp(r"^/deletechat"))
async def cmd_deletechat(message: types.Message):
    if not is_owner(message.from_user.id):
        return
    m = re.match(r"^/deletechat[_\s\-]*(\d+)", message.text)
    if not m:
        await message.answer("Usage: /deletechat_07")
        return
    n = int(m.group(1))
    res = await archives_col.delete_one({"_id": n})
    await message.answer(f"Chat-{n:02d} deleted." if res.deleted_count else "Not found.")


@router.message(Command("block"))
async def cmd_block(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        return
    target = await resolve_user(command.args)
    if not target:
        await message.answer("Usage: /block @username  or  /block 123456789")
        return
    if is_owner(target):
        await message.answer("You can't block yourself.")
        return
    await message.answer(await block_user(target))


@router.message(Command("unblock"))
async def cmd_unblock(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        return
    target = await resolve_user(command.args)
    if not target:
        await message.answer("Usage: /unblock @username  or  /unblock 123456789")
        return
    await users_col.update_one({"user_id": target}, {"$set": {"banned": False}})
    await safe_send(target, "✅ Your account has been restored. You can use /search again.")
    await message.answer(f"✅ Unblocked <code>{target}</code>")


@router.message(Command("whois"))
async def cmd_whois(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        return
    target = await resolve_user(command.args)
    d = await users_col.find_one({"user_id": target}) if target else None
    if not d:
        await message.answer("User not found.")
        return
    await message.answer(
        f"👤 {who(d, d['user_id'])}\n"
        f"Status: {'⛔ BLOCKED' if d.get('banned') else '✅ active'}\n"
        f"Language: {d.get('language', '-')} · Interest: {d.get('interest', '-')}\n"
        f"Chats: {d.get('chats', 0)} · 👍 {d.get('good', 0)} · 👎 {d.get('bad', 0)} · {badge(d)}\n"
        f"Invited: {d.get('referrals', 0)}"
    )


@router.callback_query(F.data.startswith("adm_block:"))
async def cb_block(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer()
        return
    target = int(callback.data.split(":", 1)[1])
    await callback.answer("Blocked")
    await safe_send(callback.from_user.id, await block_user(target))


@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id) or not command.args:
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

PERSONAL_INFO = re.compile(r"(\+?\d[\d\s\-]{8,}\d)|(https?://)|(t\.me/)", re.IGNORECASE)


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
            spawn(finish_chat(gone, partner_id))
        return
    except Exception as e:
        logger.warning("Routing error: %s", e)
        await message.answer("couldn't send that one, try again")
        return

    # keep the log for /openchat
    try:
        updated = await chats_col.find_one_and_update(
            {"_id": chat["_id"]},
            {"$inc": {"msg_count": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if updated and updated.get("msg_count", 0) <= MAX_LOGGED_MESSAGES:
            mtype, text, file_id = extract_content(message)
            await msgs_col.insert_one(
                {
                    "chat_key": str(chat["_id"]),
                    "sender": user_id,
                    "type": mtype,
                    "text": text,
                    "file_id": file_id,
                }
            )
    except Exception as e:
        logger.warning("log failed: %s", e)

    # one gentle privacy reminder per user per chat
    text = message.text or message.caption or ""
    if text and PERSONAL_INFO.search(text):
        res = await chats_col.update_one(
            {"_id": chat["_id"], "warned": {"$ne": user_id}},
            {"$addToSet": {"warned": user_id}},
        )
        if res.modified_count:
            await safe_send(
                user_id,
                "🛡 Tip: keep your phone number, links and socials private. "
                "You can safely swap usernames with 🤝 Reveal if you both agree.",
            )


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
# HEALTH / MAIN
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
