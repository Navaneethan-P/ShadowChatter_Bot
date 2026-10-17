"""
SHADOW CHATTER v4: anonymous chat + dating
-------------------------------------------
* 18+ gate, gender + "looking for" matching (no language screen)
* Timepass: anonymous 1-to-1 chat, games, music, mutual /reveal
* Dating: 2-minute profile (photos, name, age, city, bio), Discover, likes,
  admirers, mutual match -> usernames revealed
* AI buddies (Baka, Kavi, Maya): always labelled as AI, optional
* Dynamic messages, GIF/sticker pool the admin fills with /addmedia,
  message effects, animated search screen
* Admin: /chats /openchat_N /block /whois /profileof /addmedia /stats ...
  Finished chats, new dating profiles, reports and matches go to the owner.
"""

import asyncio
import html
import logging
import os
import random
import re
import string
import time
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router, types
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
    InputMediaPhoto,
)
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

try:
    from openai import AsyncOpenAI
except ImportError:          # AI buddies simply stay disabled
    AsyncOpenAI = None


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
OWNER_ID = int(os.environ["OWNER_ID"]) if os.environ.get("OWNER_ID") else None
AUTO_REPLAY = os.environ.get("AUTO_REPLAY", "0") == "1"
PORT = int(os.environ.get("PORT", "10000"))

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
AI_MODELS = [
    m.strip()
    for m in os.environ.get(
        "AI_MODELS", "stealth/space-bunny-alpha,meta-llama/llama-3.3-70b-instruct:free"
    ).split(",")
    if m.strip()
]
AI_TIMEOUT = 25

MAX_LOGGED_MESSAGES = 1000
IST = timezone(timedelta(hours=5, minutes=30))

LIKES_BASE = 25                 # daily likes
LIKES_PER_REFERRAL = 5          # extra daily likes per invited friend (max +50)
AMBASSADOR_REFS = 3             # badge after inviting this many friends
LIKE_PING_COOLDOWN = 6 * 3600   # "someone liked you" notification cooldown
AUTO_HIDE_REPORTS = 3           # reports before a profile is auto-hidden
MAX_PHOTOS = 3

if not TOKEN:
    raise SystemExit("CRITICAL ERROR: BOT_TOKEN is missing")
if not MONGO_URI:
    raise SystemExit("CRITICAL ERROR: MONGO_URI is missing")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("shadowchatter")
if not OWNER_ID:
    logger.warning("OWNER_ID is not set: admin tools are disabled")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
router.message.filter(F.chat.type == "private")   # groups are ignored

mongo = AsyncMongoClient(MONGO_URI)
db = mongo.anon_chat_db

queue_col = db.queue
chats_col = db.active_chats
users_col = db.users
msgs_col = db.chat_msgs
reviews_col = db.pending_reviews
counters_col = db.counters
archives_col = db.archives
likes_col = db.likes            # _id "from:to", v=1 like / v=0 skip
matches_col = db.matches
dreports_col = db.dating_reports
media_col = db.media_pool       # GIFs/stickers the admin adds
ai_hist_col = db.ai_history

ai_client = (
    AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_API_KEY)
    if (AsyncOpenAI and OPENROUTER_API_KEY)
    else None
)
AI_ENABLED = ai_client is not None

esc = html.escape
_bot_username = None


def is_owner(uid) -> bool:
    return OWNER_ID is not None and uid == OWNER_ID


# =========================================================
# TEXT & VARIANTS (dynamic messages)
# =========================================================

PRIVACY_TEXT = (
    "<b>Privacy & safety</b>\n"
    "• 18+ only.\n"
    "• Anonymous chats hide your name and username from partners "
    "(unless you both /reveal).\n"
    "• For safety, finished human chats (text & media) and dating profiles "
    "(photos, name, age, city, bio) are saved and reviewed by the bot admin.\n"
    "• Dating profiles are shown to other users in Discover. Usernames are shared "
    "only after a mutual match.\n"
    "• AI buddies are AI, and their chats are processed by an AI provider.\n"
    "• Never share passwords, OTPs or money details."
)

SAFETY_TEXT = (
    "🛡 <b>Staying safe here</b>\n"
    "• Never send money or OTPs to anyone you meet here.\n"
    "• Keep your phone number, address and socials private until you trust someone.\n"
    "• Meet in public places if you ever meet in person, and tell a friend.\n"
    "• Tap 🚩 Report if anyone is rude, fake or inappropriate: reports are reviewed "
    "and users get blocked.\n"
    "• /next skips instantly, /stop ends a chat."
)

WELCOME_TEXT = (
    "<b>Shadow Chatter 🕶</b>\n"
    "Anonymous chat + dating. Meet new people, play games, find your match 💘\n\n"
    + PRIVACY_TEXT
)

AGE_TEXT = "🔞 <b>Quick check</b>\nShadow Chatter is for adults only. Are you 18 or older?"

GENDER_LABEL = {"male": "👨 Male", "female": "👩 Female", "other": "🧑 Other"}
LOOK_LABEL = {"male": "👨 Men", "female": "👩 Women", "any": "🌍 Anyone"}

GREETINGS = [
    "Hey {n} 👋 what's the vibe today?",
    "Welcome back, {n}! 🔥",
    "{n}! Ready to meet someone new? ✨",
    "Good to see you, {n} 😎",
    "Look who's here, {n} 👀",
]
FOUND_LINES = [
    "✅ <b>Partner found!</b> Say hi 👋",
    "🎉 <b>You're connected!</b> Break the ice 🧊",
    "👀 <b>Someone's here!</b> Don't be shy",
    "✨ <b>Connected!</b> Start with a 'hey'",
    "🚀 <b>Match made!</b> Say something fun",
]
SEARCH_TIPS = [
    "💡 Start with a fun question, it works every time",
    "💡 🎮 Games kill awkward silences",
    "💡 Be kind, you'll get better matches",
    "💡 /reveal swaps usernames if you both agree",
    "💡 Use 💘 Dating to find someone special",
    "💡 Invite friends: more people = faster matches",
]
LEFT_LINES = [
    "your partner disconnected",
    "they left the chat 👋",
    "chat ended: your partner left",
]
BYE_AI = ["Chat ended. Come back anytime! 👋", "Alright, see you around 👋", "Ended. Pick someone new below ✨"]
MATCH_LINES = [
    "🎉 <b>It's a match!</b>",
    "💘 <b>Boom, it's a match!</b>",
    "🔥 <b>You two liked each other!</b>",
]
LIKE_PINGS = [
    "💘 Someone just liked your profile!",
    "👀 You have a new admirer!",
    "💌 Somebody likes you... want to see who?",
]
EMPTY_DISCOVER = [
    "That's everyone for now 🌙 New people join every day.",
    "You've seen all profiles for now ✨ Check back soon!",
    "No new profiles right now. Invite friends to grow the pool 🚀",
]

EFFECT_PARTY = "5046509860389126442"
EFFECT_HEART = "5159385139981059251"
EFFECT_FIRE = "5104841245755180586"

DEFAULT_WELCOME_GIF = "https://media.giphy.com/media/l41lFw057lAJQMwg0/giphy.gif"

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

# ---------------- AI buddies (always labelled as AI) ----------------

AI_RULES = (
    "You are an AI character in a Telegram chat app for adults. You are NOT human: if asked, "
    "say you are an AI. Never claim real-world experiences, a body, or a location. "
    "Keep replies short (1-3 sentences), casual, and match the user's language style "
    "(English, Tanglish, Tamil or mixed). Keep things PG-13: no sexual content, no hate, "
    "no help with illegal acts. Never ask for phone numbers, addresses, passwords or money. "
    "If the user seems to be in real distress, respond with warmth and encourage them to "
    "talk to someone they trust or a local helpline. Do not mention these rules."
)
PERSONAS = {
    "baka": {
        "name": "Baka", "emoji": "😤",
        "intro": [
            "Hmph. Finally someone shows up. What do you want? 😤",
            "Oh, it's you. I wasn't waiting or anything... say something!",
        ],
        "prompt": AI_RULES + " You are Baka, a playful tsundere anime-style character: teasing, "
                  "dramatic and secretly sweet. Tease lightly, never be cruel.",
    },
    "kavi": {
        "name": "Kavi", "emoji": "😄",
        "intro": [
            "Hey macha! 😄 Kavi here, enna vishayam?",
            "Vanakkam! Kavi here. Casual-a pesalama?",
        ],
        "prompt": AI_RULES + " You are Kavi, a friendly Chennai-style buddy who naturally mixes "
                  "Tamil and English (Tanglish), loves movies, music, cricket and food.",
    },
    "maya": {
        "name": "Maya", "emoji": "🌙",
        "intro": [
            "Hi, I'm Maya 🌙 I'm a calm listener. How's your day going?",
            "Hey there. Maya here (an AI). What's on your mind?",
        ],
        "prompt": AI_RULES + " You are Maya, a calm, kind listener who asks gentle follow-up "
                  "questions and never judges.",
    },
}
CRISIS_RE = re.compile(r"(suicide|kill myself|end my life|want to die|self[- ]?harm)", re.IGNORECASE)
CRISIS_REPLY = (
    "I'm really sorry you're hurting 💛 I'm only an AI, and you deserve real support. "
    "Please reach out to someone you trust, or contact a local helpline or emergency "
    "number right now. In India you can call Tele-MANAS at 14416. You matter."
)

# ---------------- dating wizard ----------------

WIZ_PROMPTS = {
    "name": "<b>Step 1/5</b> · What's your name (or nickname)?",
    "age": "<b>Step 2/5</b> · How old are you? (18+)",
    "city": "<b>Step 3/5</b> · Where are you from? (your city)",
    "bio": "<b>Step 4/5</b> · Write a short bio (max 150 characters): your vibe, hobbies, what you want.",
    "photo": "<b>Step 5/5</b> · Send your best photo 📸 (you can add up to 3).",
}


# =========================================================
# KEYBOARDS
# =========================================================

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data=None, url=None):
    if url:
        return InlineKeyboardButton(text=text, url=url)
    return InlineKeyboardButton(text=text, callback_data=data)


def age_keyboard():
    return kb([[btn("✅ I'm 18 or older", "ag_yes")], [btn("❌ I'm under 18", "ag_no")]])


def gender_keyboard():
    return kb([[btn(v, f"gen_{k}")] for k, v in GENDER_LABEL.items()])


def look_keyboard():
    return kb([[btn(v, f"look_{k}")] for k, v in LOOK_LABEL.items()])


def mode_keyboard():
    return kb([
        [btn("💘 Dating: find someone special", "mode_dating")],
        [btn("😎 Timepass: chat for fun", "mode_chat")],
    ])


def home_keyboard():
    rows = [
        [btn("💬 Anonymous Chat", "action_search"), btn("💘 Dating", "dmenu")],
    ]
    second = [btn("🔥 Popular", "topmenu")]
    if AI_ENABLED:
        second.insert(0, btn("🤖 AI Friends", "aim"))
    rows.append(second)
    rows.append([btn("🎁 Invite friends", "invite"), btn("⚙️ Settings", "settings")])
    return kb(rows)


def search_keyboard():
    return kb([[btn("🔍 Find a Partner", "action_search")], [btn("🏠 Menu", "home")]])


def chat_keyboard():
    return kb([
        [btn("⏭ Next", "action_next"), btn("⏹ Stop", "action_stop")],
        [btn("🎮 Games", "game_menu"), btn("🎵 Music", "music_menu")],
        [btn("🤝 Reveal", "rev_ask"), btn("🚩 Report", "action_report")],
    ])


def ai_chat_keyboard():
    return kb([[btn("⏭ New chat", "action_next"), btn("⏹ Stop", "action_stop")]])


def games_keyboard():
    return kb([
        [btn("⭕ Tic-Tac-Toe", "ttt_start"), btn("✊ Rock-Paper-Scissors", "rps_start")],
        [btn("🎲 Dice duel", "dice_d"), btn("🎯 Darts", "dice_t"), btn("🎳 Bowling", "dice_b")],
        [btn("🧊 Icebreaker", "fun_ice"), btn("🤔 Truth", "fun_truth")],
        [btn("😈 Dare", "fun_dare"), btn("⚖️ Would you rather", "fun_wyr")],
    ])


def review_keyboard(chat_no: int):
    return kb([[btn("👍 Good", f"rv:{chat_no}:good"), btn("👎 Bad", f"rv:{chat_no}:bad")]])


def discover_keyboard(target: int, photos: int):
    row2 = []
    if photos > 1:
        row2.append(btn("🖼 More photos", f"dmore:{target}"))
    row2.append(btn("🚩 Report", f"drp:{target}"))
    return kb([[btn("💚 Like", f"dl:{target}"), btn("❌ Skip", f"dk:{target}")], row2,
               [btn("🏠 Menu", "home")]])


PUBLIC_COMMANDS = [
    BotCommand(command="start", description="Start"),
    BotCommand(command="menu", description="Main menu"),
    BotCommand(command="search", description="Find an anonymous partner"),
    BotCommand(command="next", description="Skip to next partner"),
    BotCommand(command="stop", description="End chat"),
    BotCommand(command="dating", description="Dating: discover & matches"),
    BotCommand(command="ai", description="Chat with an AI friend"),
    BotCommand(command="games", description="Play games with your partner"),
    BotCommand(command="music", description="Listen to music together"),
    BotCommand(command="reveal", description="Share usernames (both must agree)"),
    BotCommand(command="settings", description="Gender & preferences"),
    BotCommand(command="invite", description="Invite friends"),
    BotCommand(command="safety", description="Safety tips"),
    BotCommand(command="report", description="Report your partner"),
    BotCommand(command="privacy", description="How data is handled"),
]
ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand(command="admin", description="Admin help"),
    BotCommand(command="chats", description="List saved chats"),
    BotCommand(command="block", description="Block a user"),
    BotCommand(command="unblock", description="Unblock a user"),
    BotCommand(command="whois", description="Look up a user"),
    BotCommand(command="profileof", description="Show a dating profile"),
    BotCommand(command="addmedia", description="Add GIFs/stickers to the pool"),
    BotCommand(command="media", description="Media pool counts"),
    BotCommand(command="broadcast", description="Message all users"),
]
ADMIN_HELP = (
    "🛠 <b>Admin commands</b>\n"
    "/chats [bad|good|unrated] : last 15 saved chats\n"
    "/openchat_07 : replay a chat · /deletechat_07 : delete it\n"
    "/block @user|id · /unblock @user|id · /whois @user|id\n"
    "/profileof @user|id : show someone's dating profile\n"
    "/addmedia welcome|match|found|love|bye : then send GIFs/stickers; /donemedia to finish\n"
    "/media : pool counts · /stats · /broadcast text"
)


async def set_bot_commands():
    await bot.set_my_commands(PUBLIC_COMMANDS)
    if OWNER_ID:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=OWNER_ID))
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


async def send_fx(chat_id, text, effect, **kwargs):
    """Message with a Telegram effect; falls back to a normal message."""
    try:
        return await bot.send_message(chat_id, text, message_effect_id=effect, **kwargs)
    except Exception:
        return await safe_send(chat_id, text, **kwargs)


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


async def try_delete(message):
    try:
        await message.delete()
    except Exception:
        pass


async def edit_or_answer(callback: CallbackQuery, text: str, markup=None):
    try:
        if callback.message.caption is not None or callback.message.animation:
            await callback.message.edit_caption(caption=text, reply_markup=markup)
        else:
            await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


async def find_chat(user_id: int):
    return await chats_col.find_one({"$or": [{"user_1": user_id}, {"user_2": user_id}]})


def other_party(chat: dict, user_id: int):
    return chat["user_2"] if chat["user_1"] == user_id else chat["user_1"]


def new_code(n=7) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


async def touch_user(user: types.User, **extra):
    await users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "username": user.username,
            "username_lower": (user.username or "").lower(),
            "first_name": user.first_name,
            "last_seen": time.time(),
            **extra,
        }},
        upsert=True,
    )


def badge(doc: dict) -> str:
    good, bad = doc.get("good", 0), doc.get("bad", 0)
    total = good + bad
    if total < 3:
        return "🌱 New"
    return "⭐ Trusted" if good / total >= 0.8 else "⚠️ Mixed reviews"


def who(doc: dict, uid: int) -> str:
    doc = doc or {}
    name = esc(doc.get("first_name") or "?")
    uname = doc.get("username")
    return f"{name} | {'@' + esc(uname) if uname else 'no username'} | <code>{uid}</code>"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


async def hide_dating(uid: int, removed: bool = False):
    upd = {"dating.active": False}
    if removed:
        upd["dating.removed"] = True
    await users_col.update_one({"user_id": uid, "dating": {"$exists": True}}, {"$set": upd})


async def resolve_user(arg: str):
    arg = (arg or "").strip()
    if not arg:
        return None
    if arg.lstrip("-").isdigit():
        return int(arg)
    doc = await users_col.find_one({"username_lower": arg.lstrip("@").lower()})
    return doc["user_id"] if doc else None


async def count_online() -> int:
    return await users_col.count_documents({"last_seen": {"$gt": time.time() - 600}})


def today_ist() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


bg_tasks: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    bg_tasks.add(t)
    t.add_done_callback(bg_tasks.discard)
    return t


wait_tasks: dict[int, asyncio.Task] = {}
ai_tasks: dict[int, asyncio.Task] = {}
media_pending: dict[int, str] = {}


async def notify_owner(text: str, **kwargs):
    if OWNER_ID:
        await safe_send(OWNER_ID, text, **kwargs)


# =========================================================
# GIF / STICKER POOL
# =========================================================

async def send_flair(chat_id, cat: str, prob: float = 1.0) -> bool:
    if random.random() > prob:
        return False
    try:
        docs = await media_col.find({"cat": cat}).to_list(100)
    except Exception:
        return False
    if not docs:
        return False
    m = random.choice(docs)
    try:
        if m["kind"] == "sticker":
            await bot.send_sticker(chat_id, m["file_id"])
        else:
            await bot.send_animation(chat_id, m["file_id"])
        return True
    except Exception as e:
        logger.warning("flair failed: %s", e)
        return False


async def send_welcome_media(chat_id):
    if await send_flair(chat_id, "welcome"):
        return
    try:
        await bot.send_animation(chat_id, DEFAULT_WELCOME_GIF)
    except Exception:
        pass


# =========================================================
# MESSAGE LOG, ARCHIVE, REVIEWS (human chats)
# =========================================================

def extract_content(m: types.Message):
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
            await tg_call(bot.send_message, chat_id, f"{label} {esc(text)}")
        elif t in CAPTION_TYPES and m.get("f"):
            await tg_call(getattr(bot, f"send_{t}"), chat_id, **{t: m["f"]},
                          caption=esc(f"{label} {text}".strip()))
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
    users, info = a["users"], a["info"]
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
    return kb([[btn("⛔ Block 🅰️", f"adm_block:{u[0]}"), btn("⛔ Block 🅱️", f"adm_block:{u[1]}")]])


async def replay_archive(chat_id: int, a: dict):
    await safe_send(chat_id, archive_header(a), reply_markup=archive_admin_keyboard(a))
    for m in a["messages"]:
        await send_logged(chat_id, m, "🅰️" if m["s"] == "a" else "🅱️")
    await safe_send(chat_id, f"🏁 End of Chat-{a['_id']:02d}")


async def archive_chat(chat: dict):
    chat_key = str(chat["_id"])
    try:
        refs = await msgs_col.find({"chat_key": chat_key}).sort("_id", 1).to_list(None)
        if not refs:
            return None
        counter = await counters_col.find_one_and_update(
            {"_id": "archive"}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
        number = counter["n"]
        a, b = chat["user_1"], chat["user_2"]
        da = await users_col.find_one({"user_id": a}) or {}
        db_ = await users_col.find_one({"user_id": b}) or {}
        archive = {
            "_id": number, "users": [a, b],
            "info": {
                "a": {"first_name": da.get("first_name"), "username": da.get("username")},
                "b": {"first_name": db_.get("first_name"), "username": db_.get("username")},
            },
            "date_text": datetime.now(IST).strftime("%d %b %Y, %I:%M %p"),
            "created_at": time.time(),
            "duration": time.time() - chat.get("created_at", time.time()),
            "count": len(refs), "tag": "unrated",
            "messages": [{"s": "a" if r["sender"] == a else "b", "t": r["type"],
                          "x": r.get("text", ""), "f": r.get("file_id")} for r in refs],
        }
        await archives_col.insert_one(archive)
        if OWNER_ID:
            await safe_send(OWNER_ID, archive_header(archive) + f"\n\n▶️ Open: /openchat_{number:02d}",
                            reply_markup=archive_admin_keyboard(archive))
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


async def record_vote(chat_no: int, voter: int, vote: str, reported: bool = False):
    review = await reviews_col.find_one_and_update(
        {"_id": str(chat_no), "users": voter, f"votes.{voter}": {"$exists": False}},
        {"$set": {f"votes.{voter}": vote}}, return_document=ReturnDocument.AFTER)
    if not review:
        return False
    votes = list(review.get("votes", {}).values())
    tag = "bad" if "bad" in votes else ("good" if "good" in votes else "unrated")
    upd = {"tag": tag}
    if reported:
        upd["reported_by"] = voter
    await archives_col.update_one({"_id": chat_no}, {"$set": upd})
    target = [u for u in review["users"] if u != voter][0]
    updated = await users_col.find_one_and_update(
        {"user_id": target}, {"$inc": {"good" if vote == "good" else "bad": 1}},
        return_document=ReturnDocument.AFTER)
    icon = "🚩" if reported else ("👍" if vote == "good" else "👎")
    voter_doc = await users_col.find_one({"user_id": voter})
    target_doc = updated or {}
    await notify_owner(
        f"{icon} <b>Chat-{chat_no:02d}</b>: {who(voter_doc, voter)}\n"
        f"{'reported' if reported else 'rated'} partner {who(target_doc, target)} "
        f"as <b>{vote.upper()}</b>\n▶️ /openchat_{chat_no:02d}")
    bad, good = target_doc.get("bad", 0), target_doc.get("good", 0)
    if bad >= 5 and bad > 2 * good and not target_doc.get("banned"):
        await block_user(target, auto=True)
    return True


async def finish_chat(chat: dict, ender_id: int, reported: bool = False):
    partner_id = other_party(chat, ender_id)
    await safe_send(ender_id, "you disconnected")
    await safe_send(partner_id, random.choice(LEFT_LINES))
    number = await archive_chat(chat)
    if number is None:
        await safe_send(ender_id, "Ready for another?", reply_markup=search_keyboard())
        await safe_send(partner_id, "Ready for another?", reply_markup=search_keyboard())
        return
    await reviews_col.replace_one(
        {"_id": str(number)},
        {"_id": str(number), "users": [chat["user_1"], chat["user_2"]], "votes": {},
         "created_at": datetime.now(timezone.utc)}, upsert=True)
    if reported:
        await record_vote(number, ender_id, "bad", reported=True)
        await safe_send(ender_id, "Thanks, your report was sent for review.", reply_markup=search_keyboard())
    else:
        await safe_send(ender_id, "How was your partner?", reply_markup=review_keyboard(number))
    await safe_send(partner_id, "How was your partner?", reply_markup=review_keyboard(number))


# =========================================================
# BLOCKING
# =========================================================

async def block_user(target: int, auto: bool = False) -> str:
    await users_col.update_one({"user_id": target}, {"$set": {"banned": True}}, upsert=True)
    await hide_dating(target)
    t = wait_tasks.pop(target, None)
    if t:
        t.cancel()
    cancel_ai_task(target)
    await queue_col.delete_many({"user_id": target})
    chat = await chats_col.find_one_and_delete({"$or": [{"user_1": target}, {"user_2": target}]})
    if chat and not isinstance(other_party(chat, target), str):
        spawn(finish_chat(chat, target))
    await safe_send(target, "⛔ Your account has been restricted for breaking the rules.")
    doc = await users_col.find_one({"user_id": target})
    msg = f"⛔ {'Auto-blocked' if auto else 'Blocked'} {who(doc, target)}"
    if auto:
        await notify_owner(msg)
    return msg


# =========================================================
# AI BUDDIES
# =========================================================

def cancel_ai_task(uid: int):
    t = ai_tasks.get(uid)
    if t and not t.done():
        t.cancel()


async def call_model(messages):
    last = None
    for model in AI_MODELS:
        try:
            return await asyncio.wait_for(
                ai_client.chat.completions.create(model=model, messages=messages), timeout=AI_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            last = e
            logger.warning("model %s failed: %s", model, e)
    raise RuntimeError(f"all models failed: {last}")


async def ai_reply(uid: int, text: str, chat_id: int, key: str):
    persona = PERSONAS[key]
    try:
        if CRISIS_RE.search(text):
            await safe_send(chat_id, CRISIS_REPLY)
            return
        doc = await ai_hist_col.find_one({"_id": uid})
        msgs = doc["messages"] if doc and doc.get("persona") == key else [
            {"role": "system", "content": persona["prompt"]}]
        msgs.append({"role": "user", "content": text})
        if len(msgs) > 13:
            msgs = [msgs[0]] + msgs[-12:]
        try:
            await bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        completion = await call_model(msgs)
        reply = (completion.choices[0].message.content or "").strip() or "hmm"
        msgs.append({"role": "assistant", "content": reply})
        await ai_hist_col.update_one(
            {"_id": uid}, {"$set": {"messages": msgs, "persona": key, "ts": time.time()}}, upsert=True)
        await asyncio.sleep(random.uniform(0.6, 1.6))
        current = await find_chat(uid)
        if not current or other_party(current, uid) != f"AI:{key}":
            return
        await safe_send(chat_id, f"{persona['emoji']} {esc(reply)}")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("AI error: %s", e)
        await safe_send(chat_id, "I zoned out for a sec 😅 say that again?")
    finally:
        if ai_tasks.get(uid) is asyncio.current_task():
            ai_tasks.pop(uid, None)


async def start_ai_chat(uid: int, chat_id: int, key: str):
    if not AI_ENABLED or key not in PERSONAS:
        await safe_send(chat_id, "AI friends are not available right now.")
        return
    if await find_chat(uid):
        await safe_send(chat_id, "you're already in a chat. /stop first")
        return
    t = wait_tasks.pop(uid, None)
    if t:
        t.cancel()
    await queue_col.delete_many({"user_id": uid})
    p = PERSONAS[key]
    await chats_col.insert_one(
        {"user_1": uid, "user_2": f"AI:{key}", "created_at": time.time(), "msg_count": 0})
    await ai_hist_col.delete_one({"_id": uid})
    await safe_send(
        chat_id,
        f"🤖 <b>You're chatting with {p['name']}, an AI character.</b> Say something!\n"
        f"<i>(Use /stop to end. AI can be wrong or silly.)</i>",
        reply_markup=ai_chat_keyboard())
    await send_flair(chat_id, "love", 0.3)
    await safe_send(chat_id, f"{p['emoji']} {esc(random.choice(p['intro']))}")


def ai_menu_keyboard():
    return kb([[btn(f"{p['emoji']} {p['name']}", f"ai:{k}")] for k, p in PERSONAS.items()]
              + [[btn("🏠 Menu", "home")]])


# =========================================================
# MATCHMAKING (gender-aware)
# =========================================================

async def start_human_chat(user_a: int, user_b: int):
    await chats_col.insert_one(
        {"user_1": user_a, "user_2": user_b, "created_at": time.time(), "msg_count": 0})
    da = await users_col.find_one({"user_id": user_a}) or {}
    db_ = await users_col.find_one({"user_id": user_b}) or {}
    await users_col.update_many({"user_id": {"$in": [user_a, user_b]}}, {"$inc": {"chats": 1}})

    def intro(partner):
        return (f"{random.choice(FOUND_LINES)}\n{badge(partner)}\n"
                "Try 🎮 Games or 🎵 Music if you need something to do together.")

    await send_fx(user_a, intro(db_), EFFECT_PARTY, reply_markup=chat_keyboard())
    await send_fx(user_b, intro(da), EFFECT_PARTY, reply_markup=chat_keyboard())
    await send_flair(user_a, "found", 0.4)
    await send_flair(user_b, "found", 0.4)


async def search_flow(user_id: int, chat_id: int, mid):
    """Animated waiting screen + AI offer + invite nudge."""
    frames = ["🔍 Searching", "🔍 Searching.", "🔍 Searching..", "🔍 Searching..."]
    start = time.monotonic()
    offered = nudged = False
    tip = random.choice(SEARCH_TIPS)
    try:
        while True:
            await asyncio.sleep(3)
            if not await queue_col.find_one({"user_id": user_id}):
                return
            el = time.monotonic() - start
            if int(el) % 15 < 3:
                tip = random.choice(SEARCH_TIPS)
            if mid:
                try:
                    await bot.edit_message_text(
                        f"{frames[int(el / 3) % 4]}\n👥 {await count_online()} online now\n{tip}",
                        chat_id=chat_id, message_id=mid)
                except Exception:
                    pass
            if el > 25 and not offered and AI_ENABLED:
                offered = True
                await safe_send(
                    chat_id, "⏳ Nobody yet. Chat with an AI friend while you wait? (clearly an AI 🤖)",
                    reply_markup=kb([[btn(f"{p['emoji']} {p['name']}", f"ai:{k}")]
                                     for k, p in PERSONAS.items()]))
            if el > 75 and not nudged:
                nudged = True
                link = f"https://t.me/{await get_username()}?start=ref_{user_id}"
                await safe_send(
                    chat_id,
                    "It's quiet right now. More people = faster matches. Invite a friend, "
                    "or stay in the queue and I'll connect you as soon as someone joins.",
                    reply_markup=kb([[btn("📤 Invite a friend", url=(
                        "https://t.me/share/url?url=" + link + "&text=Meet%20new%20people%20anonymously%20%F0%9F%95%B6"))]]))
            if el > 900:
                await queue_col.delete_one({"user_id": user_id})
                await safe_send(chat_id, "No one found for now 😔 Try again later, or try 💘 Dating.",
                                reply_markup=search_keyboard())
                return
    except asyncio.CancelledError:
        raise
    finally:
        if wait_tasks.get(user_id) is asyncio.current_task():
            wait_tasks.pop(user_id, None)


async def execute_search(user_id: int, chat_id: int):
    u = await users_col.find_one({"user_id": user_id}) or {}
    if u.get("banned"):
        await safe_send(chat_id, "your account is restricted")
        return
    if u.get("setup"):
        await safe_send(chat_id, "Finish your dating profile first, or send /cancel.")
        return
    if not (u.get("age_ok") and u.get("gender") and u.get("looking")):
        await onboarding_next(user_id, chat_id)
        return
    if await find_chat(user_id):
        await safe_send(chat_id, "you're already in a chat")
        return
    if await queue_col.find_one({"user_id": user_id}):
        await safe_send(chat_id, "you're already searching...")
        return

    g, look = u["gender"], u["looking"]
    q = {"user_id": {"$ne": user_id}, "looking": {"$in": [g, "any"]}}
    if look != "any":
        q["gender"] = look

    partner = await queue_col.find_one_and_delete(q)
    if partner:
        t = wait_tasks.pop(partner["user_id"], None)
        if t:
            t.cancel()
        await start_human_chat(user_id, partner["user_id"])
        return

    await queue_col.insert_one(
        {"user_id": user_id, "gender": g, "looking": look, "joined_at": time.time()})

    # second look closes the race where two people queue at the same moment
    partner = await queue_col.find_one_and_delete(q)
    if partner:
        mine = await queue_col.find_one_and_delete({"user_id": user_id})
        if mine:
            t = wait_tasks.pop(partner["user_id"], None)
            if t:
                t.cancel()
            await start_human_chat(user_id, partner["user_id"])
            return
        await queue_col.insert_one(partner)   # somebody already matched me

    m = await safe_send(chat_id, "🔍 Searching...")
    wait_tasks[user_id] = asyncio.create_task(
        search_flow(user_id, chat_id, m.message_id if m else None))


async def execute_stop(user_id: int, chat_id: int, reported: bool = False):
    t = wait_tasks.pop(user_id, None)
    if t:
        t.cancel()
    if await queue_col.find_one_and_delete({"user_id": user_id}):
        await safe_send(chat_id, "you left the waiting queue", reply_markup=search_keyboard())
        return
    chat = await chats_col.find_one_and_delete({"$or": [{"user_1": user_id}, {"user_2": user_id}]})
    if not chat:
        await safe_send(chat_id, "you're not in a chat", reply_markup=search_keyboard())
        return
    partner_id = other_party(chat, user_id)
    if isinstance(partner_id, str):       # AI buddy
        cancel_ai_task(user_id)
        await ai_hist_col.delete_one({"_id": user_id})
        await safe_send(chat_id, random.choice(BYE_AI), reply_markup=search_keyboard())
        return
    spawn(finish_chat(chat, user_id, reported=reported))


# =========================================================
# GATE MIDDLEWARE (18+ and bans)
# =========================================================

class GateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        is_cb = isinstance(event, CallbackQuery)
        if is_cb:
            private = event.message is not None and event.message.chat.type == "private"
        else:
            private = event.chat.type == "private"
        if not user or user.is_bot or is_owner(user.id) or not private:
            return await handler(event, data)

        d = await users_col.find_one({"user_id": user.id}, {"banned": 1, "underage": 1, "age_ok": 1}) or {}
        if d.get("banned") or d.get("underage"):
            msg = "⛔ This account can't use the bot."
            if is_cb:
                await event.answer(msg, show_alert=True)
            else:
                await event.answer(msg)
            return
        if not d.get("age_ok"):
            allowed = (is_cb and (event.data or "").startswith("ag_")) or (
                not is_cb and (event.text or "").startswith("/start"))
            if not allowed:
                if is_cb:
                    await event.answer("Press /start first (18+ only).", show_alert=True)
                else:
                    await event.answer("Press /start first (18+ only).")
                return
        return await handler(event, data)


router.message.outer_middleware(GateMiddleware())
router.callback_query.outer_middleware(GateMiddleware())


# =========================================================
# ONBOARDING
# =========================================================

def home_text(name: str) -> str:
    return random.choice(GREETINGS).format(n=esc(name or "friend"))


async def show_home(uid: int, chat_id: int):
    u = await users_col.find_one({"user_id": uid}) or {}
    await safe_send(chat_id, home_text(u.get("first_name")), reply_markup=home_keyboard())


async def onboarding_next(uid: int, chat_id: int):
    u = await users_col.find_one({"user_id": uid}) or {}
    if u.get("underage") or u.get("banned"):
        return
    if not u.get("age_ok"):
        await safe_send(chat_id, AGE_TEXT, reply_markup=age_keyboard())
    elif not u.get("gender"):
        await safe_send(chat_id, "👤 <b>I am...</b>", reply_markup=gender_keyboard())
    elif not u.get("looking"):
        await safe_send(chat_id, "💞 <b>I'm looking to meet...</b>", reply_markup=look_keyboard())
    elif not u.get("mode"):
        await safe_send(chat_id, "✨ <b>What brings you here?</b>", reply_markup=mode_keyboard())
    else:
        await show_home(uid, chat_id)
        if u.get("pending"):
            pending = u["pending"]
            await users_col.update_one({"user_id": uid}, {"$unset": {"pending": ""}})
            if pending.startswith("u_"):
                await show_profile_by_code(uid, chat_id, pending[2:])


@router.callback_query(F.data == "ag_yes")
async def cb_age_yes(cb: CallbackQuery):
    await users_col.update_one({"user_id": cb.from_user.id}, {"$set": {"age_ok": True}})
    await cb.answer("Thanks!")
    await try_delete(cb.message)
    await onboarding_next(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data == "ag_no")
async def cb_age_no(cb: CallbackQuery):
    await users_col.update_one({"user_id": cb.from_user.id}, {"$set": {"underage": True}})
    await hide_dating(cb.from_user.id)
    await cb.answer()
    await edit_or_answer(cb, "Sorry, Shadow Chatter is for adults (18+) only. Stay safe online 💛")


@router.callback_query(F.data.startswith("gen_"))
async def cb_gender(cb: CallbackQuery):
    v = cb.data[4:]
    if v not in GENDER_LABEL:
        await cb.answer()
        return
    await users_col.update_one({"user_id": cb.from_user.id}, {"$set": {"gender": v}})
    await cb.answer("Saved")
    await try_delete(cb.message)
    await onboarding_next(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data.startswith("look_"))
async def cb_look(cb: CallbackQuery):
    v = cb.data[5:]
    if v not in LOOK_LABEL:
        await cb.answer()
        return
    await users_col.update_one({"user_id": cb.from_user.id}, {"$set": {"looking": v}})
    await cb.answer("Saved")
    await try_delete(cb.message)
    await onboarding_next(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data.startswith("mode_"))
async def cb_mode(cb: CallbackQuery):
    uid = cb.from_user.id
    mode = cb.data[5:]
    await users_col.update_one({"user_id": uid}, {"$set": {"mode": mode}})
    await cb.answer()
    await try_delete(cb.message)
    u = await users_col.find_one({"user_id": uid}) or {}
    if mode == "dating" and not u.get("dating"):
        await wiz_start(uid, uid)
    elif mode == "dating":
        await dating_menu(uid, uid)
    else:
        await onboarding_next(uid, uid)


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery):
    await cb.answer()
    await try_delete(cb.message)
    await show_home(cb.from_user.id, cb.from_user.id)


@router.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await onboarding_next(message.from_user.id, message.chat.id)


@router.message(Command("settings"))
async def cmd_settings(message: types.Message):
    await send_settings(message.from_user.id, message.chat.id)


async def send_settings(uid: int, chat_id: int):
    u = await users_col.find_one({"user_id": uid}) or {}
    await safe_send(
        chat_id,
        "⚙️ <b>Settings</b>\n"
        f"I am: {GENDER_LABEL.get(u.get('gender'), '-')}\n"
        f"Looking for: {LOOK_LABEL.get(u.get('looking'), '-')}\n"
        f"Mode: {'💘 Dating' if u.get('mode') == 'dating' else '😎 Timepass'}",
        reply_markup=kb([
            [btn("Change my gender", "set_gender"), btn("Change who I want", "set_look")],
            [btn("Switch mode", "set_mode")],
            [btn("🗑 Delete my dating profile", "ddelete")],
            [btn("🏠 Menu", "home")],
        ]))


@router.callback_query(F.data == "settings")
async def cb_settings(cb: CallbackQuery):
    await cb.answer()
    await send_settings(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data == "set_gender")
async def cb_set_gender(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("👤 <b>I am...</b>", reply_markup=gender_keyboard())


@router.callback_query(F.data == "set_look")
async def cb_set_look(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("💞 <b>I'm looking to meet...</b>", reply_markup=look_keyboard())


@router.callback_query(F.data == "set_mode")
async def cb_set_mode(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("✨ <b>What brings you here?</b>", reply_markup=mode_keyboard())


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
                    {"user_id": ref}, {"$inc": {"referrals": 1}}, return_document=ReturnDocument.AFTER)
                if r:
                    n = r.get("referrals", 1)
                    extra = f" Daily likes +{LIKES_PER_REFERRAL}!" if n * LIKES_PER_REFERRAL <= 50 else ""
                    amb = " ⭐ You're now an Ambassador!" if n == AMBASSADOR_REFS else ""
                    await safe_send(ref, f"🎉 A friend joined with your link! Invites: {n}.{extra}{amb}")
        except ValueError:
            pass
    if arg.startswith("u_"):
        await users_col.update_one({"user_id": uid}, {"$set": {"pending": arg}})

    u = await users_col.find_one({"user_id": uid}) or {}
    if not u.get("age_ok") or not u.get("mode"):
        await send_welcome_media(message.chat.id)
        await message.answer(WELCOME_TEXT)
    await onboarding_next(uid, message.chat.id)


@router.message(Command("invite"))
async def cmd_invite(message: types.Message):
    await send_invite(message.from_user.id, message.chat.id)


@router.callback_query(F.data == "invite")
async def cb_invite(cb: CallbackQuery):
    await cb.answer()
    await send_invite(cb.from_user.id, cb.from_user.id)


async def send_invite(uid: int, chat_id: int):
    link = f"https://t.me/{await get_username()}?start=ref_{uid}"
    u = await users_col.find_one({"user_id": uid}) or {}
    n = u.get("referrals", 0)
    await safe_send(
        chat_id,
        f"🎁 <b>Invite friends</b>\nEvery friend gives you <b>+{LIKES_PER_REFERRAL} daily likes</b>. "
        f"Invite {AMBASSADOR_REFS} to earn the ⭐ Ambassador badge on your profile.\n"
        f"Invited so far: <b>{n}</b>\n\n{link}",
        reply_markup=kb([[btn("📤 Share with friends", url=(
            "https://t.me/share/url?url=" + link + "&text=Anonymous%20chat%20%2B%20dating%20on%20Telegram%20%F0%9F%95%B6%F0%9F%92%98"))]]))


@router.message(Command("privacy"))
async def cmd_privacy(message: types.Message):
    await message.answer(PRIVACY_TEXT)


@router.message(Command("safety"))
async def cmd_safety(message: types.Message):
    await message.answer(SAFETY_TEXT)


@router.message(Command("cancel"))
async def cmd_cancel(message: types.Message):
    await users_col.update_one({"user_id": message.from_user.id}, {"$set": {"setup": None}, "$unset": {"wiz": ""}})
    await message.answer("Cancelled ✅", reply_markup=home_keyboard())


# =========================================================
# CHAT COMMANDS
# =========================================================

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
async def btn_search(cb: CallbackQuery):
    await cb.answer()
    await touch_user(cb.from_user)
    await execute_search(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data == "action_stop")
async def btn_stop(cb: CallbackQuery):
    await cb.answer()
    await execute_stop(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data == "action_report")
async def btn_report(cb: CallbackQuery):
    await cb.answer("Reported")
    await execute_stop(cb.from_user.id, cb.from_user.id, reported=True)


@router.callback_query(F.data == "action_next")
async def btn_next(cb: CallbackQuery):
    await cb.answer()
    await touch_user(cb.from_user)
    await execute_stop(cb.from_user.id, cb.from_user.id)
    await asyncio.sleep(random.uniform(0.6, 1.2))
    await execute_search(cb.from_user.id, cb.from_user.id)


@router.message(Command("ai"))
async def cmd_ai(message: types.Message):
    if not AI_ENABLED:
        await message.answer("AI friends aren't available right now.")
        return
    await message.answer("🤖 <b>AI friends</b>: clearly AI, always up for a chat. Pick one:",
                         reply_markup=ai_menu_keyboard())


@router.callback_query(F.data == "aim")
async def cb_ai_menu(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("🤖 <b>AI friends</b>: clearly AI, always up for a chat. Pick one:",
                            reply_markup=ai_menu_keyboard())


@router.callback_query(F.data.startswith("ai:"))
async def cb_ai_pick(cb: CallbackQuery):
    await cb.answer()
    await try_delete(cb.message)
    await start_ai_chat(cb.from_user.id, cb.from_user.id, cb.data[3:])


# =========================================================
# GAMES, MUSIC (human partners only)
# =========================================================

GAMES_PROMPT = "🎮 Pick something to do together:"


@router.message(Command("games"))
@router.message(Command("fun"))
async def cmd_games(message: types.Message):
    chat = await find_chat(message.from_user.id)
    if not chat or isinstance(other_party(chat, message.from_user.id), str):
        await message.answer("Games need a real chat partner. Start one with /search")
        return
    await message.answer(GAMES_PROMPT, reply_markup=games_keyboard())


@router.callback_query(F.data == "game_menu")
async def games_menu(cb: CallbackQuery):
    await cb.answer()
    chat = await find_chat(cb.from_user.id)
    if chat and not isinstance(other_party(chat, cb.from_user.id), str):
        await cb.message.answer(GAMES_PROMPT, reply_markup=games_keyboard())


async def get_pair(cb: CallbackQuery):
    chat = await find_chat(cb.from_user.id)
    if not chat:
        await cb.answer("You're not in a chat", show_alert=True)
        return None, None
    partner = other_party(chat, cb.from_user.id)
    if isinstance(partner, str):
        await cb.answer("Games need a real partner", show_alert=True)
        return None, None
    return chat, partner


@router.callback_query(F.data.in_({"fun_ice", "fun_truth", "fun_dare", "fun_wyr"}))
async def fun_pick(cb: CallbackQuery):
    chat, _ = await get_pair(cb)
    if not chat:
        return
    await cb.answer()
    title, bank = FUN_BANK[cb.data.split("_", 1)[1]]
    text = f"{title}\n<b>{random.choice(bank)}</b>"
    for uid in (chat["user_1"], chat["user_2"]):
        await safe_send(uid, text)


async def send_music_menu(user_id: int):
    chat = await find_chat(user_id)
    if not chat:
        await safe_send(user_id, "Start a chat first: /search")
        return
    rows = []
    for i in range(0, len(MUSIC_MOODS), 2):
        rows.append([btn(label, url="https://www.youtube.com/results?search_query=" + q.replace(" ", "+"))
                     for label, q in MUSIC_MOODS[i:i + 2]])
    text = ("🎵 <b>Listen together</b>\nPick a vibe, press play at the same time, "
            "then tell each other what you think.")
    for uid in (chat["user_1"], chat["user_2"]):
        if isinstance(uid, int):
            await safe_send(uid, text, reply_markup=kb(rows))


@router.message(Command("music"))
async def cmd_music(message: types.Message):
    await send_music_menu(message.from_user.id)


@router.callback_query(F.data == "music_menu")
async def btn_music(cb: CallbackQuery):
    await cb.answer()
    await send_music_menu(cb.from_user.id)


DICE_KINDS = {"dice_d": "🎲", "dice_t": "🎯", "dice_b": "🎳"}
dice_busy: set = set()


@router.callback_query(F.data.in_(set(DICE_KINDS)))
async def dice_game(cb: CallbackQuery):
    chat, _ = await get_pair(cb)
    if not chat:
        return
    key = str(chat["_id"])
    if key in dice_busy:
        await cb.answer("Round in progress")
        return
    await cb.answer()
    dice_busy.add(key)
    emoji = DICE_KINDS[cb.data]
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
        again = kb([[btn("🔁 Again", cb.data)]])
        await safe_send(a, ra, reply_markup=again)
        await safe_send(b, rb, reply_markup=again)
    except Exception as e:
        logger.warning("dice failed: %s", e)
    finally:
        dice_busy.discard(key)


RPS_NAME = {"r": "✊ Rock", "p": "✋ Paper", "s": "✌️ Scissors"}
RPS_BEATS = {"r": "s", "s": "p", "p": "r"}


@router.callback_query(F.data == "rps_start")
async def rps_start(cb: CallbackQuery):
    chat, _ = await get_pair(cb)
    if not chat:
        return
    if chat.get("rps"):
        await cb.answer("Round in progress")
        return
    await cb.answer()
    await chats_col.update_one({"_id": chat["_id"]}, {"$set": {"rps": {"on": True}}})
    markup = kb([[btn(RPS_NAME["r"], "rps_r"), btn(RPS_NAME["p"], "rps_p"), btn(RPS_NAME["s"], "rps_s")]])
    for uid in (chat["user_1"], chat["user_2"]):
        await safe_send(uid, "✊✋✌️ <b>Rock Paper Scissors</b>\nChoose, picks stay hidden until you both lock in:",
                        reply_markup=markup)


@router.callback_query(F.data.in_({"rps_r", "rps_p", "rps_s"}))
async def rps_pick(cb: CallbackQuery):
    chat, _ = await get_pair(cb)
    if not chat:
        return
    uid = cb.from_user.id
    rps = chat.get("rps")
    if not rps:
        await cb.answer("This round is over")
        return
    if f"c{uid}" in rps:
        await cb.answer("Already locked in")
        return
    updated = await chats_col.find_one_and_update(
        {"_id": chat["_id"]}, {"$set": {f"rps.c{uid}": cb.data[-1]}}, return_document=ReturnDocument.AFTER)
    await cb.answer("Locked in ✅")
    picks = {k: v for k, v in (updated.get("rps") or {}).items() if k.startswith("c")}
    if len(picks) < 2:
        await edit_or_answer(cb, "Locked in ✅ Waiting for your partner...")
        return
    await chats_col.update_one({"_id": chat["_id"]}, {"$unset": {"rps": ""}})
    a, b = chat["user_1"], chat["user_2"]
    ca, cb_ = picks[f"c{a}"], picks[f"c{b}"]

    def result(mine, theirs):
        if mine == theirs:
            return "🤝 Draw!"
        return "🏆 You win!" if RPS_BEATS[mine] == theirs else "😅 You lost."

    again = kb([[btn("🔁 Play again", "rps_start")]])
    await safe_send(a, f"You: {RPS_NAME[ca]}\nPartner: {RPS_NAME[cb_]}\n<b>{result(ca, cb_)}</b>", reply_markup=again)
    await safe_send(b, f"You: {RPS_NAME[cb_]}\nPartner: {RPS_NAME[ca]}\n<b>{result(cb_, ca)}</b>", reply_markup=again)


TTT_SYM = {"x": "❌", "o": "⭕", "-": "▫️"}
TTT_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def ttt_winner(board: str):
    for a, b, c in TTT_LINES:
        if board[a] != "-" and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if "-" not in board else None


def ttt_render(state: dict, viewer: int):
    board, players = state["board"], state["players"]
    mine = "x" if players["x"] == viewer else "o"
    result = ttt_winner(board)
    if result == "draw":
        status = "🤝 Draw!"
    elif result:
        status = "🏆 You win!" if result == mine else "😅 You lost."
    else:
        status = "Your turn" if state["turn"] == viewer else "Partner's turn..."
    rows = [[btn(TTT_SYM[board[r * 3 + c]], f"tm:{r * 3 + c}") for c in range(3)] for r in range(3)]
    if result:
        rows.append([btn("🔁 Play again", "ttt_start")])
    return f"⭕❌ <b>Tic-Tac-Toe</b>\nYou are {TTT_SYM[mine]}\n<b>{status}</b>", kb(rows)


@router.callback_query(F.data == "ttt_start")
async def ttt_start(cb: CallbackQuery):
    chat, partner = await get_pair(cb)
    if not chat:
        return
    if chat.get("ttt"):
        await cb.answer("A game is already running")
        return
    await cb.answer("Invite sent")
    me = cb.from_user.id
    await safe_send(partner, "⭕❌ Your partner wants to play <b>Tic-Tac-Toe</b>!",
                    reply_markup=kb([[btn("✅ Play", f"ttt_yes:{me}"), btn("❌ No", f"ttt_no:{me}")]]))
    await safe_send(me, "Invite sent, waiting for your partner...")


@router.callback_query(F.data.startswith("ttt_no:"))
async def ttt_decline(cb: CallbackQuery):
    starter = int(cb.data.split(":", 1)[1])
    await cb.answer()
    await edit_or_answer(cb, "You declined the game.")
    await safe_send(starter, "Your partner skipped the game. Try another one 🎮")


@router.callback_query(F.data.startswith("ttt_yes:"))
async def ttt_accept(cb: CallbackQuery):
    starter = int(cb.data.split(":", 1)[1])
    me = cb.from_user.id
    chat = await find_chat(me)
    if not chat or other_party(chat, me) != starter or chat.get("ttt"):
        await cb.answer("Not available anymore", show_alert=True)
        return
    await cb.answer()
    await edit_or_answer(cb, "Game on! 🎮")
    state = {"board": "-" * 9, "turn": starter, "players": {"x": starter, "o": me}, "msgs": {}}
    for uid in (starter, me):
        text, markup = ttt_render(state, uid)
        sent = await safe_send(uid, text, reply_markup=markup)
        if sent:
            state["msgs"][str(uid)] = sent.message_id
    await chats_col.update_one({"_id": chat["_id"]}, {"$set": {"ttt": state}})


@router.callback_query(F.data.startswith("tm:"))
async def ttt_move(cb: CallbackQuery):
    chat, _ = await get_pair(cb)
    if not chat:
        return
    state = chat.get("ttt")
    uid = cb.from_user.id
    if not state:
        await cb.answer("Game over")
        return
    if state["turn"] != uid:
        await cb.answer("Wait for your turn")
        return
    idx = int(cb.data.split(":", 1)[1])
    board = state["board"]
    if board[idx] != "-":
        await cb.answer("Taken!")
        return
    mark = "x" if state["players"]["x"] == uid else "o"
    state["board"] = board[:idx] + mark + board[idx + 1:]
    state["turn"] = other_party(chat, uid)
    if ttt_winner(state["board"]):
        await chats_col.update_one({"_id": chat["_id"]}, {"$unset": {"ttt": ""}})
    else:
        await chats_col.update_one({"_id": chat["_id"]}, {"$set": {"ttt": state}})
    await cb.answer()
    for pid_str, mid in state["msgs"].items():
        text, markup = ttt_render(state, int(pid_str))
        try:
            await bot.edit_message_text(text, chat_id=int(pid_str), message_id=mid, reply_markup=markup)
        except Exception as e:
            logger.debug("ttt edit failed: %s", e)


# =========================================================
# REVEAL (anonymous chat, mutual)
# =========================================================

async def ask_reveal(user_id: int):
    chat = await find_chat(user_id)
    if not chat:
        await safe_send(user_id, "Start a chat first: /search")
        return
    partner = other_party(chat, user_id)
    if isinstance(partner, str):
        await safe_send(user_id, "Reveal only works with real people.")
        return
    await safe_send(partner, "🤝 Your partner wants to swap Telegram usernames. Share?",
                    reply_markup=kb([[btn("✅ Share", f"rev_yes:{user_id}"), btn("❌ No thanks", f"rev_no:{user_id}")]]))
    await safe_send(user_id, "Request sent. Usernames are shared only if your partner agrees.")


@router.message(Command("reveal"))
async def cmd_reveal(message: types.Message):
    await ask_reveal(message.from_user.id)


@router.callback_query(F.data == "rev_ask")
async def btn_reveal(cb: CallbackQuery):
    await cb.answer()
    await ask_reveal(cb.from_user.id)


@router.callback_query(F.data.startswith("rev_yes:") | F.data.startswith("rev_no:"))
async def reveal_answer(cb: CallbackQuery):
    action, requester = cb.data.split(":", 1)
    requester = int(requester)
    me = cb.from_user.id
    chat = await find_chat(me)
    await cb.answer()
    if not chat or other_party(chat, me) != requester:
        await edit_or_answer(cb, "That chat has ended.")
        return
    if action == "rev_no":
        await edit_or_answer(cb, "You declined.")
        await safe_send(requester, "Your partner prefers to stay anonymous.")
        return
    me_doc = await users_col.find_one({"user_id": me}) or {}
    req_doc = await users_col.find_one({"user_id": requester}) or {}

    def handle(doc, uid):
        return f"@{esc(doc['username'])}" if doc.get("username") else f'<a href="tg://user?id={uid}">open profile</a>'

    await edit_or_answer(cb, f"Shared! Their username: {handle(req_doc, requester)}")
    await send_fx(requester, f"🤝 Your partner agreed. Their username: {handle(me_doc, me)}", EFFECT_HEART)
    await notify_owner(f"🤝 Reveal: {who(me_doc, me)} ↔ {who(req_doc, requester)}")


@router.callback_query(F.data.startswith("rv:"))
async def on_review(cb: CallbackQuery):
    try:
        _, no, vote = cb.data.split(":", 2)
        no = int(no)
    except ValueError:
        await cb.answer()
        return
    if vote not in ("good", "bad"):
        await cb.answer()
        return
    ok = await record_vote(no, cb.from_user.id, vote)
    await cb.answer("Thanks!" if ok else "Already recorded")
    await edit_or_answer(cb, "Thanks for the feedback! 🙌", search_keyboard())


# =========================================================
# DATING: wizard
# =========================================================

async def wiz_prompt(uid: int, chat_id: int, step: str):
    markup = None
    if step == "name":
        markup = kb([[btn("Use my Telegram name", "w_name_tg")]])
    elif step == "bio":
        markup = kb([[btn("Skip", "w_skip_bio")]])
    await safe_send(chat_id, WIZ_PROMPTS[step], reply_markup=markup)


async def wiz_start(uid: int, chat_id: int):
    if await find_chat(uid) or await queue_col.find_one({"user_id": uid}):
        await safe_send(chat_id, "Leave your current chat/search first (/stop), then set up your profile.")
        return
    await users_col.update_one({"user_id": uid}, {"$set": {"setup": "name", "wiz": {}}})
    await safe_send(chat_id, "💘 <b>Let's create your dating profile</b>: it takes about 2 minutes.\n"
                             "Send /cancel any time.")
    await wiz_prompt(uid, chat_id, "name")


async def wiz_set(uid: int, field: str, value, next_step: str):
    await users_col.update_one(
        {"user_id": uid}, {"$set": {f"wiz.{field}": value, "setup": next_step}})


@router.callback_query(F.data == "w_name_tg")
async def cb_wiz_name_tg(cb: CallbackQuery):
    u = await users_col.find_one({"user_id": cb.from_user.id}) or {}
    if u.get("setup") != "name":
        await cb.answer()
        return
    await cb.answer()
    await try_delete(cb.message)
    await wiz_set(cb.from_user.id, "name", (cb.from_user.first_name or "Friend")[:20], "age")
    await wiz_prompt(cb.from_user.id, cb.from_user.id, "age")


@router.callback_query(F.data == "w_skip_bio")
async def cb_wiz_skip_bio(cb: CallbackQuery):
    u = await users_col.find_one({"user_id": cb.from_user.id}) or {}
    if u.get("setup") != "bio":
        await cb.answer()
        return
    await cb.answer()
    await try_delete(cb.message)
    await wiz_set(cb.from_user.id, "bio", "", "photo")
    await wiz_prompt(cb.from_user.id, cb.from_user.id, "photo")


@router.callback_query(F.data == "w_done")
async def cb_wiz_done(cb: CallbackQuery):
    u = await users_col.find_one({"user_id": cb.from_user.id}) or {}
    if u.get("setup") != "photo" or not (u.get("wiz") or {}).get("photos"):
        await cb.answer("Add at least one photo first", show_alert=True)
        return
    await cb.answer()
    await try_delete(cb.message)
    await finalize_profile(cb.from_user.id, cb.from_user.id)


async def wiz_input(message: types.Message, u: dict):
    uid = message.from_user.id
    step = u["setup"]
    wiz = u.get("wiz") or {}
    text = (message.text or "").strip()

    if step == "name":
        if not text or len(text) > 20:
            await message.answer("Keep your name under 20 characters.")
            return
        await wiz_set(uid, "name", text, "age")
        await wiz_prompt(uid, message.chat.id, "age")
    elif step == "age":
        if not text.isdigit():
            await message.answer("Please send your age as a number, e.g. 22")
            return
        age = int(text)
        if age < 18:
            await users_col.update_one({"user_id": uid}, {"$set": {"underage": True, "setup": None}})
            await hide_dating(uid)
            await message.answer("Sorry, Shadow Chatter is 18+ only. Stay safe online 💛")
            return
        if age > 80:
            await message.answer("Please enter a valid age.")
            return
        await wiz_set(uid, "age", age, "city")
        await wiz_prompt(uid, message.chat.id, "city")
    elif step == "city":
        if not text or len(text) > 30:
            await message.answer("Send your city (max 30 characters).")
            return
        await wiz_set(uid, "city", text, "bio")
        await wiz_prompt(uid, message.chat.id, "bio")
    elif step == "bio":
        if not text or len(text) > 150:
            await message.answer("Keep your bio under 150 characters, or tap Skip.")
            return
        await wiz_set(uid, "bio", text, "photo")
        await wiz_prompt(uid, message.chat.id, "photo")
    elif step == "photo":
        if not message.photo:
            await message.answer("Please send a photo 📸 (as a normal photo, not a file).")
            return
        photos = wiz.get("photos", [])
        if len(photos) >= MAX_PHOTOS:
            await message.answer("That's the maximum. Tap Done ✅")
            return
        photos.append(message.photo[-1].file_id)
        await users_col.update_one({"user_id": uid}, {"$set": {"wiz.photos": photos}})
        extra = f"Send another (up to {MAX_PHOTOS}) or tap Done." if len(photos) < MAX_PHOTOS else "Tap Done."
        await message.answer(f"📸 Photo {len(photos)}/{MAX_PHOTOS} saved. {extra}",
                             reply_markup=kb([[btn("✅ Done", "w_done")]]))


def card_caption(u: dict) -> str:
    d = u["dating"]
    g = {"male": "👨", "female": "👩"}.get(u.get("gender"), "🧑")
    amb = " ⭐" if u.get("referrals", 0) >= AMBASSADOR_REFS else ""
    lines = [f"{g} <b>{esc(d['name'])}, {d['age']}</b>{amb}", f"📍 {esc(d['city'])}"]
    if d.get("bio"):
        lines.append(f"\n“{esc(d['bio'])}”")
    lines.append(f"\n🔎 Looking for: {LOOK_LABEL.get(u.get('looking'), 'Anyone')}")
    return "\n".join(lines)


async def send_card(chat_id: int, u: dict, markup=None, header: str = ""):
    d = u["dating"]
    caption = header + card_caption(u)
    try:
        return await bot.send_photo(chat_id, d["photos"][0], caption=caption, reply_markup=markup)
    except Exception as e:
        logger.warning("card photo failed: %s", e)
        return await safe_send(chat_id, caption, reply_markup=markup)


async def finalize_profile(uid: int, chat_id: int):
    u = await users_col.find_one({"user_id": uid}) or {}
    wiz = u.get("wiz") or {}
    old = u.get("dating") or {}
    dating = {
        "name": wiz.get("name") or u.get("first_name") or "Friend",
        "age": wiz.get("age", 18), "city": wiz.get("city", ""), "bio": wiz.get("bio", ""),
        "photos": wiz.get("photos", []), "active": True, "removed": False,
        "code": old.get("code") or new_code(), "updated": time.time(),
    }
    await users_col.update_one(
        {"user_id": uid}, {"$set": {"dating": dating, "setup": None, "mode": "dating"}, "$unset": {"wiz": ""}})
    u = await users_col.find_one({"user_id": uid})
    await send_fx(chat_id, "🎉 <b>Your profile is live!</b> Here's how others see you:", EFFECT_PARTY)
    await send_card(chat_id, u)
    await send_flair(chat_id, "love", 0.5)
    await dating_menu(uid, chat_id)

    if OWNER_ID:   # admin review copy
        markup = kb([[btn("🗑 Remove profile", f"adm_pdel:{uid}"), btn("⛔ Block user", f"adm_block:{uid}")]])
        await send_card(OWNER_ID, u, markup,
                        header=f"🆕 <b>New dating profile</b> · {who(u, uid)}\n\n")
        if len(dating["photos"]) > 1:
            try:
                await bot.send_media_group(
                    OWNER_ID, [InputMediaPhoto(media=f) for f in dating["photos"][1:]])
            except Exception:
                pass


# =========================================================
# DATING: discover, likes, matches
# =========================================================

async def seen_ids(uid: int) -> set:
    return {d["to"] async for d in likes_col.find({"from": uid}, {"to": 1})}


async def pending_admirers(uid: int, seen: set) -> list:
    out = []
    async for l in likes_col.find({"to": uid, "v": 1}).sort("ts", -1).limit(100):
        if l["from"] not in seen:
            out.append(l["from"])
    return out


async def pick_candidate(me: dict, admirers_only: bool = False):
    uid = me["user_id"]
    seen = await seen_ids(uid)
    for aid in await pending_admirers(uid, seen):
        c = await users_col.find_one(
            {"user_id": aid, "dating.active": True, "banned": {"$ne": True}, "underage": {"$ne": True}})
        if c:
            return c, True
    if admirers_only:
        return None, False
    q = {"user_id": {"$nin": list(seen) + [uid]}, "dating.active": True,
         "banned": {"$ne": True}, "underage": {"$ne": True},
         "looking": {"$in": [me.get("gender", "other"), "any"]}}
    if me.get("looking") in ("male", "female"):
        q["gender"] = me["looking"]
    docs = await users_col.find(q).limit(40).to_list(40)
    return (random.choice(docs), False) if docs else (None, False)


async def show_next(uid: int, chat_id: int, admirers_only: bool = False):
    me = await users_col.find_one({"user_id": uid}) or {}
    d = me.get("dating")
    if not d or not d.get("active"):
        await safe_send(chat_id, "Create (or unhide) your dating profile first 💘",
                        reply_markup=kb([[btn("✏️ Create profile", "dedit")], [btn("🏠 Menu", "home")]]))
        return
    cand, is_admirer = await pick_candidate(me, admirers_only)
    if not cand:
        text = "No admirers waiting right now 💌" if admirers_only else random.choice(EMPTY_DISCOVER)
        link = f"https://t.me/{await get_username()}?start=ref_{uid}"
        await safe_send(chat_id, text, reply_markup=kb([
            [btn("📤 Invite friends", url="https://t.me/share/url?url=" + link)],
            [btn("💘 Dating menu", "dmenu")]]))
        return
    header = "💌 <b>This person liked you!</b>\n\n" if is_admirer else ""
    await send_card(chat_id, cand, discover_keyboard(cand["user_id"], len(cand["dating"]["photos"])), header)


async def likes_left(me: dict) -> int:
    used = me.get("likes_n", 0) if me.get("likes_day") == today_ist() else 0
    limit = LIKES_BASE + min(50, LIKES_PER_REFERRAL * me.get("referrals", 0))
    return max(0, limit - used)


async def dating_menu(uid: int, chat_id: int):
    me = await users_col.find_one({"user_id": uid}) or {}
    d = me.get("dating")
    if not d:
        await safe_send(chat_id, "💘 <b>Dating</b>\nCreate your profile in 2 minutes and start matching!",
                        reply_markup=kb([[btn("✏️ Create profile", "dedit")], [btn("🏠 Menu", "home")]]))
        return
    seen = await seen_ids(uid)
    admirers = len(await pending_admirers(uid, seen))
    matches = await matches_col.count_documents({"users": uid})
    status = "✅ live" if d.get("active") else "⏸ hidden"
    await safe_send(
        chat_id,
        f"💘 <b>Dating</b>\nProfile: {status}\n💌 Admirers waiting: <b>{admirers}</b>\n"
        f"💞 Matches: <b>{matches}</b>\n❤️ Likes left today: <b>{await likes_left(me)}</b>",
        reply_markup=kb([
            [btn("🔥 Discover", "dsee"), btn(f"💌 Admirers ({admirers})", "dadm")],
            [btn("💞 My matches", "dmat"), btn("👁 My profile", "dprof")],
            [btn("✏️ Edit profile", "dedit"), btn("⏸/▶️ Hide/Show", "dtoggle")],
            [btn("📤 Share my profile", "dshare"), btn("🏠 Menu", "home")],
        ]))


@router.message(Command("dating"))
async def cmd_dating(message: types.Message):
    await dating_menu(message.from_user.id, message.chat.id)


@router.callback_query(F.data == "dmenu")
async def cb_dmenu(cb: CallbackQuery):
    await cb.answer()
    await try_delete(cb.message)
    await dating_menu(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data == "dedit")
async def cb_dedit(cb: CallbackQuery):
    await cb.answer()
    await wiz_start(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data == "dsee")
async def cb_dsee(cb: CallbackQuery):
    await cb.answer()
    await show_next(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data == "dadm")
async def cb_dadm(cb: CallbackQuery):
    await cb.answer()
    await show_next(cb.from_user.id, cb.from_user.id, admirers_only=True)


@router.callback_query(F.data == "dprof")
async def cb_dprof(cb: CallbackQuery):
    await cb.answer()
    u = await users_col.find_one({"user_id": cb.from_user.id}) or {}
    if u.get("dating"):
        await send_card(cb.from_user.id, u)
    else:
        await dating_menu(cb.from_user.id, cb.from_user.id)


@router.callback_query(F.data == "dtoggle")
async def cb_dtoggle(cb: CallbackQuery):
    u = await users_col.find_one({"user_id": cb.from_user.id}) or {}
    d = u.get("dating")
    if not d:
        await cb.answer("Create a profile first", show_alert=True)
        return
    if d.get("removed"):
        await cb.answer("Moderators removed this profile. Edit it to resubmit.", show_alert=True)
        return
    new = not d.get("active")
    await users_col.update_one({"user_id": cb.from_user.id}, {"$set": {"dating.active": new}})
    await cb.answer("Profile is now " + ("visible ✅" if new else "hidden ⏸"), show_alert=True)


@router.callback_query(F.data == "ddelete")
async def cb_ddelete(cb: CallbackQuery):
    await users_col.update_one({"user_id": cb.from_user.id}, {"$unset": {"dating": ""}})
    await cb.answer("Dating profile deleted", show_alert=True)


@router.callback_query(F.data == "dshare")
async def cb_dshare(cb: CallbackQuery):
    u = await users_col.find_one({"user_id": cb.from_user.id}) or {}
    d = u.get("dating")
    await cb.answer()
    if not d:
        await dating_menu(cb.from_user.id, cb.from_user.id)
        return
    link = f"https://t.me/{await get_username()}?start=u_{d['code']}"
    await safe_send(cb.from_user.id, f"📤 Share your profile link. Anyone who opens it sees your card:\n{link}",
                    reply_markup=kb([[btn("📤 Share", url="https://t.me/share/url?url=" + link +
                                          "&text=Check%20out%20my%20profile%20%F0%9F%92%98")]]))


async def show_profile_by_code(viewer: int, chat_id: int, code: str):
    cand = await users_col.find_one(
        {"dating.code": code, "dating.active": True, "banned": {"$ne": True}})
    if not cand or cand["user_id"] == viewer:
        return
    await send_card(chat_id, cand, discover_keyboard(cand["user_id"], len(cand["dating"]["photos"])),
                    "🔗 <b>Shared profile</b>\n\n")


@router.callback_query(F.data.startswith("dmore:"))
async def cb_dmore(cb: CallbackQuery):
    tid = int(cb.data.split(":")[1])
    t = await users_col.find_one({"user_id": tid}) or {}
    photos = (t.get("dating") or {}).get("photos", [])
    await cb.answer()
    if len(photos) > 1:
        try:
            await bot.send_media_group(cb.from_user.id, [InputMediaPhoto(media=f) for f in photos[1:]])
        except Exception as e:
            logger.warning("more photos failed: %s", e)


async def maybe_ping_liked(target: int):
    now = time.time()
    got = await users_col.find_one_and_update(
        {"user_id": target, "banned": {"$ne": True},
         "$or": [{"last_like_ping": {"$exists": False}}, {"last_like_ping": {"$lt": now - LIKE_PING_COOLDOWN}}]},
        {"$set": {"last_like_ping": now}})
    if got:
        await send_fx(target, random.choice(LIKE_PINGS), EFFECT_HEART,
                      reply_markup=kb([[btn("👀 See who", "dadm")]]))


async def make_match(a: int, b: int):
    key = ":".join(str(x) for x in sorted((a, b)))
    try:
        await matches_col.insert_one({"_id": key, "users": [a, b], "ts": time.time()})
    except DuplicateKeyError:
        return
    ua = await users_col.find_one({"user_id": a}) or {}
    ub = await users_col.find_one({"user_id": b}) or {}

    def contact(u, uid):
        name = esc((u.get("dating") or {}).get("name") or u.get("first_name") or "them")
        link = f'<a href="tg://user?id={uid}">{name}</a>'
        return f"{link} (@{esc(u['username'])})" if u.get("username") else link

    invite = kb([[btn("📤 Invite friends", "invite")], [btn("🏠 Menu", "home")]])
    await send_fx(a, f"{random.choice(MATCH_LINES)}\nYou and {contact(ub, b)} liked each other.\n"
                     "👉 Tap their name to say hi!", EFFECT_HEART, reply_markup=invite)
    await send_fx(b, f"{random.choice(MATCH_LINES)}\nYou and {contact(ua, a)} liked each other.\n"
                     "👉 Tap their name to say hi!", EFFECT_HEART, reply_markup=invite)
    await send_flair(a, "match")
    await send_flair(b, "match")
    await notify_owner(f"💞 <b>New match</b>\n{who(ua, a)}\n{who(ub, b)}")


@router.callback_query(F.data.startswith("dl:"))
async def cb_like(cb: CallbackQuery):
    uid, target = cb.from_user.id, int(cb.data.split(":")[1])
    me = await users_col.find_one({"user_id": uid}) or {}
    if not (me.get("dating") or {}).get("active"):
        await cb.answer("Create/unhide your profile to like people", show_alert=True)
        return
    if await likes_left(me) <= 0:
        await cb.answer()
        link = f"https://t.me/{await get_username()}?start=ref_{uid}"
        await safe_send(uid, f"❤️ You've used all your likes for today. Invite friends for +{LIKES_PER_REFERRAL} "
                             "daily likes each, or come back tomorrow!",
                        reply_markup=kb([[btn("📤 Invite friends", url="https://t.me/share/url?url=" + link)]]))
        return
    today = today_ist()
    if me.get("likes_day") == today:
        await users_col.update_one({"user_id": uid}, {"$inc": {"likes_n": 1}})
    else:
        await users_col.update_one({"user_id": uid}, {"$set": {"likes_day": today, "likes_n": 1}})
    await likes_col.update_one(
        {"_id": f"{uid}:{target}"}, {"$set": {"from": uid, "to": target, "v": 1, "ts": time.time()}}, upsert=True)
    await users_col.update_one({"user_id": target}, {"$inc": {"likes_received": 1}})
    reverse = await likes_col.find_one({"_id": f"{target}:{uid}", "v": 1})
    await cb.answer("💚 Liked!")
    await try_delete(cb.message)
    if reverse:
        await make_match(uid, target)
    else:
        await maybe_ping_liked(target)
    await show_next(uid, uid)


@router.callback_query(F.data.startswith("dk:"))
async def cb_skip(cb: CallbackQuery):
    uid, target = cb.from_user.id, int(cb.data.split(":")[1])
    await likes_col.update_one(
        {"_id": f"{uid}:{target}"}, {"$set": {"from": uid, "to": target, "v": 0, "ts": time.time()}}, upsert=True)
    await cb.answer()
    await try_delete(cb.message)
    await show_next(uid, uid)


@router.callback_query(F.data.startswith("drp:"))
async def cb_dreport(cb: CallbackQuery):
    uid, target = cb.from_user.id, int(cb.data.split(":")[1])
    try:
        await dreports_col.insert_one({"_id": f"{uid}:{target}", "target": target, "ts": time.time()})
    except DuplicateKeyError:
        await cb.answer("Already reported")
        return
    await likes_col.update_one(
        {"_id": f"{uid}:{target}"}, {"$set": {"from": uid, "to": target, "v": 0, "ts": time.time()}}, upsert=True)
    await cb.answer("Reported, thank you 🙏", show_alert=True)
    await try_delete(cb.message)
    t = await users_col.find_one({"user_id": target})
    count = await dreports_col.count_documents({"target": target})
    if t and t.get("dating"):
        if count >= AUTO_HIDE_REPORTS and t["dating"].get("active"):
            await hide_dating(target)
        await send_card(OWNER_ID, t, kb([[btn("🗑 Remove profile", f"adm_pdel:{target}"),
                                          btn("⛔ Block user", f"adm_block:{target}")]]),
                        header=f"🚩 <b>Profile reported</b> ({count}x) by <code>{uid}</code>\n"
                               f"{who(t, target)}\n\n") if OWNER_ID else None
    await show_next(uid, uid)


@router.callback_query(F.data == "dmat")
async def cb_matches(cb: CallbackQuery):
    uid = cb.from_user.id
    await cb.answer()
    docs = await matches_col.find({"users": uid}).sort("ts", -1).limit(15).to_list(15)
    if not docs:
        await safe_send(uid, "No matches yet. Keep liking, it happens fast 💘",
                        reply_markup=kb([[btn("🔥 Discover", "dsee")]]))
        return
    lines = ["💞 <b>Your matches</b>"]
    for m in docs:
        other = [x for x in m["users"] if x != uid][0]
        u = await users_col.find_one({"user_id": other}) or {}
        name = esc((u.get("dating") or {}).get("name") or u.get("first_name") or "?")
        tail = f" (@{esc(u['username'])})" if u.get("username") else ""
        lines.append(f'• <a href="tg://user?id={other}">{name}</a>{tail}')
    await safe_send(uid, "\n".join(lines), reply_markup=kb([[btn("🏠 Menu", "home")]]))


@router.callback_query(F.data == "topmenu")
async def cb_top(cb: CallbackQuery):
    await cb.answer()
    docs = await users_col.find(
        {"dating.active": True, "banned": {"$ne": True}, "likes_received": {"$gt": 0}}
    ).sort("likes_received", -1).limit(10).to_list(10)
    lines = ["🔥 <b>Most popular profiles</b>"]
    medals = ["🥇", "🥈", "🥉"]
    for i, u in enumerate(docs):
        d = u["dating"]
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {esc(d['name'])}, {d['age']} · "
                     f"📍{esc(d['city'])} · ❤️ {u.get('likes_received', 0)}")
    if not docs:
        lines.append("Be the first! Create your profile 💘")
    await safe_send(cb.from_user.id, "\n".join(lines), reply_markup=kb([
        [btn("💘 Dating", "dmenu"), btn("🏠 Menu", "home")]]))


# =========================================================
# ADMIN
# =========================================================

@router.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if is_owner(message.from_user.id):
        await message.answer(ADMIN_HELP)


@router.message(Command("stats"))
async def cmd_stats(message: types.Message):
    uid = message.from_user.id
    if not is_owner(uid):
        d = await users_col.find_one({"user_id": uid}) or {}
        await message.answer(
            "📊 <b>Your stats</b>\n"
            f"Chats: {d.get('chats', 0)}\nReputation: {badge(d)} (👍 {d.get('good', 0)} / 👎 {d.get('bad', 0)})\n"
            f"❤️ Likes received: {d.get('likes_received', 0)}\nFriends invited: {d.get('referrals', 0)}")
        return
    counter = await counters_col.find_one({"_id": "archive"}) or {}
    await message.answer(
        "📊 <b>Admin stats</b>\n"
        f"👤 Users: {await users_col.count_documents({})} (🟢 {await count_online()} active 10 min)\n"
        f"💬 Live chats: {await chats_col.count_documents({})} · ⏳ waiting: {await queue_col.count_documents({})}\n"
        f"💘 Dating profiles: {await users_col.count_documents({'dating.active': True})} · "
        f"💞 matches: {await matches_col.count_documents({})}\n"
        f"⛔ Blocked: {await users_col.count_documents({'banned': True})} · "
        f"🔞 under-18 blocked: {await users_col.count_documents({'underage': True})}\n"
        f"📁 Saved chats: {counter.get('n', 0)}")


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

    def nm(i):
        return f"@{esc(i['username'])}" if i.get("username") else esc(i.get("first_name") or "?")

    await message.answer("\n\n".join(
        f"{TAG_LABEL.get(a.get('tag', 'unrated'))} · <b>Chat-{a['_id']:02d}</b> · {a['date_text']}\n"
        f"   {nm(a['info']['a'])} ↔ {nm(a['info']['b'])} · {a['count']} msgs\n"
        f"   ▶️ /openchat_{a['_id']:02d}" for a in docs))


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
    res = await archives_col.delete_one({"_id": int(m.group(1))})
    await message.answer("Deleted." if res.deleted_count else "Not found.")


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
    await safe_send(target, "✅ Your account has been restored. Use /menu to continue.")
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
    dd = d.get("dating") or {}
    await message.answer(
        f"👤 {who(d, d['user_id'])}\n"
        f"Status: {'⛔ BLOCKED' if d.get('banned') else '🔞 under-18' if d.get('underage') else '✅ active'}\n"
        f"Gender: {d.get('gender', '-')} → looking for {d.get('looking', '-')} · mode {d.get('mode', '-')}\n"
        f"Chats: {d.get('chats', 0)} · 👍 {d.get('good', 0)} · 👎 {d.get('bad', 0)} · {badge(d)}\n"
        f"Dating: {'yes' if dd else 'no'}"
        + (f" ({esc(dd.get('name', ''))}, {dd.get('age')}, {esc(dd.get('city', ''))}, "
           f"{'live' if dd.get('active') else 'hidden'})" if dd else "")
        + f"\n❤️ received {d.get('likes_received', 0)} · invited {d.get('referrals', 0)}")


@router.message(Command("profileof"))
async def cmd_profileof(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        return
    target = await resolve_user(command.args)
    u = await users_col.find_one({"user_id": target}) if target else None
    if not u or not u.get("dating"):
        await message.answer("No dating profile found.")
        return
    await send_card(message.chat.id, u, kb([[btn("🗑 Remove profile", f"adm_pdel:{target}"),
                                             btn("⛔ Block user", f"adm_block:{target}")]]),
                    header=f"{who(u, target)}\n\n")


@router.callback_query(F.data.startswith("adm_block:"))
async def cb_adm_block(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    await cb.answer("Blocked")
    await safe_send(cb.from_user.id, await block_user(int(cb.data.split(":", 1)[1])))


@router.callback_query(F.data.startswith("adm_pdel:"))
async def cb_adm_pdel(cb: CallbackQuery):
    if not is_owner(cb.from_user.id):
        await cb.answer()
        return
    target = int(cb.data.split(":", 1)[1])
    await hide_dating(target, removed=True)
    await cb.answer("Profile removed")
    await safe_send(target, "⚠️ A moderator removed your dating profile for breaking the rules. "
                            "You can edit it and resubmit from the Dating menu.")
    await safe_send(cb.from_user.id, f"🗑 Removed profile of <code>{target}</code>")


@router.message(Command("addmedia"))
async def cmd_addmedia(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id):
        return
    cat = (command.args or "").strip().lower()
    if cat not in ("welcome", "match", "found", "love", "bye"):
        await message.answer("Usage: /addmedia welcome | match | found | love | bye\n"
                             "Then send GIFs or stickers. /donemedia when finished.")
        return
    media_pending[message.from_user.id] = cat
    await message.answer(f"📥 Send GIFs/stickers for <b>{cat}</b> now. /donemedia to stop.")


@router.message(Command("donemedia"))
async def cmd_donemedia(message: types.Message):
    if is_owner(message.from_user.id):
        media_pending.pop(message.from_user.id, None)
        await message.answer("✅ Done.")


@router.message(Command("media"))
async def cmd_media(message: types.Message):
    if not is_owner(message.from_user.id):
        return
    lines = ["🎞 <b>Media pool</b>"]
    for cat in ("welcome", "match", "found", "love", "bye"):
        lines.append(f"• {cat}: {await media_col.count_documents({'cat': cat})}")
    await message.answer("\n".join(lines))


@router.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if not is_owner(message.from_user.id) or not command.args:
        return
    sent = 0
    async for u in users_col.find({"banned": {"$ne": True}, "underage": {"$ne": True}}, {"user_id": 1}):
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

    # owner is adding GIFs/stickers to the pool
    if is_owner(user_id) and user_id in media_pending:
        kind = fid = None
        if message.animation:
            kind, fid = "animation", message.animation.file_id
        elif message.sticker:
            kind, fid = "sticker", message.sticker.file_id
        if fid:
            await media_col.update_one(
                {"file_id": fid}, {"$set": {"cat": media_pending[user_id], "kind": kind, "file_id": fid}}, upsert=True)
            await message.answer(f"✅ Saved to <b>{media_pending[user_id]}</b>. Send more or /donemedia")
            return

    u = await users_col.find_one({"user_id": user_id}) or {}
    await users_col.update_one({"user_id": user_id}, {"$set": {"last_seen": time.time()}})

    if u.get("setup"):                       # dating profile wizard
        await wiz_input(message, u)
        return

    chat = await find_chat(user_id)
    if not chat:
        if await queue_col.find_one({"user_id": user_id}):
            await message.answer("still looking for someone...")
        else:
            await message.answer("you're not connected to anyone.", reply_markup=search_keyboard())
        return

    partner_id = other_party(chat, user_id)

    # AI buddy
    if isinstance(partner_id, str):
        text = message.text or message.caption or ""
        if not text:
            text = "[the user sent media you can't see]"
        key = partner_id.split(":", 1)[1]
        cancel_ai_task(user_id)
        ai_tasks[user_id] = asyncio.create_task(ai_reply(user_id, text, message.chat.id, key))
        return

    # human chat
    try:
        await bot.copy_message(chat_id=partner_id, from_chat_id=message.chat.id, message_id=message.message_id)
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

    try:
        updated = await chats_col.find_one_and_update(
            {"_id": chat["_id"]}, {"$inc": {"msg_count": 1}}, return_document=ReturnDocument.AFTER)
        if updated and updated.get("msg_count", 0) <= MAX_LOGGED_MESSAGES:
            mtype, text, file_id = extract_content(message)
            await msgs_col.insert_one({"chat_key": str(chat["_id"]), "sender": user_id,
                                       "type": mtype, "text": text, "file_id": file_id})
    except Exception as e:
        logger.warning("log failed: %s", e)

    text = message.text or message.caption or ""
    if text and PERSONAL_INFO.search(text):
        res = await chats_col.update_one(
            {"_id": chat["_id"], "warned": {"$ne": user_id}}, {"$addToSet": {"warned": user_id}})
        if res.modified_count:
            await safe_send(user_id, "🛡 Tip: keep your phone number, links and socials private. "
                                     "You can swap usernames safely with 🤝 Reveal if you both agree.")


# =========================================================
# GROUP / CHANNEL PROTECTION, HEALTH, MAIN
# =========================================================

@router.my_chat_member()
async def on_added_somewhere(event: types.ChatMemberUpdated):
    if event.chat.type != "private" and event.new_chat_member.status in ("member", "administrator"):
        try:
            await bot.leave_chat(event.chat.id)
        except Exception as e:
            logger.warning("leave_chat failed: %s", e)


async def ping(request: web.Request):
    return web.Response(text="OK", status=200, content_type="text/plain",
                        headers={"Cache-Control": "no-store"})


async def start_http_server():
    app = web.Application()
    app.router.add_get("/", ping)
    app.router.add_get("/health", ping)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("HTTP health server running on port %s", PORT)
    return runner


async def create_indexes():
    await reviews_col.create_index("created_at", expireAfterSeconds=86400)
    await likes_col.create_index([("to", 1), ("v", 1)])
    await likes_col.create_index([("from", 1)])
    await matches_col.create_index([("users", 1)])
    await media_col.create_index([("cat", 1)])
    await users_col.create_index([("dating.active", 1), ("gender", 1), ("looking", 1)])
    await users_col.create_index([("dating.code", 1)])
    await users_col.create_index([("username_lower", 1)])


async def shutdown():
    pending = list(wait_tasks.values()) + list(ai_tasks.values()) + list(bg_tasks)
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
        await create_indexes()
        logger.info("ShadowChatter v4 started (AI buddies: %s)", "on" if AI_ENABLED else "off")
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
