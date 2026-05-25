"""
Telegram-бот: владелец регистрирует номер MAX, админ запрашивает код входа,
владелец добровольно отправляет код — бот пересылает админу.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
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

LOGO_PATH = Path(__file__).resolve().parent / "logo.png"


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
CODE_TIMEOUT_SEC = 60
CODE_REWARD_RUB = float(os.getenv("CODE_REWARD_RUB", "50"))
PHONE_HINT = "Только номер России: +79991234567 (11 цифр, начинается с +7)"

_timer_task: asyncio.Task | None = None


class OwnerRegister(StatesGroup):
    waiting_phone = State()


class OwnerSendCode(StatesGroup):
    waiting_code = State()


class AdminSettings(StatesGroup):
    waiting_price = State()
    waiting_broadcast = State()


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
BTN_PROFILE = "👤 Профиль"
BTN_DECLINE = "🚫 Отказаться"
BTN_MENU = "🏠 Меню"

# Настройки (только для админа)
BTN_SETTINGS = "⚙️ Настройки"
BTN_SET_PRICE = "💰 Прайс"
BTN_SET_WORK = "🔄 Ворк"
BTN_SET_MSG = "📢 Сообщение"
BTN_BACK = "◀️ Назад"

MENU_BUTTONS = {
    BTN_OWNERS,
    BTN_REQUEST,
    BTN_CANCEL,
    BTN_REGISTER,
    BTN_PROFILE,
    BTN_DECLINE,
    BTN_MENU,
    BTN_SETTINGS,
    BTN_SET_PRICE,
    BTN_SET_WORK,
    BTN_SET_MSG,
    BTN_BACK,
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
        if store.active or store.queue:
            rows.append([KeyboardButton(text=BTN_CANCEL)])
        rows.append([KeyboardButton(text=BTN_SETTINGS)])

    if can_be_owner(user_id) and not is_admin(user_id):
        rows.append(
            [
                KeyboardButton(text=BTN_REGISTER),
                KeyboardButton(text=BTN_PROFILE),
            ]
        )
        if store.get_pending(user_id):
            rows.append([KeyboardButton(text=BTN_DECLINE)])

    rows.append([KeyboardButton(text=BTN_MENU)])

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def settings_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SET_PRICE), KeyboardButton(text=BTN_SET_WORK)],
            [KeyboardButton(text=BTN_SET_MSG)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def work_inline() -> InlineKeyboardMarkup:
    on_mark = "✅ " if store.bot_status.lower() in ("включён", "включен", "on", "вкл") else ""
    off_mark = "✅ " if not on_mark else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{on_mark}Включен", callback_data="work:on"),
                InlineKeyboardButton(text=f"{off_mark}Выключен", callback_data="work:off"),
            ]
        ]
    )


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
        pending = ""
        if store.active and store.active.owner_id == o.user_id:
            pending = f" ⏳ код ({store.seconds_left()} сек)"
        elif any(r.owner_id == o.user_id for r in store.queue):
            pending = " 📥 в очереди"
        uname = f" @{o.username}" if o.username else ""
        prof = store.get_profile(o.user_id)
        lines.append(
            f"\n**{i}. {o.name or 'Без имени'}**{uname}{pending}\n"
            f"ID: `{o.user_id}` · номеров: **{len(o.phones)}** · "
            f"баланс: **{prof.balance:.0f}$**"
        )
        for phone in o.phones:
            lines.append(f"  • `{phone}`")
    if store.active:
        left = store.seconds_left()
        lines.append(
            f"\n⏳ **Сейчас в работе:** `{store.active.phone}` — осталось **{left} сек**"
        )
    if store.queue:
        lines.append(f"\n📥 **Очередь** ({store.queue_size()} номеров):")
        for i, req in enumerate(store.queue[:15], 1):
            lines.append(f"  {i}. `{req.phone}`")
        if store.queue_size() > 15:
            lines.append(f"  … и ещё {store.queue_size() - 15}")
    lines.append("\n🔐 Кнопки ниже — запросить код (по одному, 1 мин на ответ).")
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


_MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _esc(text: str) -> str:
    return _MDV2_SPECIAL.sub(r"\\\1", text)


def format_queue_status() -> str:
    total = store.total_phones()
    if total == 0:
        return "пусто"
    return str(total)


def welcome_text() -> str:
    status_icon = "✅" if store.bot_status.lower() in ("включён", "включен", "on", "вкл") else "🔴"
    queue_str = _esc(format_queue_status())
    return (
        "Добро пожаловать в бота TrustMax\\_bot\\!\n\n"
        f"┌ Статус работы: {status_icon} {_esc(store.bot_status)}\n"
        f"├ Актуальный прайс: {_esc(store.price)}\n"
        f"└ Актуальная очередь: {queue_str}\n\n"
        "👇 Выберите раздел для продолжения:"
    )


def format_profile_text(user_id: int, *, for_admin: bool = False) -> str:
    profile = store.get_profile(user_id)
    owner = store.owners.get(user_id)
    phones = owner.phones if owner else []

    name = (owner.name if owner else None) or profile.name or "—"
    username = (owner.username if owner else None) or profile.username
    uname = f"@{username}" if username else "—"

    in_queue = sum(1 for r in store.queue if r.owner_id == user_id)
    in_active = 1 if store.active and store.active.owner_id == user_id else 0

    lines = [
        "👤 *ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ*\n",
        f"┌ Имя: {name}",
        f"├ Юзернейм: {uname}",
        f"└ ID: `{user_id}`",
        "",
        "💰 *ФИНАНСОВАЯ СТАТИСТИКА*\n",
        f"┌ Баланс: *{profile.balance:.2f}$*",
        f"├ Заработано: *{profile.total_earned:.2f}$*",
        f"└ Выведено: *{profile.withdrawn:.2f}$*",
        "",
        "📱 *СДАННЫЕ АККАУНТЫ*\n",
        f"   MAX аккаунтов: *{len(phones)}*",
        "",
        "⏳ *ТЕКУЩИЕ ЗАЯВКИ*\n",
        f"┌ В очереди: *{in_queue}*",
        f"├ В обработке: *{in_active}*",
        f"└ Ожидают код: *{in_active}*",
    ]
    return "\n".join(lines)


async def _send_welcome(message: Message, uid: int) -> None:
    kb = main_menu_keyboard(uid)
    if LOGO_PATH.exists():
        await message.answer_photo(
            photo=FSInputFile(LOGO_PATH),
            caption=welcome_text(),
            parse_mode="MarkdownV2",
            reply_markup=kb,
        )
    else:
        await message.answer(welcome_text(), parse_mode="MarkdownV2", reply_markup=kb)


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    uid = user.id if user else 0
    if uid and user:
        store.track_user(uid)
        store.touch_profile(uid, name=user.full_name, username=user.username)
        if uid in store.owners:
            owner = store.owners[uid]
            owner.name = user.full_name
            owner.username = user.username
            store.save()

    if is_admin(uid):
        await _send_welcome(message, uid)
        return

    if can_be_owner(uid):
        await _send_welcome(message, uid)
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


@router.message(Command("profile"))
async def cmd_profile(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id if message.from_user else 0
    if is_admin(uid):
        if command.args and command.args.strip().isdigit():
            target = int(command.args.strip())
            await message.answer(
                format_profile_text(target, for_admin=True),
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(uid),
            )
            return
        await message.answer(
            "Профиль владельца: `/profile 123456789`\n"
            "Пополнить баланс: `/addbal 123456789 100`",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(uid),
        )
        return
    await message.answer(
        format_profile_text(uid),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(uid),
    )


@router.message(Command("addbal"))
async def cmd_addbal(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Только для администратора.")
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer(
            "Формат: `/addbal user_id сумма`\nПример: `/addbal 123456789 100`",
            parse_mode="Markdown",
        )
        return
    target = int(parts[0])
    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer("Некорректная сумма.")
        return
    new_balance = store.add_balance(target, amount)
    await message.answer(
        f"Баланс пользователя `{target}`: **{new_balance:.2f}$**",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@router.message(Command("setstatus"))
async def cmd_setstatus(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Только для администратора.")
        return
    value = (command.args or "").strip()
    if not value:
        await message.answer(
            "Формат: `/setstatus включён` или `/setstatus выключен`",
            parse_mode="Markdown",
        )
        return
    store.bot_status = value
    store.save()
    await message.answer(
        welcome_text(),
        parse_mode="MarkdownV2",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@router.message(Command("setprice"))
async def cmd_setprice(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Только для администратора.")
        return
    value = (command.args or "").strip()
    if not value:
        await message.answer(
            "Формат: `/setprice 50 ₽ за код`",
            parse_mode="Markdown",
        )
        return
    store.price = value
    store.save()
    await message.answer(
        welcome_text(),
        parse_mode="MarkdownV2",
        reply_markup=main_menu_keyboard(message.from_user.id),
    )


@router.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else 0
    if is_admin(uid):
        await message.answer(
            "Администратору эта кнопка недоступна.",
            reply_markup=main_menu_keyboard(uid),
        )
        return
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


async def _cancel_timer() -> None:
    global _timer_task
    if _timer_task and not _timer_task.done():
        _timer_task.cancel()
        try:
            await _timer_task
        except asyncio.CancelledError:
            pass
    _timer_task = None


async def _start_code_timer(bot: Bot, req: CodeRequest) -> None:
    global _timer_task

    async def _on_timeout() -> None:
        await asyncio.sleep(CODE_TIMEOUT_SEC)
        active = store.active
        if not active or active.phone != req.phone:
            return
        await _finish_active(bot, reason="timeout")

    await _cancel_timer()
    _timer_task = asyncio.create_task(_on_timeout())


async def _notify_owner_request(bot: Bot, req: CodeRequest) -> bool:
    try:
        await bot.send_message(
            req.owner_id,
            "🔐 **Запрос кода входа в MAX**\n\n"
            f"Номер: `{req.phone}`\n"
            f"⏱ У вас **{CODE_TIMEOUT_SEC} сек** (1 минута), чтобы прислать код.\n\n"
            f"Отправьте ровно **{CODE_LEN} цифр** в этот чат.\n"
            "Отказ: 🚫 Отказаться",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(req.owner_id),
        )
        return True
    except Exception:
        log.exception("Не удалось написать владельцу %s", req.owner_id)
        return False


async def _activate_request(bot: Bot, req: CodeRequest) -> bool:
    if not await _notify_owner_request(bot, req):
        return False
    store.set_active(req, timeout_sec=CODE_TIMEOUT_SEC)
    await _start_code_timer(bot, req)
    try:
        q = store.queue_size()
        extra = f"\nВ очереди после этого: **{q}**" if q else ""
        await bot.send_message(
            req.admin_id,
            f"⏳ Ожидание кода для `{req.phone}`\n"
            f"Владельцу дано **{CODE_TIMEOUT_SEC} сек**.{extra}\n"
            "Запустите вход в MAX на этот номер.",
            parse_mode="Markdown",
        )
    except Exception:
        log.warning("Не удалось уведомить админа %s", req.admin_id)
    return True


async def _process_next_in_queue(bot: Bot, admin_id: int) -> None:
    while True:
        nxt = store.pop_next()
        if not nxt:
            try:
                await bot.send_message(
                    admin_id,
                    "✅ Очередь обработана\\. Все номера пройдены\\.",
                    parse_mode="MarkdownV2",
                    reply_markup=main_menu_keyboard(admin_id),
                )
            except Exception:
                pass
            return
        if await _activate_request(bot, nxt):
            return
        try:
            await bot.send_message(
                admin_id,
                f"⚠️ Пропуск {mask_phone(nxt.phone)} — владелец недоступен.",
            )
        except Exception:
            pass


async def _queue_all_phones(bot: Bot, admin_id: int, reply_to: Message) -> None:
    """Добавить все зарегистрированные номера в очередь и запустить обработку."""
    if not store.owners:
        await reply_to.answer(
            "Владельцев нет. Попросите нажать «Сдать номер📱».",
            reply_markup=main_menu_keyboard(admin_id),
        )
        return

    added = 0
    already = 0
    for owner in store.owners.values():
        for phone in owner.phones:
            status = store.phone_status(phone)
            if status in ("active", "queued"):
                already += 1
                continue
            req = CodeRequest(owner_id=owner.user_id, admin_id=admin_id, phone=phone)
            if store.active is None and added == 0:
                if await _activate_request(bot, req):
                    added += 1
                else:
                    store.push_queue(req)
                    added += 1
            else:
                store.push_queue(req)
                added += 1

    if added == 0:
        await reply_to.answer(
            "Все номера уже в очереди или обрабатываются.",
            reply_markup=main_menu_keyboard(admin_id),
        )
        return

    total = store.queue_size()
    skip_note = f" · {already} уже в очереди" if already else ""
    await reply_to.answer(
        f"📥 Запущена очередь: *{added}* номеров{skip_note}\n"
        f"Осталось в очереди: *{total}*\n\n"
        "Каждому владельцу даётся 1 минута на код.\n"
        "Не ответил — автоматически следующий.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(admin_id),
    )


async def _finish_active(
    bot: Bot,
    *,
    reason: str,
    code: str | None = None,
    message: Message | None = None,
) -> None:
    await _cancel_timer()
    req = store.clear_active()
    if not req:
        return

    owner = store.owners.get(req.owner_id)
    owner_name = owner.name if owner else str(req.owner_id)
    admin_id = req.admin_id

    if reason == "code" and code:
        profile = store.record_code_success(req.owner_id, CODE_REWARD_RUB)
        reward_note = ""
        if CODE_REWARD_RUB > 0:
            reward_note = (
                f"\n💰 +{CODE_REWARD_RUB:.0f}$ · баланс **{profile.balance:.2f}$**"
            )
        if message:
            await message.answer(
                f"Код передан администратору. Спасибо!{reward_note}",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(req.owner_id),
            )
        try:
            await bot.send_message(
                admin_id,
                "✅ **Код от владельца**\n\n"
                f"Владелец: {owner_name}\n"
                f"Номер: `{req.phone}`\n"
                f"Код: `{code}`",
                parse_mode="Markdown",
            )
        except Exception:
            log.exception("Не удалось отправить код админу %s", admin_id)
    elif reason == "timeout":
        store.record_code_fail(req.owner_id)
        try:
            await bot.send_message(
                req.owner_id,
                f"⏱ Время вышло ({CODE_TIMEOUT_SEC} сек). Код не получен.",
                reply_markup=main_menu_keyboard(req.owner_id),
            )
        except Exception:
            pass
        try:
            await bot.send_message(
                admin_id,
                f"⏱ **Время вышло** для `{req.phone}` — код не прислан за 1 минуту.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    elif reason == "decline":
        store.record_code_fail(req.owner_id)
        if message:
            await message.answer(
                "Вы отказались передавать код.",
                reply_markup=main_menu_keyboard(req.owner_id),
            )
        try:
            await bot.send_message(
                admin_id,
                f"🚫 Владелец отказался передавать код для `{req.phone}`.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    elif reason == "cancel":
        try:
            await bot.send_message(
                req.owner_id,
                "Запрос кода отменён администратором.",
                reply_markup=main_menu_keyboard(req.owner_id),
            )
        except Exception:
            pass

    if store.queue:
        try:
            await bot.send_message(
                admin_id,
                f"▶️ Следующий в очереди ({store.queue_size()} осталось)…",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    await _process_next_in_queue(bot, admin_id)


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

    status = store.phone_status(phone)
    if status == "active":
        text = (
            f"Номер {mask_phone(phone)} уже обрабатывается "
            f"({store.seconds_left()} сек до конца)."
        )
        if reply_to:
            await reply_to.answer(text, reply_markup=main_menu_keyboard(admin_id))
        return False
    if status == "queued":
        pos = store.queue_position(phone)
        text = f"Номер {mask_phone(phone)} уже в очереди (позиция {pos})."
        if reply_to:
            await reply_to.answer(text, reply_markup=main_menu_keyboard(admin_id))
        return False

    req = CodeRequest(owner_id=owner.user_id, admin_id=admin_id, phone=phone)

    if store.active is None:
        if not await _activate_request(bot, req):
            if reply_to:
                await reply_to.answer(
                    "Не удалось связаться с владельцем.",
                    reply_markup=main_menu_keyboard(admin_id),
                )
            return False
        text = (
            f"Запрос для {mask_phone(phone)} активен.\n"
            f"У владельца **{CODE_TIMEOUT_SEC} сек** на ответ."
        )
        if reply_to:
            await reply_to.answer(text, reply_markup=main_menu_keyboard(admin_id))
        return True

    pos = store.push_queue(req)
    text = (
        f"📥 {mask_phone(phone)} добавлен в **очередь**.\n"
        f"Позиция: **{pos}** (обработка по одному номеру, 1 мин на код)."
    )
    if reply_to:
        await reply_to.answer(text, reply_markup=main_menu_keyboard(admin_id))
    else:
        try:
            await bot.send_message(admin_id, text, parse_mode="Markdown")
        except Exception:
            pass
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
        await _queue_all_phones(bot, admin_id or 0, message)
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
        await _cancel_timer()
        cancelled, was_active = store.cancel_all_for_admin(uid or 0)
        if was_active:
            try:
                await message.bot.send_message(
                    was_active.owner_id,
                    "Администратор отменил запрос кода.",
                    reply_markup=main_menu_keyboard(was_active.owner_id),
                )
            except Exception:
                pass
        await message.answer(
            f"Отменено: {cancelled} (очередь и активный запрос)."
            if cancelled
            else "Очередь пуста.",
            reply_markup=main_menu_keyboard(uid or 0),
        )
        if store.active is None and store.queue:
            await _process_next_in_queue(message.bot, uid or 0)
        return

    req = store.get_pending(uid or 0)
    if not req:
        await message.answer(
            "Нет активного запроса.",
            reply_markup=main_menu_keyboard(uid or 0),
        )
        return
    await _finish_active(message.bot, reason="decline", message=message)


@router.message(Command("decline"))
async def cmd_decline(message: Message, bot: Bot) -> None:
    uid = message.from_user.id if message.from_user else 0
    req = store.get_pending(uid)
    if not req:
        await message.answer(
            "Сейчас нет активного запроса (или ваша очередь ещё не подошла).",
            reply_markup=main_menu_keyboard(uid),
        )
        return
    await _finish_active(bot, reason="decline", message=message)


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

    await _finish_active(bot, reason="code", code=code, message=message)


async def _show_settings(message: Message) -> None:
    status_icon = "✅" if store.bot_status.lower() in ("включён", "включен", "on", "вкл") else "🔴"
    await message.answer(
        f"⚙️ *Настройки*\n\n"
        f"Статус: {status_icon} {store.bot_status}\n"
        f"Прайс: {store.price}",
        parse_mode="Markdown",
        reply_markup=settings_keyboard(),
    )


# ─── FSM: ввод нового прайса ───────────────────────────────────────────────

@router.message(AdminSettings.waiting_price, F.text, ~F.text.in_(MENU_BUTTONS))
async def settings_price_input(message: Message, state: FSMContext) -> None:
    text = message.text or ""
    store.price = text
    store.save()
    await state.clear()
    await message.answer(f"Прайс обновлён: *{text}*", parse_mode="Markdown")
    await _show_settings(message)


# ─── FSM: рассылка сообщения всем участникам ───────────────────────────────

@router.message(AdminSettings.waiting_broadcast, F.text, ~F.text.in_(MENU_BUTTONS))
async def settings_broadcast_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = message.text or ""

    sender_id = message.from_user.id if message.from_user else 0
    recipients = store.all_users - {sender_id}
    ok = 0
    fail = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1

    await state.clear()
    await message.answer(
        f"Рассылка завершена: {ok} доставлено, {fail} ошибок.",
        reply_markup=settings_keyboard(),
    )


# ─── Callback: переключение Ворк ───────────────────────────────────────────

@router.callback_query(F.data.startswith("work:"))
async def cb_work_toggle(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    value = (callback.data or "").removeprefix("work:")
    store.bot_status = "включён" if value == "on" else "выключен"
    store.save()
    await callback.answer("Обновлено")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=work_inline())
    status_icon = "✅" if value == "on" else "🔴"
    if callback.message:
        await callback.message.answer(
            f"Статус изменён: {status_icon} {store.bot_status}",
            reply_markup=settings_keyboard(),
        )


@router.message(F.text.in_(MENU_BUTTONS))
async def on_menu_button(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else 0
    text = message.text or ""

    if text in (BTN_MENU, BTN_BACK):
        await cmd_start(message, state)
        return
    if text == BTN_SETTINGS and is_admin(uid):
        await _show_settings(message)
        return
    if text == BTN_SET_PRICE and is_admin(uid):
        await state.set_state(AdminSettings.waiting_price)
        await message.answer(
            "Введите новый текст прайса:",
            reply_markup=settings_keyboard(),
        )
        return
    if text == BTN_SET_WORK and is_admin(uid):
        await message.answer(
            "Выберите статус работы:",
            reply_markup=work_inline(),
        )
        return
    if text == BTN_SET_MSG and is_admin(uid):
        await state.set_state(AdminSettings.waiting_broadcast)
        await message.answer(
            "Введите сообщение для рассылки всем участникам:",
            reply_markup=settings_keyboard(),
        )
        return
    if text == BTN_OWNERS and is_admin(uid):
        await cmd_owners(message)
        return
    if text == BTN_REQUEST and is_admin(uid):
        await _queue_all_phones(bot, uid, message)
        return
    if text == BTN_CANCEL:
        await cmd_cancel(message, state)
        return
    if text == BTN_REGISTER and can_be_owner(uid) and not is_admin(uid):
        await cmd_register(message, state)
        return
    if text == BTN_PROFILE and not is_admin(uid):
        await message.answer(
            format_profile_text(uid),
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard(uid),
        )
        return
    if text == BTN_DECLINE:
        await cmd_decline(message, bot)
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

    await _finish_active(bot, reason="code", code=code, message=message)


async def _recover_queue_on_startup(bot: Bot) -> None:
    if not store.active:
        if store.queue:
            admin_id = store.queue[0].admin_id
            await _process_next_in_queue(bot, admin_id)
        return
    left = store.seconds_left()
    if left <= 0:
        req = store.clear_active()
        if req:
            try:
                await bot.send_message(
                    req.admin_id,
                    f"⏱ После перезапуска: время для `{req.phone}` истекло.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        if store.queue:
            await _process_next_in_queue(bot, store.queue[0].admin_id)
        return
    await _start_code_timer(bot, store.active)
    log.info("Восстановлен активный запрос %s, осталось %s сек", store.active.phone, left)


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
    await _recover_queue_on_startup(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
