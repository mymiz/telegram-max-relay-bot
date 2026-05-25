"""
Telegram-бот: владелец регистрирует номер MAX, админ запрашивает код входа,
владелец добровольно отправляет код — бот пересылает админу.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

from storage import CodeRequest, Store, normalize_phone

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("telegram-max-relay")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip() or None


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))
OWNER_WHITELIST = _parse_ids(os.getenv("OWNER_USER_IDS", ""))

store = Store()
router = Router()


class OwnerRegister(StatesGroup):
    waiting_phone = State()


class OwnerSendCode(StatesGroup):
    waiting_code = State()


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def can_be_owner(user_id: int | None) -> bool:
    if user_id is None:
        return False
    if not OWNER_WHITELIST:
        return True
    return user_id in OWNER_WHITELIST


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def mask_phone(phone: str) -> str:
    if len(phone) < 8:
        return phone
    return phone[:4] + " *** " + phone[-2:]


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    role = []
    if is_admin(uid):
        role.append("админ")
    if uid in store.owners:
        role.append("владелец")
    roles = ", ".join(role) if role else "пользователь"
    await message.answer(
        f"Ваш Telegram ID: `{uid}`\nРоль: {roles}",
        parse_mode="Markdown",
    )


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else 0

    if is_admin(uid):
        await message.answer(
            "Вы — **администратор**.\n\n"
            "Команды:\n"
            "/owners — список владельцев и номеров\n"
            "/request +79991234567 — запросить код входа в MAX на номер\n"
            "/cancel — отменить свой активный запрос\n"
            "/myid — ваш ID\n\n"
            "Владелец должен сначала зарегистрировать номер: /register",
            parse_mode="Markdown",
        )
        return

    if can_be_owner(uid):
        await message.answer(
            "Вы — **владелец** (или можете им стать).\n\n"
            "/register — привязать номер телефона MAX\n"
            "/myid — ваш ID\n\n"
            "Когда админ запросит код, пришлите его сюда "
            "(цифры или команда /code 123456).",
            parse_mode="Markdown",
        )
        if uid in store.owners:
            o = store.owners[uid]
            await message.answer(f"Ваш номер уже в системе: {mask_phone(o.phone)}")
        return

    await message.answer(
        "Бот для передачи кода входа в MAX между владельцем и администратором.\n"
        "У вас нет доступа. Обратитесь к администратору."
    )


@router.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not can_be_owner(uid):
        await message.answer("Регистрация владельцев отключена для вашего аккаунта.")
        return

    await state.set_state(OwnerRegister.waiting_phone)
    await message.answer(
        "Отправьте номер телефона, привязанный к аккаунту MAX:\n"
        "• кнопкой ниже, или\n"
        "• текстом: +79991234567",
        reply_markup=contact_keyboard(),
    )


@router.message(OwnerRegister.waiting_phone, F.contact)
async def register_contact(message: Message, state: FSMContext) -> None:
    contact = message.contact
    if not contact or not contact.phone_number:
        await message.answer("Не удалось прочитать номер. Попробуйте ещё раз.")
        return
    phone = normalize_phone(contact.phone_number)
    if not phone:
        await message.answer("Некорректный номер. Пример: +79991234567")
        return
    await _finish_register(message, state, phone)


@router.message(OwnerRegister.waiting_phone, F.text)
async def register_text(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer(
            "Некорректный номер. Пример: +79991234567",
            reply_markup=contact_keyboard(),
        )
        return
    await _finish_register(message, state, phone)


async def _finish_register(message: Message, state: FSMContext, phone: str) -> None:
    user = message.from_user
    if not user:
        return

    existing = store.owner_by_phone(phone)
    if existing and existing.user_id != user.id:
        await message.answer(
            "Этот номер уже зарегистрирован другим пользователем. "
            "Обратитесь к администратору.",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.clear()
        return

    store.register_owner(
        user.id,
        phone,
        name=user.full_name,
        username=user.username,
    )
    await state.clear()
    await message.answer(
        f"Номер {mask_phone(phone)} сохранён.\n"
        "Когда админ запросит код входа в MAX, вы получите уведомление.",
        reply_markup=ReplyKeyboardRemove(),
    )

    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(
                admin_id,
                f"Владелец зарегистрирован: {user.full_name or user.id}\n"
                f"Номер: {phone}\n"
                f"ID: `{user.id}`",
                parse_mode="Markdown",
            )
        except Exception:
            log.warning("Не удалось уведомить админа %s", admin_id)


@router.message(Command("owners"))
async def cmd_owners(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Команда только для администратора.")
        return

    if not store.owners:
        await message.answer("Владельцев пока нет. Попросите владельца выполнить /register")
        return

    lines = ["Зарегистрированные владельцы:\n"]
    for o in store.owners.values():
        pending = " ⏳ ждёт код" if store.get_pending(o.user_id) else ""
        uname = f"@{o.username}" if o.username else ""
        lines.append(
            f"• {o.name or '—'} {uname}\n"
            f"  Номер: `{o.phone}`\n"
            f"  ID: `{o.user_id}`{pending}\n"
            f"  Запрос: /request {o.phone}"
        )
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("request"))
async def cmd_request(message: Message, command: CommandObject, bot: Bot) -> None:
    admin_id = message.from_user.id if message.from_user else None
    if not is_admin(admin_id):
        await message.answer("Команда только для администратора.")
        return

    if not command.args:
        await message.answer(
            "Укажите номер:\n`/request +79991234567`",
            parse_mode="Markdown",
        )
        return

    phone = normalize_phone(command.args.strip())
    if not phone:
        await message.answer("Некорректный номер.")
        return

    owner = store.owner_by_phone(phone)
    if not owner:
        await message.answer(
            f"Владелец с номером {phone} не зарегистрирован.\n"
            "Попросите его написать боту /register"
        )
        return

    if store.get_pending(owner.user_id):
        await message.answer("По этому владельцу уже есть активный запрос кода.")
        return

    store.set_pending(
        CodeRequest(owner_id=owner.user_id, admin_id=admin_id or 0, phone=phone)
    )

    await message.answer(
        f"Запрос отправлен владельцу {owner.name or owner.user_id} "
        f"({mask_phone(phone)}).\n\n"
        f"Запустите вход в MAX на этот номер. "
        f"Когда владелец пришлёт код — он появится здесь."
    )

    try:
        await bot.send_message(
            owner.user_id,
            "🔐 **Запрос кода входа в MAX**\n\n"
            f"Администратор запрашивает код для номера `{phone}`.\n\n"
            "1. Если вы согласны — дождитесь SMS/кода от MAX\n"
            "2. Отправьте код сюда (только цифры) или: `/code 123456`\n"
            "3. Чтобы отказать: /decline",
            parse_mode="Markdown",
        )
        await bot.send_message(
            owner.user_id,
            "Ожидаю код…",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception:
        log.exception("Не удалось написать владельцу %s", owner.user_id)
        store.clear_pending(owner.user_id)
        await message.answer("Не удалось связаться с владельцем в Telegram.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else None

    if is_admin(uid):
        cancelled = 0
        for owner_id, req in list(store.pending.items()):
            if req.admin_id == uid:
                store.clear_pending(owner_id)
                cancelled += 1
                try:
                    await message.bot.send_message(
                        owner_id,
                        "Администратор отменил запрос кода.",
                    )
                except Exception:
                    pass
        await message.answer(
            f"Отменено запросов: {cancelled}" if cancelled else "Активных запросов нет."
        )
        return

    req = store.clear_pending(uid or 0)
    if req:
        await message.answer("Ваш запрос кода отменён.")
        try:
            await message.bot.send_message(req.admin_id, "Владелец отменил передачу кода.")
        except Exception:
            pass
    else:
        await message.answer("Нет активного запроса.")


@router.message(Command("decline"))
async def cmd_decline(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    req = store.clear_pending(uid)
    if not req:
        await message.answer("Нет активного запроса.")
        return
    await message.answer("Вы отказались передавать код.")
    try:
        await message.bot.send_message(
            req.admin_id,
            f"Владелец {uid} отказался передавать код для {mask_phone(req.phone)}.",
        )
    except Exception:
        pass


@router.message(Command("code"))
async def cmd_code(message: Message, command: CommandObject, bot: Bot) -> None:
    uid = message.from_user.id if message.from_user else 0
    req = store.get_pending(uid)
    if not req:
        await message.answer("Сейчас нет запроса кода. Ждите уведомление от админа.")
        return

    if not command.args:
        await message.answer("Пример: /code 123456")
        return

    code = re.sub(r"\D", "", command.args)
    if len(code) < 4:
        await message.answer("Код слишком короткий.")
        return

    await _deliver_code(message, bot, req, code)


@router.message(F.text)
async def on_text(message: Message, bot: Bot, state: FSMContext) -> None:
    if await state.get_state():
        return

    uid = message.from_user.id if message.from_user else 0
    req = store.get_pending(uid)
    if not req or not message.text:
        if is_admin(uid):
            await message.answer("Команды: /owners, /request +7..., /cancel")
        elif uid in store.owners:
            await message.answer("Жду запрос от админа или используйте /register")
        return

    code = re.sub(r"\D", "", message.text.strip())
    if len(code) < 4:
        await message.answer(
            "Отправьте код цифрами (4+ символов) или /code 123456\n"
            "Отказ: /decline"
        )
        return

    await _deliver_code(message, bot, req, code)


async def _deliver_code(
    message: Message, bot: Bot, req: CodeRequest, code: str
) -> None:
    owner = store.owners.get(req.owner_id)
    owner_name = owner.name if owner else str(req.owner_id)

    store.clear_pending(req.owner_id)

    await message.answer("Код передан администратору. Спасибо!")

    try:
        await bot.send_message(
            req.admin_id,
            "✅ **Код от владельца**\n\n"
            f"Владелец: {owner_name}\n"
            f"Номер: `{req.phone}`\n"
            f"Код: `{code}`\n\n"
            "_Используйте только с согласия владельца._",
            parse_mode="Markdown",
        )
    except Exception:
        log.exception("Не удалось отправить код админу %s", req.admin_id)
        await message.answer("Ошибка доставки админу. Напишите администратору вручную.")


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")
    if not ADMIN_IDS:
        raise SystemExit("Задайте ADMIN_USER_IDS в .env (ваш Telegram ID)")

    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else AiohttpSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    if TELEGRAM_PROXY:
        log.info("Используется прокси для Telegram API")
    log.info("Бот запущен. Админы: %s", ADMIN_IDS)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
