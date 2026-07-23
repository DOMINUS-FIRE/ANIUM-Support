import asyncio
import hashlib
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Union

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.types import KeyboardButton, Message, ReactionTypeEmoji, ReplyKeyboardMarkup, ReplyParameters
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPPORT_CHAT_RAW = os.getenv("SUPPORT_CHAT_ID", "").strip()
SUPPORT_THREAD_RAW = os.getenv("SUPPORT_THREAD_ID", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "support.db"))
PORT = int(os.getenv("PORT", "10000"))
WEBSITE_URL = os.getenv("WEBSITE_URL", "").strip().rstrip("/")
WEBSITE_ORIGINS = {
    item.strip()
    for item in os.getenv(
        "WEBSITE_ORIGINS",
        "http://localhost:5500,http://127.0.0.1:5500,null",
    ).split(",")
    if item.strip()
}
AUTH_REQUEST_SECONDS = int(os.getenv("AUTH_REQUEST_SECONDS", "600"))
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))

if not BOT_TOKEN:
    raise RuntimeError("Не указан BOT_TOKEN.")
if not SUPPORT_CHAT_RAW:
    raise RuntimeError("Не указан SUPPORT_CHAT_ID.")


def parse_chat_id(value: str) -> Union[int, str]:
    if value.lstrip("-").isdigit():
        return int(value)
    return value


SUPPORT_CHAT_ID = parse_chat_id(SUPPORT_CHAT_RAW)
SUPPORT_THREAD_ID = int(SUPPORT_THREAD_RAW) if SUPPORT_THREAD_RAW.isdigit() else None
SUPPORT_CHAT_NUMERIC_ID: int | None = None
BOT_USERNAME = "anium_service_bot"

router = Router()
TICKET_CREATION_LOCK = asyncio.Lock()
CHAT_SEND_LOCK = asyncio.Lock()
USER_ID_PATTERN = re.compile(r"#id(\d+)", re.IGNORECASE)
LOGIN_PAYLOAD_PATTERN = re.compile(r"^login_([A-Za-z0-9_-]{20,55})$")
LOGIN_CODE_PATTERN = re.compile(r"^\d{8}$")
REGISTER_BUTTON_TEXT = "🔐 Регистрация / вход"


def now_ts() -> int:
    return int(time.time())


def db_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    with db_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                first_name TEXT,
                last_name TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_map (
                support_message_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS website_tickets (
                ticket_number INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL UNIQUE,
                message_thread_id INTEGER NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
            );

            CREATE TABLE IF NOT EXISTS ticket_archive (
                archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_number INTEGER NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                closed_at INTEGER NOT NULL,
                messages_json TEXT NOT NULL,
                FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_ticket_archive_user
            ON ticket_archive(telegram_user_id, closed_at DESC);

            CREATE TABLE IF NOT EXISTS auth_requests (
                request_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                telegram_user_id INTEGER,
                session_token TEXT,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                approved_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS auth_codes (
                code TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY (request_id) REFERENCES auth_requests(request_id)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                telegram_user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
            );

            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                direction TEXT NOT NULL,
                source TEXT NOT NULL,
                text TEXT NOT NULL,
                support_message_id INTEGER,
                client_message_id TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (telegram_user_id) REFERENCES users(telegram_user_id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_client_message
            ON chat_messages(client_message_id)
            WHERE client_message_id IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_chat_user_id
            ON chat_messages(telegram_user_id, id);
            """
        )
        connection.commit()


def upsert_user_from_message(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    timestamp = now_ts()
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO users (
                telegram_user_id, username, full_name, first_name, last_name,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                updated_at = excluded.updated_at
            """,
            (
                user.id,
                user.username,
                user.full_name,
                user.first_name,
                user.last_name,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()


def save_mapping(support_message_id: int, user_id: int) -> None:
    with db_connection() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO message_map (support_message_id, user_id, created_at) VALUES (?, ?, ?)",
            (support_message_id, user_id, now_ts()),
        )
        connection.commit()


def get_user_id_from_db(support_message_id: int) -> int | None:
    with db_connection() as connection:
        row = connection.execute(
            "SELECT user_id FROM message_map WHERE support_message_id = ?",
            (support_message_id,),
        ).fetchone()
    return int(row["user_id"]) if row else None


def extract_user_id(message: Message) -> int | None:
    user_id = get_user_id_from_db(message.message_id)
    if user_id is not None:
        return user_id
    for value in (message.text, message.caption):
        if value:
            match = USER_ID_PATTERN.search(value)
            if match:
                return int(match.group(1))
    return None


def support_send_kwargs() -> dict[str, int]:
    return {"message_thread_id": SUPPORT_THREAD_ID} if SUPPORT_THREAD_ID else {}


def get_ticket_by_user(user_id: int) -> dict[str, Any] | None:
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM website_tickets WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def get_ticket_user_by_thread(thread_id: int | None) -> int | None:
    if thread_id is None:
        return None
    with db_connection() as connection:
        row = connection.execute(
            "SELECT telegram_user_id FROM website_tickets WHERE message_thread_id = ?",
            (thread_id,),
        ).fetchone()
    return int(row["telegram_user_id"]) if row else None


def get_ticket_by_thread(thread_id: int | None) -> dict[str, Any] | None:
    if thread_id is None:
        return None
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM website_tickets WHERE message_thread_id = ?",
            (thread_id,),
        ).fetchone()
    return dict(row) if row else None


def archive_and_delete_ticket(thread_id: int, user_id: int) -> None:
    with db_connection() as connection:
        ticket = connection.execute(
            "SELECT * FROM website_tickets WHERE message_thread_id = ?",
            (thread_id,),
        ).fetchone()
        messages = connection.execute(
            """
            SELECT id, direction, source, text, created_at
            FROM chat_messages
            WHERE telegram_user_id = ?
            ORDER BY id ASC
            """,
            (user_id,),
        ).fetchall()
        if ticket is not None:
            connection.execute(
                """
                INSERT INTO ticket_archive (
                    ticket_number, telegram_user_id, created_at, closed_at, messages_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ticket["ticket_number"],
                    user_id,
                    ticket["created_at"],
                    now_ts(),
                    json.dumps([dict(row) for row in messages], ensure_ascii=False),
                ),
            )
        connection.execute(
            "DELETE FROM website_tickets WHERE message_thread_id = ?",
            (thread_id,),
        )
        connection.execute("DELETE FROM message_map WHERE user_id = ?", (user_id,))
        connection.execute(
            "DELETE FROM chat_messages WHERE telegram_user_id = ?",
            (user_id,),
        )
        connection.commit()


def clear_website_history(user_id: int) -> None:
    with db_connection() as connection:
        connection.execute(
            "DELETE FROM chat_messages WHERE telegram_user_id = ?",
            (user_id,),
        )
        connection.commit()


def get_ticket_list(user_id: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    active = get_ticket_by_user(user_id)
    if active:
        result.append({
            "id": "active",
            "ticket_number": active["ticket_number"],
            "status": "active",
            "created_at": active["created_at"],
            "closed_at": None,
        })
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT archive_id, ticket_number, created_at, closed_at
            FROM ticket_archive
            WHERE telegram_user_id = ?
            ORDER BY closed_at DESC
            LIMIT 100
            """,
            (user_id,),
        ).fetchall()
    result.extend({
        "id": f"archive:{row['archive_id']}",
        "ticket_number": row["ticket_number"],
        "status": "closed",
        "created_at": row["created_at"],
        "closed_at": row["closed_at"],
    } for row in rows)
    return result


def get_archived_ticket_messages(user_id: int, archive_id: int) -> list[dict[str, Any]] | None:
    with db_connection() as connection:
        row = connection.execute(
            """
            SELECT messages_json FROM ticket_archive
            WHERE archive_id = ? AND telegram_user_id = ?
            """,
            (archive_id, user_id),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["messages_json"])
    except (TypeError, json.JSONDecodeError):
        return []


def ticket_topic_name(ticket: dict[str, Any], claimed: bool = False) -> str:
    user = get_user(int(ticket["telegram_user_id"])) or {}
    display_name = user.get("full_name") or user.get("username") or str(ticket["telegram_user_id"])
    prefix = "🕐 " if claimed else ""
    return f"{prefix}Тикет #{ticket['ticket_number']} · {display_name}"[:128]


async def _get_or_create_website_ticket_unlocked(bot: Bot, user_id: int) -> dict[str, Any]:
    existing = get_ticket_by_user(user_id)
    if existing:
        return existing

    user = get_user(user_id) or {}
    display_name = user.get("full_name") or user.get("username") or str(user_id)
    # Telegram limits forum topic names to 128 characters.
    topic_name = f"Тикет · {display_name}"[:128]
    topic = await bot.create_forum_topic(
        chat_id=SUPPORT_CHAT_NUMERIC_ID,
        name=topic_name,
    )
    with db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO website_tickets (
                telegram_user_id, message_thread_id, created_at
            ) VALUES (?, ?, ?)
            """,
            (user_id, topic.message_thread_id, now_ts()),
        )
        connection.commit()
        ticket_number = int(cursor.lastrowid)

    final_name = f"Тикет #{ticket_number} · {display_name}"[:128]
    await bot.edit_forum_topic(
        chat_id=SUPPORT_CHAT_NUMERIC_ID,
        message_thread_id=topic.message_thread_id,
        name=final_name,
    )
    return {
        "ticket_number": ticket_number,
        "telegram_user_id": user_id,
        "message_thread_id": topic.message_thread_id,
    }


async def get_or_create_website_ticket(bot: Bot, user_id: int) -> dict[str, Any]:
    async with TICKET_CREATION_LOCK:
        return await _get_or_create_website_ticket_unlocked(bot, user_id)


def get_user(user_id: int) -> dict[str, Any] | None:
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def user_profile_payload(user_id: int) -> dict[str, Any]:
    user = get_user(user_id) or {}
    return {
        "telegram_user_id": user_id,
        "username": user.get("username"),
        "full_name": user.get("full_name") or "Пользователь Telegram",
        "first_name": user.get("first_name") or user.get("full_name") or "Пользователь",
    }


def user_description_by_id(user_id: int) -> str:
    user = get_user(user_id) or {}
    full_name = html.escape(user.get("full_name") or "Пользователь Telegram")
    username = user.get("username")
    username_text = f"@{html.escape(username)}" if username else "username не указан"
    return f"<b>{full_name}</b>, {username_text} (<code>#id{user_id}</code>)"


def user_description(message: Message) -> str:
    if message.from_user is None:
        return "Неизвестный пользователь"
    return user_description_by_id(message.from_user.id)


def user_description_plain(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "Неизвестный пользователь"
    username = f"@{user.username}" if user.username else "username не указан"
    return f"{user.full_name}, {username} (#id{user.id})"


def insert_chat_message(
    user_id: int,
    direction: str,
    source: str,
    text: str,
    support_message_id: int | None = None,
    client_message_id: str | None = None,
) -> dict[str, Any]:
    created_at = now_ts()
    with db_connection() as connection:
        try:
            cursor = connection.execute(
                """
                INSERT INTO chat_messages (
                    telegram_user_id, direction, source, text,
                    support_message_id, client_message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    direction,
                    source,
                    text,
                    support_message_id,
                    client_message_id,
                    created_at,
                ),
            )
            connection.commit()
            message_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            if not client_message_id:
                raise
            row = connection.execute(
                "SELECT * FROM chat_messages WHERE client_message_id = ?",
                (client_message_id,),
            ).fetchone()
            return dict(row)
    return {
        "id": message_id,
        "telegram_user_id": user_id,
        "direction": direction,
        "source": source,
        "text": text,
        "support_message_id": support_message_id,
        "client_message_id": client_message_id,
        "created_at": created_at,
    }


def get_chat_messages(user_id: int, after_id: int) -> list[dict[str, Any]]:
    with db_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, direction, source, text, created_at
            FROM chat_messages
            WHERE telegram_user_id = ? AND id > ?
            ORDER BY id ASC
            LIMIT 150
            """,
            (user_id, max(0, after_id)),
        ).fetchall()
    return [dict(row) for row in rows]


def create_auth_request() -> tuple[str, str]:
    request_id = secrets.token_urlsafe(24)
    timestamp = now_ts()
    with db_connection() as connection:
        connection.execute(
            """
            INSERT INTO auth_requests (request_id, status, created_at, expires_at)
            VALUES (?, 'pending', ?, ?)
            """,
            (request_id, timestamp, timestamp + AUTH_REQUEST_SECONDS),
        )
        while True:
            login_code = f"{secrets.randbelow(100_000_000):08d}"
            try:
                connection.execute(
                    """
                    INSERT INTO auth_codes (code, request_id, created_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (login_code, request_id, timestamp, timestamp + AUTH_REQUEST_SECONDS),
                )
                break
            except sqlite3.IntegrityError:
                continue
        connection.execute("DELETE FROM auth_codes WHERE expires_at < ?", (timestamp - 3600,))
        connection.execute("DELETE FROM auth_requests WHERE expires_at < ?", (timestamp - 3600,))
        connection.commit()
    return request_id, login_code


def approve_auth_request(request_id: str, user_id: int) -> bool:
    timestamp = now_ts()
    with db_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE auth_requests
            SET status = 'approved', telegram_user_id = ?, approved_at = ?
            WHERE request_id = ? AND status = 'pending' AND expires_at >= ?
            """,
            (user_id, timestamp, request_id, timestamp),
        )
        connection.commit()
    return cursor.rowcount > 0


def approve_auth_code(code: str, user_id: int) -> str:
    timestamp = now_ts()
    with db_connection() as connection:
        row = connection.execute(
            """
            SELECT request_id, expires_at
            FROM auth_codes
            WHERE code = ?
            """,
            (code,),
        ).fetchone()
    if row is None:
        return "not_found"
    if int(row["expires_at"]) < timestamp:
        return "expired"
    return "approved" if approve_auth_request(str(row["request_id"]), user_id) else "used"


def registration_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=REGISTER_BUTTON_TEXT)]],
        resize_keyboard=True,
        input_field_placeholder="Нажмите «Регистрация / вход»",
    )


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def auth_status(request_id: str) -> dict[str, Any]:
    timestamp = now_ts()
    with db_connection() as connection:
        row = connection.execute(
            "SELECT * FROM auth_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return {"status": "not_found"}
        row = dict(row)
        if row["expires_at"] < timestamp:
            connection.execute(
                "UPDATE auth_requests SET status = 'expired' WHERE request_id = ?",
                (request_id,),
            )
            connection.commit()
            return {"status": "expired"}
        if row["status"] != "approved" or not row["telegram_user_id"]:
            return {"status": row["status"]}

        raw_token = row.get("session_token")
        if not raw_token:
            raw_token = secrets.token_urlsafe(40)
            connection.execute(
                """
                INSERT INTO sessions (token_hash, telegram_user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    token_hash(raw_token),
                    row["telegram_user_id"],
                    timestamp,
                    timestamp + SESSION_DAYS * 86400,
                ),
            )
            connection.execute(
                "UPDATE auth_requests SET session_token = ? WHERE request_id = ?",
                (raw_token, request_id),
            )
            connection.commit()

    return {
        "status": "authorized",
        "session_token": raw_token,
        "user": user_profile_payload(int(row["telegram_user_id"])),
    }


def authenticate_request(request: web.Request) -> int | None:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return None
    raw_token = authorization[7:].strip()
    if not raw_token:
        return None
    timestamp = now_ts()
    with db_connection() as connection:
        row = connection.execute(
            """
            SELECT telegram_user_id
            FROM sessions
            WHERE token_hash = ? AND revoked = 0 AND expires_at >= ?
            """,
            (token_hash(raw_token), timestamp),
        ).fetchone()
    return int(row["telegram_user_id"]) if row else None


def make_media_caption(message: Message, description: str) -> str:
    original_caption = message.caption or ""
    separator = "\n\n" if original_caption else ""
    available = 1024 - len(separator) - len(description)
    return f"{original_caption[:max(0, available)]}{separator}{description}"[:1024]


def message_supports_caption(message: Message) -> bool:
    return bool(
        message.photo
        or message.video
        or message.animation
        or message.document
        or message.audio
        or message.voice
    )


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start_handler(message: Message, command: CommandObject) -> None:
    upsert_user_from_message(message)
    payload = (command.args or "").strip()
    login_match = LOGIN_PAYLOAD_PATTERN.fullmatch(payload)

    if login_match and message.from_user is not None:
        request_id = login_match.group(1)
        if approve_auth_request(request_id, message.from_user.id):
            await message.answer(
                "✅ <b>Вход в ANIUM подтверждён.</b>\n\n"
                "Вернитесь на сайт — аккаунт откроется автоматически.",
                reply_markup=registration_keyboard(),
            )
        else:
            await message.answer(
                "Ссылка для входа устарела или уже недействительна. "
                "Вернитесь на сайт и создайте новую ссылку."
            )
        return

    await message.answer(
        "Приветствуем! 🚀\n\n"
        "Ваш аккаунт ANIUM создан или обновлён.\n\n"
        "Чтобы войти на сайт, нажмите кнопку <b>«Регистрация / вход»</b>, "
        "затем отправьте 8 цифр, показанных на сайте.\n\n"
        "Через этого бота также можно получать ответы поддержки.\n\n"
        "⏳ График работы: ежедневно с 13:00 до 22:00 по МСК.\n"
        "📬 Время ответа: обычно в течение 1–3 часов.",
        reply_markup=registration_keyboard(),
    )


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_message_handler(message: Message, bot: Bot) -> None:
    if message.from_user is None:
        return
    upsert_user_from_message(message)
    user_id = message.from_user.id

    if message.text == REGISTER_BUTTON_TEXT:
        await message.answer(
            "Введите <b>8-значный код</b>, который показан в окне регистрации на сайте.\n\n"
            "Код действует ограниченное время и подходит только для одного входа.",
            reply_markup=registration_keyboard(),
        )
        return

    login_code = (message.text or "").strip().replace(" ", "")
    if LOGIN_CODE_PATTERN.fullmatch(login_code):
        result = approve_auth_code(login_code, user_id)
        if result == "approved":
            await message.answer(
                "✅ <b>Регистрация и вход подтверждены.</b>\n\n"
                "Вернитесь на сайт — личный кабинет откроется автоматически.",
                reply_markup=registration_keyboard(),
            )
        elif result == "expired":
            await message.answer("⌛ Код устарел. Сгенерируйте новый код на сайте.")
        elif result == "used":
            await message.answer("Этот код уже использован. Сгенерируйте новый код на сайте.")
        else:
            await message.answer("Код не найден. Проверьте 8 цифр или сгенерируйте новый код на сайте.")
        return

    description = user_description(message)
    plain_description = user_description_plain(message)

    try:
        if message.text:
            ticket_text = f"📱 <b>Сообщение из Telegram</b>\n\n{html.escape(message.text)}\n\n{description}"
            support_message = await bot.send_message(
                chat_id=SUPPORT_CHAT_NUMERIC_ID,
                text=ticket_text[:4096],
                **support_send_kwargs(),
            )
            save_mapping(support_message.message_id, user_id)
            insert_chat_message(user_id, "user", "telegram", message.text, support_message.message_id)
        elif message_supports_caption(message):
            copied = await bot.copy_message(
                chat_id=SUPPORT_CHAT_NUMERIC_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=make_media_caption(message, plain_description),
                parse_mode=None,
                **support_send_kwargs(),
            )
            save_mapping(copied.message_id, user_id)
            insert_chat_message(
                user_id,
                "user",
                "telegram",
                message.caption or "📎 Пользователь отправил вложение в Telegram.",
                copied.message_id,
            )
        else:
            copied = await bot.copy_message(
                chat_id=SUPPORT_CHAT_NUMERIC_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                **support_send_kwargs(),
            )
            save_mapping(copied.message_id, user_id)
            info_message = await bot.send_message(
                chat_id=SUPPORT_CHAT_NUMERIC_ID,
                text=description,
                reply_parameters=ReplyParameters(
                    message_id=copied.message_id,
                    allow_sending_without_reply=True,
                ),
                **support_send_kwargs(),
            )
            save_mapping(info_message.message_id, user_id)
            insert_chat_message(
                user_id,
                "user",
                "telegram",
                "📎 Пользователь отправил вложение в Telegram.",
                copied.message_id,
            )

        await message.answer("Сообщение передано поддержке ANIUM.")
    except (TelegramBadRequest, TelegramForbiddenError):
        logging.exception("Не удалось передать сообщение в поддержку")
        await message.answer("Не удалось передать сообщение. Попробуйте ещё раз позже.")


@router.message(Command("claim"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def claim_ticket_handler(message: Message, bot: Bot) -> None:
    if SUPPORT_CHAT_NUMERIC_ID is None or message.chat.id != SUPPORT_CHAT_NUMERIC_ID:
        return
    ticket = get_ticket_by_thread(message.message_thread_id)
    if ticket is None:
        await message.delete()
        return
    await bot.edit_forum_topic(
        chat_id=SUPPORT_CHAT_NUMERIC_ID,
        message_thread_id=int(ticket["message_thread_id"]),
        name=ticket_topic_name(ticket, claimed=True),
    )
    await message.delete()


@router.message(Command("close"), F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def close_ticket_handler(message: Message, bot: Bot) -> None:
    if SUPPORT_CHAT_NUMERIC_ID is None or message.chat.id != SUPPORT_CHAT_NUMERIC_ID:
        return
    ticket = get_ticket_by_thread(message.message_thread_id)
    if ticket is None:
        await message.delete()
        return
    thread_id = int(ticket["message_thread_id"])
    user_id = int(ticket["telegram_user_id"])
    async with CHAT_SEND_LOCK:
        await bot.delete_forum_topic(
            chat_id=SUPPORT_CHAT_NUMERIC_ID,
            message_thread_id=thread_id,
        )
        archive_and_delete_ticket(thread_id, user_id)


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def support_reply_handler(message: Message, bot: Bot) -> None:
    if SUPPORT_CHAT_NUMERIC_ID is None or message.chat.id != SUPPORT_CHAT_NUMERIC_ID:
        return
    if message.sender_chat is None and message.from_user and message.from_user.is_bot:
        return
    if message.forum_topic_closed:
        ticket = get_ticket_by_thread(message.message_thread_id)
        if ticket:
            async with CHAT_SEND_LOCK:
                archive_and_delete_ticket(
                    int(ticket["message_thread_id"]),
                    int(ticket["telegram_user_id"]),
                )
        return
    if message.forum_topic_created or message.forum_topic_reopened:
        return

    replied_message = message.reply_to_message
    user_id = extract_user_id(replied_message) if replied_message else None
    if user_id is None:
        user_id = get_ticket_user_by_thread(message.message_thread_id)
    if user_id is None:
        if replied_message is None:
            return
        await message.reply(
            "❌ Не удалось определить клиента. Ответьте на сообщение, где указан <code>#id...</code>."
        )
        return

    sender_name = (
        message.sender_chat.title
        if message.sender_chat
        else message.from_user.full_name if message.from_user else "Поддержка ANIUM"
    )
    website_text = message.text or message.caption or "📎 Поддержка отправила вложение. Оно также отправлено вам в Telegram."

    try:
        if message.text:
            await bot.send_message(
                chat_id=user_id,
                text="💬 <b>Ответ поддержки ANIUM:</b>\n\n" + html.escape(message.text),
            )
        else:
            await bot.send_message(chat_id=user_id, text="💬 <b>Ответ поддержки ANIUM:</b>")
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )

        insert_chat_message(
            user_id,
            "support",
            "support_group",
            website_text,
            message.message_id,
        )
        save_mapping(message.message_id, user_id)

        try:
            await message.react([ReactionTypeEmoji(emoji="✅")])
        except TelegramBadRequest:
            await message.reply("✅ Ответ отправлен клиенту на сайт и в Telegram.")
    except TelegramForbiddenError:
        insert_chat_message(
            user_id,
            "support",
            "support_group",
            website_text,
            message.message_id,
        )
        await message.reply(
            "⚠ Ответ появился в чате на сайте, но Telegram-сообщение не доставлено: клиент заблокировал бота."
        )
    except TelegramBadRequest as error:
        logging.exception("Ошибка доставки ответа")
        await message.reply(f"❌ Ошибка доставки: <code>{html.escape(str(error))}</code>")


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = web.json_response(
                {"error": error.reason or "Ошибка запроса"},
                status=error.status,
            )
        except Exception:
            logging.exception("Необработанная ошибка API")
            response = web.json_response({"error": "Внутренняя ошибка сервера"}, status=500)

    origin = request.headers.get("Origin")
    if origin and (origin in WEBSITE_ORIGINS or "*" in WEBSITE_ORIGINS):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    elif origin == "null" and "null" in WEBSITE_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = "null"
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "86400"
    return response


async def json_body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except (json.JSONDecodeError, web.HTTPBadRequest):
        raise web.HTTPBadRequest(reason="Некорректный JSON")
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(reason="Ожидается JSON-объект")
    return data


def require_user(request: web.Request) -> int:
    user_id = authenticate_request(request)
    if user_id is None:
        raise web.HTTPUnauthorized(reason="Требуется вход через Telegram")
    return user_id


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "service": "ANIUM Bot + Website API",
            "bot_username": BOT_USERNAME,
            "support_ready": SUPPORT_CHAT_NUMERIC_ID is not None,
        }
    )


async def api_auth_start(_: web.Request) -> web.Response:
    request_id, login_code = create_auth_request()
    payload = f"login_{request_id}"
    return web.json_response(
        {
            "request_id": request_id,
            "login_code": login_code,
            "bot_url": f"https://t.me/{BOT_USERNAME}?start={payload}",
            "expires_in": AUTH_REQUEST_SECONDS,
        }
    )


async def api_auth_status(request: web.Request) -> web.Response:
    request_id = request.query.get("request_id", "").strip()
    if not request_id:
        raise web.HTTPBadRequest(reason="Не указан request_id")
    result = auth_status(request_id)
    status = 404 if result["status"] == "not_found" else 200
    return web.json_response(result, status=status)


async def api_me(request: web.Request) -> web.Response:
    user_id = require_user(request)
    return web.json_response({"user": user_profile_payload(user_id)})


async def api_logout(request: web.Request) -> web.Response:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        raw_token = authorization[7:].strip()
        with db_connection() as connection:
            connection.execute(
                "UPDATE sessions SET revoked = 1 WHERE token_hash = ?",
                (token_hash(raw_token),),
            )
            connection.commit()
    return web.json_response({"ok": True})


async def api_chat_messages(request: web.Request) -> web.Response:
    user_id = require_user(request)
    try:
        after_id = int(request.query.get("after_id", "0"))
    except ValueError:
        after_id = 0
    # An active website ticket is the source of truth. This also cleans up
    # orphaned history left by topics removed before the close handler existed.
    if get_ticket_by_user(user_id) is None:
        clear_website_history(user_id)
        return web.json_response({"messages": [], "reset": True, "ticket_active": False})

    messages = get_chat_messages(user_id, after_id)
    with db_connection() as connection:
        has_history = connection.execute(
            "SELECT 1 FROM chat_messages WHERE telegram_user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone() is not None
    return web.json_response({
        "messages": messages,
        "reset": after_id > 0 and not has_history,
        "ticket_active": True,
    })


async def api_tickets(request: web.Request) -> web.Response:
    user_id = require_user(request)
    return web.json_response({"tickets": get_ticket_list(user_id)})


async def api_ticket_messages(request: web.Request) -> web.Response:
    user_id = require_user(request)
    try:
        archive_id = int(request.match_info["archive_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(reason="Некорректный номер тикета")
    messages = get_archived_ticket_messages(user_id, archive_id)
    if messages is None:
        raise web.HTTPNotFound(reason="Тикет не найден")
    return web.json_response({"messages": messages, "status": "closed"})


def build_group_ticket(user_id: int, text: str, order: dict[str, Any] | None) -> str:
    heading = "🛒 <b>Заявка с сайта</b>" if order else "🌐 <b>Сообщение с сайта</b>"
    details: list[str] = [heading, "", html.escape(text)]
    if order:
        details.extend(
            [
                "",
                f"<b>Карточка:</b> {html.escape(str(order.get('product') or 'Не указана'))}",
                f"<b>Product ID:</b> <code>{html.escape(str(order.get('productId') or ''))}</code>",
            ]
        )
        if order.get("variant"):
            details.append(f"<b>Вариант:</b> {html.escape(str(order['variant']))}")
        if order.get("contact"):
            details.append(f"<b>Данные:</b> {html.escape(str(order['contact']))}")
        if order.get("orderId"):
            details.append(f"<b>Заказ:</b> <code>{html.escape(str(order['orderId']))}</code>")
    details.extend(["", user_description_by_id(user_id)])
    return "\n".join(details)[:4096]


async def _api_chat_send_unlocked(request: web.Request) -> web.Response:
    user_id = require_user(request)
    data = await json_body(request)
    text = str(data.get("text") or "").strip()
    if not text:
        raise web.HTTPBadRequest(reason="Сообщение пустое")
    if len(text) > 4000:
        raise web.HTTPBadRequest(reason="Сообщение слишком длинное")

    client_message_id = str(data.get("client_message_id") or "").strip()[:100] or None
    order = data.get("order") if isinstance(data.get("order"), dict) else None

    with db_connection() as connection:
        recent_count = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM chat_messages
            WHERE telegram_user_id = ? AND direction = 'user' AND created_at >= ?
            """,
            (user_id, now_ts() - 60),
        ).fetchone()["count"]
    if recent_count >= 15:
        raise web.HTTPTooManyRequests(reason="Слишком много сообщений. Подождите минуту.")

    if client_message_id:
        with db_connection() as connection:
            existing = connection.execute(
                "SELECT * FROM chat_messages WHERE client_message_id = ?",
                (client_message_id,),
            ).fetchone()
        if existing:
            return web.json_response({"message": dict(existing), "duplicate": True})

    if SUPPORT_CHAT_NUMERIC_ID is None:
        raise web.HTTPServiceUnavailable(reason="Группа поддержки ещё не подключена")

    bot: Bot = request.app["bot"]
    ticket = await get_or_create_website_ticket(bot, user_id)
    group_text = build_group_ticket(user_id, text, order)
    history_reset = False
    try:
        support_message = await bot.send_message(
            chat_id=SUPPORT_CHAT_NUMERIC_ID,
            text=group_text,
            message_thread_id=ticket["message_thread_id"],
        )
    except TelegramBadRequest:
        # The topic may have been removed manually in Telegram while its local
        # mapping still existed. Reset it and retry in a fresh ticket.
        logging.warning(
            "Тема тикета %s недоступна; создаём новую для пользователя %s",
            ticket["message_thread_id"],
            user_id,
        )
        archive_and_delete_ticket(int(ticket["message_thread_id"]), user_id)
        ticket = await get_or_create_website_ticket(bot, user_id)
        support_message = await bot.send_message(
            chat_id=SUPPORT_CHAT_NUMERIC_ID,
            text=group_text,
            message_thread_id=ticket["message_thread_id"],
        )
        history_reset = True
    save_mapping(support_message.message_id, user_id)
    saved = insert_chat_message(
        user_id,
        "user",
        "website_order" if order else "website",
        text,
        support_message.message_id,
        client_message_id,
    )
    return web.json_response({"message": saved, "reset": history_reset})


async def api_chat_send(request: web.Request) -> web.Response:
    # Keep the duplicate check and insert in one process-wide critical section.
    # This prevents simultaneous auth callbacks/tabs from forwarding one order
    # several times before its client_message_id is stored.
    async with CHAT_SEND_LOCK:
        return await _api_chat_send_unlocked(request)


async def start_http_server(bot: Bot) -> web.AppRunner:
    app = web.Application(middlewares=[cors_middleware])
    app["bot"] = bot
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_post("/api/auth/start", api_auth_start)
    app.router.add_get("/api/auth/status", api_auth_status)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/logout", api_logout)
    app.router.add_get("/api/chat/messages", api_chat_messages)
    app.router.add_post("/api/chat/send", api_chat_send)
    app.router.add_get("/api/tickets", api_tickets)
    app.router.add_get("/api/tickets/{archive_id:\\d+}/messages", api_ticket_messages)
    app.router.add_route("OPTIONS", "/{tail:.*}", lambda _: web.Response(status=204))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logging.info("HTTP API запущен на 0.0.0.0:%s", PORT)
    return runner


async def main() -> None:
    global SUPPORT_CHAT_NUMERIC_ID, BOT_USERNAME

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    http_runner: web.AppRunner | None = None
    try:
        bot_info = await bot.get_me()
        BOT_USERNAME = bot_info.username or BOT_USERNAME

        http_runner = await start_http_server(bot)

        support_chat = await bot.get_chat(SUPPORT_CHAT_ID)
        SUPPORT_CHAT_NUMERIC_ID = support_chat.id
        logging.info(
            "Группа поддержки найдена: %s (%s)",
            support_chat.title or support_chat.username,
            SUPPORT_CHAT_NUMERIC_ID,
        )

        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        if http_runner is not None:
            await http_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
