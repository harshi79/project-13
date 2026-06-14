import base64
import html
import io
import json
import logging
import os
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
import telebot
from dotenv import load_dotenv
from telebot import types

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("telegram-image-bot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
REGISTRY_PATH = os.path.join(DATA_DIR, "registry.json")
os.makedirs(DATA_DIR, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
IMAGE_API_URL = os.getenv(
    "IMAGE_API_URL",
    "https://nepcoderapis.pages.dev/api/v1/images/generations",
).strip()
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-image-1").strip().lower()
DEFAULT_SIZE = os.getenv("DEFAULT_SIZE", "1024x1024").strip().lower()
DEFAULT_QUALITY = os.getenv("DEFAULT_QUALITY", "standard").strip().lower()
DEFAULT_ENHANCE = os.getenv("DEFAULT_ENHANCE", "off").strip().lower()
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "180"))
REQUEST_RETRIES = int(os.getenv("REQUEST_RETRIES", "2"))
WELCOME_IMAGE_URL = os.getenv(
    "WELCOME_IMAGE_URL",
    "https://files.catbox.moe/xmqm6h.png",
).strip()
DEVELOPER_URL = os.getenv("DEVELOPER_URL", "https://t.me/yorichiiprime").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/yorifederation").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "7728424218").split(",")
    if value.strip().isdigit()
}

SUPPORTED_MODELS = ["gpt-image-1", "dall-e-3", "nextlm-image-1", "dall-e-2"]
MODEL_ALIASES = {
    "gpt-image-1": "gpt-image-1",
    "dalle3": "dall-e-3",
    "dall-e-3": "dall-e-3",
    "othmaker2": "nextlm-image-1",
    "nextlm-image-1": "nextlm-image-1",
    "dalle2": "dall-e-2",
    "dall-e-2": "dall-e-2",
}
SUPPORTED_SIZES = ["1024x1024", "1024x1536", "1536x1024", "auto"]
SUPPORTED_QUALITIES = ["standard", "hd", "high", "auto"]
MAX_PROMPT_CACHE = 300
MAX_NEGATIVE_PROMPT_LEN = 320
LOADING_FRAMES = ["⏳", "⌛", "🌘", "🌗", "🌕", "⚡"]

MODEL_LABELS = {
    "gpt-image-1": "gpt-image-1",
    "dall-e-3": "dall·e 3",
    "nextlm-image-1": "nextlm image 1",
    "dall-e-2": "dall·e 2",
}
QUALITY_LABELS = {
    "standard": "standard",
    "hd": "hd",
    "high": "high",
    "auto": "auto",
}

BRAND_LINK = f'<a href="{CHANNEL_URL}">@yorifederation</a>'
DEV_LINK = f'<a href="{DEVELOPER_URL}">@yorichiiprime</a>'
BRAND_FOOTER = f"channel {BRAND_LINK}"
DEV_FOOTER = f"dev {DEV_LINK}"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "Missing TELEGRAM_BOT_TOKEN. Add it to your environment or .env file."
    )

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode="HTML")
state_lock = threading.Lock()
user_settings: Dict[int, Dict[str, Any]] = {}
prompt_cache: Dict[str, Dict[str, Any]] = {}
ui_messages: Dict[Tuple[int, int], Dict[str, Any]] = {}
ui_owners: Dict[Tuple[int, int], int] = {}
pending_inputs: Dict[int, Dict[str, Any]] = {}
active_generation_users: Set[int] = set()
queued_generation_jobs: Dict[int, List[Dict[str, Any]]] = {}


def safe_trim(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def normalize_model(value: str) -> str:
    value = (value or "").strip().lower()
    canonical = MODEL_ALIASES.get(value, value)
    return canonical if canonical in SUPPORTED_MODELS else "gpt-image-1"


def normalize_size(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value in SUPPORTED_SIZES else "1024x1024"


def normalize_quality(value: str) -> str:
    value = (value or "").strip().lower()
    return value if value in SUPPORTED_QUALITIES else "standard"


def normalize_enhance(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "on", "yes", "enable", "enabled"}


def display_model(model: str) -> str:
    return MODEL_LABELS.get(model, model)


def display_quality(quality: str) -> str:
    return QUALITY_LABELS.get(quality, quality)


def display_check(current: str, value: str, label: str) -> str:
    return f"● {label}" if current == value else label


def load_registry() -> Tuple[Set[int], Set[int]]:
    if not os.path.exists(REGISTRY_PATH):
        return set(), set()

    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as exc:
        logger.warning("Could not read registry.json: %s", exc)
        return set(), set()

    chat_ids = {
        int(value)
        for value in data.get("chat_ids", [])
        if str(value).strip().lstrip("-").isdigit()
    }
    user_ids = {
        int(value)
        for value in data.get("user_ids", [])
        if str(value).strip().lstrip("-").isdigit()
    }
    return chat_ids, user_ids


known_chat_ids, known_user_ids = load_registry()


def save_registry() -> None:
    with open(REGISTRY_PATH, "w", encoding="utf-8") as file:
        json.dump(
            {
                "chat_ids": sorted(known_chat_ids),
                "user_ids": sorted(known_user_ids),
            },
            file,
            ensure_ascii=False,
            indent=2,
        )


def register_interaction(user_id: int, chat_id: int) -> None:
    changed = False
    with state_lock:
        if user_id and user_id not in known_user_ids:
            known_user_ids.add(user_id)
            changed = True
        if chat_id and chat_id not in known_chat_ids:
            known_chat_ids.add(chat_id)
            changed = True
    if changed:
        try:
            save_registry()
        except Exception as exc:
            logger.warning("Could not save registry: %s", exc)


def register_message_context(message) -> None:
    if message and message.from_user and message.chat:
        register_interaction(message.from_user.id, message.chat.id)


def register_callback_context(call) -> None:
    if call and call.from_user and call.message and call.message.chat:
        register_interaction(call.from_user.id, call.message.chat.id)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def get_ui_view(user_id: int, chat_id: int) -> str:
    with state_lock:
        entry = ui_messages.get((user_id, chat_id))
    if not entry:
        return "home"
    return str(entry.get("view") or "home")


def get_user_queue_snapshot(user_id: int) -> Tuple[bool, int]:
    with state_lock:
        is_active = user_id in active_generation_users
        queued = len(queued_generation_jobs.get(user_id, []))
    return is_active, queued


def get_global_queue_snapshot() -> Tuple[int, int]:
    with state_lock:
        active_total = len(active_generation_users)
        queued_total = sum(len(items) for items in queued_generation_jobs.values())
    return active_total, queued_total


def reserve_generation_slot(job: Dict[str, Any]) -> Tuple[bool, int]:
    user_id = int(job["user_id"])
    with state_lock:
        if user_id in active_generation_users:
            queue = queued_generation_jobs.setdefault(user_id, [])
            queue.append(job)
            return False, len(queue)
        active_generation_users.add(user_id)
        return True, 0


def complete_generation_and_get_next(user_id: int) -> Tuple[Optional[Dict[str, Any]], int]:
    with state_lock:
        queue = queued_generation_jobs.get(user_id, [])
        if queue:
            next_job = queue.pop(0)
            remaining = len(queue)
            if not queue:
                queued_generation_jobs.pop(user_id, None)
            return next_job, remaining

        active_generation_users.discard(user_id)
        queued_generation_jobs.pop(user_id, None)
        return None, 0


def get_user_settings(user_id: int) -> Dict[str, Any]:
    with state_lock:
        settings = user_settings.setdefault(
            user_id,
            {
                "model": normalize_model(DEFAULT_MODEL),
                "size": normalize_size(DEFAULT_SIZE),
                "quality": normalize_quality(DEFAULT_QUALITY),
                "enhance": normalize_enhance(DEFAULT_ENHANCE),
                "negative_prompt": "",
            },
        )
        return settings


def remember_prompt(
    user_id: int,
    prompt: str,
    model: str,
    size: str,
    quality: str,
    enhance: bool,
    negative_prompt: str,
) -> str:
    prompt_id = secrets.token_hex(4)
    with state_lock:
        prompt_cache[prompt_id] = {
            "user_id": user_id,
            "prompt": prompt,
            "model": model,
            "size": size,
            "quality": quality,
            "enhance": enhance,
            "negative_prompt": negative_prompt,
        }
        while len(prompt_cache) > MAX_PROMPT_CACHE:
            oldest_key = next(iter(prompt_cache))
            prompt_cache.pop(oldest_key, None)
    return prompt_id


def get_ui_message(user_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
    with state_lock:
        return ui_messages.get((user_id, chat_id))


def save_ui_message(
    user_id: int,
    chat_id: int,
    message_id: int,
    is_photo: bool,
    view: str,
) -> None:
    with state_lock:
        ui_messages[(user_id, chat_id)] = {
            "message_id": message_id,
            "is_photo": is_photo,
            "view": view,
        }
        ui_owners[(chat_id, message_id)] = user_id


def is_ui_owner(chat_id: int, message_id: int, user_id: int) -> bool:
    with state_lock:
        return ui_owners.get((chat_id, message_id)) == user_id


def set_pending_input(user_id: int, action: str) -> None:
    with state_lock:
        pending_inputs[user_id] = {"action": action}


def pop_pending_input(user_id: int) -> Optional[Dict[str, Any]]:
    with state_lock:
        return pending_inputs.pop(user_id, None)


def has_pending_input(user_id: int) -> bool:
    with state_lock:
        return user_id in pending_inputs


def clear_pending_input(user_id: int) -> bool:
    with state_lock:
        existed = user_id in pending_inputs
        pending_inputs.pop(user_id, None)
        return existed


def message_is_photo(message) -> bool:
    return bool(getattr(message, "photo", None))


def format_negative_preview(negative_prompt: str) -> str:
    preview = negative_prompt if negative_prompt.strip() else "none"
    return html.escape(safe_trim(preview, 68))


def build_notice(notice: Optional[str]) -> str:
    if not notice:
        return ""
    return f"\n<b>status</b> <code>{html.escape(safe_trim(notice, 120))}</code>\n"


def build_queue_line(user_id: int) -> str:
    is_active, queued = get_user_queue_snapshot(user_id)
    state_label = "busy" if is_active else "idle"
    return f"• ǫᴜᴇᴜᴇ <code>{queued}</code> • sᴛᴀᴛᴇ <code>{state_label}</code>\n"


def build_home_caption(user_id: int, notice: Optional[str] = None) -> str:
    settings = get_user_settings(user_id)
    return (
        "✦ <b>ʏᴏʀɪ ɪᴍᴀɢᴇ sᴛᴜᴅɪᴏ</b>\n"
        "ᴘʀᴏᴍᴘᴛ → ᴘʀᴏ ᴀɪ ᴀʀᴛ ɪɴ sᴇᴄᴏɴᴅs\n"
        f"{build_notice(notice)}"
        f"• ᴍᴏᴅᴇʟ <code>{html.escape(display_model(settings['model']))}</code>\n"
        f"• sɪᴢᴇ <code>{html.escape(settings['size'])}</code>\n"
        f"• ǫᴜᴀʟɪᴛʏ <code>{html.escape(display_quality(settings['quality']))}</code>\n"
        f"• ᴇɴʜᴀɴᴄᴇ <code>{'on' if settings['enhance'] else 'off'}</code>\n"
        f"{build_queue_line(user_id)}\n"
        "send <code>/img your prompt</code>\n"
        "or type prompt directly in private\n\n"
        f"{BRAND_FOOTER}\n{DEV_FOOTER}"
    )


def build_panel_caption(user_id: int, notice: Optional[str] = None) -> str:
    settings = get_user_settings(user_id)
    return (
        "✦ <b>sᴛᴜᴅɪᴏ ᴄᴏɴᴛʀᴏʟ ᴘᴀɴᴇʟ</b>\n"
        f"{build_notice(notice)}"
        f"• ᴍᴏᴅᴇʟ <code>{html.escape(display_model(settings['model']))}</code>\n"
        f"• sɪᴢᴇ <code>{html.escape(settings['size'])}</code>\n"
        f"• ǫᴜᴀʟɪᴛʏ <code>{html.escape(display_quality(settings['quality']))}</code>\n"
        f"• ᴇɴʜᴀɴᴄᴇ <code>{'on' if settings['enhance'] else 'off'}</code>\n"
        f"• ɴᴇɢ <code>{format_negative_preview(settings['negative_prompt'])}</code>\n"
        f"{build_queue_line(user_id)}\n"
        "tap buttons below to update this same message\n"
        "set neg → next normal text becomes your negative prompt\n\n"
        f"{BRAND_FOOTER}"
    )


def build_help_caption(user_id: int, notice: Optional[str] = None) -> str:
    return (
        "✦ <b>ǫᴜɪᴄᴋ ʜᴇʟᴘ</b>\n"
        f"{build_notice(notice)}"
        "• <code>/img neon tiger in tokyo rain</code>\n"
        "• <code>/img dall-e-3 | astronaut on a horse</code>\n"
        "• <code>/img storm knight --enhance</code>\n"
        "• <code>/img dark castle --neg text, blurry, watermark</code>\n"
        "• <code>/setneg</code> then send neg prompt\n"
        "• tap result buttons to rerender fast\n\n"
        f"{BRAND_FOOTER}\n{DEV_FOOTER}"
    )


def build_models_caption(user_id: int, notice: Optional[str] = None) -> str:
    settings = get_user_settings(user_id)
    return (
        "✦ <b>ᴍᴏᴅᴇʟ ɢᴜɪᴅᴇ</b>\n"
        f"{build_notice(notice)}"
        "• <code>gpt-image-1</code> versatile + sharp\n"
        "• <code>dall-e-3</code> creative + polished\n"
        "• <code>nextlm-image-1</code> stylized + bold\n"
        "• <code>dall-e-2</code> classic fallback\n\n"
        f"current <code>{html.escape(display_model(settings['model']))}</code>\n"
        f"{build_queue_line(user_id)}\n"
        "open studio to switch instantly\n\n"
        f"{BRAND_FOOTER}"
    )


def build_admin_caption(user_id: int, notice: Optional[str] = None) -> str:
    active_total, queued_total = get_global_queue_snapshot()
    with state_lock:
        total_users = len(known_user_ids)
        total_chats = len(known_chat_ids)

    return (
        "✦ <b>ᴏᴡɴᴇʀ ᴘᴀɴᴇʟ</b>\n"
        f"{build_notice(notice)}"
        f"• ᴜsᴇʀs <code>{total_users}</code>\n"
        f"• ᴄʜᴀᴛs <code>{total_chats}</code>\n"
        f"• ᴀᴄᴛɪᴠᴇ ʀᴇɴᴅᴇʀs <code>{active_total}</code>\n"
        f"• ǫᴜᴇᴜᴇᴅ ᴊᴏʙs <code>{queued_total}</code>\n\n"
        "broadcast:\n"
        "• reply to any msg with <code>/broadcast</code>\n"
        "• or use <code>/broadcast your text</code>\n\n"
        f"{BRAND_FOOTER}\n{DEV_FOOTER}"
    )


def build_ui_caption(view: str, user_id: int, notice: Optional[str] = None) -> str:
    if view == "panel":
        return build_panel_caption(user_id, notice)
    if view == "help":
        return build_help_caption(user_id, notice)
    if view == "models":
        return build_models_caption(user_id, notice)
    if view == "admin" and is_admin(user_id):
        return build_admin_caption(user_id, notice)
    return build_home_caption(user_id, notice)


def build_home_markup(user_id: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton("🪄 create", callback_data="ui:create"),
        types.InlineKeyboardButton("🎛 studio", callback_data="view:panel"),
    )
    markup.row(
        types.InlineKeyboardButton("🧠 models", callback_data="view:models"),
        types.InlineKeyboardButton("❔ help", callback_data="view:help"),
    )
    if is_admin(user_id):
        markup.row(
            types.InlineKeyboardButton("🛡 owner", callback_data="view:admin"),
        )
    markup.row(
        types.InlineKeyboardButton("📢 channel", url=CHANNEL_URL),
        types.InlineKeyboardButton("👨‍💻 developer", url=DEVELOPER_URL),
    )
    return markup


def build_panel_markup(user_id: int) -> types.InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    markup = types.InlineKeyboardMarkup(row_width=4)
    markup.row(
        types.InlineKeyboardButton(
            display_check(settings["model"], "gpt-image-1", "gpt-1"),
            callback_data="set:model:gpt-image-1",
        ),
        types.InlineKeyboardButton(
            display_check(settings["model"], "dall-e-3", "dall·e 3"),
            callback_data="set:model:dall-e-3",
        ),
        types.InlineKeyboardButton(
            display_check(settings["model"], "nextlm-image-1", "nextlm"),
            callback_data="set:model:nextlm-image-1",
        ),
        types.InlineKeyboardButton(
            display_check(settings["model"], "dall-e-2", "dall·e 2"),
            callback_data="set:model:dall-e-2",
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            display_check(settings["size"], "1024x1024", "1:1"),
            callback_data="set:size:1024x1024",
        ),
        types.InlineKeyboardButton(
            display_check(settings["size"], "1024x1536", "2:3"),
            callback_data="set:size:1024x1536",
        ),
        types.InlineKeyboardButton(
            display_check(settings["size"], "1536x1024", "3:2"),
            callback_data="set:size:1536x1024",
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            display_check(settings["quality"], "standard", "std"),
            callback_data="set:quality:standard",
        ),
        types.InlineKeyboardButton(
            display_check(settings["quality"], "hd", "hd"),
            callback_data="set:quality:hd",
        ),
        types.InlineKeyboardButton(
            display_check(settings["quality"], "high", "high"),
            callback_data="set:quality:high",
        ),
        types.InlineKeyboardButton(
            display_check(settings["quality"], "auto", "auto"),
            callback_data="set:quality:auto",
        ),
    )
    markup.row(
        types.InlineKeyboardButton(
            f"✨ enhance {'on' if settings['enhance'] else 'off'}",
            callback_data="toggle:enhance",
        ),
        types.InlineKeyboardButton("➖ set neg", callback_data="panel:setneg"),
        types.InlineKeyboardButton("🧹 clear neg", callback_data="panel:clrneg"),
    )
    nav_row = [
        types.InlineKeyboardButton("📌 example", callback_data="ui:example"),
        types.InlineKeyboardButton("🏠 home", callback_data="view:home"),
        types.InlineKeyboardButton("❔ help", callback_data="view:help"),
    ]
    if is_admin(user_id):
        nav_row.append(types.InlineKeyboardButton("🛡 owner", callback_data="view:admin"))
    markup.row(*nav_row)
    markup.row(
        types.InlineKeyboardButton("📢 channel", url=CHANNEL_URL),
        types.InlineKeyboardButton("👨‍💻 developer", url=DEVELOPER_URL),
    )
    return markup


def build_help_markup(user_id: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton("🎛 studio", callback_data="view:panel"),
        types.InlineKeyboardButton("🏠 home", callback_data="view:home"),
    )
    second_row = [
        types.InlineKeyboardButton("🧠 models", callback_data="view:models"),
        types.InlineKeyboardButton("📌 example", callback_data="ui:example"),
    ]
    if is_admin(user_id):
        second_row.append(types.InlineKeyboardButton("🛡 owner", callback_data="view:admin"))
    markup.row(*second_row)
    markup.row(
        types.InlineKeyboardButton("📢 channel", url=CHANNEL_URL),
        types.InlineKeyboardButton("👨‍💻 developer", url=DEVELOPER_URL),
    )
    return markup


def build_models_markup(user_id: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton("🎛 studio", callback_data="view:panel"),
        types.InlineKeyboardButton("🏠 home", callback_data="view:home"),
    )
    second_row = [
        types.InlineKeyboardButton("❔ help", callback_data="view:help"),
        types.InlineKeyboardButton("📌 example", callback_data="ui:example"),
    ]
    if is_admin(user_id):
        second_row.append(types.InlineKeyboardButton("🛡 owner", callback_data="view:admin"))
    markup.row(*second_row)
    markup.row(
        types.InlineKeyboardButton("📢 channel", url=CHANNEL_URL),
        types.InlineKeyboardButton("👨‍💻 developer", url=DEVELOPER_URL),
    )
    return markup


def build_admin_markup(user_id: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.row(
        types.InlineKeyboardButton("👥 audience", callback_data="admin:audience"),
        types.InlineKeyboardButton("📦 queue", callback_data="admin:queue"),
        types.InlineKeyboardButton("📣 broadcast", callback_data="admin:broadcasthelp"),
    )
    markup.row(
        types.InlineKeyboardButton("🎛 studio", callback_data="view:panel"),
        types.InlineKeyboardButton("🏠 home", callback_data="view:home"),
        types.InlineKeyboardButton("🔄 refresh", callback_data="view:admin"),
    )
    markup.row(
        types.InlineKeyboardButton("📢 channel", url=CHANNEL_URL),
        types.InlineKeyboardButton("👨‍💻 developer", url=DEVELOPER_URL),
    )
    return markup


def build_ui_markup(view: str, user_id: int) -> types.InlineKeyboardMarkup:
    if view == "panel":
        return build_panel_markup(user_id)
    if view == "help":
        return build_help_markup(user_id)
    if view == "models":
        return build_models_markup(user_id)
    if view == "admin" and is_admin(user_id):
        return build_admin_markup(user_id)
    return build_home_markup(user_id)


def safe_edit_ui_message(
    chat_id: int,
    message_id: int,
    is_photo: bool,
    text: str,
    markup: types.InlineKeyboardMarkup,
) -> bool:
    try:
        if is_photo:
            bot.edit_message_caption(
                caption=text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
                reply_markup=markup,
            )
        else:
            bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        return True
    except Exception as exc:
        error = str(exc).lower()
        if "message is not modified" in error:
            return True
        logger.debug("ui edit failed for chat=%s message=%s: %s", chat_id, message_id, exc)
        return False


def upsert_ui_message(
    chat_id: int,
    user_id: int,
    view: str,
    notice: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
) -> None:
    text = build_ui_caption(view, user_id, notice)
    markup = build_ui_markup(view, user_id)
    existing = get_ui_message(user_id, chat_id)

    if existing:
        edited = safe_edit_ui_message(
            chat_id=chat_id,
            message_id=int(existing["message_id"]),
            is_photo=bool(existing["is_photo"]),
            text=text,
            markup=markup,
        )
        if edited:
            save_ui_message(
                user_id=user_id,
                chat_id=chat_id,
                message_id=int(existing["message_id"]),
                is_photo=bool(existing["is_photo"]),
                view=view,
            )
            return

    try:
        sent = bot.send_photo(
            chat_id,
            photo=WELCOME_IMAGE_URL,
            caption=text,
            parse_mode="HTML",
            reply_markup=markup,
            reply_to_message_id=reply_to_message_id,
        )
        save_ui_message(user_id, chat_id, sent.message_id, True, view)
    except Exception as exc:
        logger.warning("Welcome image send failed, using text UI: %s", exc)
        sent = bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            reply_to_message_id=reply_to_message_id,
            disable_web_page_preview=True,
        )
        save_ui_message(user_id, chat_id, sent.message_id, False, view)


def update_current_ui_from_callback(
    call,
    view: str,
    notice: Optional[str] = None,
) -> None:
    is_photo = message_is_photo(call.message)
    text = build_ui_caption(view, call.from_user.id, notice)
    markup = build_ui_markup(view, call.from_user.id)
    success = safe_edit_ui_message(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        is_photo=is_photo,
        text=text,
        markup=markup,
    )
    if success:
        save_ui_message(
            user_id=call.from_user.id,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            is_photo=is_photo,
            view=view,
        )
    else:
        upsert_ui_message(
            chat_id=call.message.chat.id,
            user_id=call.from_user.id,
            view=view,
            notice=notice,
        )


def extract_text_error(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("error", "message", "detail", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = extract_text_error(value)
                if nested:
                    return nested
        return json.dumps(payload, ensure_ascii=False)[:400]
    if isinstance(payload, list) and payload:
        return extract_text_error(payload[0])
    return str(payload)[:400]


def _decode_base64_image(value: str) -> Optional[io.BytesIO]:
    try:
        raw = base64.b64decode(value)
        bio = io.BytesIO(raw)
        bio.name = "generated.png"
        bio.seek(0)
        return bio
    except Exception:
        return None


def extract_first_image_payload(
    payload: Any,
) -> Tuple[Optional[str], Optional[io.BytesIO], Dict[str, Any]]:
    metadata: Dict[str, Any] = {}

    if not isinstance(payload, dict):
        return None, None, metadata

    for meta_key in ("created", "revised_prompt", "model"):
        if meta_key in payload:
            metadata[meta_key] = payload[meta_key]

    possible_collections = []
    for key in ("data", "images", "result", "results", "output"):
        if key in payload:
            possible_collections.append(payload[key])

    if not possible_collections:
        possible_collections = [payload]

    for collection in possible_collections:
        if isinstance(collection, list) and collection:
            item = collection[0]
        else:
            item = collection

        if isinstance(item, str):
            if item.startswith("http://") or item.startswith("https://"):
                return item, None, metadata
            decoded = _decode_base64_image(item)
            if decoded:
                return None, decoded, metadata

        if isinstance(item, dict):
            if "revised_prompt" in item:
                metadata["revised_prompt"] = item["revised_prompt"]
            if "url" in item and isinstance(item["url"], str):
                return item["url"], None, metadata
            if "image_url" in item and isinstance(item["image_url"], str):
                return item["image_url"], None, metadata
            for b64_key in ("b64_json", "base64", "image_base64"):
                if b64_key in item and isinstance(item[b64_key], str):
                    decoded = _decode_base64_image(item[b64_key])
                    if decoded:
                        return None, decoded, metadata
            image_value = item.get("image")
            if isinstance(image_value, str):
                if image_value.startswith("http://") or image_value.startswith("https://"):
                    return image_value, None, metadata
                decoded = _decode_base64_image(image_value)
                if decoded:
                    return None, decoded, metadata

    for key in ("url", "image_url"):
        value = payload.get(key)
        if isinstance(value, str):
            return value, None, metadata

    for b64_key in ("b64_json", "base64", "image_base64"):
        value = payload.get(b64_key)
        if isinstance(value, str):
            decoded = _decode_base64_image(value)
            if decoded:
                return None, decoded, metadata

    return None, None, metadata


def enhance_prompt_text(prompt: str) -> str:
    core = (prompt or "").strip().rstrip(". ")
    return (
        f"{core}. ultra detailed, strong composition, cinematic lighting, rich atmosphere, "
        "clean subject focus, polished textures, depth, balanced contrast, premium quality, "
        "visually striking final image"
    )


def compose_effective_prompt(prompt: str, enhance: bool, negative_prompt: str) -> str:
    final_prompt = enhance_prompt_text(prompt) if enhance else (prompt or "").strip()
    if negative_prompt.strip():
        final_prompt += f"\n\nAvoid: {negative_prompt.strip()}"
    return final_prompt


def generate_image(
    prompt: str,
    model: str,
    size: str,
    quality: str,
) -> Tuple[Optional[io.BytesIO], Optional[str], Dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    body = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "n": 1,
    }

    logger.info(
        "Generating image with model=%s size=%s quality=%s",
        model,
        size,
        quality,
    )

    last_error = "unknown error"
    max_attempts = max(1, REQUEST_RETRIES + 1)

    for attempt in range(1, max_attempts + 1):
        response = requests.post(
            IMAGE_API_URL,
            headers=headers,
            json=body,
            timeout=REQUEST_TIMEOUT,
        )

        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text[:500]}

        if response.status_code < 400:
            image_url, image_bytes, metadata = extract_first_image_payload(payload)

            if image_bytes is not None:
                return image_bytes, None, metadata

            if image_url:
                image_response = requests.get(image_url, timeout=REQUEST_TIMEOUT)
                image_response.raise_for_status()
                bio = io.BytesIO(image_response.content)
                bio.name = "generated.png"
                bio.seek(0)
                return bio, image_url, metadata

            raise RuntimeError(
                f"no image found in api response: {json.dumps(payload, ensure_ascii=False)[:600]}"
            )

        last_error = f"api {response.status_code}: {extract_text_error(payload)}"
        is_retryable = response.status_code >= 500 or "530" in last_error
        if is_retryable and attempt < max_attempts:
            logger.warning("Retrying image generation after error: %s", last_error)
            time.sleep(min(2 * attempt, 4))
            continue

        raise RuntimeError(last_error)

    raise RuntimeError(last_error)


def parse_img_command(command_text: str, current_model: str) -> Dict[str, Any]:
    raw = (command_text or "").strip()
    if not raw:
        raise ValueError("send a prompt after /img")

    model = current_model

    if "|" in raw:
        maybe_model, prompt_part = raw.split("|", 1)
        maybe_model = normalize_model(maybe_model)
        if maybe_model in SUPPORTED_MODELS:
            model = maybe_model
            raw = prompt_part.strip()
    else:
        parts = raw.split(maxsplit=1)
        if len(parts) == 2 and normalize_model(parts[0]) in SUPPORTED_MODELS:
            model = normalize_model(parts[0])
            raw = parts[1].strip()

    enhance = False
    cleaned_tokens: List[str] = []
    for token in raw.split():
        if token.lower() == "--enhance":
            enhance = True
        else:
            cleaned_tokens.append(token)
    raw = " ".join(cleaned_tokens).strip()

    negative_prompt = ""
    lower_raw = f" {raw} "
    marker_used = None
    marker_index = -1
    for marker in [" --neg ", " --negative "]:
        index = lower_raw.lower().find(marker)
        if index != -1:
            marker_used = marker
            marker_index = index
            break

    if marker_used is not None:
        prompt_part = lower_raw[1:marker_index].strip()
        neg_part = lower_raw[marker_index + len(marker_used) - 1 :].strip()
        raw = prompt_part
        negative_prompt = neg_part

    if not raw:
        raise ValueError("send a valid prompt after /img")

    return {
        "model": model,
        "prompt": raw,
        "enhance": enhance,
        "negative_prompt": safe_trim(negative_prompt, MAX_NEGATIVE_PROMPT_LEN),
    }


def build_loading_text(
    prompt: str,
    model: str,
    size: str,
    quality: str,
    enhance: bool,
    negative_prompt: str,
    frame: str,
) -> str:
    text = (
        f"{frame} <b>ʀᴇɴᴅᴇʀɪɴɢ</b>\n"
        f"• ᴍᴏᴅᴇʟ <code>{html.escape(display_model(model))}</code>\n"
        f"• sɪᴢᴇ <code>{html.escape(size)}</code>\n"
        f"• ǫᴜᴀʟɪᴛʏ <code>{html.escape(display_quality(quality))}</code>\n"
        f"• ᴇɴʜᴀɴᴄᴇ <code>{'on' if enhance else 'off'}</code>\n"
        f"• ᴘʀᴏᴍᴘᴛ <code>{html.escape(safe_trim(prompt, 90))}</code>"
    )
    if negative_prompt.strip():
        text += f"\n• ɴᴇɢ <code>{html.escape(safe_trim(negative_prompt, 70))}</code>"
    text += f"\n\n{BRAND_FOOTER}"
    return text


def build_success_caption(
    model: str,
    prompt: str,
    size: str,
    quality: str,
    enhance: bool,
    negative_prompt: str,
    metadata: Dict[str, Any],
) -> str:
    caption = (
        "✦ <b>ʀᴇɴᴅᴇʀ ᴄᴏᴍᴘʟᴇᴛᴇ</b>\n"
        f"• ᴍᴏᴅᴇʟ <code>{html.escape(display_model(model))}</code>\n"
        f"• sɪᴢᴇ <code>{html.escape(size)}</code>\n"
        f"• ǫᴜᴀʟɪᴛʏ <code>{html.escape(display_quality(quality))}</code>\n"
        f"• ᴇɴʜᴀɴᴄᴇ <code>{'on' if enhance else 'off'}</code>\n"
        f"• ᴘʀᴏᴍᴘᴛ <code>{html.escape(safe_trim(prompt, 110))}</code>"
    )
    if negative_prompt.strip():
        caption += f"\n• ɴᴇɢ <code>{html.escape(safe_trim(negative_prompt, 90))}</code>"
    revised_prompt = str(metadata.get("revised_prompt", "")).strip()
    if revised_prompt and revised_prompt != prompt:
        caption += f"\n• ʀᴇᴠɪsᴇᴅ <code>{html.escape(safe_trim(revised_prompt, 95))}</code>"
    caption += f"\n\n{BRAND_FOOTER}\n{DEV_FOOTER}"
    return caption


def build_error_text(error_text: str) -> str:
    return (
        "✦ <b>ʀᴇɴᴅᴇʀ ғᴀɪʟᴇᴅ</b>\n"
        f"<code>{html.escape(safe_trim(error_text, 3200))}</code>\n\n"
        "switch model, simplify prompt, or try again"
    )


def build_result_markup(prompt_id: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.row(
        types.InlineKeyboardButton("🔁 rerender", callback_data=f"run:{prompt_id}:same"),
        types.InlineKeyboardButton("✨ pro", callback_data=f"enh:{prompt_id}"),
        types.InlineKeyboardButton("🎛 studio", callback_data="openui:panel"),
    )
    markup.row(
        types.InlineKeyboardButton("gpt-1", callback_data=f"run:{prompt_id}:gpt-image-1"),
        types.InlineKeyboardButton("dall·e 3", callback_data=f"run:{prompt_id}:dall-e-3"),
        types.InlineKeyboardButton("nextlm", callback_data=f"run:{prompt_id}:nextlm-image-1"),
    )
    markup.row(
        types.InlineKeyboardButton("dall·e 2", callback_data=f"run:{prompt_id}:dall-e-2"),
    )
    return markup


class StatusAnimator(threading.Thread):
    def __init__(
        self,
        chat_id: int,
        message_id: int,
        prompt: str,
        model: str,
        size: str,
        quality: str,
        enhance: bool,
        negative_prompt: str,
    ) -> None:
        super().__init__(daemon=True)
        self.chat_id = chat_id
        self.message_id = message_id
        self.prompt = prompt
        self.model = model
        self.size = size
        self.quality = quality
        self.enhance = enhance
        self.negative_prompt = negative_prompt
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        index = 0
        while not self._stop_event.wait(1.2):
            frame = LOADING_FRAMES[index % len(LOADING_FRAMES)]
            try:
                bot.send_chat_action(self.chat_id, "typing")
            except Exception:
                pass
            try:
                bot.edit_message_text(
                    build_loading_text(
                        prompt=self.prompt,
                        model=self.model,
                        size=self.size,
                        quality=self.quality,
                        enhance=self.enhance,
                        negative_prompt=self.negative_prompt,
                        frame=frame,
                    ),
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.debug("status animation edit skipped: %s", exc)
            index += 1


def launch_generation_job(job: Dict[str, Any]) -> None:
    status_message = bot.send_message(
        int(job["chat_id"]),
        build_loading_text(
            prompt=str(job["prompt"]),
            model=str(job["model"]),
            size=str(job["size"]),
            quality=str(job["quality"]),
            enhance=bool(job["enhance"]),
            negative_prompt=str(job["negative_prompt"]),
            frame=LOADING_FRAMES[0],
        ),
        reply_to_message_id=job.get("reply_to_message_id"),
        disable_web_page_preview=True,
    )

    thread = threading.Thread(
        target=process_generation,
        kwargs={
            "chat_id": int(job["chat_id"]),
            "user_id": int(job["user_id"]),
            "prompt": str(job["prompt"]),
            "model": str(job["model"]),
            "size": str(job["size"]),
            "quality": str(job["quality"]),
            "enhance": bool(job["enhance"]),
            "negative_prompt": str(job["negative_prompt"]),
            "reply_to_message_id": job.get("reply_to_message_id"),
            "status_message_id": status_message.message_id,
        },
        daemon=True,
    )
    thread.start()


def process_generation(
    chat_id: int,
    user_id: int,
    prompt: str,
    model: str,
    size: str,
    quality: str,
    enhance: bool,
    negative_prompt: str,
    reply_to_message_id: Optional[int],
    status_message_id: int,
) -> None:
    animator = StatusAnimator(
        chat_id=chat_id,
        message_id=status_message_id,
        prompt=prompt,
        model=model,
        size=size,
        quality=quality,
        enhance=enhance,
        negative_prompt=negative_prompt,
    )
    animator.start()

    try:
        bot.send_chat_action(chat_id, "typing")
        effective_prompt = compose_effective_prompt(prompt, enhance, negative_prompt)
        image_file, image_url, metadata = generate_image(
            prompt=effective_prompt,
            model=model,
            size=size,
            quality=quality,
        )
        prompt_id = remember_prompt(
            user_id=user_id,
            prompt=prompt,
            model=model,
            size=size,
            quality=quality,
            enhance=enhance,
            negative_prompt=negative_prompt,
        )
        animator.stop()
        animator.join(timeout=0.4)
        try:
            bot.delete_message(chat_id, status_message_id)
        except Exception:
            pass

        bot.send_chat_action(chat_id, "upload_photo")
        bot.send_photo(
            chat_id,
            photo=image_file,
            caption=build_success_caption(
                model=model,
                prompt=prompt,
                size=size,
                quality=quality,
                enhance=enhance,
                negative_prompt=negative_prompt,
                metadata=metadata,
            ),
            parse_mode="HTML",
            reply_markup=build_result_markup(prompt_id),
            reply_to_message_id=reply_to_message_id,
        )
        if image_url:
            logger.info("Generated image from URL: %s", image_url)
    except Exception as exc:
        animator.stop()
        animator.join(timeout=0.4)
        logger.exception("Image generation failed")
        try:
            bot.edit_message_text(
                build_error_text(str(exc)),
                chat_id=chat_id,
                message_id=status_message_id,
                parse_mode="HTML",
                reply_markup=types.InlineKeyboardMarkup().add(
                    types.InlineKeyboardButton("🎛 studio", callback_data="openui:panel")
                ),
            )
        except Exception:
            bot.send_message(chat_id, build_error_text(str(exc)), disable_web_page_preview=True)
    finally:
        next_job, remaining = complete_generation_and_get_next(user_id)
        if next_job:
            try:
                upsert_ui_message(
                    chat_id=int(next_job["chat_id"]),
                    user_id=int(next_job["user_id"]),
                    view=get_ui_view(int(next_job["user_id"]), int(next_job["chat_id"])),
                    notice=(
                        f"queue started • {remaining} left"
                        if remaining > 0
                        else "queue started"
                    ),
                )
            except Exception as exc:
                logger.debug("queue ui update skipped: %s", exc)
            launch_generation_job(next_job)


def start_generation(
    chat_id: int,
    user_id: int,
    prompt: str,
    reply_to_message_id: Optional[int] = None,
    model_override: Optional[str] = None,
    size_override: Optional[str] = None,
    quality_override: Optional[str] = None,
    enhance_override: Optional[bool] = None,
    negative_prompt_override: Optional[str] = None,
) -> None:
    settings = get_user_settings(user_id)
    job = {
        "chat_id": chat_id,
        "user_id": user_id,
        "prompt": prompt,
        "reply_to_message_id": reply_to_message_id,
        "model": normalize_model(model_override or settings["model"]),
        "size": normalize_size(size_override or settings["size"]),
        "quality": normalize_quality(quality_override or settings["quality"]),
        "enhance": settings["enhance"] if enhance_override is None else bool(enhance_override),
        "negative_prompt": (
            settings.get("negative_prompt", "")
            if negative_prompt_override is None
            else safe_trim(negative_prompt_override, MAX_NEGATIVE_PROMPT_LEN)
        ),
    }

    can_start, queue_position = reserve_generation_slot(job)
    if not can_start:
        upsert_ui_message(
            chat_id=chat_id,
            user_id=user_id,
            view=get_ui_view(user_id, chat_id),
            notice=f"queued #{queue_position} • current render still running",
        )
        return

    launch_generation_job(job)


def setup_bot_commands() -> None:
    try:
        commands = [
            types.BotCommand("start", "open studio"),
            types.BotCommand("img", "generate image"),
            types.BotCommand("settings", "open panel"),
            types.BotCommand("panel", "open panel"),
            types.BotCommand("setneg", "set negative prompt"),
            types.BotCommand("clearneg", "clear negative prompt"),
            types.BotCommand("enhance", "toggle enhance"),
            types.BotCommand("help", "quick help"),
            types.BotCommand("admin", "owner panel"),
            types.BotCommand("users", "owner stats"),
            types.BotCommand("broadcast", "owner broadcast"),
        ]
        bot.set_my_commands(commands)
    except Exception as exc:
        logger.warning("Could not set bot commands: %s", exc)


def broadcast_copy(admin_message) -> Tuple[int, int]:
    with state_lock:
        targets = list(sorted(known_chat_ids))

    success = 0
    failed = 0
    for chat_id in targets:
        try:
            bot.copy_message(
                chat_id,
                admin_message.chat.id,
                admin_message.reply_to_message.message_id,
            )
            success += 1
        except Exception as exc:
            failed += 1
            logger.warning("Broadcast copy failed for chat %s: %s", chat_id, exc)
    return success, failed


def broadcast_text(text: str) -> Tuple[int, int]:
    with state_lock:
        targets = list(sorted(known_chat_ids))

    success = 0
    failed = 0
    for chat_id in targets:
        try:
            bot.send_message(chat_id, text, disable_web_page_preview=True)
            success += 1
        except Exception as exc:
            failed += 1
            logger.warning("Broadcast text failed for chat %s: %s", chat_id, exc)
    return success, failed


@bot.message_handler(commands=["start"])
def start_handler(message):
    register_message_context(message)
    upsert_ui_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        view="home",
        reply_to_message_id=message.message_id,
    )


@bot.message_handler(commands=["help"])
def help_handler(message):
    register_message_context(message)
    upsert_ui_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        view="help",
        reply_to_message_id=message.message_id,
    )


@bot.message_handler(commands=["models"])
def models_handler(message):
    register_message_context(message)
    upsert_ui_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        view="models",
        reply_to_message_id=message.message_id,
    )


@bot.message_handler(commands=["settings", "panel"])
def settings_handler(message):
    register_message_context(message)
    upsert_ui_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        view="panel",
        reply_to_message_id=message.message_id,
    )


@bot.message_handler(commands=["cancel"])
def cancel_handler(message):
    register_message_context(message)
    if clear_pending_input(message.from_user.id):
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "pending input cancelled")
    else:
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "nothing pending")


@bot.message_handler(commands=["setmodel"])
def setmodel_handler(message):
    register_message_context(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "usage: /setmodel gpt-image-1")
        return

    raw_model = parts[1].strip().lower()
    normalized_model = normalize_model(raw_model)
    if raw_model not in MODEL_ALIASES and normalized_model not in SUPPORTED_MODELS:
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "unsupported model")
        return

    settings = get_user_settings(message.from_user.id)
    settings["model"] = normalized_model
    upsert_ui_message(message.chat.id, message.from_user.id, "panel", f"model set to {display_model(normalized_model)}")


@bot.message_handler(commands=["setsize"])
def setsize_handler(message):
    register_message_context(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "usage: /setsize 1024x1024")
        return

    raw_size = parts[1].strip().lower()
    if raw_size not in SUPPORTED_SIZES:
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "unsupported size")
        return

    settings = get_user_settings(message.from_user.id)
    settings["size"] = raw_size
    upsert_ui_message(message.chat.id, message.from_user.id, "panel", f"size set to {raw_size}")


@bot.message_handler(commands=["setquality"])
def setquality_handler(message):
    register_message_context(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "usage: /setquality standard")
        return

    raw_quality = parts[1].strip().lower()
    if raw_quality not in SUPPORTED_QUALITIES:
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "unsupported quality")
        return

    settings = get_user_settings(message.from_user.id)
    settings["quality"] = raw_quality
    upsert_ui_message(
        message.chat.id,
        message.from_user.id,
        "panel",
        f"quality set to {display_quality(raw_quality)}",
    )


@bot.message_handler(commands=["enhance"])
def enhance_handler(message):
    register_message_context(message)
    settings = get_user_settings(message.from_user.id)
    parts = (message.text or "").split(maxsplit=1)

    if len(parts) == 1:
        settings["enhance"] = not bool(settings["enhance"])
    else:
        mode = parts[1].strip().lower()
        if mode in {"on", "true", "1", "yes"}:
            settings["enhance"] = True
        elif mode in {"off", "false", "0", "no"}:
            settings["enhance"] = False
        else:
            upsert_ui_message(message.chat.id, message.from_user.id, "panel", "usage: /enhance on or /enhance off")
            return

    upsert_ui_message(
        message.chat.id,
        message.from_user.id,
        "panel",
        f"enhance {'on' if settings['enhance'] else 'off'}",
    )


@bot.message_handler(commands=["setneg"])
def setneg_handler(message):
    register_message_context(message)
    parts = (message.text or "").split(maxsplit=1)
    settings = get_user_settings(message.from_user.id)

    if len(parts) == 1:
        set_pending_input(message.from_user.id, "negative_prompt")
        upsert_ui_message(
            message.chat.id,
            message.from_user.id,
            "panel",
            "waiting for next text as negative prompt",
        )
        return

    negative_prompt = safe_trim(parts[1], MAX_NEGATIVE_PROMPT_LEN)
    settings["negative_prompt"] = negative_prompt
    clear_pending_input(message.from_user.id)
    upsert_ui_message(message.chat.id, message.from_user.id, "panel", "negative prompt saved")


@bot.message_handler(commands=["clearneg"])
def clearneg_handler(message):
    register_message_context(message)
    settings = get_user_settings(message.from_user.id)
    settings["negative_prompt"] = ""
    clear_pending_input(message.from_user.id)
    upsert_ui_message(message.chat.id, message.from_user.id, "panel", "negative prompt cleared")


@bot.message_handler(commands=["admin"])
def admin_handler(message):
    register_message_context(message)
    if not is_admin(message.from_user.id):
        upsert_ui_message(message.chat.id, message.from_user.id, "home", "owner only")
        return

    upsert_ui_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        view="admin",
        reply_to_message_id=message.message_id,
    )


@bot.message_handler(commands=["users"])
def users_handler(message):
    register_message_context(message)
    if not is_admin(message.from_user.id):
        upsert_ui_message(message.chat.id, message.from_user.id, "home", "owner only")
        return

    with state_lock:
        total_users = len(known_user_ids)
        total_chats = len(known_chat_ids)

    upsert_ui_message(
        message.chat.id,
        message.from_user.id,
        "admin",
        f"users {total_users} • chats {total_chats}",
    )


@bot.message_handler(commands=["broadcast"])
def broadcast_handler(message):
    register_message_context(message)
    if not is_admin(message.from_user.id):
        upsert_ui_message(message.chat.id, message.from_user.id, "home", "owner only")
        return

    status = bot.reply_to(message, "📡 broadcasting...")

    if message.reply_to_message:
        success, failed = broadcast_copy(message)
    else:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            bot.edit_message_text(
                "usage:\n• reply to any message with <code>/broadcast</code>\n• or <code>/broadcast your text here</code>",
                chat_id=status.chat.id,
                message_id=status.message_id,
                parse_mode="HTML",
            )
            return
        success, failed = broadcast_text(parts[1].strip())

    bot.edit_message_text(
        f"✅ broadcast done\n• sent <code>{success}</code>\n• failed <code>{failed}</code>",
        chat_id=status.chat.id,
        message_id=status.message_id,
        parse_mode="HTML",
    )


@bot.message_handler(commands=["img"])
def img_handler(message):
    register_message_context(message)
    settings = get_user_settings(message.from_user.id)
    command_text = message.text[len("/img") :].strip() if message.text else ""

    try:
        parsed = parse_img_command(command_text, settings["model"])
    except ValueError as exc:
        upsert_ui_message(message.chat.id, message.from_user.id, "help", str(exc))
        return

    enhance_override = True if parsed["enhance"] else None
    negative_override = parsed["negative_prompt"] if parsed["negative_prompt"] else None

    start_generation(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        prompt=parsed["prompt"],
        reply_to_message_id=message.message_id,
        model_override=parsed["model"],
        enhance_override=enhance_override,
        negative_prompt_override=negative_override,
    )


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    register_callback_context(call)
    data = call.data or ""

    if data == "ui:create":
        bot.answer_callback_query(
            call.id,
            "send /img your prompt or just type in private",
            show_alert=True,
        )
        return

    if data == "ui:example":
        bot.answer_callback_query(
            call.id,
            "Example:\n/img cinematic fox mage in misty forest --enhance --neg text, blurry, watermark",
            show_alert=True,
        )
        return

    if data.startswith("openui:"):
        view = data.split(":", 1)[1].strip() or "panel"
        if view == "admin" and not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "owner only", show_alert=False)
            return
        bot.answer_callback_query(call.id, "studio updated")
        upsert_ui_message(call.message.chat.id, call.from_user.id, view)
        return

    if data.startswith("view:"):
        view = data.split(":", 1)[1].strip() or "home"
        if view == "admin" and not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "owner only", show_alert=False)
            return
        if not is_ui_owner(call.message.chat.id, call.message.message_id, call.from_user.id):
            bot.answer_callback_query(call.id, "open your own studio", show_alert=False)
            upsert_ui_message(call.message.chat.id, call.from_user.id, view)
            return
        bot.answer_callback_query(call.id, view)
        update_current_ui_from_callback(call, view)
        return

    if data == "admin:audience":
        if not is_ui_owner(call.message.chat.id, call.message.message_id, call.from_user.id) or not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "owner only", show_alert=False)
            return
        with state_lock:
            total_users = len(known_user_ids)
            total_chats = len(known_chat_ids)
        bot.answer_callback_query(call.id, "audience refreshed")
        update_current_ui_from_callback(
            call,
            "admin",
            f"users {total_users} • chats {total_chats}",
        )
        return

    if data == "admin:queue":
        if not is_ui_owner(call.message.chat.id, call.message.message_id, call.from_user.id) or not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "owner only", show_alert=False)
            return
        active_total, queued_total = get_global_queue_snapshot()
        bot.answer_callback_query(call.id, "queue refreshed")
        update_current_ui_from_callback(
            call,
            "admin",
            f"active {active_total} • queued {queued_total}",
        )
        return

    if data == "admin:broadcasthelp":
        if not is_ui_owner(call.message.chat.id, call.message.message_id, call.from_user.id) or not is_admin(call.from_user.id):
            bot.answer_callback_query(call.id, "owner only", show_alert=False)
            return
        bot.answer_callback_query(call.id, "broadcast help")
        update_current_ui_from_callback(
            call,
            "admin",
            "use /broadcast text or reply with /broadcast",
        )
        return

    if data == "panel:setneg":
        if not is_ui_owner(call.message.chat.id, call.message.message_id, call.from_user.id):
            bot.answer_callback_query(call.id, "open your own studio", show_alert=False)
            return
        set_pending_input(call.from_user.id, "negative_prompt")
        bot.answer_callback_query(call.id, "send next text as negative prompt")
        update_current_ui_from_callback(call, "panel", "waiting for next text as negative prompt")
        return

    if data == "panel:clrneg":
        if not is_ui_owner(call.message.chat.id, call.message.message_id, call.from_user.id):
            bot.answer_callback_query(call.id, "open your own studio", show_alert=False)
            return
        settings = get_user_settings(call.from_user.id)
        settings["negative_prompt"] = ""
        clear_pending_input(call.from_user.id)
        bot.answer_callback_query(call.id, "negative prompt cleared")
        update_current_ui_from_callback(call, "panel", "negative prompt cleared")
        return

    if data == "toggle:enhance":
        if not is_ui_owner(call.message.chat.id, call.message.message_id, call.from_user.id):
            bot.answer_callback_query(call.id, "open your own studio", show_alert=False)
            return
        settings = get_user_settings(call.from_user.id)
        settings["enhance"] = not bool(settings["enhance"])
        bot.answer_callback_query(call.id, f"enhance {'on' if settings['enhance'] else 'off'}")
        update_current_ui_from_callback(
            call,
            "panel",
            f"enhance {'on' if settings['enhance'] else 'off'}",
        )
        return

    if data.startswith("set:"):
        if not is_ui_owner(call.message.chat.id, call.message.message_id, call.from_user.id):
            bot.answer_callback_query(call.id, "open your own studio", show_alert=False)
            return

        try:
            _, field, value = data.split(":", 2)
        except ValueError:
            bot.answer_callback_query(call.id, "bad action")
            return

        settings = get_user_settings(call.from_user.id)
        changed = False
        notice = "saved"

        if field == "model" and value in SUPPORTED_MODELS:
            settings["model"] = value
            changed = True
            notice = f"model set to {display_model(value)}"
        elif field == "size" and value in SUPPORTED_SIZES:
            settings["size"] = value
            changed = True
            notice = f"size set to {value}"
        elif field == "quality" and value in SUPPORTED_QUALITIES:
            settings["quality"] = value
            changed = True
            notice = f"quality set to {display_quality(value)}"

        if not changed:
            bot.answer_callback_query(call.id, "unsupported option")
            return

        bot.answer_callback_query(call.id, "saved")
        update_current_ui_from_callback(call, "panel", notice)
        return

    if data.startswith("enh:"):
        try:
            _, prompt_id = data.split(":", 1)
        except ValueError:
            bot.answer_callback_query(call.id, "bad action")
            return

        with state_lock:
            cached = prompt_cache.get(prompt_id)

        if not cached:
            bot.answer_callback_query(call.id, "prompt expired", show_alert=True)
            return
        if cached["user_id"] != call.from_user.id:
            bot.answer_callback_query(call.id, "this render is not yours", show_alert=False)
            return

        bot.answer_callback_query(call.id, "rerendering with enhance")
        start_generation(
            chat_id=call.message.chat.id,
            user_id=call.from_user.id,
            prompt=str(cached["prompt"]),
            reply_to_message_id=call.message.message_id,
            model_override=str(cached["model"]),
            size_override=str(cached["size"]),
            quality_override=str(cached["quality"]),
            enhance_override=True,
            negative_prompt_override=str(cached.get("negative_prompt", "")),
        )
        return

    if data.startswith("run:"):
        try:
            _, prompt_id, requested_model = data.split(":", 2)
        except ValueError:
            bot.answer_callback_query(call.id, "bad action")
            return

        with state_lock:
            cached = prompt_cache.get(prompt_id)

        if not cached:
            bot.answer_callback_query(call.id, "prompt expired", show_alert=True)
            return
        if cached["user_id"] != call.from_user.id:
            bot.answer_callback_query(call.id, "this render is not yours", show_alert=False)
            return

        model_override = str(cached["model"]) if requested_model == "same" else requested_model
        if model_override not in SUPPORTED_MODELS:
            bot.answer_callback_query(call.id, "unsupported model")
            return

        bot.answer_callback_query(call.id, "rerendering")
        start_generation(
            chat_id=call.message.chat.id,
            user_id=call.from_user.id,
            prompt=str(cached["prompt"]),
            reply_to_message_id=call.message.message_id,
            model_override=model_override,
            size_override=str(cached["size"]),
            quality_override=str(cached["quality"]),
            enhance_override=bool(cached.get("enhance", False)),
            negative_prompt_override=str(cached.get("negative_prompt", "")),
        )
        return

    bot.answer_callback_query(call.id, "ok")


@bot.message_handler(
    func=lambda message: bool(message.text)
    and has_pending_input(message.from_user.id)
    and not message.text.strip().startswith("/"),
    content_types=["text"],
)
def pending_input_handler(message):
    register_message_context(message)
    pending = pop_pending_input(message.from_user.id)
    if not pending:
        return

    if pending.get("action") == "negative_prompt":
        settings = get_user_settings(message.from_user.id)
        settings["negative_prompt"] = safe_trim(message.text, MAX_NEGATIVE_PROMPT_LEN)
        upsert_ui_message(message.chat.id, message.from_user.id, "panel", "negative prompt saved")
        return


@bot.message_handler(
    func=lambda message: bool(message.text)
    and message.chat.type == "private"
    and not message.text.strip().startswith("/"),
    content_types=["text"],
)
def plain_text_handler(message):
    register_message_context(message)
    text = (message.text or "").strip()
    if not text:
        return

    start_generation(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        prompt=text,
        reply_to_message_id=message.message_id,
    )


@bot.message_handler(
    func=lambda message: bool(message.text) and message.text.strip().startswith("/"),
    content_types=["text"],
)
def unknown_command_handler(message):
    register_message_context(message)
    upsert_ui_message(message.chat.id, message.from_user.id, "help", "unknown command")


if __name__ == "__main__":
    logger.info("Bot is starting...")
    setup_bot_commands()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
