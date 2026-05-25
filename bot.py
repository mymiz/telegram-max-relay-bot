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
)
from dotenv import load_dotenv

from storage import CodeRequest, Store, normalize_phone

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-max-relay")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "").strip() or None

_LOGO = Path(__file__).resolve().parent / "logo.png"
LOGO_FILE = FSInputFile(_LOGO) if _LOGO.exists() else None


def _parse_ids(raw: str) -> set[int]:
    return {int(p) for p in raw.replace(";", ",").split(",") if p.strip().isdigit()}


ADMIN_IDS = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))
OWNER_WHITELIST = _parse_ids(os.getenv("OWNER_USER_IDS", ""))

store = Store()
router = Router()

CODE_LEN = 6
CODE_TIMEOUT_SEC = 60
CODE_REWARD = float(os.getenv("CODE_REWARD_RUB", "50"))
PHONE_HINT = "Только номер России: +79991234567 (11 цифр, начинается с +7)"

_timer_task: asyncio.Task | None = None


class OwnerRegister(StatesGroup):
    waiting_phone = State()


class AdminSettings(StatesGroup):
    waiting_price = State()
    waiting_broadcast = State()


def is_admin(uid: int | None) -> bool:
    return uid is not None and uid in ADMIN_IDS


def can_be_owner(uid: int | None) -> bool:
    if uid is None:
        return False
    return not OWNER_WHITELIST or uid in OWNER_WHITELIST


# ─── Кнопки ────────────────────────────────────────────────────────────────

BTN_OWNERS   = "📋 Владельцы"
BTN_REQUEST  = "🔐 Запросить код"
BTN_CANCEL   = "❌ Отменить"
BTN_REGISTER = "Сдать номер📱"
BTN_PROFILE  = "👤 Профиль"
BTN_DECLINE  = "🚫 Отказаться"
BTN_MENU     = "🏠 Меню"
BTN_SETTINGS = "⚙️ Настройки"
BTN_SET_PRICE = "💰 Прайс"
BTN_SET_WORK  = "🔄 Ворк"
BTN_SET_MSG   = "📢 Сообщение"
BTN_BACK      = "◀️ Назад"

MENU_BUTTONS = {
    BTN_OWNERS, BTN_REQUEST, BTN_CANCEL, BTN_REGISTER, BTN_PROFILE,
    BTN_DECLINE, BTN_MENU, BTN_SETTINGS, BTN_SET_PRICE, BTN_SET_WORK,
    BTN_SET_MSG, BTN_BACK,
}

# ─── Клавиатуры ────────────────────────────────────────────────────────────

def main_menu_keyboard(uid: int) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    if is_admin(uid):
        rows.append([KeyboardButton(text=BTN_OWNERS), KeyboardButton(text=BTN_REQUEST)])
        if store.active or store.queue:
            rows.append([KeyboardButton(text=BTN_CANCEL)])
        rows.append([KeyboardButton(text=BTN_SETTINGS)])
    elif can_be_owner(uid):
        rows.append([KeyboardButton(text=BTN_REGISTER), KeyboardButton(text=BTN_PROFILE)])
        if store.get_pending(uid):
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


def contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить номер", request_contact=True)],
            [KeyboardButton(text=BTN_MENU)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def work_inline() -> InlineKeyboardMarkup:
    on  = "✅ " if store.bot_status.lower() in ("включён", "включен", "on", "вкл") else ""
    off = "" if on else "✅ "
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{on}Включен",   callback_data="work:on"),
        InlineKeyboardButton(text=f"{off}Выключен", callback_data="work:off"),
    ]])

# ─── Форматирование ─────────────────────────────────────────────────────────

_MDV2 = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _esc(t: str) -> str:
    return _MDV2.sub(r"\\\1", t)


def _md(t: str) -> str:
    for ch in ("_", "*", "`", "["):
        t = t.replace(ch, f"\\{ch}")
    return t


def mask_phone(phone: str) -> str:
    return phone[:4] + " *** " + phone[-2:] if len(phone) >= 8 else phone


def parse_code(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw.strip())
    return digits if len(digits) == CODE_LEN else None


def format_queue_status() -> str:
    total = store.total_phones()
    return str(total) if total else "пусто"


def welcome_text() -> str:
    icon = "✅" if store.bot_status.lower() in ("включён", "включен", "on", "вкл") else "🔴"
    return (
        "Добро пожаловать в бота TrustMax\\_bot\\!\n\n"
        f"┌ Статус работы: {icon} {_esc(store.bot_status)}\n"
        f"├ Актуальный прайс: {_esc(store.price)}\n"
        f"└ Актуальная очередь: {_esc(format_queue_status())}\n\n"
        "👇 Выберите раздел для продолжения:"
    )


def format_owners_list() -> str:
    lines = [f"📋 **Владельцы**\nЛюдей: **{len(store.owners)}** · Номеров: **{store.total_phones()}**\n"]
    for i, o in enumerate(store.owners.values(), 1):
        status = ""
        if store.active and store.active.owner_id == o.user_id:
            status = f" ⏳ код ({store.seconds_left()} сек)"
        elif any(r.owner_id == o.user_id for r in store.queue):
            status = " 📥 в очереди"
        uname = f" @{o.username}" if o.username else ""
        prof = store.get_profile(o.user_id)
        lines.append(
            f"\n**{i}. {o.name or 'Без имени'}**{uname}{status}\n"
            f"ID: `{o.user_id}` · номеров: **{len(o.phones)}** · баланс: **{prof.balance:.0f}$**"
        )
        for phone in o.phones:
            lines.append(f"  • `{phone}`")
    if store.active:
        lines.append(f"\n⏳ **В работе:** `{store.active.phone}` — {store.seconds_left()} сек")
    if store.queue:
        lines.append(f"\n📥 **Очередь** ({store.queue_size()}):")
        for i, req in enumerate(store.queue[:15], 1):
            lines.append(f"  {i}. `{req.phone}`")
        if store.queue_size() > 15:
            lines.append(f"  … ещё {store.queue_size() - 15}")
    return "\n".join(lines)


def owners_request_inline() -> InlineKeyboardMarkup | None:
    if not store.owners:
        return None
    buttons = [
        [InlineKeyboardButton(
            text=f"🔐 {mask_phone(phone)} — {(o.name or str(o.user_id))[:20]}",
            callback_data=f"req:{phone}",
        )]
        for o in store.owners.values()
        for phone in o.phones
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_profile_text(user_id: int) -> str:
    profile = store.get_profile(user_id)
    owner = store.owners.get(user_id)
    phones = owner.phones if owner else []
    raw_name = (owner.name if owner else None) or profile.name or "—"
    raw_uname = (owner.username if owner else None) or profile.username
    in_queue  = sum(1 for r in store.queue if r.owner_id == user_id)
    in_active = 1 if store.active and store.active.owner_id == user_id else 0
    return "\n".join([
        "👤 *ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ*\n",
        f"┌ Имя: {_md(raw_name)}",
        f"├ Юзернейм: @{_md(raw_uname)}" if raw_uname else "├ Юзернейм: —",
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
    ])

# ─── Отправка меню ──────────────────────────────────────────────────────────

_SUPPORT_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🛠 Тех. поддержка", url="https://t.me/Don1_Tomas1"),
]])


async def _send_welcome(message: Message, uid: int, *, first_time: bool = False) -> None:
    kb = main_menu_keyboard(uid)
    if first_time and LOGO_FILE:
        await message.answer_photo(photo=LOGO_FILE, caption=welcome_text(),
                                   parse_mode="MarkdownV2", reply_markup=_SUPPORT_KB)
    else:
        await message.answer(welcome_text(), parse_mode="MarkdownV2", reply_markup=_SUPPORT_KB)
    await message.answer("👇 Выберите раздел:", reply_markup=kb)

# ─── Команды ────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    uid = user.id if user else 0
    first_time = uid not in store.all_users
    if uid and user:
        store.track_user(uid)
        store.touch_profile(uid, name=user.full_name, username=user.username)
        if uid in store.owners:
            o = store.owners[uid]
            o.name, o.username = user.full_name, user.username
            store.save()

    if is_admin(uid) or can_be_owner(uid):
        await _send_welcome(message, uid, first_time=first_time)
        if can_be_owner(uid) and not is_admin(uid) and uid in store.owners:
            o = store.owners[uid]
            phones = "\n".join(f"  • {mask_phone(p)}" for p in o.phones)
            await message.answer(f"Ваши номера ({len(o.phones)}):\n{phones}",
                                 reply_markup=main_menu_keyboard(uid))
        return
    await message.answer(
        "Бот для передачи кода входа в MAX.\n"
        "У вас нет доступа. Обратитесь к администратору."
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id if message.from_user else 0
    if is_admin(uid):
        if command.args and command.args.strip().isdigit():
            await message.answer(format_profile_text(int(command.args.strip())),
                                 parse_mode="Markdown", reply_markup=main_menu_keyboard(uid))
            return
        await message.answer(
            "Профиль: `/profile 123456789`\nБаланс: `/addbal 123456789 100`",
            parse_mode="Markdown", reply_markup=main_menu_keyboard(uid))
        return
    await message.answer(format_profile_text(uid), parse_mode="Markdown",
                         reply_markup=main_menu_keyboard(uid))


@router.message(Command("addbal"))
async def cmd_addbal(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Только для администратора.")
        return
    parts = (command.args or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Формат: `/addbal user_id сумма`", parse_mode="Markdown")
        return
    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        await message.answer("Некорректная сумма.")
        return
    bal = store.add_balance(int(parts[0]), amount)
    await message.answer(f"Баланс `{parts[0]}`: **{bal:.2f}$**",
                         parse_mode="Markdown", reply_markup=main_menu_keyboard(message.from_user.id))


@router.message(Command("setstatus"))
async def cmd_setstatus(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Только для администратора.")
        return
    value = (command.args or "").strip()
    if not value:
        await message.answer("Формат: `/setstatus включён`", parse_mode="Markdown")
        return
    store.bot_status = value
    store.save()
    await message.answer(welcome_text(), parse_mode="MarkdownV2",
                         reply_markup=main_menu_keyboard(message.from_user.id))


@router.message(Command("setprice"))
async def cmd_setprice(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Только для администратора.")
        return
    value = (command.args or "").strip()
    if not value:
        await message.answer("Формат: `/setprice текст`", parse_mode="Markdown")
        return
    store.price = value
    store.save()
    await message.answer(welcome_text(), parse_mode="MarkdownV2",
                         reply_markup=main_menu_keyboard(message.from_user.id))


@router.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else 0
    if is_admin(uid):
        await message.answer("Администратору недоступно.", reply_markup=main_menu_keyboard(uid))
        return
    if not can_be_owner(uid):
        await message.answer("Регистрация отключена для вашего аккаунта.")
        return
    await state.set_state(OwnerRegister.waiting_phone)
    await message.answer(
        f"Отправьте номер MAX:\n• кнопкой ниже, или\n• текстом: {PHONE_HINT}",
        reply_markup=contact_keyboard(),
    )


@router.message(OwnerRegister.waiting_phone, F.contact)
async def register_contact(message: Message, state: FSMContext) -> None:
    contact = message.contact
    if not contact or not contact.phone_number:
        await message.answer("Не удалось прочитать номер.")
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
        await message.answer(PHONE_HINT, reply_markup=contact_keyboard())
        return
    await _finish_register(message, state, phone)


async def _finish_register(message: Message, state: FSMContext, phone: str) -> None:
    user = message.from_user
    if not user:
        return
    existing = store.owner_by_phone(phone)
    if existing and existing.user_id != user.id:
        await message.answer("Этот номер уже зарегистрирован. Обратитесь к администратору.",
                             reply_markup=main_menu_keyboard(user.id))
        await state.clear()
        return
    owner, already = store.register_owner(user.id, phone, name=user.full_name, username=user.username)
    await state.clear()
    if already:
        await message.answer(f"Номер {mask_phone(phone)} уже в вашем списке ({len(owner.phones)} всего).",
                             reply_markup=main_menu_keyboard(user.id))
        return
    await message.answer(
        f"Номер {mask_phone(phone)} сохранён. Всего: {len(owner.phones)}.\n"
        "Когда нужен код — получите уведомление.",
        reply_markup=main_menu_keyboard(user.id),
    )
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(
                admin_id,
                f"Новый номер: {user.full_name or user.id} · `{phone}` · ID: `{user.id}`",
                parse_mode="Markdown",
            )
        except Exception:
            log.warning("Не удалось уведомить админа %s", admin_id)


@router.message(Command("owners"))
async def cmd_owners(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not is_admin(uid):
        await message.answer("Команда только для администратора.")
        return
    if not store.owners:
        await message.answer("Владельцев нет.", reply_markup=main_menu_keyboard(uid))
        return
    await message.answer(format_owners_list(), parse_mode="Markdown",
                         reply_markup=owners_request_inline())

# ─── Таймер и очередь ───────────────────────────────────────────────────────

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
        if store.active and store.active.phone == req.phone:
            await _finish_active(bot, reason="timeout")

    await _cancel_timer()
    _timer_task = asyncio.create_task(_on_timeout())


async def _notify_owner_request(bot: Bot, req: CodeRequest) -> bool:
    try:
        await bot.send_message(
            req.owner_id,
            f"🔐 **Запрос кода MAX**\n\nНомер: `{req.phone}`\n"
            f"⏱ У вас **{CODE_TIMEOUT_SEC} сек** чтобы прислать **{CODE_LEN} цифр**.\n"
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
    q = store.queue_size()
    try:
        await bot.send_message(
            req.admin_id,
            f"⏳ Ожидание кода `{req.phone}`\n{CODE_TIMEOUT_SEC} сек."
            + (f" В очереди: **{q}**" if q else ""),
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
                await bot.send_message(admin_id, "✅ Очередь обработана.",
                                       reply_markup=main_menu_keyboard(admin_id))
            except Exception:
                pass
            return
        if await _activate_request(bot, nxt):
            return
        try:
            await bot.send_message(admin_id, f"⚠️ Пропуск {mask_phone(nxt.phone)} — недоступен.")
        except Exception:
            pass


async def _queue_all_phones(bot: Bot, admin_id: int, reply_to: Message) -> None:
    if not store.owners:
        await reply_to.answer("Владельцев нет.", reply_markup=main_menu_keyboard(admin_id))
        return

    added = already = 0
    for owner in store.owners.values():
        for phone in owner.phones:
            if store.phone_status(phone) in ("active", "queued"):
                already += 1
                continue
            req = CodeRequest(owner_id=owner.user_id, admin_id=admin_id, phone=phone)
            if store.active is None and added == 0:
                await _activate_request(bot, req)
            else:
                store.push_queue(req)
            added += 1

    if added == 0:
        await reply_to.answer("Все номера уже в очереди.", reply_markup=main_menu_keyboard(admin_id))
        return

    note = f" · {already} уже в очереди" if already else ""
    await reply_to.answer(
        f"📥 Запущено: *{added}* номеров{note}. В очереди: *{store.queue_size()}*",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(admin_id),
    )


async def _finish_active(bot: Bot, *, reason: str, code: str | None = None,
                         message: Message | None = None) -> None:
    await _cancel_timer()
    req = store.clear_active()
    if not req:
        return

    owner = store.owners.get(req.owner_id)
    owner_name = owner.name if owner else str(req.owner_id)
    admin_id = req.admin_id

    if reason == "code" and code:
        profile = store.record_code_success(req.owner_id, CODE_REWARD)
        note = f"\n💰 +{CODE_REWARD:.0f}$ · баланс **{profile.balance:.2f}$**" if CODE_REWARD > 0 else ""
        if message:
            await message.answer(f"Код передан. Спасибо!{note}", parse_mode="Markdown",
                                 reply_markup=main_menu_keyboard(req.owner_id))
        try:
            await bot.send_message(admin_id,
                                   f"✅ **Код**\nВладелец: {owner_name}\nНомер: `{req.phone}`\nКод: `{code}`",
                                   parse_mode="Markdown")
        except Exception:
            log.exception("Не удалось отправить код админу %s", admin_id)

    elif reason == "timeout":
        store.record_code_fail(req.owner_id)
        try:
            await bot.send_message(req.owner_id, "⏱ Время вышло. Код не получен.",
                                   reply_markup=main_menu_keyboard(req.owner_id))
        except Exception:
            pass
        try:
            await bot.send_message(admin_id, f"⏱ Время вышло для `{req.phone}`.",
                                   parse_mode="Markdown")
        except Exception:
            pass

    elif reason == "decline":
        store.record_code_fail(req.owner_id)
        if message:
            await message.answer("Вы отказались.", reply_markup=main_menu_keyboard(req.owner_id))
        try:
            await bot.send_message(admin_id, f"🚫 Отказ для `{req.phone}`.", parse_mode="Markdown")
        except Exception:
            pass

    elif reason == "cancel":
        try:
            await bot.send_message(req.owner_id, "Запрос отменён администратором.",
                                   reply_markup=main_menu_keyboard(req.owner_id))
        except Exception:
            pass

    await _process_next_in_queue(bot, admin_id)


async def _do_request(bot: Bot, admin_id: int, phone: str, reply_to: Message | None = None) -> bool:
    owner = store.owner_by_phone(phone)
    if not owner:
        if reply_to:
            await reply_to.answer(f"Номер {phone} не зарегистрирован.",
                                  reply_markup=main_menu_keyboard(admin_id))
        return False

    status = store.phone_status(phone)
    if status == "active":
        if reply_to:
            await reply_to.answer(f"{mask_phone(phone)} уже обрабатывается ({store.seconds_left()} сек).",
                                  reply_markup=main_menu_keyboard(admin_id))
        return False
    if status == "queued":
        if reply_to:
            await reply_to.answer(f"{mask_phone(phone)} в очереди (позиция {store.queue_position(phone)}).",
                                  reply_markup=main_menu_keyboard(admin_id))
        return False

    req = CodeRequest(owner_id=owner.user_id, admin_id=admin_id, phone=phone)
    if store.active is None:
        if not await _activate_request(bot, req):
            if reply_to:
                await reply_to.answer("Не удалось связаться с владельцем.",
                                      reply_markup=main_menu_keyboard(admin_id))
            return False
        if reply_to:
            await reply_to.answer(f"Запрос для {mask_phone(phone)} активен.",
                                  reply_markup=main_menu_keyboard(admin_id))
        return True

    pos = store.push_queue(req)
    text = f"📥 {mask_phone(phone)} в очереди (позиция {pos})."
    if reply_to:
        await reply_to.answer(text, reply_markup=main_menu_keyboard(admin_id))
    else:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass
    return True

# ─── Callbacks ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("req:"))
async def cb_request_code(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    phone = normalize_phone((callback.data or "").removeprefix("req:"))
    if not phone:
        await callback.answer("Некорректный номер", show_alert=True)
        return
    if not await _do_request(bot, callback.from_user.id, phone, None):
        await callback.answer("Не удалось отправить запрос", show_alert=True)
        return
    await callback.answer("Запрос отправлен")
    if callback.message:
        await callback.message.answer(f"Запрос для {mask_phone(phone)} отправлен.",
                                      reply_markup=main_menu_keyboard(callback.from_user.id))


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
                await message.bot.send_message(was_active.owner_id, "Запрос кода отменён.",
                                               reply_markup=main_menu_keyboard(was_active.owner_id))
            except Exception:
                pass
        await message.answer(
            f"Отменено: {cancelled}." if cancelled else "Очередь пуста.",
            reply_markup=main_menu_keyboard(uid or 0),
        )
        if store.active is None and store.queue:
            await _process_next_in_queue(message.bot, uid or 0)
        return
    req = store.get_pending(uid or 0)
    if not req:
        await message.answer("Нет активного запроса.", reply_markup=main_menu_keyboard(uid or 0))
        return
    await _finish_active(message.bot, reason="decline", message=message)


@router.message(Command("decline"))
async def cmd_decline(message: Message, bot: Bot) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not store.get_pending(uid):
        await message.answer("Нет активного запроса.", reply_markup=main_menu_keyboard(uid))
        return
    await _finish_active(bot, reason="decline", message=message)


@router.message(Command("code"))
async def cmd_code(message: Message, command: CommandObject, bot: Bot) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not store.get_pending(uid):
        await message.answer("Нет запроса кода.")
        return
    if not command.args:
        await message.answer(f"Пример: /code {'1' * CODE_LEN}")
        return
    code = parse_code(command.args)
    if not code:
        await message.answer(f"Код — ровно {CODE_LEN} цифр.")
        return
    await _finish_active(bot, reason="code", code=code, message=message)

# ─── Настройки ──────────────────────────────────────────────────────────────

async def _show_settings(message: Message) -> None:
    icon = "✅" if store.bot_status.lower() in ("включён", "включен", "on", "вкл") else "🔴"
    await message.answer(
        f"⚙️ Настройки\n\nСтатус: {icon} {store.bot_status}\nПрайс: {store.price}",
        reply_markup=settings_keyboard(),
    )


@router.message(AdminSettings.waiting_price, F.text, ~F.text.in_(MENU_BUTTONS))
async def settings_price_input(message: Message, state: FSMContext) -> None:
    store.price = message.text or ""
    store.save()
    await state.clear()
    log.info("Прайс обновлён: %s", store.price)
    await message.answer(f"Прайс обновлён: {store.price}")
    await _show_settings(message)


@router.message(AdminSettings.waiting_broadcast, F.text, ~F.text.in_(MENU_BUTTONS))
async def settings_broadcast_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text = message.text or ""
    sender_id = message.from_user.id if message.from_user else 0
    ok = fail = 0
    for uid in store.all_users - {sender_id}:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
    await state.clear()
    await message.answer(f"Рассылка: {ok} доставлено, {fail} ошибок.", reply_markup=settings_keyboard())


@router.callback_query(F.data.startswith("work:"))
async def cb_work_toggle(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    value = (callback.data or "").removeprefix("work:")
    store.bot_status = "включён" if value == "on" else "выключен"
    store.save()
    icon = "✅" if value == "on" else "🔴"
    await callback.answer(f"Статус: {store.bot_status}")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=work_inline())
        except Exception:
            pass
        await callback.message.answer(f"Статус: {icon} {store.bot_status}",
                                      reply_markup=settings_keyboard())

# ─── Обработчик кнопок меню ─────────────────────────────────────────────────

@router.message(F.text.in_(MENU_BUTTONS))
async def on_menu_button(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else 0
    text = message.text or ""

    if text in (BTN_MENU, BTN_BACK):
        await cmd_start(message, state)
    elif text == BTN_SETTINGS and is_admin(uid):
        await _show_settings(message)
    elif text == BTN_SET_PRICE and is_admin(uid):
        await state.set_state(AdminSettings.waiting_price)
        await message.answer("Введите новый текст прайса:", reply_markup=settings_keyboard())
    elif text == BTN_SET_WORK and is_admin(uid):
        await message.answer("Выберите статус:", reply_markup=work_inline())
    elif text == BTN_SET_MSG and is_admin(uid):
        await state.set_state(AdminSettings.waiting_broadcast)
        await message.answer("Введите сообщение для рассылки:", reply_markup=settings_keyboard())
    elif text == BTN_OWNERS and is_admin(uid):
        await cmd_owners(message)
    elif text == BTN_REQUEST and is_admin(uid):
        await _queue_all_phones(bot, uid, message)
    elif text == BTN_CANCEL:
        await cmd_cancel(message, state)
    elif text == BTN_REGISTER and can_be_owner(uid) and not is_admin(uid):
        await cmd_register(message, state)
    elif text == BTN_PROFILE and not is_admin(uid):
        await message.answer(format_profile_text(uid), parse_mode="Markdown",
                             reply_markup=main_menu_keyboard(uid))
    elif text == BTN_DECLINE:
        await cmd_decline(message, bot)
    else:
        await message.answer("Команда недоступна.", reply_markup=main_menu_keyboard(uid))


@router.message(F.text)
async def on_text(message: Message, bot: Bot, state: FSMContext) -> None:
    if await state.get_state():
        return
    uid = message.from_user.id if message.from_user else 0
    req = store.get_pending(uid)
    if not req or not message.text:
        if is_admin(uid) or can_be_owner(uid):
            await message.answer("Используйте кнопки меню 👇", reply_markup=main_menu_keyboard(uid))
        return
    code = parse_code(message.text)
    if not code:
        await message.answer(f"Код — {CODE_LEN} цифр (например 123456). Отказ: 🚫 Отказаться")
        return
    await _finish_active(bot, reason="code", code=code, message=message)

# ─── Запуск ─────────────────────────────────────────────────────────────────

async def _recover_queue_on_startup(bot: Bot) -> None:
    if not store.active:
        if store.queue:
            await _process_next_in_queue(bot, store.queue[0].admin_id)
        return
    left = store.seconds_left()
    if left <= 0:
        req = store.clear_active()
        if req:
            try:
                await bot.send_message(req.admin_id,
                                       f"⏱ После перезапуска: время для `{req.phone}` истекло.",
                                       parse_mode="Markdown")
            except Exception:
                pass
        if store.queue:
            await _process_next_in_queue(bot, store.queue[0].admin_id)
        return
    await _start_code_timer(bot, store.active)
    log.info("Восстановлен запрос %s, осталось %s сек", store.active.phone, left)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")
    if not ADMIN_IDS:
        raise SystemExit("Задайте ADMIN_USER_IDS в .env")

    session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else AiohttpSession()
    bot = Bot(token=BOT_TOKEN, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("Бот запущен. Админы: %s", ADMIN_IDS)
    await _recover_queue_on_startup(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
