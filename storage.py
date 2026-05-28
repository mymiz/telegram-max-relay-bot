"""SQLite-хранилище владельцев, очереди и активного запроса кода."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent / "data"))
DB_FILE = DATA_DIR / "store.db"
_JSON_FILE = DATA_DIR / "store.json"

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS owners (
    user_id   INTEGER PRIMARY KEY,
    name      TEXT,
    username  TEXT
);

CREATE TABLE IF NOT EXISTS owner_phones (
    owner_id  INTEGER NOT NULL,
    phone     TEXT    NOT NULL,
    PRIMARY KEY (owner_id, phone),
    FOREIGN KEY (owner_id) REFERENCES owners(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id      INTEGER PRIMARY KEY,
    balance      REAL    NOT NULL DEFAULT 0,
    codes_ok     INTEGER NOT NULL DEFAULT 0,
    codes_fail   INTEGER NOT NULL DEFAULT 0,
    total_earned REAL    NOT NULL DEFAULT 0,
    withdrawn    REAL    NOT NULL DEFAULT 0,
    name         TEXT,
    username     TEXT,
    created_at   REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS queue (
    pos        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL,
    phone      TEXT    NOT NULL,
    created_at REAL    NOT NULL DEFAULT 0,
    expires_at REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS active_request (
    lock       INTEGER PRIMARY KEY DEFAULT 1 CHECK(lock = 1),
    owner_id   INTEGER NOT NULL,
    admin_id   INTEGER NOT NULL,
    phone      TEXT    NOT NULL,
    created_at REAL    NOT NULL DEFAULT 0,
    expires_at REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS all_users (
    user_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS phone_cooldowns (
    phone        TEXT PRIMARY KEY,
    last_success TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS banned_phones (
    phone TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS phone_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER NOT NULL,
    phone        TEXT    NOT NULL,
    processed_at REAL    NOT NULL,
    status       TEXT    NOT NULL,
    date         TEXT    NOT NULL
);
"""


def normalize_phone(raw: str) -> str | None:
    """Только российские номера: +7 и 10 цифр после кода страны."""
    digits = re.sub(r"\D", "", raw.strip())
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"
    return None


@dataclass
class Owner:
    user_id: int
    phones: list[str] = field(default_factory=list)
    name: str | None = None
    username: str | None = None


@dataclass
class UserProfile:
    user_id: int
    balance: float = 0.0
    codes_ok: int = 0
    codes_fail: int = 0
    total_earned: float = 0.0
    withdrawn: float = 0.0
    name: str | None = None
    username: str | None = None
    created_at: float = field(default_factory=time.time)


@dataclass
class CodeRequest:
    owner_id: int
    admin_id: int
    phone: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0


class Store:
    def __init__(self) -> None:
        self.owners: dict[int, Owner] = {}
        self.profiles: dict[int, UserProfile] = {}
        self.active: CodeRequest | None = None
        self.queue: list[CodeRequest] = []
        self.bot_status: str = "включён"
        self.price: str = "не указан"
        self.all_users: set[int] = set()
        self.phone_cooldowns: dict[str, float] = {}  # phone -> Unix timestamp успеха
        self.banned_phones:   set[str] = set()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(DB_FILE), check_same_thread=False)
        self._db.executescript(_DDL)
        self._db.commit()
        # Миграция: добавляем колонку success_ts если её ещё нет
        try:
            self._db.execute(
                "ALTER TABLE phone_cooldowns ADD COLUMN success_ts REAL NOT NULL DEFAULT 0"
            )
            self._db.commit()
        except Exception:
            pass  # Колонка уже существует
        self._load()

    # ── загрузка ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Загружает данные из SQLite. При первом запуске мигрирует из JSON."""
        cur = self._db.execute("SELECT COUNT(*) FROM owners")
        has_data = cur.fetchone()[0] > 0

        if not has_data and _JSON_FILE.exists():
            self._migrate_from_json()
            return

        for row in self._db.execute("SELECT user_id, name, username FROM owners"):
            uid, name, username = row
            self.owners[uid] = Owner(user_id=uid, name=name, username=username)

        for row in self._db.execute("SELECT owner_id, phone FROM owner_phones"):
            owner_id, phone = row
            if owner_id in self.owners:
                self.owners[owner_id].phones.append(phone)

        for row in self._db.execute(
            "SELECT user_id, balance, codes_ok, codes_fail, total_earned, "
            "withdrawn, name, username, created_at FROM profiles"
        ):
            uid, bal, ok, fail, earned, withdrawn, name, uname, cat = row
            self.profiles[uid] = UserProfile(
                user_id=uid, balance=bal, codes_ok=ok, codes_fail=fail,
                total_earned=earned, withdrawn=withdrawn,
                name=name, username=uname, created_at=cat,
            )

        row = self._db.execute(
            "SELECT owner_id, admin_id, phone, created_at, expires_at "
            "FROM active_request WHERE lock=1"
        ).fetchone()
        if row:
            self.active = CodeRequest(*row)

        for row in self._db.execute(
            "SELECT owner_id, admin_id, phone, created_at, expires_at "
            "FROM queue ORDER BY pos"
        ):
            self.queue.append(CodeRequest(*row))

        row = self._db.execute(
            "SELECT value FROM settings WHERE key='bot_status'"
        ).fetchone()
        self.bot_status = row[0] if row else "включён"

        row = self._db.execute(
            "SELECT value FROM settings WHERE key='price'"
        ).fetchone()
        self.price = row[0] if row else "не указан"

        self.all_users = {
            r[0] for r in self._db.execute("SELECT user_id FROM all_users")
        }

        self.phone_cooldowns = {
            r[0]: r[1]
            for r in self._db.execute("SELECT phone, success_ts FROM phone_cooldowns")
            if r[1]  # пропускаем записи со старым форматом (ts = 0)
        }

        self.banned_phones = {
            r[0] for r in self._db.execute("SELECT phone FROM banned_phones")
        }

    def _migrate_from_json(self) -> None:
        """Однократная миграция из store.json → SQLite."""
        raw: dict[str, Any] = json.loads(_JSON_FILE.read_text(encoding="utf-8"))

        for uid, o in raw.get("owners", {}).items():
            phones: list[str] = []
            if "phones" in o and isinstance(o["phones"], list):
                phones = [p for p in o["phones"] if p]
            elif o.get("phone"):
                phones = [o["phone"]]
            owner = Owner(
                user_id=int(o["user_id"]),
                phones=phones,
                name=o.get("name"),
                username=o.get("username"),
            )
            self.owners[owner.user_id] = owner

        for uid, p in raw.get("profiles", {}).items():
            self.profiles[int(uid)] = UserProfile(
                user_id=int(p.get("user_id", uid)),
                balance=float(p.get("balance", 0)),
                codes_ok=int(p.get("codes_ok", 0)),
                codes_fail=int(p.get("codes_fail", 0)),
                total_earned=float(p.get("total_earned", 0)),
                withdrawn=float(p.get("withdrawn", 0)),
                name=p.get("name"),
                username=p.get("username"),
                created_at=float(p.get("created_at", time.time())),
            )

        if raw.get("active"):
            d = raw["active"]
            self.active = CodeRequest(
                owner_id=int(d["owner_id"]), admin_id=int(d["admin_id"]),
                phone=d["phone"],
                created_at=float(d.get("created_at", time.time())),
                expires_at=float(d.get("expires_at", 0)),
            )

        for r in raw.get("queue", []):
            self.queue.append(CodeRequest(
                owner_id=int(r["owner_id"]), admin_id=int(r["admin_id"]),
                phone=r["phone"],
                created_at=float(r.get("created_at", time.time())),
                expires_at=float(r.get("expires_at", 0)),
            ))

        # старый формат: pending → очередь / active
        for p in raw.get("pending", {}).values():
            req = CodeRequest(
                owner_id=int(p["owner_id"]), admin_id=int(p["admin_id"]),
                phone=p["phone"],
                created_at=float(p.get("created_at", time.time())),
                expires_at=float(p.get("expires_at", 0)),
            )
            if self._phone_taken(req.phone):
                continue
            if self.active is None:
                self.active = req
            else:
                self.queue.append(req)

        self.bot_status = raw.get("bot_status", "включён")
        self.price = raw.get("price", "не указан")
        self.all_users = {int(u) for u in raw.get("all_users", [])}

        self.save()

    # ── сохранение ────────────────────────────────────────────────────────────

    def _write_profile(self, p: "UserProfile") -> None:
        """Targeted upsert for a single profile — avoids full-table save."""
        with self._db:
            self._db.execute(
                """INSERT INTO profiles
                   (user_id, balance, codes_ok, codes_fail, total_earned,
                    withdrawn, name, username, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   balance=excluded.balance, codes_ok=excluded.codes_ok,
                   codes_fail=excluded.codes_fail, total_earned=excluded.total_earned,
                   withdrawn=excluded.withdrawn, name=excluded.name,
                   username=excluded.username, created_at=excluded.created_at""",
                (p.user_id, p.balance, p.codes_ok, p.codes_fail,
                 p.total_earned, p.withdrawn, p.name, p.username, p.created_at),
            )

    def _write_setting(self, key: str, value: str) -> None:
        """Targeted write for a single settings row."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)",
                (key, value),
            )

    def set_bot_status(self, value: str) -> None:
        self.bot_status = value
        self._write_setting("bot_status", value)

    def set_price(self, value: str) -> None:
        clean = value.strip()
        self.price = clean if clean else "не указан"
        self._write_setting("price", self.price)
        if self.price == "не указан":
            self.bot_status = "выключен"
            self._write_setting("bot_status", "выключен")

    def update_owner_info(self, uid: int, name: str | None, username: str | None) -> None:
        """Update owner name/username in memory and DB without a full save."""
        owner = self.owners.get(uid)
        if not owner:
            return
        owner.name = name
        owner.username = username
        with self._db:
            self._db.execute(
                "UPDATE owners SET name=?, username=? WHERE user_id=?",
                (name, username, uid),
            )

    def save(self) -> None:
        with self._db:
            self._db.execute("DELETE FROM owner_phones")
            self._db.execute("DELETE FROM owners")
            self._db.executemany(
                "INSERT INTO owners(user_id, name, username) VALUES(?,?,?)",
                [(o.user_id, o.name, o.username) for o in self.owners.values()],
            )
            self._db.executemany(
                "INSERT INTO owner_phones(owner_id, phone) VALUES(?,?)",
                [
                    (o.user_id, phone)
                    for o in self.owners.values()
                    for phone in o.phones
                ],
            )

            self._db.executemany(
                """INSERT INTO profiles
                   (user_id, balance, codes_ok, codes_fail, total_earned,
                    withdrawn, name, username, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                   balance=excluded.balance, codes_ok=excluded.codes_ok,
                   codes_fail=excluded.codes_fail, total_earned=excluded.total_earned,
                   withdrawn=excluded.withdrawn, name=excluded.name,
                   username=excluded.username, created_at=excluded.created_at""",
                [
                    (p.user_id, p.balance, p.codes_ok, p.codes_fail,
                     p.total_earned, p.withdrawn, p.name, p.username, p.created_at)
                    for p in self.profiles.values()
                ],
            )

            self._db.execute("DELETE FROM active_request")
            if self.active:
                a = self.active
                self._db.execute(
                    "INSERT INTO active_request"
                    "(lock, owner_id, admin_id, phone, created_at, expires_at)"
                    " VALUES(1,?,?,?,?,?)",
                    (a.owner_id, a.admin_id, a.phone, a.created_at, a.expires_at),
                )

            self._db.execute("DELETE FROM queue")
            self._db.executemany(
                "INSERT INTO queue(owner_id, admin_id, phone, created_at, expires_at)"
                " VALUES(?,?,?,?,?)",
                [
                    (r.owner_id, r.admin_id, r.phone, r.created_at, r.expires_at)
                    for r in self.queue
                ],
            )

            self._db.executemany(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)",
                [("bot_status", self.bot_status), ("price", self.price)],
            )

            self._db.executemany(
                "INSERT OR IGNORE INTO all_users(user_id) VALUES(?)",
                [(u,) for u in self.all_users],
            )

    # ── вспомогательные ───────────────────────────────────────────────────────

    def track_user(self, user_id: int) -> None:
        if user_id not in self.all_users:
            self.all_users.add(user_id)
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO all_users(user_id) VALUES(?)", (user_id,)
                )

    def total_phones(self) -> int:
        return sum(len(o.phones) for o in self.owners.values())

    def owner_by_phone(self, phone: str) -> Owner | None:
        """Ищет владельца по номеру: сначала в active/queue, затем в owner.phones."""
        if self.active and self.active.phone == phone:
            return self.owners.get(self.active.owner_id)
        for req in self.queue:
            if req.phone == phone:
                return self.owners.get(req.owner_id)
        for owner in self.owners.values():
            if phone in owner.phones:
                return owner
        return None

    def ensure_owner(
        self,
        user_id: int,
        *,
        name: str | None = None,
        username: str | None = None,
    ) -> Owner:
        """Создаёт или обновляет запись владельца — без добавления телефона в список."""
        profile = self.get_profile(user_id)
        if name:
            profile.name = name
        if username:
            profile.username = username
        self._write_profile(profile)
        if user_id in self.owners:
            owner = self.owners[user_id]
            if name:
                owner.name = name
            if username:
                owner.username = username
            self.update_owner_info(user_id, name, username)
            return owner
        owner = Owner(user_id=user_id, phones=[], name=name, username=username)
        self.owners[user_id] = owner
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO owners(user_id, name, username) VALUES(?,?,?)",
                (user_id, name, username),
            )
        return owner

    def get_today_successes(self) -> list[dict]:
        """Возвращает успешные номера за сегодня из phone_history."""
        today = date.today().isoformat()
        cur = self._db.execute(
            "SELECT phone, owner_id, processed_at FROM phone_history"
            " WHERE date=? AND status='success' ORDER BY processed_at",
            (today,),
        )
        return [{"phone": r[0], "owner_id": r[1], "processed_at": r[2]} for r in cur.fetchall()]

    def get_today_success_counts(self) -> dict[int, int]:
        """Возвращает dict owner_id → кол-во успешных номеров за сегодня."""
        today = date.today().isoformat()
        cur = self._db.execute(
            "SELECT owner_id, COUNT(*) FROM phone_history"
            " WHERE date=? AND status='success' GROUP BY owner_id",
            (today,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}

    def get_profile(self, user_id: int) -> UserProfile:
        if user_id not in self.profiles:
            owner = self.owners.get(user_id)
            self.profiles[user_id] = UserProfile(
                user_id=user_id,
                name=owner.name if owner else None,
                username=owner.username if owner else None,
            )
        return self.profiles[user_id]

    def touch_profile(
        self,
        user_id: int,
        *,
        name: str | None = None,
        username: str | None = None,
    ) -> UserProfile:
        profile = self.get_profile(user_id)
        if name:
            profile.name = name
        if username:
            profile.username = username
        self._write_profile(profile)
        return profile

    def add_balance(self, user_id: int, amount: float) -> float:
        profile = self.get_profile(user_id)
        profile.balance = round(profile.balance + amount, 2)
        self._write_profile(profile)
        return profile.balance

    def withdraw_balance(self, user_id: int, amount: float) -> float:
        profile = self.get_profile(user_id)
        profile.balance   = round(max(0.0, profile.balance - amount), 2)
        profile.withdrawn = round(profile.withdrawn + amount, 2)
        self._write_profile(profile)
        return profile.balance

    def record_code_success(self, user_id: int, reward: float) -> UserProfile:
        profile = self.get_profile(user_id)
        profile.codes_ok += 1
        profile.balance = round(profile.balance + reward, 2)
        profile.total_earned = round(profile.total_earned + reward, 2)
        self._write_profile(profile)
        return profile

    def record_code_fail(self, user_id: int) -> UserProfile:
        profile = self.get_profile(user_id)
        profile.codes_fail += 1
        self._write_profile(profile)
        return profile

    def register_owner(
        self,
        user_id: int,
        phone: str,
        *,
        name: str | None = None,
        username: str | None = None,
    ) -> tuple[Owner, bool]:
        profile = self.get_profile(user_id)
        if name:
            profile.name = name
        if username:
            profile.username = username

        if user_id in self.owners:
            owner = self.owners[user_id]
            if name:
                owner.name = name
            if username:
                owner.username = username
            already = phone in owner.phones
            if not already:
                owner.phones.append(phone)
            self.save()
            return owner, already

        owner = Owner(user_id=user_id, phones=[phone], name=name, username=username)
        self.owners[user_id] = owner
        self.save()
        return owner, False

    def _phone_taken(self, phone: str) -> bool:
        if self.active and self.active.phone == phone:
            return True
        return any(r.phone == phone for r in self.queue)

    def is_phone_on_cooldown(self, phone: str) -> bool:
        """True если номер уже был успешно обработан сегодня (UTC-сутки)."""
        ts = self.phone_cooldowns.get(phone)
        if not ts:
            return False
        return date.fromtimestamp(ts) == date.today()

    def record_phone_success(self, phone: str) -> None:
        """Фиксируем успешную обработку номера — следующая доступна завтра."""
        ts    = time.time()
        today = date.today().isoformat()
        self.phone_cooldowns[phone] = ts
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO phone_cooldowns(phone, last_success, success_ts)"
                " VALUES(?,?,?)",
                (phone, today, ts),
            )

    def reset_daily_phones(self) -> int:
        """Сбрасывает все телефоны владельцев, очередь и кулдауны. Возвращает кол-во удалённых номеров."""
        total = sum(len(o.phones) for o in self.owners.values())
        for owner in self.owners.values():
            owner.phones.clear()
        self.active = None
        self.queue.clear()
        self.phone_cooldowns.clear()
        with self._db:
            self._db.execute("DELETE FROM owner_phones")
            self._db.execute("DELETE FROM active_request")
            self._db.execute("DELETE FROM queue")
            self._db.execute("DELETE FROM phone_cooldowns")
        return total

    def record_phone_history(self, owner_id: int, phone: str, status: str) -> None:
        """Записывает итог обработки номера в историю."""
        ts  = time.time()
        day = date.today().isoformat()
        with self._db:
            self._db.execute(
                "INSERT INTO phone_history(owner_id, phone, processed_at, status, date)"
                " VALUES(?,?,?,?,?)",
                (owner_id, phone, ts, status, day),
            )

    def get_phone_history(self, owner_id: int) -> list[dict]:
        """Возвращает историю номеров владельца, от новых к старым."""
        cur = self._db.execute(
            "SELECT phone, processed_at, status, date FROM phone_history"
            " WHERE owner_id=? ORDER BY processed_at DESC",
            (owner_id,),
        )
        return [
            {"phone": r[0], "processed_at": r[1], "status": r[2], "date": r[3]}
            for r in cur.fetchall()
        ]

    def get_active_phones_for_owner(self, owner_id: int) -> list[tuple[str, float]]:
        """Возвращает (phone, success_ts) для номеров владельца на кулдауне (успешно обработаны сегодня)."""
        owner = self.owners.get(owner_id)
        if not owner:
            return []
        result = [
            (phone, self.phone_cooldowns.get(phone, 0.0))
            for phone in owner.phones
            if self.phone_status(phone) == "cooldown"
        ]
        return sorted(result, key=lambda x: x[1], reverse=True)

    def ban_phone(self, phone: str) -> None:
        self.banned_phones.add(phone)
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO banned_phones(phone) VALUES(?)", (phone,)
            )

    def is_phone_banned(self, phone: str) -> bool:
        return phone in self.banned_phones

    def phone_status(self, phone: str) -> Literal["free", "active", "queued", "cooldown", "banned"]:
        if phone in self.banned_phones:
            return "banned"
        if self.active and self.active.phone == phone:
            return "active"
        if any(r.phone == phone for r in self.queue):
            return "queued"
        if self.is_phone_on_cooldown(phone):
            return "cooldown"
        return "free"

    def queue_position(self, phone: str) -> int:
        for i, req in enumerate(self.queue, 1):
            if req.phone == phone:
                return i
        return 0

    def queue_size(self) -> int:
        return len(self.queue)

    def get_pending(self, owner_id: int) -> CodeRequest | None:
        if self.active and self.active.owner_id == owner_id:
            return self.active
        return None

    def set_active(self, req: CodeRequest, *, timeout_sec: int) -> None:
        req.expires_at = time.time() + timeout_sec
        self.active = req
        self.save()

    def try_set_active(self, req: CodeRequest, *, timeout_sec: int) -> bool:
        """Атомарно занять слот. Возвращает False если уже занят."""
        if self.active is not None:
            return False
        req.expires_at = time.time() + timeout_sec
        self.active = req
        self.save()
        return True

    def clear_active(self) -> CodeRequest | None:
        req = self.active
        self.active = None
        self.save()
        return req

    def push_queue(self, req: CodeRequest) -> int:
        """Добавить в очередь. Возвращает позицию (1-based)."""
        self.queue.append(req)
        self.save()
        return len(self.queue)

    def pop_next(self) -> CodeRequest | None:
        if not self.queue:
            return None
        req = self.queue.pop(0)
        self.save()
        return req

    def remove_phone(self, phone: str) -> CodeRequest | None:
        """Убрать номер из очереди. Возвращает удалённый запрос."""
        for i, req in enumerate(self.queue):
            if req.phone == phone:
                removed = self.queue.pop(i)
                self.save()
                return removed
        return None

    def cancel_all_for_admin(self, admin_id: int) -> tuple[int, CodeRequest | None]:
        """Сколько отменено; активный запрос админа (если был)."""
        cancelled = 0
        was_active: CodeRequest | None = None
        if self.active and self.active.admin_id == admin_id:
            was_active = self.clear_active()
            cancelled += 1
        before = len(self.queue)
        self.queue = [r for r in self.queue if r.admin_id != admin_id]
        cancelled += before - len(self.queue)
        self.save()
        return cancelled, was_active

    def seconds_left(self) -> int:
        if not self.active or not self.active.expires_at:
            return 0
        return max(0, int(self.active.expires_at - time.time()))
