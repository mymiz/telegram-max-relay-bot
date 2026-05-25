"""Простое JSON-хранилище владельцев и активных запросов кода."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
    phone: str
    name: str | None = None
    username: str | None = None


@dataclass
class CodeRequest:
    owner_id: int
    admin_id: int
    phone: str


class Store:
    def __init__(self) -> None:
        self.owners: dict[int, Owner] = {}
        self.pending: dict[int, CodeRequest] = {}
        self._load()

    def _load(self) -> None:
        if not STORE_FILE.exists():
            return
        raw = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        for uid, o in raw.get("owners", {}).items():
            self.owners[int(uid)] = Owner(
                user_id=int(o["user_id"]),
                phone=o["phone"],
                name=o.get("name"),
                username=o.get("username"),
            )
        for oid, p in raw.get("pending", {}).items():
            self.pending[int(oid)] = CodeRequest(
                owner_id=int(p["owner_id"]),
                admin_id=int(p["admin_id"]),
                phone=p["phone"],
            )

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "owners": {
                str(k): asdict(v) for k, v in self.owners.items()
            },
            "pending": {
                str(k): asdict(v) for k, v in self.pending.items()
            },
        }
        STORE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def owner_by_phone(self, phone: str) -> Owner | None:
        for owner in self.owners.values():
            if owner.phone == phone:
                return owner
        return None

    def register_owner(
        self,
        user_id: int,
        phone: str,
        *,
        name: str | None = None,
        username: str | None = None,
    ) -> Owner:
        owner = Owner(
            user_id=user_id,
            phone=phone,
            name=name,
            username=username,
        )
        self.owners[user_id] = owner
        self.save()
        return owner

    def set_pending(self, request: CodeRequest) -> None:
        self.pending[request.owner_id] = request
        self.save()

    def clear_pending(self, owner_id: int) -> CodeRequest | None:
        req = self.pending.pop(owner_id, None)
        if req is not None:
            self.save()
        return req

    def get_pending(self, owner_id: int) -> CodeRequest | None:
        return self.pending.get(owner_id)
