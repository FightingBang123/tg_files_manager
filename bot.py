"""Telegram 文件助手 Bot —— 类似微信「文件传输助手」。

核心特性：
- 服务器不保存文件本体，只存 file_id 等元数据（SQLite），文件始终在 Telegram 服务器上；
- 收到任何图片 / 文件 / 视频 / 语音等自动保存并分类，支持 #标签 与自定义标签；
- 分类浏览、关键词搜索、inline mode（任意聊天 @bot 关键词 直接取回）；
- Bot 消息自带 Telegram 原生 ⭐ 收藏按钮，可一键存入「收藏消息」。
"""

import asyncio
import hashlib
import html
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InlineQueryResultCachedDocument,
    InlineQueryResultCachedGif,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedSticker,
    InlineQueryResultCachedVideo,
    InlineQueryResultCachedVoice,
    InputTextMessageContent,
    Update,
)
from telegram.error import NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from storage import FileStore

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 分类与展示
# --------------------------------------------------------------------------- #

CATEGORIES = ["图片", "文档", "文本", "视频", "音频", "语音", "贴纸", "动画", "视频笔记"]
CATEGORY_EMOJI = {
    "图片": "📷",
    "文档": "📄",
    "文本": "📝",
    "视频": "🎬",
    "音频": "🎵",
    "语音": "🎙️",
    "贴纸": "😀",
    "动画": "✨",
    "视频笔记": "📹",
}
FILE_TYPE_TO_CATEGORY = {
    "photo": "图片",
    "document": "文档",
    "text": "文本",
    "video": "视频",
    "audio": "音频",
    "voice": "语音",
    "sticker": "贴纸",
    "animation": "动画",
    "video_note": "视频笔记",
}

TAG_RE = re.compile(r"#([\w\u4e00-\u9fa5][\w\u4e00-\u9fa5\-]*)")
PAGE_SIZE = 10


def fmt_size(size: Optional[int]) -> str:
    if not size:
        return "?"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return "?"


def fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone(TZ)
    except (ValueError, TypeError):
        return iso
    return dt.strftime("%m-%d %H:%M")


def parse_tags(caption: Optional[str]) -> list[str]:
    if not caption:
        return []
    return TAG_RE.findall(caption)


def display_name(record) -> str:
    name = record["file_name"]
    if not name and record["file_type"] == "text":
        first_line = (record["text_content"] or "").strip().splitlines()
        name = first_line[0] if first_line else "(空文本)"
    name = name or record["caption"] or "(无文件名)"
    if len(name) > 40:
        name = name[:37] + "…"
    return name


def fmt_record_size(record) -> str:
    """文本显示字数，媒体显示文件大小。"""
    if record["file_type"] == "text":
        return f"{len(record['text_content'] or '')}字"
    return fmt_size(record["file_size"])


def title_from_text(text: str, limit: int = 60) -> str:
    """取文本第一行作为标题。"""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:limit] if first else "(空文本)"


# --------------------------------------------------------------------------- #
# 环境配置
# --------------------------------------------------------------------------- #

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = Path(os.getenv("DB_PATH", "files.db"))
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Shanghai"))
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

# --- 网络配置（解决 ConnectError / 网络不稳定） ---
# 可选代理，如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080
PROXY_URL = os.getenv("PROXY_URL", "").strip()
# httpx 超时（秒）。网络差时调大；轮询的 read_timeout 会自动加上长轮询 timeout
CONNECT_TIMEOUT = float(os.getenv("CONNECT_TIMEOUT", "15"))
READ_TIMEOUT = float(os.getenv("READ_TIMEOUT", "30"))
WRITE_TIMEOUT = float(os.getenv("WRITE_TIMEOUT", "30"))
POOL_TIMEOUT = float(os.getenv("POOL_TIMEOUT", "15"))


def sanitize_proxy(url: str) -> str:
    """只显示 scheme://host:port，避免在日志中泄露代理账号密码。"""
    try:
        from urllib.parse import urlparse

        p = urlparse(url)
        host = p.hostname or ""
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{host}{port}" if host else "(proxy)"
    except Exception:  # noqa: BLE001
        return "(proxy)"


def is_allowed(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    return not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS


def get_store(context: ContextTypes.DEFAULT_TYPE) -> FileStore:
    return context.bot_data["store"]


# --------------------------------------------------------------------------- #
# 媒体提取
# --------------------------------------------------------------------------- #

def extract_media(message) -> Optional[dict]:
    """从消息中提取 (file_id, file_unique_id, file_type, ...)。"""
    if message.photo:
        largest = message.photo[-1]  # 取最大分辨率
        return {
            "file_unique_id": largest.file_unique_id,
            "file_id": largest.file_id,
            "file_type": "photo",
            "file_size": largest.file_size,
        }
    if message.document:
        d = message.document
        return {
            "file_unique_id": d.file_unique_id,
            "file_id": d.file_id,
            "file_type": "document",
            "file_size": d.file_size,
            "file_name": d.file_name,
            "mime_type": d.mime_type,
        }
    if message.video:
        v = message.video
        return {
            "file_unique_id": v.file_unique_id,
            "file_id": v.file_id,
            "file_type": "video",
            "file_size": v.file_size,
            "file_name": getattr(v, "file_name", None),
            "mime_type": v.mime_type,
        }
    if message.audio:
        a = message.audio
        fallback = None
        if a.performer and a.title:
            fallback = f"{a.performer} - {a.title}"
        elif a.title:
            fallback = a.title
        return {
            "file_unique_id": a.file_unique_id,
            "file_id": a.file_id,
            "file_type": "audio",
            "file_size": a.file_size,
            "file_name": getattr(a, "file_name", None) or fallback,
            "mime_type": a.mime_type,
        }
    if message.voice:
        v = message.voice
        return {
            "file_unique_id": v.file_unique_id,
            "file_id": v.file_id,
            "file_type": "voice",
            "file_size": v.file_size,
            "mime_type": v.mime_type,
        }
    if message.sticker:
        s = message.sticker
        return {
            "file_unique_id": s.file_unique_id,
            "file_id": s.file_id,
            "file_type": "sticker",
            "file_size": s.file_size,
        }
    if message.animation:
        a = message.animation
        return {
            "file_unique_id": a.file_unique_id,
            "file_id": a.file_id,
            "file_type": "animation",
            "file_size": a.file_size,
            "file_name": a.file_name,
            "mime_type": a.mime_type,
        }
    if message.video_note:
        v = message.video_note
        return {
            "file_unique_id": v.file_unique_id,
            "file_id": v.file_id,
            "file_type": "video_note",
            "file_size": v.file_size,
        }
    return None


# --------------------------------------------------------------------------- #
# 发送文件（用已存 file_id，本体仍在 Telegram 服务器）
# --------------------------------------------------------------------------- #

async def send_record(bot, chat_id: int, record) -> None:
    ftype = record["file_type"]
    file_id = record["file_id"]
    caption = record["caption"] or None
    file_name = record["file_name"] or None

    if ftype == "text":
        await bot.send_message(chat_id, text=record["text_content"] or "(空文本)")
        return
    if ftype == "photo":
        await bot.send_photo(chat_id, photo=file_id, caption=caption)
    elif ftype == "video":
        await bot.send_video(chat_id, video=file_id, caption=caption)
    elif ftype == "audio":
        await bot.send_audio(chat_id, audio=file_id, caption=caption, title=file_name)
    elif ftype == "voice":
        await bot.send_voice(chat_id, voice=file_id, caption=caption)
    elif ftype == "sticker":
        await bot.send_sticker(chat_id, sticker=file_id)
    elif ftype == "animation":
        await bot.send_animation(chat_id, animation=file_id, caption=caption)
    elif ftype == "video_note":
        await bot.send_video_note(chat_id, video_note=file_id)
    else:  # document
        await bot.send_document(chat_id, document=file_id, caption=caption)


# --------------------------------------------------------------------------- #
# 列表 / 搜索 渲染
# --------------------------------------------------------------------------- #

def build_list_text(category: str, total: int, page: int, rows) -> str:
    label = "全部文件" if category == "all" else f"{CATEGORY_EMOJI.get(category, '📁')} {category}"
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"📁 {label} · 共 {total} 个（第 {page + 1}/{pages} 页）", ""]
    for r in rows:
        emoji = CATEGORY_EMOJI.get(r["category"], "📎")
        lines.append(
            f"#{r['id']} {emoji} {display_name(r)} · {fmt_record_size(r)} · {fmt_time(r['created_at'])}"
        )
    return "\n".join(lines)


def build_search_text(query: str, total: int, page: int, rows) -> str:
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    lines = [f"🔍 “{query}” 共 {total} 个结果（第 {page + 1}/{pages} 页）", ""]
    for r in rows:
        emoji = CATEGORY_EMOJI.get(r["category"], "📎")
        lines.append(
            f"#{r['id']} {emoji} {display_name(r)} · {fmt_record_size(r)} · {fmt_time(r['created_at'])}"
        )
    return "\n".join(lines)


def get_buttons_for(rows) -> list[list[InlineKeyboardButton]]:
    return [
        [InlineKeyboardButton(f"📥 获取 #{r['id']}", callback_data=f"g:{r['id']}")]
        for r in rows
    ]


async def show_list(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    category: str,
    page: int,
    edit: bool = False,
) -> None:
    store = get_store(context)
    total = store.count(category)
    offset = page * PAGE_SIZE
    rows = store.list_files(category=category, limit=PAGE_SIZE, offset=offset)

    if not rows:
        text = "😕 这里还没有文件。\n直接把文件 / 图片 / 视频发给我，就会自动保存并分类。"
        markup = None
    else:
        text = build_list_text(category, total, page, rows)
        keyboard = get_buttons_for(rows)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ 上一页", callback_data=f"l:{category}:{page - 1}"))
        if offset + PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("下一页 ▶️", callback_data=f"l:{category}:{page + 1}"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("🏠 返回分类", callback_data="cat")])
        markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def show_search(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    query: str,
    page: int,
    edit: bool = False,
) -> None:
    store = get_store(context)
    context.user_data["search_query"] = query
    total = store.count_search(query)
    rows = store.search(query, limit=PAGE_SIZE, offset=page * PAGE_SIZE)

    if not rows:
        text = f"😕 没有找到与 “{query}” 相关的文件。\n搜索范围：文件名、说明文字、#标签。"
        markup = None
    else:
        text = build_search_text(query, total, page, rows)
        keyboard = get_buttons_for(rows)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ 上一页", callback_data=f"s:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("下一页 ▶️", callback_data=f"s:{page + 1}"))
        if nav:
            keyboard.append(nav)
        markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


# --------------------------------------------------------------------------- #
# 命令 handlers
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    first_name = html.escape(user.first_name if user else "你")
    text = (
        f"你好，{first_name} 👋\n\n"
        "我是你的个人文件助手，类似微信的「文件传输助手」：\n"
        "把任何文件、图片、视频、语音、文本发给我，我会记住它们，随时取回。\n\n"
        "💡 服务器只保存 file_id / 元数据，文件本体始终在 Telegram 服务器，不占磁盘。\n\n"
        "📥 直接发文件或文本给我 → 自动保存并分类\n"
        "🔍 /search 关键词 → 搜索\n"
        "📂 /list → 按分类浏览\n"
        "🕐 /recent → 最近文件\n"
        "⚡ 任意聊天 @本Bot 关键词 → inline 直接取回\n"
        "⭐ 长按 Bot 消息 → 一键存入 Telegram「收藏消息」\n\n"
        "更多命令见 /help"
    )
    await update.effective_message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 文件助手使用说明\n\n"
        "【保存】直接发送文件 / 图片 / 视频 / 语音 / 贴纸 / 文本，自动保存并分类；\n"
        "  caption 或文本里的 #标签（如 #工作 #合同）会自动成为可搜索标签。\n"
        "  转发文本到 Bot 也会自动保存；或 /note 内容 显式保存；\n"
        "  回复某条消息后发 /note 可保存被回复消息的文本。\n\n"
        "【取回】\n"
        "  /get <id> 或点列表里的「📥 获取」按钮\n"
        "  /recent [n] 最近 n 个文件（默认 10）\n"
        "  /list 按分类浏览\n"
        "  /search <关键词> 搜索文件名 / 说明 / 标签\n"
        "  任意聊天输入 @本Bot <关键词> 直接取回（需 BotFather 开启 inline mode）\n\n"
        "【管理】\n"
        "  /tag <id> #标签... 追加标签\n"
        "  /rename <id> 新名称 重命名（便于搜索）\n"
        "  /del <id> 删除记录\n"
        "  /stats 查看统计\n\n"
        "【收藏消息】\n"
        "  Bot 发出的每条消息都自带 ⭐ 按钮（消息菜单），\n"
        "  点击即可把内容一键存入 Telegram「收藏消息」，随时离线取用。\n\n"
        "⚠️ 请勿删除与 Bot 聊天记录中的原始文件消息，否则 Telegram 服务器\n"
        "   可能清理文件，导致 file_id 失效（此时 /get 会报错）。"
    )
    await update.effective_message.reply_text(text)


async def recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        n = int(context.args[0]) if context.args else PAGE_SIZE
    except ValueError:
        n = PAGE_SIZE
    n = max(1, min(n, 30))
    store = get_store(context)
    total = store.count()
    rows = store.list_files(limit=n, offset=0)
    if not rows:
        await update.effective_message.reply_text("😕 还没有保存任何文件。直接发一个文件给我试试吧！")
        return
    text = build_list_text("all", total, 0, rows)
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(get_buttons_for(rows)))


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard, row = [], []
    for cat in CATEGORIES:
        row.append(InlineKeyboardButton(f"{CATEGORY_EMOJI[cat]} {cat}", callback_data=f"l:{cat}:0"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🗂 全部文件", callback_data="l:all:0")])
    await update.effective_message.reply_text(
        "📁 按分类浏览：", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "用法：/search <关键词>\n搜索范围：文件名、说明文字、#标签。\n例如：/search 合同"
        )
        return
    await show_search(context, update.effective_message, query, 0)


async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("用法：/get <id>，例如 /get 12")
        return
    try:
        rid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("id 必须是数字。")
        return
    record = get_store(context).get(rid)
    if not record:
        await update.effective_message.reply_text(f"❌ 没有找到记录 #{rid}。")
        return
    try:
        await send_record(context.bot, update.effective_chat.id, record)
    except Exception as exc:  # noqa: BLE001 - 文件可能已被 Telegram 清理
        logger.warning("send file #%s failed: %s", rid, exc)
        await update.effective_message.reply_text(
            f"❌ 发送失败（{exc}）。\n文件可能已被 Telegram 服务器清理，建议保留原始消息。"
        )


async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("用法：/del <id>，例如 /del 12")
        return
    try:
        rid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("id 必须是数字。")
        return
    if get_store(context).delete(rid):
        await update.effective_message.reply_text(f"🗑 已删除记录 #{rid}（仅删除本地记录，聊天里的原消息保留）。")
    else:
        await update.effective_message.reply_text(f"❌ 没有找到记录 #{rid}。")


async def tag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text("用法：/tag <id> #标签1 #标签2")
        return
    try:
        rid = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("id 必须是数字。")
        return
    new_tags = [a.lstrip("#") for a in args[1:]]
    record = get_store(context).get(rid)
    if not record:
        await update.effective_message.reply_text(f"❌ 没有找到记录 #{rid}。")
        return
    merged = list(dict.fromkeys((record["tags"].split() if record["tags"] else []) + new_tags))
    get_store(context).update_tags(rid, " ".join(merged))
    await update.effective_message.reply_text(
        f"🏷 记录 #{rid} 标签已更新：{' '.join('#' + t for t in merged)}"
    )


async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text("用法：/rename <id> 新名称（便于搜索）")
        return
    try:
        rid = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("id 必须是数字。")
        return
    name = " ".join(args[1:]).strip()
    if get_store(context).rename(rid, name):
        await update.effective_message.reply_text(f"✏️ 记录 #{rid} 已重命名为：{name}")
    else:
        await update.effective_message.reply_text(f"❌ 没有找到记录 #{rid}。")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = get_store(context)
    counts = store.counts_by_category()
    total = store.count()
    lines = ["📊 文件统计", ""]
    for cat in CATEGORIES:
        lines.append(f"{CATEGORY_EMOJI[cat]} {cat}：{counts.get(cat, 0)}")
    lines.append("")
    lines.append(f"总计：{total} 个文件（仅元数据，占用磁盘极小）")
    await update.effective_message.reply_text("\n".join(lines))


# --------------------------------------------------------------------------- #
# 媒体保存
# --------------------------------------------------------------------------- #

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    message = update.effective_message
    meta = extract_media(message)
    if not meta:
        return

    caption = message.caption
    tags = parse_tags(caption)
    category = FILE_TYPE_TO_CATEGORY[meta["file_type"]]
    store = get_store(context)

    record, is_new = store.add(
        file_unique_id=meta["file_unique_id"],
        file_id=meta["file_id"],
        file_type=meta["file_type"],
        file_name=meta.get("file_name"),
        mime_type=meta.get("mime_type"),
        file_size=meta.get("file_size"),
        caption=caption,
        tags=" ".join(tags),
        category=category,
        chat_id=update.effective_chat.id,
        message_id=message.message_id,
    )

    emoji = CATEGORY_EMOJI[category]
    lines = [
        ("✅ 已保存 " if is_new else "♻️ 已更新（同一文件）") + f"#{record['id']}",
        f"{emoji} 分类：{category}",
    ]
    if record["file_name"]:
        lines.append(f"📄 文件名：{record['file_name']}")
    if tags:
        lines.append("🏷 标签：" + " ".join("#" + t for t in tags))
    lines.append(f"💾 {fmt_size(record['file_size'])} · {fmt_time(record['created_at'])}")
    lines.append("")
    lines.append("⭐ 长按此消息 → 收藏到 Telegram「收藏消息」")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 获取", callback_data=f"g:{record['id']}"),
                InlineKeyboardButton("🗑 删除", callback_data=f"d:{record['id']}"),
            ]
        ]
    )
    await message.reply_text("\n".join(lines), reply_markup=keyboard)


async def save_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    text = text.strip()
    if not text:
        return
    store = get_store(context)
    # 文本无 file_id，用内容哈希作为唯一键：相同文本重复保存自动去重
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tags = parse_tags(text)
    record, is_new = store.add(
        file_unique_id=content_hash,
        file_id="",
        file_type="text",
        file_name=title_from_text(text),
        mime_type="text/plain",
        file_size=len(text.encode("utf-8")),
        caption=None,
        tags=" ".join(tags),
        category="文本",
        chat_id=update.effective_chat.id,
        message_id=update.effective_message.message_id,
        text_content=text,
    )

    lines = [
        ("✅ 已保存 " if is_new else "♻️ 已更新（相同内容）") + f"#{record['id']}",
        "📝 分类：文本",
    ]
    if record["file_name"]:
        lines.append(f"📄 标题：{record['file_name']}")
    if tags:
        lines.append("🏷 标签：" + " ".join("#" + t for t in tags))
    lines.append(f"📝 {len(text)}字 · {fmt_time(record['created_at'])}")
    lines.append("")
    lines.append("⭐ 长按此消息 → 收藏到 Telegram「收藏消息」")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📥 获取", callback_data=f"g:{record['id']}"),
                InlineKeyboardButton("🗑 删除", callback_data=f"d:{record['id']}"),
            ]
        ]
    )
    await update.effective_message.reply_text("\n".join(lines), reply_markup=keyboard)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """非命令文本消息：自动保存（含转发来的文本）。"""
    if not is_allowed(update):
        return
    message = update.effective_message
    if not message or not message.text:
        return
    await save_text(update, context, message.text)


async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/note 内容 或 回复消息后 /note：显式保存一段文本。"""
    if not is_allowed(update):
        return
    message = update.effective_message
    text = " ".join(context.args).strip()
    if not text and message.reply_to_message:
        replied = message.reply_to_message
        text = (replied.text or replied.caption or "").strip()
    if not text:
        await message.reply_text(
            "用法：\n"
            "/note 内容 — 保存一段文本\n"
            "回复某条消息后再发 /note — 保存被回复消息的文本\n"
            "直接发送或转发文本消息 — 也会自动保存"
        )
        return
    await save_text(update, context, text)


# --------------------------------------------------------------------------- #
# 回调（内联按钮）
# --------------------------------------------------------------------------- #

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_allowed(update):
        await query.answer("无权使用", show_alert=True)
        return
    data = query.data or ""

    if data == "cat":
        await query.answer()
        await list_command(update, context)  # 重新发送分类键盘
        return

    if data.startswith("l:"):
        _, category, page = data.split(":")
        await query.answer()
        await show_list(context, query.message, category, int(page), edit=True)
        return

    if data.startswith("s:"):
        await query.answer()
        page = int(data.split(":")[1])
        q = context.user_data.get("search_query", "")
        await show_search(context, query.message, q, page, edit=True)
        return

    if data.startswith("g:"):
        rid = int(data[2:])
        record = get_store(context).get(rid)
        if not record:
            await query.answer("记录不存在", show_alert=True)
            return
        await query.answer("发送中…")
        try:
            await send_record(context.bot, query.message.chat_id, record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("callback send file #%s failed: %s", rid, exc)
            await context.bot.send_message(
                query.message.chat_id,
                f"❌ 发送失败（{exc}）。文件可能已被 Telegram 清理，建议保留原始消息。",
            )
        return

    if data.startswith("d:"):
        rid = int(data[2:])
        record = get_store(context).get(rid)
        if not record:
            await query.answer("记录不存在", show_alert=True)
            return
        await query.answer()
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🗑 确认删除", callback_data=f"dc:{rid}"),
                    InlineKeyboardButton("取消", callback_data="cancel"),
                ]
            ]
        )
        await query.message.reply_text(
            f"确定删除记录 #{rid}（{display_name(record)}）？\n仅删除本地记录，聊天中的原消息不受影响。",
            reply_markup=kb,
        )
        return

    if data.startswith("dc:"):
        rid = int(data[2:])
        get_store(context).delete(rid)
        await query.answer("已删除")
        await query.message.edit_text(f"🗑 已删除记录 #{rid}。")
        return

    if data == "cancel":
        await query.answer("已取消")
        return

    await query.answer()


# --------------------------------------------------------------------------- #
# Inline mode：任意聊天 @bot <关键词> 直接取回
# --------------------------------------------------------------------------- #

def to_inline_result(record) -> Optional[object]:
    rid = str(record["id"])
    ftype = record["file_type"]
    fid = record["file_id"]
    title = record["file_name"] or f"#{record['id']} · {record['category']}"
    caption = record["caption"] or None

    if ftype == "photo":
        return InlineQueryResultCachedPhoto(id=rid, photo_file_id=fid, caption=caption)
    if ftype == "text":
        content = record["text_content"] or ""
        return InlineQueryResultArticle(
            id=rid,
            title=title,
            description=f"{fmt_record_size(record)} · {content[:60]}",
            input_message_content=InputTextMessageContent(content),
        )
    if ftype == "document":
        return InlineQueryResultCachedDocument(
            id=rid, document_file_id=fid, title=title,
            description=f"{record['category']} · {fmt_size(record['file_size'])}",
        )
    if ftype == "video":
        return InlineQueryResultCachedVideo(
            id=rid, video_file_id=fid, title=title, caption=caption,
            description=f"{record['category']} · {fmt_size(record['file_size'])}",
        )
    if ftype == "audio":
        return InlineQueryResultCachedAudio(id=rid, audio_file_id=fid, title=title, caption=caption)
    if ftype == "voice":
        return InlineQueryResultCachedVoice(id=rid, voice_file_id=fid, title=title, caption=caption)
    if ftype == "sticker":
        return InlineQueryResultCachedSticker(id=rid, sticker_file_id=fid)
    if ftype == "animation":
        return InlineQueryResultCachedGif(id=rid, gif_file_id=fid, caption=caption)
    return None  # video_note 不支持 inline，跳过


async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.inline_query.answer([], cache_time=0, is_personal=True)
        return
    store = get_store(context)
    keyword = update.inline_query.query.strip()
    rows = store.search(keyword, limit=10) if keyword else store.list_files(limit=10)

    results = []
    for r in rows:
        ir = to_inline_result(r)
        if ir:
            results.append(ir)

    await update.inline_query.answer(results, cache_time=0, is_personal=True)


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #

async def configure_bot(application: Application) -> None:
    me = await application.bot.get_me()
    logger.info(
        "Connected to bot @%s (id=%s)",
        me.username,
        me.id,
    )
    commands = [
        BotCommand("start", "开始使用"),
        BotCommand("help", "帮助"),
        BotCommand("recent", "最近文件"),
        BotCommand("list", "按分类浏览"),
        BotCommand("search", "搜索文件"),
        BotCommand("get", "获取文件"),
        BotCommand("tag", "添加标签"),
        BotCommand("rename", "重命名"),
        BotCommand("del", "删除记录"),
        BotCommand("stats", "统计"),
        BotCommand("note", "保存文本"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Configured %d bot commands.", len(commands))


def build_application() -> Application:
    store = FileStore(DB_PATH)

    builder = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(configure_bot)
        .connect_timeout(CONNECT_TIMEOUT)
        .read_timeout(READ_TIMEOUT)
        .write_timeout(WRITE_TIMEOUT)
        .pool_timeout(POOL_TIMEOUT)
    )
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
        logger.info("使用代理连接 Telegram: %s", sanitize_proxy(PROXY_URL))
    else:
        # 未显式配置时，httpx 仍会读取系统环境变量 HTTP_PROXY / HTTPS_PROXY
        logger.info("未配置 PROXY_URL，将使用系统环境代理（若有）")

    app = builder.build()
    app.bot_data["store"] = store

    media_filter = (
        filters.PHOTO
        | filters.Document.ALL
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.Sticker.ALL
        | filters.ANIMATION
        | filters.VIDEO_NOTE
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("recent", recent))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("get", get_command))
    app.add_handler(CommandHandler("del", del_command))
    app.add_handler(CommandHandler("tag", tag_command))
    app.add_handler(CommandHandler("rename", rename_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(media_filter, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CommandHandler("note", note_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(InlineQueryHandler(inline_query))

    app.add_error_handler(error_handler)

    return app


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """统一错误处理：网络抖动只记一行日志（轮询内部会自动无限重试），
    真正的代码 bug 才打印完整堆栈。"""
    exc = context.error
    if isinstance(exc, NetworkError):
        logger.error("网络错误（PTB 会自动重试，无需处理）: %s", exc)
    else:
        logger.error("处理更新时出错: %s", exc, exc_info=True)


def main() -> None:
    if not TOKEN:
        print("Missing BOT_TOKEN. Copy .env.example to .env and fill your token.", file=sys.stderr)
        sys.exit(1)

    app = build_application()
    logger.info("Bot is running with long polling. Metadata DB: %s", DB_PATH)
    # Python 3.12+ 不再隐式创建事件循环，3.14 下 asyncio.get_event_loop()
    # 会直接抛 RuntimeError（PTB 的 run_polling 内部依赖它），
    # 因此启动前先显式创建并设置一个事件循环。
    asyncio.set_event_loop(asyncio.new_event_loop())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
