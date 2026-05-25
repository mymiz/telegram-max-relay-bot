"""
Следит за изменениями *.py файлов, проверяет синтаксис и делает auto-commit + push.
Railway автоматически деплоит при каждом пуше в GitHub.

Запуск: python watch_and_push.py
"""

from __future__ import annotations

import py_compile
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from watchdog.events import FileModifiedEvent, FileSystemEventHandler
from watchdog.observers import Observer

WATCH_DIR = Path(__file__).resolve().parent
WATCH_EXTENSIONS = {".py"}
DEBOUNCE_SEC = 3.0

# Файлы, которые не триггерят пуш
IGNORE_FILES = {Path(__file__).name, "watch_and_push.py"}


def run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=WATCH_DIR)
    return result.returncode, (result.stdout + result.stderr).strip()


def check_syntax(path: Path) -> bool:
    try:
        py_compile.compile(str(path), doraise=True)
        return True
    except py_compile.PyCompileError as e:
        print(f"  [ERR] Синтаксическая ошибка: {e}")
        return False


def git_push(changed_file: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_msg = f"auto: {changed_file} [{now}]"

    code, out = run(["git", "add", "-A"])
    if code != 0:
        print(f"  [ERR] git add failed: {out}")
        return

    code, out = run(["git", "diff", "--cached", "--quiet"])
    if code == 0:
        print("  [INFO] Нет изменений для коммита.")
        return

    code, out = run(["git", "commit", "-m", commit_msg])
    if code != 0:
        print(f"  [ERR] git commit failed: {out}")
        return
    print(f"  [OK] Коммит: {commit_msg}")

    code, out = run(["git", "push"])
    if code != 0:
        print(f"  [ERR] git push failed: {out}")
        return
    print("  [PUSH] Запушено в GitHub -> Railway деплоит автоматически")


class ChangeHandler(FileSystemEventHandler):
    def __init__(self) -> None:
        self._pending: dict[str, float] = {}

    def on_modified(self, event: FileModifiedEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        path = Path(str(event.src_path))
        if path.suffix not in WATCH_EXTENSIONS:
            return
        if path.name in IGNORE_FILES:
            return
        self._pending[str(path)] = time.monotonic()

    def flush_pending(self) -> None:
        now = time.monotonic()
        to_process = [
            p for p, ts in list(self._pending.items())
            if now - ts >= DEBOUNCE_SEC
        ]
        for p in to_process:
            del self._pending[p]
            path = Path(p)
            print(f"\n[CHANGE] {path.name}")
            if not check_syntax(path):
                print("  [SKIP] Пуш отменён -- исправьте ошибку.")
                continue
            print("  [OK] Синтаксис OK")
            git_push(path.name)


def main() -> None:
    print(f"[WATCH] Слежу за *.py в {WATCH_DIR}")
    print("        Сохраните файл -- автоматически проверю синтаксис и запушу.\n")

    handler = ChangeHandler()
    observer = Observer()
    observer.schedule(handler, str(WATCH_DIR), recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(0.5)
            handler.flush_pending()
    except KeyboardInterrupt:
        print("\n[STOP] Остановлено.")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    sys.exit(main())
