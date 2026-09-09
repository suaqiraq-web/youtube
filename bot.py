import asyncio
import json
import logging
import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

asyncio.set_event_loop(asyncio.new_event_loop())

import yt_dlp
from dotenv import load_dotenv
from pyrogram import Client
from pytgcalls import GroupCallFactory
from pytgcalls.implementation.group_call_file import GroupCallFile
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
SESSION_STRINGS = os.getenv("SESSION_STRINGS", "")
SESSION_STRING_2 = os.getenv("SESSION_STRING_2", "") or os.getenv("SESSION_STRINGS_2", "")

configured_session_strings = [
    value.strip()
    for value in re.split(r"[,\n]+", f"{SESSION_STRING},{SESSION_STRINGS}")
    if value.strip()
]
if SESSION_STRING_2:
    configured_session_strings.extend(
        value.strip()
        for value in re.split(r"[,\n]+", SESSION_STRING_2)
        if value.strip()
    )
configured_session_strings = list(dict.fromkeys(configured_session_strings))
CACHE_DIR = Path(os.getenv("CACHE_DIR", "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_PATH = BASE_DIR / "cookies.txt"
AUDIO_FILE_IDS_PATH = CACHE_DIR / "audio_file_ids.json"
WARNINGS_PATH = CACHE_DIR / "warnings.json"
BAD_WORDS_PATH = BASE_DIR / "bad_words.txt"

try:
    AUDIO_FILE_IDS: dict[str, str] = json.loads(AUDIO_FILE_IDS_PATH.read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    AUDIO_FILE_IDS = {}

try:
    raw_warnings = json.loads(WARNINGS_PATH.read_text(encoding="utf-8"))
    if isinstance(raw_warnings, dict):
        WARNINGS: dict[str, int] = {
            str(key): max(0, int(value))
            for key, value in raw_warnings.items()
            if int(value) > 0
        }
    else:
        WARNINGS = {}
except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
    WARNINGS = {}

voice_client: Client | None = None
voice_clients: list[Client] = []
voice_client_users: dict[Client, Any] = {}
voice_calls: Any | None = None
voice_calls_by_group: dict[int, Any] = {}
voice_clients_by_group: dict[int, Client] = {}


def normalize_moderation_text(text: str) -> str:
    """توحيد النص قبل فحص الكلمات مع الحفاظ على حدود الكلمات."""
    text = text.casefold().replace("ـ", "")
    text = re.sub(r"[\u064B-\u065F\u0670]", "", text)
    text = text.translate(str.maketrans("إأآٱى", "ااااي"))
    return re.sub(r"\s+", " ", text).strip()


class BadWordsMatcher:
    """بحث متعدد الكلمات في مرور واحد باستخدام Aho-Corasick."""

    def __init__(self, words: list[str]) -> None:
        self.transitions: list[dict[str, int]] = [{}]
        self.failures: list[int] = [0]
        self.outputs: list[bool] = [False]
        for word in words:
            node = 0
            for character in word:
                if character not in self.transitions[node]:
                    self.transitions[node][character] = len(self.transitions)
                    self.transitions.append({})
                    self.failures.append(0)
                    self.outputs.append(False)
                node = self.transitions[node][character]
            self.outputs[node] = True

        queue = deque()
        for node in self.transitions[0].values():
            queue.append(node)
        while queue:
            node = queue.popleft()
            for character, child in self.transitions[node].items():
                fallback = self.failures[node]
                while fallback and character not in self.transitions[fallback]:
                    fallback = self.failures[fallback]
                self.failures[child] = self.transitions[fallback].get(character, 0)
                self.outputs[child] |= self.outputs[self.failures[child]]
                queue.append(child)

    def contains_bad_word(self, text: str) -> bool:
        node = 0
        for character in text:
            while node and character not in self.transitions[node]:
                node = self.failures[node]
            node = self.transitions[node].get(character, 0)
            if self.outputs[node]:
                return True
        return False


def load_bad_words() -> BadWordsMatcher:
    try:
        lines = BAD_WORDS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    words = {
        normalize_moderation_text(line)
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    }
    words.discard("")
    logger.info("Loaded %s moderation terms", len(words))
    return BadWordsMatcher(sorted(words, key=len, reverse=True))


BAD_WORDS_MATCHER = load_bad_words()


def build_voice_panel(group_id: int) -> InlineKeyboardMarkup:
    """لوحة تحكم ثابتة تبقى ظاهرة أثناء التشغيل."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏸ اوكف", callback_data=f"voice_pause:{group_id}"),
                InlineKeyboardButton("▶ غني", callback_data=f"voice_resume:{group_id}"),
            ],
            [InlineKeyboardButton("⏭ تخطي", callback_data=f"voice_skip:{group_id}")],
        ]
    )


def search_song(query: str, download: bool = False) -> dict[str, Any]:
    """البحث عن أغنية وتحميلها من يوتيوب"""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "default_search": "ytsearch5",
        "extractaudio": True,
        "audioformat": "mp3",
        "extractor_retries": 2,
        "fragment_retries": 2,
        "retries": 2,
        "sleep_interval_requests": 0,
        "force_ipv4": True,
        "js_runtimes": {"deno": {}},
    }
    if not download:
        # البحث عن النتيجة فقط؛ لا تطلب صيغ الفيديو قبل بدء التنزيل.
        options["extract_flat"] = "in_playlist"
    if COOKIES_PATH.is_file() and os.getenv("USE_YOUTUBE_COOKIES", "0") == "1":
        options["cookiefile"] = str(COOKIES_PATH)
    if download:
        options.update(
            {
                "format": "bestaudio[ext=m4a]/bestaudio/best",
                "concurrent_fragment_downloads": 8,
                "socket_timeout": 8,
                "outtmpl": str(CACHE_DIR / "%(id)s.%(ext)s"),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "128",
                    },
                ],
                "postprocessor_args": {
                    "ExtractAudio": ["-ac", "2", "-ar", "44100"],
                },
            }
        )

    is_youtube_url = bool(re.match(r"^https?://(?:www\.)?(?:youtube\.com|youtu\.be)/", query, re.IGNORECASE))
    lookup_options = options
    if download and not is_youtube_url:
        lookup_options = options.copy()
        lookup_options["extract_flat"] = "in_playlist"
        lookup_options["skip_download"] = True

    client_profiles = (None, ["web_safari"], ["android_vr"])
    last_error: Exception | None = None
    for attempt, client_profile in enumerate(client_profiles):
        try:
            attempt_options = lookup_options.copy()
            if client_profile is not None:
                # لا تعيد استخدام كوكيز قديمة مع العملاء البدلاء؛ قد تسبب 403.
                attempt_options.pop("cookiefile", None)
                attempt_options["extractor_args"] = {
                    "youtube": {"player_client": client_profile},
                }
            with yt_dlp.YoutubeDL(attempt_options) as downloader:
                info = downloader.extract_info(query, download=download)
            break
        except Exception as error:
            last_error = error
            if attempt == len(client_profiles) - 1:
                raise
            logger.warning(
                "YouTube request failed (%s/%s), retrying with another client: %s",
                attempt + 1,
                len(client_profiles),
                error,
            )
            time.sleep(0.5)
    else:
        raise last_error or RuntimeError("فشل طلب YouTube")

    if not info:
        raise ValueError("لم يتم العثور على نتيجة")
    if "entries" in info:
        entries = [entry for entry in info["entries"] if entry]
        if not entries:
            raise ValueError("لم يتم العثور على نتيجة")
        if not download:
            return entries[0]

        # جرّب النتائج التالية إذا كانت أول نتيجة محجوبة أو غير قابلة للتنزيل.
        for entry in entries:
            try:
                entry_url = entry.get("webpage_url") or entry.get("url")
                if not entry_url:
                    continue
                with yt_dlp.YoutubeDL(options) as downloader:
                    return downloader.extract_info(entry_url, download=True)
            except Exception as error:
                last_error = error
                logger.warning("تعذر تنزيل نتيجة YouTube، تجربة النتيجة التالية: %s", error)
        raise last_error or ValueError("تعذر تنزيل أي نتيجة")
    return info


def downloaded_audio(song: dict[str, Any]) -> Path:
    """الحصول على مسار الملف الصوتي المحمّل، أو إرجاع ملف موجود من الكاش."""
    song_id = song.get("id", "")
    matches = list(CACHE_DIR.glob(f"{song_id}.*"))

    audio_files = [path for path in matches if path.suffix.lower() == ".mp3"]
    if not audio_files:
        audio_files = [path for path in matches if path.suffix.lower() in {".m4a", ".webm", ".opus", ".wav"}]

    if not audio_files:
        all_files = list(CACHE_DIR.glob("*.mp3")) + list(CACHE_DIR.glob("*.m4a")) + list(CACHE_DIR.glob("*.wav"))
        if all_files:
            return max(all_files, key=os.path.getctime)
        raise FileNotFoundError("تعذر تجهيز الملف الصوتي")

    return audio_files[0]


def normalized_query(query: str) -> str:
    """توحيد اسم الأغنية لاستخدامه في كاش تيليجرام."""
    return re.sub(r"\s+", " ", query.strip().casefold())


def save_audio_file_id(query: str, file_id: str) -> None:
    """حفظ معرف الملف حتى يعاد إرساله من تيليجرام بدون رفع جديد."""
    AUDIO_FILE_IDS[normalized_query(query)] = file_id
    AUDIO_FILE_IDS_PATH.write_text(
        json.dumps(AUDIO_FILE_IDS, ensure_ascii=False),
        encoding="utf-8",
    )


async def ensure_voice_clients_in_group(chat: Any, bot: Any) -> None:
    """محاولة إدخال الحسابات المساعدة إلى المجموعة عند توفر صلاحية تيليجرام."""
    if not voice_clients:
        raise RuntimeError("لا توجد حسابات مساعدة متصلة")

    joined_clients: list[Client] = []
    missing_clients: list[Client] = []
    for client in voice_clients:
        try:
            await client.get_chat_member(chat.id, "me")
            joined_clients.append(client)
        except Exception:
            if chat.username:
                try:
                    await client.join_chat(chat.username)
                    joined_clients.append(client)
                except Exception:
                    missing_clients.append(client)
            else:
                missing_clients.append(client)

    if missing_clients:
        invite_link = None
        try:
            invite_link = await bot.create_chat_invite_link(
                chat_id=chat.id,
                name="voice assistants",
                member_limit=len(missing_clients),
            )
            for client in missing_clients:
                try:
                    await client.join_chat(invite_link.invite_link)
                    joined_clients.append(client)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if invite_link is not None:
                try:
                    await bot.revoke_chat_invite_link(
                        chat_id=chat.id,
                        invite_link=invite_link.invite_link,
                    )
                except Exception:
                    pass

    if not joined_clients:
        raise RuntimeError(
            "الحسابات المساعدة غير موجودة في المجموعة. أضف حسابًا مساعدًا واحدًا أولًا "
            "أو اجعل المجموعة عامة ليسهل انضمامها."
        )

    for client in joined_clients:
        for other_client, user in voice_client_users.items():
            if other_client is client:
                continue
            try:
                await client.add_chat_members(chat.id, user.id)
            except Exception:
                pass


def convert_to_voice_wav(input_path: Path) -> Path:
    """تحويل الملف الصوتي إلى WAV/PCM مناسب للتشغيل داخل المكالمة."""
    output_path = input_path.with_suffix(".wav")

    if input_path.suffix.lower() == ".wav":
        return input_path
    if output_path.is_file() and output_path.stat().st_mtime >= input_path.stat().st_mtime:
        return output_path

    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i", str(input_path),
                "-vn",
                "-acodec", "pcm_s16le",
                "-ar", "48000",
                "-ac", "2",
                "-threads", "0",
                "-loglevel", "error",
                "-nostdin",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        logger.info(f"✅ تم تحويل الملف لصيغة مناسبة للمكالمة: {output_path.name}")
        return output_path
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode("utf-8", errors="ignore")
        logger.error(f"❌ فشل تحويل الملف: {stderr[:300]}")
        return input_path


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """التحقق من صلاحيات المستخدم"""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return False
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in {"administrator", "creator"}
    except Exception:
        return False


async def is_group_admin(user_id: int, group_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """التحقق من أن المستخدم مشرف في المجموعة المرتبطة باللوحة."""
    try:
        member = await context.bot.get_chat_member(group_id, user_id)
        return member.status in {"administrator", "creator"}
    except Exception:
        return False


def warning_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def save_warnings() -> None:
    
    WARNINGS_PATH.write_text(
        json.dumps(WARNINGS, ensure_ascii=False),
        encoding="utf-8",
    )


async def get_protection_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any | None:
    """الحصول على العضو المستهدف من الرد أو منشن تيليجرام الحقيقي."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return None

    target = message.reply_to_message.from_user if message.reply_to_message else None
    if target is None:
        for entity in message.entities or []:
            if entity.type == "text_mention" and entity.user is not None:
                target = entity.user
                break

    if target is None:
        await message.reply_text("⚠️ رد على رسالة العضو أو استخدم منشن تيليجرام حقيقي.")
        return None
    if target.is_bot:
        await message.reply_text("⚠️ لا يمكن تطبيق الإجراء على بوت.")
        return None

    try:
        member = await context.bot.get_chat_member(chat.id, target.id)
    except Exception:
        await message.reply_text("❌ لم أستطع العثور على هذا العضو في المجموعة.")
        return None
    if member.status in {"administrator", "creator"}:
        await message.reply_text("⚠️ لا يمكن تطبيق الإجراء على مشرف.")
        return None
    return target


async def mute_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await is_admin(update, context):
        return
    target = await get_protection_target(update, context)
    if target is None:
        return
    try:
        await context.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        await message.reply_text(f"🔇 تم كتم {target.first_name}.")
    except Exception:
        logger.exception("Could not mute user %s in chat %s", target.id, message.chat.id)
        await message.reply_text("❌ لم أستطع كتم العضو. تأكد أن البوت مشرف ولديه صلاحية تقييد الأعضاء.")


async def unmute_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await is_admin(update, context):
        return
    target = await get_protection_target(update, context)
    if target is None:
        return
    try:
        await context.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions.all_permissions(),
        )
        await message.reply_text(f"🔊 تم رفع الكتم عن {target.first_name}.")
    except Exception:
        logger.exception("Could not unmute user %s in chat %s", target.id, message.chat.id)
        await message.reply_text("❌ لم أستطع رفع الكتم. تأكد من صلاحيات البوت.")


async def warn_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await is_admin(update, context):
        return
    target = await get_protection_target(update, context)
    if target is None:
        return

    key = warning_key(message.chat.id, target.id)
    count = WARNINGS.get(key, 0) + 1
    WARNINGS[key] = count
    save_warnings()

    if count < 3:
        await message.reply_text(f"⚠️ تم تحذير {target.first_name}. التحذيرات: {count}/3")
        return

    try:
        await context.bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        await message.reply_text(f"🔇 وصل {target.first_name} إلى 3 تحذيرات وتم كتمه.")
    except Exception:
        logger.exception("Could not auto-mute user %s in chat %s", target.id, message.chat.id)
        await message.reply_text(
            f"⚠️ وصل {target.first_name} إلى 3 تحذيرات، لكن تعذر كتمه. تأكد من صلاحيات البوت."
        )


async def unwarn_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await is_admin(update, context):
        return
    target = await get_protection_target(update, context)
    if target is None:
        return

    key = warning_key(message.chat.id, target.id)
    count = max(0, WARNINGS.get(key, 0) - 1)
    if count:
        WARNINGS[key] = count
    else:
        WARNINGS.pop(key, None)
    save_warnings()
    await message.reply_text(f"✅ تم رفع تحذير عن {target.first_name}. التحذيرات: {count}/3")


async def clear_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not await is_admin(update, context):
        return
    target = await get_protection_target(update, context)
    if target is None:
        return
    WARNINGS.pop(warning_key(message.chat.id, target.id), None)
    save_warnings()
    await message.reply_text(f"✅ تم رفع كل التحذيرات عن {target.first_name}.")


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """حذف الرسائل المسيئة من الأعضاء مع إبقاء رسائل المشرفين."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat or not message.text:
        return
    if not BAD_WORDS_MATCHER.contains_bad_word(normalize_moderation_text(message.text)):
        return

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in {"administrator", "creator"}:
            return
        await message.delete()
        logger.info("Deleted a moderation match from user %s in chat %s", user.id, chat.id)
    except Exception:
        logger.exception("Could not moderate message in chat %s", chat.id)
        return

    raise ApplicationHandlerStop


async def send_download_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تنزيل الأغنية وإرسالها كملف صوتي."""
    message = update.effective_message
    if not message or not message.text:
        return

    match = re.match(r"^يوت\s+(.+)$", message.text.strip(), flags=re.IGNORECASE)
    if not match:
        return
    
    query = match.group(1).strip()
    status = await message.reply_text("✨ الموسيقى قيد التجهيز، لا تستعجل 🎵 @znvsv")

    try:
        cached_file_id = AUDIO_FILE_IDS.get(normalized_query(query))
        if cached_file_id:
            try:
                await message.reply_audio(audio=cached_file_id)
                await status.delete()
                return
            except Exception:
                AUDIO_FILE_IDS.pop(normalized_query(query), None)

        song = await asyncio.to_thread(search_song, query, True)
        audio_path = await asyncio.to_thread(downloaded_audio, song)
        sent_audio = await message.reply_audio(audio=str(audio_path))
        if sent_audio.audio:
            await asyncio.to_thread(save_audio_file_id, query, sent_audio.audio.file_id)
        await status.delete()
    except Exception as error:
        logger.exception("Song search failed")
        try:
            await status.delete()
        except Exception:
            pass
        await message.reply_text(
            "❌ ما كدرت أحمّل هاي الأغنية. جرّب اسمًا أوضح أو أرسل رابط YouTube مباشر.\n"
            f"التفاصيل: {str(error)[:180]}"
        )


async def show_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عرض أوامر البوت حسب صلاحية المستخدم."""
    message = update.effective_message
    if not message:
        return

    if await is_admin(update, context):
        commands = (
            "📋 أوامر المشرفين:\n\n"
            "يوت اسم الأغنية - تنزيل الأغنية كملف صوتي\n"
            "شغل اسم الأغنية - تشغيل الأغنية في المكالمة\n"
            "اوكف - إيقاف الأغنية مؤقتًا\n"
            "غني - استئناف تشغيل الأغنية\n"
            "تخطي - تخطي الأغنية\n"
            "توقف - إنهاء التشغيل\n"
            "/pause - إيقاف مؤقت\n"
            "/resume - استئناف التشغيل\n"
            "/skip - تخطي الأغنية\n"
            "/stop - إنهاء التشغيل\n"
            "/volume 50 - ضبط مستوى الصوت\n"
            "/clean - تنظيف ملفات الكاش\n\n"
            "كتم - كتم العضو بالرد على رسالته\n"
            "رفع كتم - رفع الكتم\n"
            "تحذير - إضافة تحذير\n"
            "رفع تحذير - حذف تحذير واحد\n"
            "مسح تحذيرات - حذف كل التحذيرات"
        )
    else:
        commands = (
            "📋 الأوامر المتاحة للأعضاء:\n\n"
            "يوت اسم الأغنية - تنزيل الأغنية كملف صوتي\n"
            "الاوامر - عرض قائمة الأوامر"
        )

    await message.reply_text(commands)


async def play_song(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global voice_calls, voice_client
    message = update.effective_message
    
    if not message or not message.text:
        return
    
    if not await is_admin(update, context):
        return
    
    match = re.match(r"^شغل\s+(.+)$", message.text.strip(), flags=re.IGNORECASE)
    if not match:
        return
    
    if voice_client is None:
        await message.reply_text("❌ ميزة المكالمة غير مهيأة.")
        return

    query = match.group(1).strip()
    status = await message.reply_text("✨ الموسيقى قيد التجهيز، لا تستعجل 🎵 @znvsv")

    try:
        info = await asyncio.to_thread(search_song, query, False)
        song_id = info.get("id") or ""
        cached_path = None
        if song_id:
            matches = list(CACHE_DIR.glob(f"{song_id}.*"))
            for path in matches:
                if path.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus", ".wav"}:
                    cached_path = path
                    break

        if cached_path is None:
            download_query = (
                song_url
                if (song_url := info.get("webpage_url") or info.get("url"))
                else query
            )
            song = await asyncio.to_thread(search_song, download_query, True)
            audio_path = await asyncio.to_thread(downloaded_audio, song)
        else:
            song = info
            audio_path = cached_path

        title = song.get("title", query)

        audio_path = await asyncio.to_thread(convert_to_voice_wav, Path(audio_path))

        group_id = message.chat.id
        existing_call = voice_calls_by_group.get(group_id)
        if existing_call is not None:
            try:
                await existing_call.stop()
            except Exception:
                pass
            voice_calls_by_group.pop(group_id, None)

        await ensure_voice_clients_in_group(message.chat, context.bot)
        assigned_client = voice_clients_by_group.get(group_id)
        if assigned_client is not None and assigned_client.is_connected:
            clients = [assigned_client]
        else:
            assigned_clients = set(voice_clients_by_group.values())
            clients = [
                client for client in voice_clients
                if client.is_connected and client not in assigned_clients
            ]
        if not clients and voice_client is not None and voice_client.is_connected:
            if not voice_clients_by_group:
                clients = [voice_client]
        if not clients:
            raise RuntimeError("لا يوجد حساب مساعد متاح لهذه المجموعة")

        last_error: Exception | None = None
        started = False
        for client in clients:
            candidate_call = GroupCallFactory(client).get_file_group_call(
                input_filename=str(audio_path),
                play_on_repeat=False,
            )
            try:
                # تحميل المجموعة قبل محاولة الانضمام للمكالمة.
                await client.get_chat(message.chat.id)
                await candidate_call.start(message.chat.id)
                voice_client = client
                voice_calls = candidate_call
                voice_calls_by_group[group_id] = candidate_call
                voice_clients_by_group[group_id] = client
                started = True
                break
            except Exception as error:
                last_error = error
                try:
                    await candidate_call.stop()
                except Exception:
                    pass

        if not started:
            raise last_error or RuntimeError("تعذر تشغيل الحسابات المساعدة")

        try:
            await status.delete()
        except Exception:
            logger.warning("Could not delete voice preparation message")
        try:
            await message.reply_text(f"▶️ يتم تشغيل: {title}")
        except Exception:
            logger.warning("Voice started, but confirmation message could not be sent")
        return

    except Exception as e:
        logger.exception("Voice playback failed")
        error_text = str(e)
        if "PEER_ID_INVALID" in error_text or "Peer id invalid" in error_text:
            error_text = "الحساب المساعد غير موجود في هذه المجموعة أو لم يتعرف عليها بعد."
        elif "GROUPCALL_INVALID" in error_text:
            error_text = "افتح مكالمة صوتية في المجموعة أولاً ثم أعد أمر شغل."
        try:
            await status.edit_text(f"❌ تعذر تشغيل الأغنية: {error_text[:150]}")
        except Exception:
            logger.warning("Could not send voice playback error message")
async def control_call(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """التحكم في المكالمة الصوتية (للمشرفين فقط)"""
    global voice_calls, voice_client
    message = update.effective_message

    if not message:
        return

    if not await is_admin(update, context):
        return

    group_id = message.chat.id
    group_client = voice_clients_by_group.get(group_id)
    group_call = voice_calls_by_group.get(group_id)
    if group_client is None or not group_client.is_connected:
        await message.reply_text("❌ الحساب المساعد غير متصل بالمكالمات الصوتية.")
        return

    if group_call is None:
        await message.reply_text("❌ لا توجد أغنية قيد التشغيل.")
        return

    try:
        text = message.text.strip().casefold()

        command_aliases = {
            "تخطي": "skip",
            "غني": "resume",
            "توقف": "stop",
            "إيقاف": "pause",
            "ايقاف": "pause",
            "اوكف": "pause",
        }
        text = command_aliases.get(text, text)

        if text in {"/pause", "pause"}:
            group_call.pause_playout()
            await message.reply_text("⏸️ تم إيقاف التشغيل مؤقتاً.")

        elif text in {"/resume", "resume"}:
            group_call.resume_playout()
            await message.reply_text("▶️ تم تشغيل الأغنية.")

        elif text in {"/skip", "skip"}:
            await group_call.stop()
            voice_calls_by_group.pop(group_id, None)
            await message.reply_text("⏭️ تم تخطي الأغنية.")

        elif text in {"/stop", "stop"}:
            await group_call.stop()
            voice_calls_by_group.pop(group_id, None)
            await message.reply_text("⏹️ تم إنهاء التشغيل.")

        elif text == "/volume":
            if not context.args:
                await message.reply_text("ℹ️ استخدم: `/volume 50` لتغيير مستوى الصوت (0-100).")
                return
            try:
                volume = int(context.args[0])
                if 0 <= volume <= 100:
                    await message.reply_text(f"🔊 تم ضبط الصوت على {volume}%.")
                else:
                    await message.reply_text("⚠️ القيمة يجب أن تكون بين 0 و 100.")
            except ValueError:
                await message.reply_text("⚠️ يرجى إدخال رقم صحيح (0-100).")

    except Exception as e:
        logger.exception("Voice control failed")
        await message.reply_text(f"❌ تعذر تنفيذ الأمر: {str(e)[:100]}")


async def voice_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """لوحة التحكم بالمكالمة الصوتية في المجموعة."""
    message = update.effective_message
    if not message or not await is_admin(update, context):
        return

    try:
        await context.bot.send_message(
            chat_id=message.from_user.id,
            text="🎛 لوحة التحكم بالمكالمة الصوتية",
            reply_markup=build_voice_panel(message.chat.id),
        )
    except Exception:
        logger.info("Admin must start a private chat with the bot before using /panel")


async def handle_voice_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """معالجة أزرار لوحة التحكم في المكالمة."""
    query = update.callback_query
    if query is None or not query.data:
        return

    await query.answer()
    data = query.data
    parts = data.split(":")
    if len(parts) != 2 or parts[0] not in {"voice_pause", "voice_resume", "voice_skip"}:
        return
    try:
        group_id = int(parts[1])
    except ValueError:
        return

    if not await is_group_admin(query.from_user.id, group_id, context):
        await query.answer("❌ هذا الأمر متاح لمشرفي المجموعة فقط.", show_alert=True)
        return
    
    group_call = voice_calls_by_group.get(group_id)
    if group_call is None:
        await query.edit_message_text("❌ لا توجد مكالمة صوتية نشطة.")
        return

    try:
        if parts[0] == "voice_pause":
            group_call.pause_playout()
            await query.answer("⏸️ تم إيقاف التشغيل مؤقتاً.")
            await query.edit_message_reply_markup(reply_markup=build_voice_panel(group_id))
        elif parts[0] == "voice_resume":
            group_call.resume_playout()
            await query.answer("▶️ تم تشغيل الأغنية.")
            await query.edit_message_reply_markup(reply_markup=build_voice_panel(group_id))
        elif parts[0] == "voice_skip":
            await group_call.stop()
            voice_calls_by_group.pop(group_id, None)
            await query.answer("⏭️ تم تخطي الأغنية.")
            await query.edit_message_reply_markup(reply_markup=build_voice_panel(group_id))
    except Exception as e:
        logger.exception("Callback voice control failed")
        await query.answer(f"❌ تعذر تنفيذ الأمر: {str(e)[:100]}")
        await query.edit_message_reply_markup(reply_markup=build_voice_panel(group_id))


async def clean_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """تنظيف ملفات الكاش (للمشرفين فقط)"""
    if not await is_admin(update, context):
        await update.message.reply_text("❌ هذا الأمر متاح فقط للمشرفين.")
        return
    
    deleted = 0
    for file in CACHE_DIR.glob("*"):
        if file.is_file() and file.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus", ".wav"}:
            try:
                file.unlink()
                deleted += 1
            except Exception:
                pass
    
    await update.message.reply_text(f"🧹 تم حذف {deleted} ملف من الكاش.")


async def post_init(_: Application) -> None:
    """تهيئة البوت عند التشغيل"""
    global voice_client, voice_clients, voice_client_users
    if not (API_ID and API_HASH and configured_session_strings):
        logger.warning("⚠️ Voice calls disabled: missing API_ID, API_HASH, or session strings")
        return

    for index, session_string in enumerate(configured_session_strings, start=1):
        client = Client(
            f"voice_session_{index}",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=session_string,
        )
        try:
            await client.start()
            voice_clients.append(client)
            me = await client.get_me()
            voice_client_users[client] = me
            if voice_client is None:
                voice_client = client
            logger.info(f"✅ Voice client {index} started as: {me.first_name} (@{me.username})")
        except Exception:
            logger.exception(f"❌ Failed to start voice client {index}")
            try:
                await client.stop()
            except Exception:
                pass


async def post_shutdown(_: Application) -> None:
    """إيقاف البوت بشكل نظيف"""
    global voice_calls_by_group, voice_clients

    for group_call in voice_calls_by_group.values():
        try:
            await group_call.stop()
        except Exception:
            pass
    voice_calls_by_group.clear()
    
    for client in voice_clients:
        try:
            await client.stop()
        except Exception:
            pass
    
    logger.info("✅ Bot shut down cleanly")


def build_application() -> Application:
    """بناء التطبيق الرئيسي"""
    if not BOT_TOKEN:
        raise RuntimeError("❌ BOT_TOKEN غير موجود في ملف .env")
    
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    
    # أوامر التحكم بالمكالمة (للمشرفين فقط)
    application.add_handler(CommandHandler(
        ["pause", "resume", "skip", "stop", "volume"],
        control_call
    ))
    application.add_handler(CommandHandler("clean", clean_cache_command))
    application.add_handler(CommandHandler("mute", mute_member))
    application.add_handler(CommandHandler("unmute", unmute_member))
    application.add_handler(CommandHandler("warn", warn_member))
    application.add_handler(CommandHandler("unwarn", unwarn_member))
    application.add_handler(CommandHandler("clearwarnings", clear_warnings))

    application.add_handler(MessageHandler(
        filters.Regex(r"^كتم(?:\s+.+)?$") & filters.TEXT & filters.ChatType.GROUPS,
        mute_member,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(r"^رفع كتم(?:\s+.+)?$") & filters.TEXT & filters.ChatType.GROUPS,
        unmute_member,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(r"^تحذير(?:\s+.+)?$") & filters.TEXT & filters.ChatType.GROUPS,
        warn_member,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(r"^رفع تحذير(?:\s+.+)?$") & filters.TEXT & filters.ChatType.GROUPS,
        unwarn_member,
    ))
    application.add_handler(MessageHandler(
        filters.Regex(r"^مسح تحذيرات(?:\s+.+)?$") & filters.TEXT & filters.ChatType.GROUPS,
        clear_warnings,
    ))

    # عرض الأوامر حسب صلاحية المستخدم
    application.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS,
        moderate_message,
    ), group=-1)

    application.add_handler(MessageHandler(
        filters.Regex(r"^الاوامر$") & filters.TEXT,
        show_commands
    ))

    # أمر تشغيل الأغنية (للمشرفين فقط)
    application.add_handler(MessageHandler(
        filters.Regex(r"^شغل\s+.+$") & filters.TEXT,
        play_song
    ))

    # أوامر التحكم العربية المباشرة للمشرفين
    application.add_handler(MessageHandler(
        filters.Regex(r"^(تخطي|غني|توقف|إيقاف|ايقاف|اوكف)$") & filters.TEXT,
        control_call
    ))
    
    # أمر البحث عن الأغنية (لجميع المستخدمين)
    application.add_handler(MessageHandler(
        filters.Regex(r"^يوت\s+.+$") & filters.TEXT,
        send_download_audio
    ))
    
    return application


if __name__ == "__main__":
    try:
        application = build_application()
        logger.info("🤖 Bot started successfully!")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("⏹️ Bot stopped by user")
    except Exception as e:
        logger.exception(f"❌ Bot crashed: {e}")
