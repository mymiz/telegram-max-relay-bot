"""
Telegram-бот: владелец регистрирует номер MAX, админ запрашивает код входа,
владелец добровольно отправляет код — бот пересылает админу.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    TelegramObject,
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


ADMIN_IDS     = _parse_ids(os.getenv("ADMIN_USER_IDS", ""))
OWNER_WHITELIST = _parse_ids(os.getenv("OWNER_USER_IDS", ""))

store  = Store()
router = Router()

CODE_LEN          = 6
CODE_TIMEOUT_SEC  = 60
CODE_REWARD       = float(os.getenv("CODE_REWARD_RUB", "50"))
RATE_LIMIT        = float(os.getenv("RATE_LIMIT_SEC", "1.0"))
CALLBACK_RATE     = float(os.getenv("CALLBACK_RATE_SEC", "0.3"))
REWARD_DELAY_SEC  = 5 * 60  # задержка начисления награды после «Встал»
PHONE_HINT        = "Только номер России: +79991234567 (11 цифр, начинается с +7)"

_timer_task:          asyncio.Task | None = None
_reward_task:         asyncio.Task | None = None
_admin_ctrl_msg_id:   int | None          = None   # ID сообщения-панели у админа
_admin_ctrl_chat_id:  int | None          = None
_password_mode:       bool                = False  # ожидаем код/пароль от владельца
_password_attempts:   int                 = 0      # оставшихся попыток «Повтор»
_request_kind:        str                 = "code" # "code" или "password"


class ThrottleMiddleware(BaseMiddleware):
    """Ограничение частоты запросов: RATE_LIMIT_SEC секунд между действиями."""

    def __init__(self, rate: float = 1.0) -> None:
        self.rate = rate
        self._last: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user and not is_admin(user.id):
            now  = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self.rate:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⏳ Не так быстро...", show_alert=False)
                    except Exception:
                        pass
                return
            self._last[user.id] = now
            if len(self._last) > 1000:
                cutoff = now - 300.0
                self._last = {k: v for k, v in self._last.items() if v > cutoff}
        return await handler(event, data)


class OwnerRegister(StatesGroup):
    waiting_phone = State()


class AdminSettings(StatesGroup):
    waiting_price     = State()
    waiting_broadcast = State()


def is_admin(uid: int | None) -> bool:
    return uid is not None and uid in ADMIN_IDS


def can_be_owner(uid: int | None) -> bool:
    if uid is None:
        return False
    return not OWNER_WHITELIST or uid in OWNER_WHITELIST


def is_bot_active() -> bool:
    return store.bot_status.lower() in ("включён", "включен", "on", "вкл")


# ─── Инлайн-клавиатуры ──────────────────────────────────────────────────────

_SUPPORT_ROW = [
    InlineKeyboardButton(text="📖 Инструкция",
                         url="https://telegra.ph/Instrukciya-po-sdache-akkaunta-MAX-v-bota-TrustMax-Bot-05-26"),
    InlineKeyboardButton(text="🛠 Поддержка 1", url="https://t.me/Don1_Tomas1"),
    InlineKeyboardButton(text="🛠 Поддержка 2", url="https://t.me/tech_is_123"),
]

# Static keyboards — built once at module load, reused on every request
_OWNER_MENU_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="👤 Профиль",       callback_data="menu:profile"),
        InlineKeyboardButton(text="📱 Сдать номер",   callback_data="menu:register"),
    ],
    [
        InlineKeyboardButton(text="⏳ Очередь",       callback_data="menu:queue"),
        InlineKeyboardButton(text="📊 Статистика",    callback_data="menu:stats"),
    ],
    [InlineKeyboardButton(text="💸 Вывод средств",    callback_data="menu:withdraw")],
    _SUPPORT_ROW,
])

_ADMIN_MENU_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="📱 Номера",         callback_data="menu:numbers"),
        InlineKeyboardButton(text="🔐 Запросить код", callback_data="menu:request"),
    ],
    [InlineKeyboardButton(text="⚙️ Настройки",        callback_data="menu:settings")],
    _SUPPORT_ROW,
])

_ADMIN_MENU_KB_CANCEL = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="📱 Номера",         callback_data="menu:numbers"),
        InlineKeyboardButton(text="🔐 Запросить код", callback_data="menu:request"),
    ],
    [InlineKeyboardButton(text="⚙️ Настройки",        callback_data="menu:settings")],
    [InlineKeyboardButton(text="❌ Отменить",          callback_data="menu:cancel")],
    _SUPPORT_ROW,
])

_BACK_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back"),
]])

_REGISTER_TYPE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="💬 Код",  callback_data="reg_type:code"),
        InlineKeyboardButton(text="📷 QR",   callback_data="reg_type:qr"),
    ],
    [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
])

_OWNER_CODE_REQUEST_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🚫 Отказаться", callback_data="owner:decline"),
]])

_NUMBERS_MENU_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="📊 Активные номера", callback_data="numbers:active"),
        InlineKeyboardButton(text="👥 Участники",       callback_data="numbers:members"),
    ],
    [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
])

_QUEUE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📊 Всего номеров в очереди", callback_data="queue:total")],
    [InlineKeyboardButton(text="📋 Мои номера в очереди",    callback_data="queue:mine")],
    [InlineKeyboardButton(text="◀️ Назад",                   callback_data="menu:back")],
])

_STATS_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✅ Активные номера", callback_data="stats:active")],
    [InlineKeyboardButton(text="⏳ Ждут очереди",    callback_data="stats:waiting")],
    [InlineKeyboardButton(text="📁 Архив",            callback_data="stats:archive")],
    [InlineKeyboardButton(text="◀️ Назад",            callback_data="menu:back")],
])

_REQUEST_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="💬 SMS", callback_data="req_type:sms"),
        InlineKeyboardButton(text="📷 QR",  callback_data="req_type:qr"),
    ],
    [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")],
])

_SETTINGS_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="💰 Прайс",        callback_data="settings:price"),
        InlineKeyboardButton(text="🔄 Ворк",         callback_data="settings:work"),
    ],
    [InlineKeyboardButton(text="📢 Сообщение",       callback_data="settings:msg")],
    [InlineKeyboardButton(text="🗑 Сброс очереди",   callback_data="settings:reset_queue")],
    [InlineKeyboardButton(text="◀️ Назад",           callback_data="menu:back")],
])

_CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="❌ Отменить", callback_data="reg:cancel"),
]])

_ACTIVE_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🔢 Код",           callback_data="active:code")],
    [InlineKeyboardButton(text="✅ Встал",          callback_data="active:met")],
    [
        InlineKeyboardButton(text="⏩ Скип",        callback_data="active:skip"),
        InlineKeyboardButton(text="🚫 Бан номера",  callback_data="active:ban"),
    ],
])

_PASSWORD_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="✅ Встал",  callback_data="password:met"),
        InlineKeyboardButton(text="🔑 Пароль", callback_data="password:password"),
    ],
    [
        InlineKeyboardButton(text="🔄 Повтор", callback_data="password:repeat"),
        InlineKeyboardButton(text="⏩ Скип",   callback_data="password:skip"),
    ],
])


def admin_menu_inline() -> InlineKeyboardMarkup:
    """Returns pre-built keyboard; adds cancel row only when queue/active exists."""
    return _ADMIN_MENU_KB_CANCEL if (store.active or store.queue) else _ADMIN_MENU_KB


def work_inline() -> InlineKeyboardMarkup:
    on  = "✅ " if is_bot_active() else ""
    off = "" if on else "✅ "
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{on}Включен",   callback_data="work:on"),
            InlineKeyboardButton(text=f"{off}Выключен", callback_data="work:off"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="settings:back")],
    ])


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
    q = store.queue_size()
    return str(q) if q else "пусто"


def _price_amount() -> float:
    """Извлекает числовую сумму из текстового прайса.
    Примеры: '50$' → 50.0 | '100 руб' → 100.0 | '1.5' → 1.5
    Если число не найдено — возвращает CODE_REWARD из .env."""
    m = re.search(r"\d+(?:[.,]\d+)?", store.price)
    return float(m.group().replace(",", ".")) if m else CODE_REWARD


def welcome_text() -> str:
    icon = "✅" if is_bot_active() else "🔴"
    return (
        "Добро пожаловать в бота TrustMax\\_bot\\!\n\n"
        f"┌ Статус работы: {icon} {_esc(store.bot_status)}\n"
        f"├ Актуальный прайс: {_esc(store.price)}\n"
        f"└ Актуальная очередь: {_esc(format_queue_status())}\n\n"
        "👇 Выберите раздел для продолжения:"
    )


def format_owners_list() -> str:
    lines = [f"📋 *Владельцы*\nЛюдей: *{len(store.owners)}* · В очереди: *{store.queue_size()}*\n"]
    for i, o in enumerate(store.owners.values(), 1):
        status = ""
        if store.active and store.active.owner_id == o.user_id:
            status = f" ⏳ код ({store.seconds_left()} сек)"
        elif any(r.owner_id == o.user_id for r in store.queue):
            cnt = sum(1 for r in store.queue if r.owner_id == o.user_id)
            status = f" 📥 {cnt} в очереди"
        uname = f" @{_md(o.username)}" if o.username else ""
        prof  = store.get_profile(o.user_id)
        lines.append(
            f"\n*{i}. {_md(o.name or 'Без имени')}*{uname}{status}\n"
            f"ID: `{o.user_id}` · баланс: *{prof.balance:.0f}$*"
        )
    if store.active:
        lines.append(f"\n⏳ *В работе:* `{store.active.phone}` — {store.seconds_left()} сек")
    if store.queue:
        lines.append(f"\n📥 *Очередь* ({store.queue_size()}):")
        for i, req in enumerate(store.queue[:15], 1):
            owner = store.owners.get(req.owner_id)
            name  = (owner.name if owner else None) or str(req.owner_id)
            lines.append(f"  {i}. `{req.phone}` — {name}")
        if store.queue_size() > 15:
            lines.append(f"  … ещё {store.queue_size() - 15}")
    return "\n".join(lines)


_MSK = timezone(timedelta(hours=3))


def format_active_phones_today() -> str:
    records = store.get_today_successes()
    if not records:
        return "📊 *Активные номера*\n\nСегодня активных номеров нет."
    lines = [f"📊 *Активные номера за сегодня*\nВсего: *{len(records)}*\n"]
    for i, r in enumerate(records, 1):
        msk_time = datetime.fromtimestamp(r["processed_at"], tz=_MSK).strftime("%H:%M")
        owner    = store.owners.get(r["owner_id"])
        username = _md(f"@{owner.username}" if owner and owner.username else str(r["owner_id"]))
        lines.append(f"  {i}. `{r['phone']}` — {username} \\[{msk_time} МСК]")
    return "\n".join(lines)


def format_members_today() -> str:
    if not store.owners:
        return "👥 *Участники*\n\nВладельцев нет."
    counts  = store.get_today_success_counts()
    members = sorted(store.owners.values(), key=lambda o: counts.get(o.user_id, 0), reverse=True)
    lines   = [f"👥 *Участники*\nВсего: *{len(members)}*\n"]
    for i, owner in enumerate(members, 1):
        uname = f" @{_md(owner.username)}" if owner.username else ""
        name  = _md(owner.name or "—")
        lines.append(f"  {i}. {name}{uname} — *{counts.get(owner.user_id, 0)}* активн.")
    return "\n".join(lines)


def _menu_kb(uid: int) -> InlineKeyboardMarkup:
    return admin_menu_inline() if is_admin(uid) else _OWNER_MENU_KB


_STATUS_ICON = {"active": "⏳", "queued": "📥", "cooldown": "🕐", "banned": "🚫", "free": "🔐"}


def owners_request_inline() -> InlineKeyboardMarkup | None:
    if not store.queue:
        return None
    buttons = []
    for i, req in enumerate(store.queue, 1):
        owner = store.owners.get(req.owner_id)
        name  = (owner.name if owner else None) or str(req.owner_id)
        buttons.append([InlineKeyboardButton(
            text=f"📥 #{i} {mask_phone(req.phone)} — {name[:20]}",
            callback_data=f"req:{req.phone}",
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_profile_text(user_id: int) -> str:
    profile   = store.get_profile(user_id)
    owner     = store.owners.get(user_id)
    raw_name  = (owner.name if owner else None) or profile.name or "—"
    raw_uname = (owner.username if owner else None) or profile.username
    in_queue  = sum(1 for r in store.queue if r.owner_id == user_id)
    in_active = 1 if store.active and store.active.owner_id == user_id else 0
    total_ok  = profile.codes_ok
    return "\n".join([
        "👤 *ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ*\n",
        f"┌ Имя: {_md(raw_name)}",
        f"├ Юзернейм: @{_md(raw_uname)}" if raw_uname else "├ Юзернейм: —",
        f"└ ID: `{user_id}`",
        "",
        "💰 *ФИНАНСОВАЯ СТАТИСТИКА*\n",
        f"┌ Баланс: *{profile.balance:.2f}$*",
        f"├ Заработано: *{profile.total_earned:.2f}$*",
        f"├ Выведено: *{profile.withdrawn:.2f}$*",
        f"└ Успешных номеров: *{total_ok}*",
        "",
        "⏳ *ТЕКУЩИЕ ЗАЯВКИ*\n",
        f"┌ В очереди: *{in_queue}*",
        f"└ В обработке: *{in_active}*",
    ])


# ─── Хелпер редактирования ──────────────────────────────────────────────────

async def _edit_or_answer(
    msg: Message | InaccessibleMessage | None,
    text: str,
    *,
    parse_mode: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Редактирует сообщение если возможно, иначе удаляет старое и отправляет новое."""
    if not isinstance(msg, Message):
        return None
    try:
        return await msg.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return msg
    except Exception:
        pass
    # Редактирование не удалось — удаляем старое сообщение (убирает зависшую клавиатуру)
    try:
        await msg.delete()
    except Exception:
        pass
    try:
        return await msg.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        return None


# ─── Отправка меню ──────────────────────────────────────────────────────────

async def _send_welcome(message: Message, uid: int, *, first_time: bool = False) -> None:
    """Всегда отправляет НОВОЕ сообщение с меню (используется из /start и on_text).
    Фото — отдельно без клавиатуры, чтобы меню-сообщение всегда было текстовым
    и могло редактироваться через edit_text при нажатии кнопок."""
    # Убираем любую reply-клавиатуру от старых версий бота
    try:
        rm = await message.answer("…", reply_markup=ReplyKeyboardRemove())
        await rm.delete()
    except Exception:
        pass
    if first_time and LOGO_FILE:
        try:
            await message.answer_photo(photo=LOGO_FILE)
        except Exception:
            pass
    await message.answer(welcome_text(), parse_mode="MarkdownV2", reply_markup=_menu_kb(uid))


async def _show_menu(msg: Message | InaccessibleMessage | None, uid: int) -> None:
    """Редактирует текущее сообщение под меню (используется из callbacks)."""
    await _edit_or_answer(msg, welcome_text(), parse_mode="MarkdownV2", reply_markup=_menu_kb(uid))


async def _show_settings(msg: Message | InaccessibleMessage | None) -> None:
    icon = "✅" if is_bot_active() else "🔴"
    await _edit_or_answer(
        msg,
        f"⚙️ *Настройки*\n\nСтатус: {icon} {_md(store.bot_status)}\nПрайс: {_md(store.price)}",
        parse_mode="Markdown",
        reply_markup=_SETTINGS_KB,
    )


# ─── Команды ────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    uid  = user.id if user else 0
    first_time = uid not in store.all_users
    if uid and user:
        store.track_user(uid)
        store.touch_profile(uid, name=user.full_name, username=user.username)
        if uid in store.owners:
            store.update_owner_info(uid, user.full_name, user.username)

    if is_admin(uid) or can_be_owner(uid):
        if not is_admin(uid) and not is_bot_active():
            await message.answer("🔴 Бот временно выключен.\nПопробуйте позже.")
            return
        await _send_welcome(message, uid, first_time=first_time)
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
                                 parse_mode="Markdown")
            return
        await message.answer("Профиль: `/profile 123456789`\nБаланс: `/addbal 123456789 100`",
                             parse_mode="Markdown")
        return
    await message.answer(format_profile_text(uid), parse_mode="Markdown", reply_markup=_BACK_KB)


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
    await message.answer(f"Баланс `{parts[0]}`: **{bal:.2f}$**", parse_mode="Markdown")


@router.message(Command("setstatus"))
async def cmd_setstatus(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Только для администратора.")
        return
    value = (command.args or "").strip()
    if not value:
        await message.answer("Формат: `/setstatus включён`", parse_mode="Markdown")
        return
    store.set_bot_status(value)
    await message.answer(welcome_text(), parse_mode="MarkdownV2", reply_markup=admin_menu_inline())


@router.message(Command("setprice"))
async def cmd_setprice(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Только для администратора.")
        return
    value = (command.args or "").strip()
    if not value:
        await message.answer("Формат: `/setprice текст`", parse_mode="Markdown")
        return
    store.set_price(value)
    await message.answer(welcome_text(), parse_mode="MarkdownV2", reply_markup=admin_menu_inline())


@router.message(Command("register"))
async def cmd_register(message: Message, state: FSMContext) -> None:
    uid = message.from_user.id if message.from_user else 0
    if is_admin(uid):
        await message.answer("Администратору недоступно.")
        return
    if not can_be_owner(uid):
        await message.answer("Регистрация отключена для вашего аккаунта.")
        return
    if not is_bot_active():
        await message.answer("🔴 Бот временно выключен. Регистрация недоступна.")
        return
    await state.set_state(OwnerRegister.waiting_phone)
    sent = await message.answer(f"📱 Введите номер MAX:\n{PHONE_HINT}", reply_markup=_CANCEL_KB)
    await state.update_data(prompt_msg_id=sent.message_id, prompt_chat_id=sent.chat.id)


@router.message(OwnerRegister.waiting_phone, F.text)
async def register_text(message: Message, state: FSMContext) -> None:
    if not message.text:
        return
    if not is_bot_active():
        await state.clear()
        await message.answer("🔴 Бот временно выключен.")
        return
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer(PHONE_HINT)
        return
    await _finish_register(message, state, phone)


async def _finish_register(message: Message, state: FSMContext, phone: str) -> None:
    user = message.from_user
    if not user:
        return
    data           = await state.get_data()
    prompt_msg_id  = data.get("prompt_msg_id")
    prompt_chat_id = data.get("prompt_chat_id", message.chat.id)

    status = store.phone_status(phone)
    await state.clear()
    if store.is_phone_banned(phone):
        result = "🚫 Этот номер заблокирован. По вопросам обращайтесь в тех. поддержку."
    elif status == "active":
        result = f"⏳ Номер {mask_phone(phone)} сейчас в обработке. Ожидайте."
    elif status == "queued":
        pos = store.queue_position(phone)
        result = f"📥 Номер {mask_phone(phone)} уже в очереди (позиция #{pos})."
    elif status == "cooldown":
        result = f"🕐 Номер {mask_phone(phone)} уже был обработан сегодня. Попробуйте завтра."
    else:
        owner = store.ensure_owner(user.id, name=user.full_name, username=user.username)
        default_admin = next(iter(ADMIN_IDS))
        req = CodeRequest(owner_id=user.id, admin_id=default_admin, phone=phone)
        store.push_queue(req)
        result = f"✅ Номер {mask_phone(phone)} добавлен в очередь!"
        for admin_id in ADMIN_IDS:
            try:
                await message.bot.send_message(
                    admin_id,
                    f"📥 Новый номер в очереди: {_md(user.full_name or str(user.id))}"
                    f" · `{phone}` · ID: `{user.id}`",
                    parse_mode="Markdown",
                )
            except Exception:
                log.warning("Не удалось уведомить админа %s", admin_id)

    # Редактируем сообщение-подсказку, если оно известно
    if prompt_msg_id:
        try:
            await message.bot.edit_message_text(
                result, chat_id=prompt_chat_id, message_id=prompt_msg_id,
                reply_markup=_OWNER_MENU_KB,
            )
            return
        except Exception:
            # Редактирование не вышло — удаляем сообщение-подсказку
            try:
                await message.bot.delete_message(prompt_chat_id, prompt_msg_id)
            except Exception:
                pass
    await message.answer(result, reply_markup=_OWNER_MENU_KB)


@router.message(Command("owners"))
async def cmd_owners(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not is_admin(uid):
        await message.answer("Команда только для администратора.")
        return
    if not store.owners:
        await message.answer("Владельцев нет.", reply_markup=admin_menu_inline())
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


async def _schedule_reward(bot: Bot, owner_id: int, phone: str) -> None:
    """Начисляет награду владельцу через REWARD_DELAY_SEC секунд.
    Сумма фиксируется из прайса в момент вызова, а не спустя 5 минут."""
    global _reward_task
    reward = _price_amount()  # берём из store.price прямо сейчас

    async def _pay() -> None:
        await asyncio.sleep(REWARD_DELAY_SEC)
        profile = store.record_code_success(owner_id, reward)
        note = (
            f"💰 Начислено *{reward:.2f}$* · баланс *{profile.balance:.2f}$*"
            if reward > 0 else "✅ Номер подтверждён."
        )
        try:
            await bot.send_message(owner_id, note, parse_mode="Markdown",
                                   reply_markup=_OWNER_MENU_KB)
        except Exception:
            pass

    _reward_task = asyncio.create_task(_pay())


async def _notify_owner_request(bot: Bot, req: CodeRequest) -> bool:
    """Уведомляет владельца что его номер берётся в работу."""
    try:
        await bot.send_message(
            req.owner_id,
            f"📱 *Ваш номер берётся в работу*\n\nНомер: `{req.phone}`\n"
            "Ожидайте запроса пароля или кода.",
            parse_mode="Markdown",
            reply_markup=_OWNER_CODE_REQUEST_KB,
        )
        return True
    except Exception:
        log.exception("Не удалось написать владельцу %s", req.owner_id)
        return False


async def _request_from_owner(
    bot: Bot, req: CodeRequest, attempt: int, kind: str = "code"
) -> None:
    """Запрашивает у владельца SMS-код или пароль.
    kind='code'  → запрос SMS-кода
    kind='password' → запрос пароля аккаунта
    """
    if kind == "code":
        text = (
            f"🔢 *Нужен SMS-код для номера* `{req.phone}`\n"
            f"Попытка *{attempt}/2* · отправьте *{CODE_LEN} цифр*.\n"
            f"⏱ У вас *{CODE_TIMEOUT_SEC // 60} минута* на ответ."
        )
    else:
        text = (
            f"🔑 *Нужен пароль для номера* `{req.phone}`\n"
            f"Попытка *{attempt}/2* · отправьте пароль."
        )
    try:
        await bot.send_message(
            req.owner_id, text,
            parse_mode="Markdown",
            reply_markup=_OWNER_CODE_REQUEST_KB,
        )
    except Exception:
        log.warning("Не удалось запросить у владельца %s", req.owner_id)


async def _forward_code_to_admin(bot: Bot, req: CodeRequest, code: str) -> None:
    """Пересылает полученный код на панель управления админа."""
    global _admin_ctrl_msg_id, _admin_ctrl_chat_id, _password_attempts
    await _cancel_timer()  # код получен — таймер больше не нужен
    if _password_attempts > 0:
        _password_attempts -= 1
    attempts_note = f"\n⚠️ Осталось повторов: {_password_attempts}" if _password_mode else ""
    text = (
        f"📱 Номер: `{req.phone}`\n"
        f"🔑 Код от владельца: `{code}`{attempts_note}\n\n"
        "Нажмите *Встал* если сработало, *Повтор* для нового кода или *Скип*."
    )
    if _admin_ctrl_msg_id and _admin_ctrl_chat_id:
        try:
            await bot.edit_message_text(
                text, chat_id=_admin_ctrl_chat_id, message_id=_admin_ctrl_msg_id,
                parse_mode="Markdown", reply_markup=_PASSWORD_KB,
            )
            return
        except Exception:
            pass
    try:
        sent = await bot.send_message(
            req.admin_id, text, parse_mode="Markdown", reply_markup=_PASSWORD_KB,
        )
        _admin_ctrl_msg_id  = sent.message_id
        _admin_ctrl_chat_id = req.admin_id
    except Exception:
        log.warning("Не удалось переслать код админу %s", req.admin_id)


async def _activate_request(bot: Bot, req: CodeRequest) -> bool:
    global _admin_ctrl_msg_id, _admin_ctrl_chat_id, _password_mode, _password_attempts
    if not await _notify_owner_request(bot, req):
        return False
    store.set_active(req, timeout_sec=CODE_TIMEOUT_SEC)
    await _start_code_timer(bot, req)
    _password_mode      = False
    _password_attempts  = 0
    q = store.queue_size()
    queue_note = f"\n📥 В очереди: {q}" if q else ""
    try:
        sent = await bot.send_message(
            req.admin_id,
            f"📱 Номер: `{req.phone}` — ожидание{queue_note}",
            parse_mode="Markdown",
            reply_markup=_ACTIVE_KB,
        )
        _admin_ctrl_msg_id  = sent.message_id
        _admin_ctrl_chat_id = req.admin_id
    except Exception:
        log.warning("Не удалось уведомить админа %s", req.admin_id)
    return True


async def _admin_take_phone(
    bot: Bot, admin_id: int, phone: str,
    msg: Message | InaccessibleMessage | None,
) -> None:
    """Резервируем номер для ручной обработки — владелец НЕ уведомляется.
    Уведомление отправляется только при нажатии «Код» или «Пароль»."""
    global _admin_ctrl_msg_id, _admin_ctrl_chat_id, _password_mode, _password_attempts

    owner = store.owner_by_phone(phone)
    if not owner:
        await _edit_or_answer(msg, f"❌ Номер {phone} не найден.", reply_markup=admin_menu_inline())
        return

    status = store.phone_status(phone)
    if status == "banned":
        await _edit_or_answer(msg, f"🚫 {mask_phone(phone)} заблокирован.",
                              reply_markup=owners_request_inline())
        return
    if status == "active":
        await _edit_or_answer(msg, f"⏳ {mask_phone(phone)} уже в обработке.",
                              reply_markup=owners_request_inline())
        return
    if status == "cooldown":
        await _edit_or_answer(msg, f"🕐 {mask_phone(phone)} на кулдауне до завтра.",
                              reply_markup=owners_request_inline())
        return
    if store.active:
        await _edit_or_answer(msg,
                              "⚠️ Уже есть активный номер — завершите его сначала.",
                              reply_markup=admin_menu_inline())
        return
    # Если номер в очереди — убираем его оттуда перед активацией
    if status == "queued":
        store.remove_phone(phone)

    req = CodeRequest(owner_id=owner.user_id, admin_id=admin_id, phone=phone)
    store.set_active(req, timeout_sec=30 * 60)  # держим слот 30 мин без таймера
    _password_mode     = False
    _password_attempts = 0

    sent = await _edit_or_answer(
        msg,
        f"📱 Номер: `{phone}`\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=_ACTIVE_KB,
    )
    if sent:
        _admin_ctrl_msg_id  = sent.message_id
        _admin_ctrl_chat_id = admin_id


async def _process_next_in_queue(bot: Bot, admin_id: int) -> None:
    while True:
        nxt = store.pop_next()
        if not nxt:
            try:
                await bot.send_message(admin_id, "✅ Очередь обработана.")
            except Exception:
                pass
            return
        if await _activate_request(bot, nxt):
            return
        try:
            await bot.send_message(admin_id, f"⚠️ Пропуск {mask_phone(nxt.phone)} — недоступен.")
        except Exception:
            pass


async def _queue_all_phones(bot: Bot, admin_id: int, msg: Message) -> None:
    if not store.owners:
        await _edit_or_answer(msg, "Владельцев нет.", reply_markup=admin_menu_inline())
        return

    added = already = cooldown = 0
    for owner in store.owners.values():
        for phone in owner.phones:
            status = store.phone_status(phone)
            if status in ("active", "queued"):
                already += 1
                continue
            if status in ("cooldown", "banned"):
                cooldown += 1
                continue
            req = CodeRequest(owner_id=owner.user_id, admin_id=admin_id, phone=phone)
            if store.active is None and added == 0:
                await _activate_request(bot, req)
            else:
                store.push_queue(req)
            added += 1

    if added == 0:
        parts = []
        if already:
            parts.append(f"{already} уже в очереди")
        if cooldown:
            parts.append(f"{cooldown} на кулдауне до завтра")
        note = " · " + ", ".join(parts) if parts else ""
        await _edit_or_answer(msg, f"Нет доступных номеров.{note}",
                              reply_markup=admin_menu_inline())
        return

    parts = []
    if already:
        parts.append(f"{already} уже в очереди")
    if cooldown:
        parts.append(f"🕐 {cooldown} на кулдауне")
    note = " · " + ", ".join(parts) if parts else ""
    await _edit_or_answer(
        msg,
        f"📥 Запущено: *{added}* номеров{note}. В очереди: *{store.queue_size()}*",
        parse_mode="Markdown",
        reply_markup=admin_menu_inline(),
    )


async def _finish_active(bot: Bot, *, reason: str, code: str | None = None,
                         message: Message | None = None) -> None:
    global _admin_ctrl_msg_id, _admin_ctrl_chat_id, _password_mode, _password_attempts
    await _cancel_timer()
    req = store.clear_active()
    if not req:
        return

    # Сброс панели управления
    _admin_ctrl_msg_id  = None
    _admin_ctrl_chat_id = None
    _password_mode      = False
    _password_attempts  = 0

    owner      = store.owners.get(req.owner_id)
    owner_name = owner.name if owner else str(req.owner_id)
    admin_id   = req.admin_id

    if reason == "met":
        # Админ подтвердил «Встал» — награда через 5 минут
        store.record_phone_success(req.phone)
        store.record_phone_history(req.owner_id, req.phone, "success")
        await _schedule_reward(bot, req.owner_id, req.phone)
        try:
            await bot.send_message(
                req.owner_id,
                f"✅ Номер `{req.phone}` принят!\n"
                f"💰 Вознаграждение придёт через 5 минут.",
                parse_mode="Markdown", reply_markup=_OWNER_MENU_KB,
            )
        except Exception:
            pass

    elif reason == "code" and code:
        profile = store.record_code_success(req.owner_id, CODE_REWARD)
        store.record_phone_success(req.phone)
        store.record_phone_history(req.owner_id, req.phone, "success")
        note = f"\n💰 +{CODE_REWARD:.0f}$ · баланс *{profile.balance:.2f}$*" if CODE_REWARD > 0 else ""
        if message:
            await message.answer(f"✅ Код передан. Спасибо!{note}",
                                 parse_mode="Markdown", reply_markup=_OWNER_MENU_KB)
        try:
            await bot.send_message(
                admin_id,
                f"✅ *Код*\nВладелец: {_md(owner_name)}\nНомер: `{req.phone}`\nКод: `{code}`",
                parse_mode="Markdown",
            )
        except Exception:
            log.exception("Не удалось отправить код админу %s", admin_id)

    elif reason == "skip":
        store.record_code_fail(req.owner_id)
        store.record_phone_history(req.owner_id, req.phone, "skip")
        try:
            await bot.send_message(
                req.owner_id,
                f"⏩ Номер `{req.phone}` пропущен.\n"
                "По вопросам обращайтесь в тех. поддержку.",
                parse_mode="Markdown", reply_markup=_OWNER_MENU_KB,
            )
        except Exception:
            pass

    elif reason == "ban":
        store.ban_phone(req.phone)
        store.record_phone_history(req.owner_id, req.phone, "ban")
        try:
            await bot.send_message(
                req.owner_id,
                f"🚫 Номер `{req.phone}` заблокирован.\n"
                "По вопросам разблокировки обращайтесь в тех. поддержку.",
                parse_mode="Markdown", reply_markup=_OWNER_MENU_KB,
            )
        except Exception:
            pass

    elif reason == "timeout":
        store.record_code_fail(req.owner_id)
        store.record_phone_history(req.owner_id, req.phone, "timeout")
        try:
            await bot.send_message(req.owner_id, "⏱ Время вышло. Код не получен.",
                                   reply_markup=_OWNER_MENU_KB)
        except Exception:
            pass
        try:
            await bot.send_message(admin_id, f"⏱ Время вышло для `{req.phone}`.",
                                   parse_mode="Markdown")
        except Exception:
            pass

    elif reason == "decline":
        store.record_code_fail(req.owner_id)
        store.record_phone_history(req.owner_id, req.phone, "decline")
        if message:
            await message.answer("🚫 Вы отказались.", reply_markup=_OWNER_MENU_KB)
        else:
            try:
                await bot.send_message(req.owner_id, "🚫 Вы отказались.",
                                       reply_markup=_OWNER_MENU_KB)
            except Exception:
                pass
        try:
            await bot.send_message(admin_id, f"🚫 Отказ для `{req.phone}`.", parse_mode="Markdown")
        except Exception:
            pass

    elif reason == "cancel":
        try:
            await bot.send_message(req.owner_id, "Запрос отменён администратором.",
                                   reply_markup=_OWNER_MENU_KB)
        except Exception:
            pass

    await _process_next_in_queue(bot, admin_id)


async def _do_request(bot: Bot, admin_id: int, phone: str, reply_to: Message | None = None) -> bool:
    owner = store.owner_by_phone(phone)
    if not owner:
        if reply_to:
            await reply_to.answer(f"Номер {phone} не зарегистрирован.")
        return False

    status = store.phone_status(phone)
    if status == "banned":
        if reply_to:
            await reply_to.answer(f"🚫 {mask_phone(phone)} заблокирован.")
        return False
    if status == "active":
        if reply_to:
            await reply_to.answer(
                f"{mask_phone(phone)} уже обрабатывается ({store.seconds_left()} сек).")
        return False
    if status == "queued":
        if reply_to:
            await reply_to.answer(
                f"{mask_phone(phone)} в очереди (позиция {store.queue_position(phone)}).")
        return False
    if status == "cooldown":
        if reply_to:
            await reply_to.answer(
                f"🕐 {mask_phone(phone)} уже обработан сегодня. Повторно — завтра.")
        return False

    req = CodeRequest(owner_id=owner.user_id, admin_id=admin_id, phone=phone)
    if store.active is None:
        if not await _activate_request(bot, req):
            if reply_to:
                await reply_to.answer("Не удалось связаться с владельцем.")
            return False
        if reply_to:
            await reply_to.answer(f"Запрос для {mask_phone(phone)} активен.")
        return True

    pos  = store.push_queue(req)
    text = f"📥 {mask_phone(phone)} в очереди (позиция {pos})."
    if reply_to:
        await reply_to.answer(text)
    else:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            pass
    return True


# ─── Callbacks ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "reg:cancel")
async def cb_reg_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    uid = callback.from_user.id if callback.from_user else 0
    await _show_menu(callback.message, uid)


@router.callback_query(F.data == "owner:decline")
async def cb_owner_decline(callback: CallbackQuery, bot: Bot) -> None:
    uid = callback.from_user.id if callback.from_user else 0
    if not store.get_pending(uid):
        await callback.answer("Нет активного запроса", show_alert=True)
        return
    await callback.answer("🚫 Запрос отклонён")
    # Убираем кнопку с сообщения-запроса
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await _finish_active(bot, reason="decline")


@router.callback_query(F.data.startswith("active:"))
async def cb_active(callback: CallbackQuery, bot: Bot) -> None:
    global _password_mode, _password_attempts, _request_kind
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    req = store.active
    if not req:
        await callback.answer("Нет активного запроса", show_alert=True)
        return
    action = (callback.data or "").removeprefix("active:")
    msg    = callback.message

    if action == "met":
        await callback.answer("✅ Встал — награда через 5 мин")
        if isinstance(msg, Message):
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await _finish_active(bot, reason="met")

    elif action == "code":
        _password_mode     = True
        _password_attempts = 1  # 1 Повтор = 2 попытки суммарно
        _request_kind      = "code"
        await callback.answer("🔢 SMS-код — запрос отправлен")
        # Только сейчас запускаем таймер и уведомляем владельца
        await _start_code_timer(bot, req)
        await _request_from_owner(bot, req, attempt=1, kind="code")
        if isinstance(msg, Message):
            try:
                await msg.edit_text(
                    f"📱 Номер: `{req.phone}`\n🔢 SMS-код запрошен у владельца...",
                    parse_mode="Markdown", reply_markup=_PASSWORD_KB,
                )
            except Exception:
                pass

    elif action == "skip":
        await callback.answer("⏩ Скип")
        if isinstance(msg, Message):
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await _finish_active(bot, reason="skip")

    elif action == "ban":
        await callback.answer("🚫 Номер забанен")
        if isinstance(msg, Message):
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await _finish_active(bot, reason="ban")


@router.callback_query(F.data.startswith("password:"))
async def cb_password(callback: CallbackQuery, bot: Bot) -> None:
    global _password_mode, _password_attempts, _request_kind
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    req = store.active
    if not req:
        await callback.answer("Нет активного запроса", show_alert=True)
        return
    action = (callback.data or "").removeprefix("password:")
    msg    = callback.message

    if action == "met":
        await callback.answer("✅ Встал — награда через 5 мин")
        if isinstance(msg, Message):
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await _finish_active(bot, reason="met")

    elif action == "password":
        _password_mode     = True
        _password_attempts = 1
        _request_kind      = "password"
        await callback.answer("🔑 Пароль — запрос отправлен")
        await _start_code_timer(bot, req)
        await _request_from_owner(bot, req, attempt=1, kind="password")
        if isinstance(msg, Message):
            try:
                await msg.edit_text(
                    f"📱 Номер: `{req.phone}`\n🔑 Пароль запрошен у владельца...",
                    parse_mode="Markdown", reply_markup=_PASSWORD_KB,
                )
            except Exception:
                pass

    elif action == "repeat":
        if _password_attempts <= 0:
            await callback.answer("⚠️ Попытки исчерпаны — скип", show_alert=True)
            if isinstance(msg, Message):
                try:
                    await msg.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
            await _finish_active(bot, reason="skip")
            return
        await callback.answer("🔄 Повторный запрос отправлен")
        await _request_from_owner(bot, req, attempt=2, kind=_request_kind)
        if isinstance(msg, Message):
            try:
                await msg.edit_text(
                    f"📱 Номер: `{req.phone}`\n🔄 Повторный запрос отправлен...",
                    parse_mode="Markdown", reply_markup=_PASSWORD_KB,
                )
            except Exception:
                pass

    elif action == "skip":
        await callback.answer("⏩ Скип")
        if isinstance(msg, Message):
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
        await _finish_active(bot, reason="skip")


@router.callback_query(F.data.startswith("reg_type:"))
async def cb_reg_type(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    uid    = callback.from_user.id if callback.from_user else 0
    action = (callback.data or "").removeprefix("reg_type:")
    msg    = callback.message

    if not can_be_owner(uid) or is_admin(uid):
        await callback.answer("Недоступно", show_alert=True)
        return

    if action == "code":
        await state.set_state(OwnerRegister.waiting_phone)
        result = await _edit_or_answer(msg, f"📱 Введите номер MAX:\n{PHONE_HINT}",
                                       reply_markup=_CANCEL_KB)
        if result:
            await state.update_data(prompt_msg_id=result.message_id,
                                    prompt_chat_id=result.chat.id)

    elif action == "qr":
        await _edit_or_answer(
            msg,
            "📷 *QR-вход*\n\nФункция в разработке.\nВернитесь позже.",
            parse_mode="Markdown",
            reply_markup=_REGISTER_TYPE_KB,
        )


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    await callback.answer()
    uid    = callback.from_user.id if callback.from_user else 0
    action = (callback.data or "").removeprefix("menu:")
    msg    = callback.message

    if not is_admin(uid) and not is_bot_active():
        await _edit_or_answer(msg, "🔴 Бот временно выключен.\nПопробуйте позже.",
                              reply_markup=None)
        return

    if action == "back":
        await _show_menu(msg, uid)

    elif action == "profile":
        await _edit_or_answer(msg, format_profile_text(uid), parse_mode="Markdown",
                              reply_markup=_BACK_KB)

    elif action == "register":
        if not can_be_owner(uid) or is_admin(uid):
            await callback.answer("Недоступно", show_alert=True)
            return
        await _edit_or_answer(
            msg,
            "📱 *Сдать номер MAX*\n\nВыберите способ входа:",
            parse_mode="Markdown",
            reply_markup=_REGISTER_TYPE_KB,
        )

    elif action == "queue":
        await _edit_or_answer(msg, "⏳ *Очередь*\n\nВыберите раздел:",
                              parse_mode="Markdown", reply_markup=_QUEUE_KB)

    elif action == "stats":
        await _edit_or_answer(msg, "📊 *Статистика*\n\nВыберите раздел:",
                              parse_mode="Markdown", reply_markup=_STATS_KB)

    elif action == "withdraw":
        profile = store.get_profile(uid)
        await _edit_or_answer(
            msg,
            f"💸 *Вывод средств*\n\n"
            f"Ваш баланс: *{profile.balance:.2f}$*\n\n"
            f"Минимальная сумма вывода: *1$*\n"
            f"Для вывода: @yirica",
            parse_mode="Markdown",
            reply_markup=_BACK_KB,
        )

    elif action == "numbers" and is_admin(uid):
        await _edit_or_answer(
            msg,
            "📱 *Номера*\n\nВыберите раздел:",
            parse_mode="Markdown",
            reply_markup=_NUMBERS_MENU_KB,
        )

    elif action == "request" and is_admin(uid):
        await _edit_or_answer(msg, "🔐 *Запросить код*\n\nВыберите тип:",
                              parse_mode="Markdown", reply_markup=_REQUEST_KB)

    elif action == "cancel" and is_admin(uid):
        await _cancel_timer()
        cancelled, was_active = store.cancel_all_for_admin(uid)
        if was_active:
            try:
                await bot.send_message(was_active.owner_id, "Запрос кода отменён.")
            except Exception:
                pass
        text = f"Отменено: {cancelled}." if cancelled else "Очередь пуста."
        await _edit_or_answer(msg, text, reply_markup=admin_menu_inline())

    elif action == "settings" and is_admin(uid):
        await _show_settings(msg)

    else:
        await callback.answer("Команда недоступна", show_alert=True)


@router.callback_query(F.data.startswith("numbers:"))
async def cb_numbers(callback: CallbackQuery) -> None:
    await callback.answer()
    uid = callback.from_user.id if callback.from_user else 0
    if not is_admin(uid):
        return
    action = (callback.data or "").removeprefix("numbers:")
    msg    = callback.message

    if action == "active":
        await _edit_or_answer(msg, format_active_phones_today(),
                              parse_mode="Markdown", reply_markup=_NUMBERS_MENU_KB)
    elif action == "members":
        await _edit_or_answer(msg, format_members_today(),
                              parse_mode="Markdown", reply_markup=_NUMBERS_MENU_KB)


_HISTORY_STATUS_LABELS: dict[str, str] = {
    "success": "✅ принят",
    "skip":    "⏩ пропущен",
    "ban":     "🚫 заблокирован",
    "timeout": "⏱ таймаут",
    "decline": "❌ отказ",
}


@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    uid    = callback.from_user.id if callback.from_user else 0
    action = (callback.data or "").removeprefix("stats:")
    msg    = callback.message

    if action == "active":
        records = store.get_today_successes()
        mine    = [r for r in records if r["owner_id"] == uid]
        if not mine:
            text = "✅ *Активные номера*\n\n_Сегодня нет успешных номеров_"
        else:
            lines = ["✅ *Активные номера*\n"]
            for r in mine:
                dt = datetime.fromtimestamp(r["processed_at"], tz=_MSK).strftime("%d.%m %H:%M")
                lines.append(f"📱 `{mask_phone(r['phone'])}` — встал в *{dt}* МСК")
            text = "\n".join(lines)
        await _edit_or_answer(msg, text, parse_mode="Markdown", reply_markup=_STATS_KB)

    elif action == "waiting":
        owner  = store.owners.get(uid)
        phones = owner.phones if owner else []
        waiting = [
            (p, store.queue_position(p))
            for p in phones if store.phone_status(p) == "queued"
        ]
        if not waiting:
            text = "⏳ *Ждут очереди*\n\n_Нет номеров в очереди_"
        else:
            lines = ["⏳ *Ждут очереди*\n"]
            for phone, pos in sorted(waiting, key=lambda x: x[1]):
                lines.append(f"📥 `{mask_phone(phone)}` — позиция *\\#{pos}*")
            text = "\n".join(lines)
        await _edit_or_answer(msg, text, parse_mode="Markdown", reply_markup=_STATS_KB)

    elif action == "archive":
        history = store.get_phone_history(uid)
        if not history:
            await _edit_or_answer(msg, "📁 *Архив*\n\n_История пуста_",
                                  parse_mode="Markdown", reply_markup=_STATS_KB)
            return
        days: dict[str, list] = {}
        for r in history:
            days.setdefault(r["date"], []).append(r)
        buttons = [
            [InlineKeyboardButton(
                text=f"📅 {day} · {len(days[day])} номеров",
                callback_data=f"stats:day:{day}",
            )]
            for day in sorted(days.keys(), reverse=True)
        ]
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="stats:back")])
        await _edit_or_answer(msg, "📁 *Архив*\n\nВыберите день:",
                              parse_mode="Markdown",
                              reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

    elif action.startswith("day:"):
        day     = action.removeprefix("day:")
        history = store.get_phone_history(uid)
        records = [r for r in history if r["date"] == day]
        if not records:
            text = f"📅 *{_esc(day)}*\n\n_Нет записей_"
        else:
            lines = [f"📅 *{_esc(day)}*\n"]
            for r in sorted(records, key=lambda x: x["processed_at"], reverse=True):
                dt    = datetime.fromtimestamp(r["processed_at"]).strftime("%H:%M")
                label = _HISTORY_STATUS_LABELS.get(r["status"], r["status"])
                lines.append(f"📱 `{mask_phone(r['phone'])}` · {dt} · {label}")
            text = "\n".join(lines)
        back_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К архиву", callback_data="stats:archive")],
        ])
        await _edit_or_answer(msg, text, parse_mode="Markdown", reply_markup=back_kb)

    elif action == "back":
        await _edit_or_answer(msg, "📊 *Статистика*\n\nВыберите раздел:",
                              parse_mode="Markdown", reply_markup=_STATS_KB)


@router.callback_query(F.data.startswith("queue:"))
async def cb_queue(callback: CallbackQuery) -> None:
    await callback.answer()
    uid    = callback.from_user.id if callback.from_user else 0
    action = (callback.data or "").removeprefix("queue:")
    msg    = callback.message

    if action == "total":
        total   = store.total_phones()
        q_size  = store.queue_size()
        in_proc = 1 if store.active else 0
        await _edit_or_answer(
            msg,
            f"📊 *Всего номеров в очереди*\n\n"
            f"Зарегистрировано номеров: *{total}*\n"
            f"В очереди ожидания: *{q_size}*\n"
            f"В обработке: *{in_proc}*",
            parse_mode="Markdown",
            reply_markup=_QUEUE_KB,
        )

    elif action == "mine":
        owner     = store.owners.get(uid)
        phones    = owner.phones if owner else []
        in_queue  = sum(1 for r in store.queue if r.owner_id == uid)
        in_active = 1 if store.active and store.active.owner_id == uid else 0
        # Показываем только номера, ожидающие обработки в очереди
        waiting = [p for p in phones if store.phone_status(p) == "queued"]
        lines = [
            f"📋 *Мои номера в очереди*\n",
            f"Всего номеров: *{len(phones)}* · В очереди: *{in_queue}* · В обработке: *{in_active}*",
        ]
        if waiting:
            lines.append("")
            for phone in waiting:
                pos = store.queue_position(phone)
                lines.append(f"📥 `{mask_phone(phone)}` — позиция *{pos}*")
        else:
            lines.append("\n_Нет номеров в ожидании_")
        await _edit_or_answer(msg, "\n".join(lines), parse_mode="Markdown",
                              reply_markup=_QUEUE_KB)


@router.callback_query(F.data.startswith("req_type:"))
async def cb_req_type(callback: CallbackQuery, bot: Bot) -> None:
    await callback.answer()
    uid    = callback.from_user.id if callback.from_user else 0
    if not is_admin(uid):
        return
    action = (callback.data or "").removeprefix("req_type:")
    msg    = callback.message

    if action == "sms":
        kb = owners_request_inline()
        if not kb:
            await _edit_or_answer(
                msg,
                "📭 *Нет номеров в очереди*\n\nВсе номера уже обработаны или ещё не добавлены в очередь.",
                parse_mode="Markdown",
                reply_markup=_REQUEST_KB,
            )
            return
        await _edit_or_answer(msg, "📥 *Номера в очереди:*", parse_mode="Markdown", reply_markup=kb)
    elif action == "qr":
        await _edit_or_answer(msg, "📷 *QR-режим*\n\nФункция в разработке.",
                              parse_mode="Markdown", reply_markup=_REQUEST_KB)


@router.callback_query(F.data.startswith("settings:"))
async def cb_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    uid    = callback.from_user.id if callback.from_user else 0
    if not is_admin(uid):
        return
    action = (callback.data or "").removeprefix("settings:")
    msg    = callback.message

    if action == "price":
        await state.set_state(AdminSettings.waiting_price)
        result = await _edit_or_answer(msg, "💰 Введите новый текст прайса:",
                                       reply_markup=_SETTINGS_KB)
        if result:
            await state.update_data(prompt_msg_id=result.message_id,
                                    prompt_chat_id=result.chat.id)

    elif action == "work":
        await _edit_or_answer(msg, "🔄 Выберите статус работы:", reply_markup=work_inline())

    elif action == "msg":
        await state.set_state(AdminSettings.waiting_broadcast)
        result = await _edit_or_answer(msg, "📢 Введите сообщение для рассылки:",
                                       reply_markup=_SETTINGS_KB)
        if result:
            await state.update_data(prompt_msg_id=result.message_id,
                                    prompt_chat_id=result.chat.id)

    elif action == "reset_queue":
        await _cancel_timer()

        # Собираем уникальных владельцев, которых нужно уведомить
        owners_to_notify: set[int] = set()
        if store.active:
            owners_to_notify.add(store.active.owner_id)
        for req in store.queue:
            owners_to_notify.add(req.owner_id)

        had_active   = store.active is not None
        queue_count  = len(store.queue)

        # Чистим состояние до уведомлений, чтобы не было гонок
        store.active = None
        store.queue.clear()
        store.save()

        # Уведомляем каждого владельца один раз
        for owner_id in owners_to_notify:
            try:
                await callback.bot.send_message(
                    owner_id,
                    "🗑 Очередь полностью сброшена администратором.\n"
                    "Все запросы кода отменены.",
                )
            except Exception:
                pass

        await _edit_or_answer(
            msg,
            f"🗑 *Очередь сброшена*\n\n"
            f"Активный запрос отменён: *{'да' if had_active else 'нет'}*\n"
            f"Удалено из очереди: *{queue_count}*\n"
            f"Уведомлено владельцев: *{len(owners_to_notify)}*",
            parse_mode="Markdown",
            reply_markup=_SETTINGS_KB,
        )

    elif action == "back":
        await _show_settings(msg)


@router.callback_query(F.data.startswith("req:"))
async def cb_request_code(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    phone = normalize_phone((callback.data or "").removeprefix("req:"))
    if not phone:
        await callback.answer("Некорректный номер", show_alert=True)
        return
    await callback.answer()
    await _admin_take_phone(bot, callback.from_user.id, phone, callback.message)


@router.callback_query(F.data.startswith("work:"))
async def cb_work_toggle(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только для администратора", show_alert=True)
        return
    value = (callback.data or "").removeprefix("work:")
    store.set_bot_status("включён" if value == "on" else "выключен")
    icon = "✅" if value == "on" else "🔴"
    await callback.answer(f"{icon} {store.bot_status}")
    if callback.message:
        await _edit_or_answer(
            callback.message,
            f"🔄 Выберите статус работы:\n\nТекущий: {icon} {store.bot_status}",
            reply_markup=work_inline(),
        )


# ─── Команды (текстовые, для удобства) ──────────────────────────────────────

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
                await message.bot.send_message(was_active.owner_id, "Запрос кода отменён.")
            except Exception:
                pass
        await message.answer(
            f"Отменено: {cancelled}." if cancelled else "Очередь пуста.",
            reply_markup=admin_menu_inline(),
        )
        if store.active is None and store.queue:
            await _process_next_in_queue(message.bot, uid or 0)
        return
    req = store.get_pending(uid or 0)
    if not req:
        await message.answer("Нет активного запроса.", reply_markup=_OWNER_MENU_KB)
        return
    await _finish_active(message.bot, reason="decline", message=message)


@router.message(Command("decline"))
async def cmd_decline(message: Message, bot: Bot) -> None:
    uid = message.from_user.id if message.from_user else 0
    if not store.get_pending(uid):
        await message.answer("Нет активного запроса.", reply_markup=_OWNER_MENU_KB)
        return
    await _finish_active(bot, reason="decline", message=message)


@router.message(Command("code"))
async def cmd_code(message: Message, command: CommandObject, bot: Bot) -> None:
    uid = message.from_user.id if message.from_user else 0
    req = store.get_pending(uid)
    if not req:
        await message.answer("Нет запроса кода.", reply_markup=_OWNER_MENU_KB)
        return
    if not command.args:
        await message.answer(f"Пример: /code {'1' * CODE_LEN}", reply_markup=_OWNER_MENU_KB)
        return
    code = parse_code(command.args)
    if not code:
        await message.answer(f"Код — ровно {CODE_LEN} цифр.", reply_markup=_OWNER_MENU_KB)
        return
    await message.answer("✅ Код отправлен администратору.", reply_markup=_OWNER_MENU_KB)
    await _forward_code_to_admin(bot, req, code)


# ─── FSM: ввод настроек ─────────────────────────────────────────────────────

@router.message(AdminSettings.waiting_price, F.text)
async def settings_price_input(message: Message, state: FSMContext) -> None:
    store.set_price(message.text or "")
    data          = await state.get_data()
    prompt_msg_id = data.get("prompt_msg_id")
    prompt_chat   = data.get("prompt_chat_id", message.chat.id)
    await state.clear()
    log.info("Прайс обновлён: %s", store.price)
    icon = "✅" if is_bot_active() else "🔴"
    result = f"⚙️ *Настройки*\n\nСтатус: {icon} {store.bot_status}\nПрайс: {store.price}"
    if prompt_msg_id:
        try:
            await message.bot.edit_message_text(
                result, chat_id=prompt_chat, message_id=prompt_msg_id,
                parse_mode="Markdown", reply_markup=_SETTINGS_KB,
            )
            return
        except Exception:
            pass
    await message.answer(result, parse_mode="Markdown", reply_markup=_SETTINGS_KB)


@router.message(AdminSettings.waiting_broadcast, F.text)
async def settings_broadcast_input(message: Message, state: FSMContext, bot: Bot) -> None:
    text      = message.text or ""
    sender_id = message.from_user.id if message.from_user else 0
    data          = await state.get_data()
    prompt_msg_id = data.get("prompt_msg_id")
    prompt_chat   = data.get("prompt_chat_id", message.chat.id)
    ok = fail = 0
    for uid in store.all_users - {sender_id}:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            fail += 1
    await state.clear()
    result = f"📢 Рассылка завершена: {ok} доставлено, {fail} ошибок."
    if prompt_msg_id:
        try:
            await message.bot.edit_message_text(
                result, chat_id=prompt_chat, message_id=prompt_msg_id,
                reply_markup=_SETTINGS_KB,
            )
            return
        except Exception:
            pass
    await message.answer(result, reply_markup=_SETTINGS_KB)


# ─── Обработчик текста ──────────────────────────────────────────────────────

@router.message(F.text)
async def on_text(message: Message, bot: Bot, state: FSMContext) -> None:
    if await state.get_state():
        return
    uid = message.from_user.id if message.from_user else 0

    if not is_admin(uid) and not is_bot_active():
        await message.answer("🔴 Бот временно выключен.\nПопробуйте позже.")
        return

    req = store.get_pending(uid)
    if req and message.text:
        code = parse_code(message.text)
        if not code:
            await message.answer(f"Код — {CODE_LEN} цифр (например 123456). Отказ: /decline")
            return
        await message.answer("✅ Код отправлен администратору.", reply_markup=_OWNER_MENU_KB)
        await _forward_code_to_admin(bot, req, code)
        return

    if is_admin(uid) or can_be_owner(uid):
        await _send_welcome(message, uid)


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
    # Запуск на Railway определяется наличием RAILWAY_ENVIRONMENT.
    # Локально бот не стартует — требуется ALLOW_LOCAL=1 в .env
    if not os.getenv("RAILWAY_ENVIRONMENT") and not os.getenv("ALLOW_LOCAL"):
        raise SystemExit(
            "⛔ Локальный запуск заблокирован.\n"
            "Бот работает на Railway. Если нужен локальный запуск — "
            "добавьте ALLOW_LOCAL=1 в файл .env"
        )

    session   = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else AiohttpSession()
    bot       = Bot(token=BOT_TOKEN, session=session)
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    dp        = Dispatcher(storage=RedisStorage.from_url(redis_url))
    # Два отдельных экземпляра — независимые счётчики и разные интервалы:
    # сообщения защищены от спама (1.0с), кнопки реагируют быстро (0.3с)
    dp.message.middleware(ThrottleMiddleware(rate=RATE_LIMIT))
    dp.callback_query.middleware(ThrottleMiddleware(rate=CALLBACK_RATE))
    dp.include_router(router)

    # Сбрасываем webhook и накопившиеся обновления — гарантируем единственный экземпляр
    await bot.delete_webhook(drop_pending_updates=True)
    # Единственная команда в меню — /start
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    log.info("Бот запущен. Админы: %s", ADMIN_IDS)
    await _recover_queue_on_startup(bot)
    await dp.start_polling(bot, drop_pending_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
