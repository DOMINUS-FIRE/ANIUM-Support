import asyncio
import html
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Union

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import Message, ReactionTypeEmoji, ReplyParameters
from dotenv import load_dotenv


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPPORT_CHAT_RAW = os.getenv("SUPPORT_CHAT_ID", "@thfjffhf").strip()
SUPPORT_THREAD_RAW = os.getenv("SUPPORT_THREAD_ID", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "support.db"))
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError(
        "Не указан BOT_TOKEN. Добавьте токен в переменные окружения Render "
        "или в локальный файл .env."
    )


def parse_chat_id(value: str) -> Union[int, str]:
    """Принимает @username либо числовой Telegram chat_id."""
    if value.lstrip("-").isdigit():
        return int(value)
    return value


SUPPORT_CHAT_ID = parse_chat_id(SUPPORT_CHAT_RAW)
SUPPORT_THREAD_ID = int(SUPPORT_THREAD_RAW) if SUPPORT_THREAD_RAW.isdigit() else None
SUPPORT_CHAT_NUMERIC_ID: int | None = None

router = Router()
USER_ID_PATTERN = re.compile(r"#id(\d+)", re.IGNORECASE)


def init_db() -> None:
    """Создаёт локальную таблицу соответствий сообщений."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_map (
                support_message_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()


def save_mapping(support_message_id: int, user_id: int) -> None:
    """Сохраняет связь: сообщение в группе -> пользователь."""
    with sqlite3.connect(DB_PATH) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO message_map (support_message_id, user_id)
            VALUES (?, ?)
            """,
            (support_message_id, user_id),
        )
        connection.commit()


def get_user_id_from_db(support_message_id: int) -> int | None:
    with sqlite3.connect(DB_PATH) as connection:
        row = connection.execute(
            """
            SELECT user_id
            FROM message_map
            WHERE support_message_id = ?
            """,
            (support_message_id,),
        ).fetchone()
    return int(row[0]) if row else None


def extract_user_id(message: Message) -> int | None:
    """
    Сначала ищет пользователя в SQLite.
    Если Render перезапустился и локальная база очистилась,
    берёт ID прямо из текста/подписи обращения: #id123456789.
    """
    user_id = get_user_id_from_db(message.message_id)
    if user_id is not None:
        return user_id

    for value in (message.text, message.caption):
        if not value:
            continue
        match = USER_ID_PATTERN.search(value)
        if match:
            return int(match.group(1))

    return None


def support_send_kwargs() -> dict:
    if SUPPORT_THREAD_ID is None:
        return {}
    return {"message_thread_id": SUPPORT_THREAD_ID}


def user_description(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "Неизвестный пользователь"

    username = (
        f"@{html.escape(user.username)}"
        if user.username
        else "юзернейм не указан"
    )

    return (
        f"<b>{html.escape(user.full_name)}</b>, "
        f"{username} (<code>#id{user.id}</code>)"
    )


def user_description_plain(message: Message) -> str:
    user = message.from_user
    if user is None:
        return "Неизвестный пользователь"

    username = f"@{user.username}" if user.username else "юзернейм не указан"
    return f"{user.full_name}, {username} (#id{user.id})"


def make_media_caption(message: Message, description: str) -> str:
    original_caption = message.caption or ""
    separator = "\n\n" if original_caption else ""
    available = 1024 - len(separator) - len(description)

    if available < 0:
        return description[:1024]

    original_caption = original_caption[:available]
    return f"{original_caption}{separator}{description}"


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
async def start_handler(message: Message) -> None:
    await message.answer(
        "Приветствуем! 🚀\n\n"
        "Вы обратились в службу поддержки сервиса ANIUM — "
        "оплата зарубежных подписок, игр и софта из РФ.\n\n"
        "⏳ График работы: ежедневно с 13:00 до 22:00 (по МСК).\n\n"
        "📬 Время ответа: в течение 1–3 часов.\n\n"
        "Напишите прямо сюда ваш вопрос, и менеджер свяжется с вами!"
    )


@router.message(F.chat.type == ChatType.PRIVATE)
async def private_message_handler(message: Message, bot: Bot) -> None:
    """Передаёт сообщение пользователя в группу поддержки."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    description = user_description(message)
    plain_description = user_description_plain(message)

    try:
        if message.text:
            ticket_text = f"{html.escape(message.text)}\n\n{description}"

            if len(ticket_text) <= 4096:
                support_message = await bot.send_message(
                    chat_id=SUPPORT_CHAT_NUMERIC_ID,
                    text=ticket_text,
                    **support_send_kwargs(),
                )
                save_mapping(support_message.message_id, user_id)
            else:
                # Для очень длинного текста сначала отправляем карточку пользователя.
                info_message = await bot.send_message(
                    chat_id=SUPPORT_CHAT_NUMERIC_ID,
                    text=description,
                    **support_send_kwargs(),
                )
                save_mapping(info_message.message_id, user_id)

                copied = await bot.copy_message(
                    chat_id=SUPPORT_CHAT_NUMERIC_ID,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_parameters=ReplyParameters(
                        message_id=info_message.message_id,
                        allow_sending_without_reply=True,
                    ),
                    **support_send_kwargs(),
                )
                save_mapping(copied.message_id, user_id)

        elif message_supports_caption(message):
            # Метаданные пользователя находятся прямо в подписи.
            copied = await bot.copy_message(
                chat_id=SUPPORT_CHAT_NUMERIC_ID,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=make_media_caption(message, plain_description),
                parse_mode=None,
                **support_send_kwargs(),
            )
            save_mapping(copied.message_id, user_id)

        else:
            # Стикеры, видеосообщения и типы без подписи.
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

        await message.answer(
            "Ожидайте ответа в течение 1–3 часов. "
            "Мы поможем, как только появится возможность."
        )

    except (TelegramBadRequest, TelegramForbiddenError):
        logging.exception("Не удалось отправить обращение в группу поддержки")
        await message.answer(
            "Не удалось передать сообщение поддержке. "
            "Попробуйте отправить его ещё раз немного позже."
        )


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.reply_to_message,
)
async def support_reply_handler(message: Message, bot: Bot) -> None:
    """
    Сотрудник отвечает через Reply в группе.
    Поддерживаются ответы как от личного аккаунта,
    так и от имени канала/группы через sender_chat.
    """
    if SUPPORT_CHAT_NUMERIC_ID is None or message.chat.id != SUPPORT_CHAT_NUMERIC_ID:
        return

    # Если сообщение отправлено от имени канала/группы, Telegram может
    # указать sender_chat и подставить технического bot-пользователя в from_user.
    # Поэтому такие ответы не отбрасываем.
    if (
        message.sender_chat is None
        and message.from_user is not None
        and message.from_user.is_bot
    ):
        return

    replied_message = message.reply_to_message
    if replied_message is None:
        return

    user_id = extract_user_id(replied_message)

    if user_id is None:
        await message.reply(
            "❌ Не удалось определить пользователя.\n\n"
            "Ответьте именно на сообщение обращения, в котором указан "
            "<code>#id...</code>."
        )
        return

    try:
        if message.text:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "💬 <b>Ответ поддержки ANIUM:</b>\n\n"
                    f"{html.escape(message.text)}"
                ),
            )
        else:
            await bot.send_message(
                chat_id=user_id,
                text="💬 <b>Ответ поддержки ANIUM:</b>",
            )
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )

        # Сохраняем и ответ администратора, чтобы можно было продолжать ветку.
        save_mapping(message.message_id, user_id)

        try:
            await message.react([ReactionTypeEmoji(emoji="✅")])
        except TelegramBadRequest:
            await message.reply("✅ Ответ отправлен пользователю.")

    except TelegramForbiddenError:
        await message.reply(
            "❌ Не удалось доставить ответ: пользователь заблокировал бота "
            "или удалил диалог."
        )
    except TelegramBadRequest as error:
        logging.exception("Ошибка доставки ответа пользователю")
        await message.reply(
            "❌ Не удалось доставить ответ.\n"
            f"<code>{html.escape(str(error))}</code>"
        )


async def health_handler(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "service": "ANIUM Support Bot",
            "telegram_polling": True,
        }
    )


async def start_http_server() -> web.AppRunner:
    """
    Открывает порт для Render Web Service.
    Render передаёт нужный порт через переменную PORT.
    """
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    logging.info("HTTP-сервер запущен на 0.0.0.0:%s", PORT)
    return runner


async def main() -> None:
    global SUPPORT_CHAT_NUMERIC_ID

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
        # Сначала открываем порт, чтобы Render сразу увидел работающий сервис.
        http_runner = await start_http_server()

        support_chat = await bot.get_chat(SUPPORT_CHAT_ID)
        SUPPORT_CHAT_NUMERIC_ID = support_chat.id

        logging.info(
            "Группа поддержки найдена: %s (%s)",
            support_chat.title or support_chat.username,
            SUPPORT_CHAT_NUMERIC_ID,
        )

        # Бот работает через long polling, webhook ему не нужен.
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
