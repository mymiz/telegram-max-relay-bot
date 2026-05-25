"""Простое JSON-хранилище владельцев, очереди и активного запроса кода."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

# Локально: ./data  |  В облаке (Railway volume): /data
DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent / "data"))
STORE_FILE = DATA_DIR / "store.json"


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
        self._load()

    def _load(self) -> None:
        if not STORE_FILE.exists():
            return
        raw = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        for uid, o in raw.get("owners", {}).items():
            phones: list[str] = []
            if "phones" in o and isinstance(o["phones"], list):
                phones = [p for p in o["phones"] if p]
            elif o.get("phone"):
                phones = [o["phone"]]
            self.owners[int(uid)] = Owner(
                user_id=int(o["user_id"]),
                phones=phones,
                name=o.get("name"),
                username=o.get("username"),
            )

        for uid, p in raw.get("profiles", {}).items():
            self.profiles[int(uid)] = UserProfile(
                user_id=int(p.get("user_id", uid)),
                balance=float(p.get("balance", 0)),
                codes_ok=int(p.get("codes_ok", 0)),
                codes_fail=int(p.get("codes_fail", 0)),
                name=p.get("name"),
                username=p.get("username"),
                created_at=float(p.get("created_at", time.time())),
            )

        if raw.get("active"):
            self.active = self._req_from_dict(raw["active"])
        self.queue = [self._req_from_dict(r) for r in raw.get("queue", [])]

        # Старый формат: pending → в очередь
        for p in raw.get("pending", {}).values():
            req = self._req_from_dict(p)
            if self._phone_taken(req.phone):
                continue
            if self.active is None:
                self.active = req
            else:
                self.queue.append(req)

    @staticmethod
    def _req_from_dict(d: dict[str, Any]) -> CodeRequest:
        return CodeRequest(
            owner_id=int(d["owner_id"]),
            admin_id=int(d["admin_id"]),
            phone=d["phone"],
            created_at=float(d.get("created_at", time.time())),
            expires_at=float(d.get("expires_at", 0)),
        )

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "owners": {str(k): asdict(v) for k, v in self.owners.items()},
            "profiles": {str(k): asdict(v) for k, v in self.profiles.items()},
            "active": asdict(self.active) if self.active else None,
            "queue": [asdict(r) for r in self.queue],
        }
        STORE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def total_phones(self) -> int:
        return sum(len(o.phones) for o in self.owners.values())

    def owner_by_phone(self, phone: str) -> Owner | None:
        for owner in self.owners.values():
            if phone in owner.phones:
                return owner
        return None

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
        self.save()
        return profile

    def add_balance(self, user_id: int, amount: float) -> float:
        profile = self.get_profile(user_id)
        profile.balance = round(profile.balance + amount, 2)
        self.save()
        return profile.balance

    def record_code_success(self, user_id: int, reward: float) -> UserProfile:
        profile = self.get_profile(user_id)
        profile.codes_ok += 1
        profile.balance = round(profile.balance + reward, 2)
        self.save()
        return profile

    def record_code_fail(self, user_id: int) -> UserProfile:
        profile = self.get_profile(user_id)
        profile.codes_fail += 1
        self.save()
        return profile

    def register_owner(
        self,
        user_id: int,
        phone: str,
        *,
        name: str | None = None,
        username: str | None = None,
    ) -> tuple[Owner, bool]:
        self.touch_profile(user_id, name=name, username=username)
        if user_id in self.owners:
            owner = self.owners[user_id]
            if phone in owner.phones:
                return owner, True
            owner.phones.append(phone)
            if name:
                owner.name = name
            if username:
                owner.username = username
            self.save()
            return owner, False

        owner = Owner(
            user_id=user_id,
            phones=[phone],
            name=name,
            username=username,
        )
        self.owners[user_id] = owner
        self.save()
        return owner, False

    def _phone_taken(self, phone: str) -> bool:
        if self.active and self.active.phone == phone:
            return True
        return any(r.phone == phone for r in self.queue)

    def phone_status(self, phone: str) -> Literal["free", "active", "queued"] | None:
        if self.active and self.active.phone == phone:
            return "active"
        if any(r.phone == phone for r in self.queue):
            return "queued"
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
