# Telegram → MAX: передача кода входа

## Файлы в проекте

```
bot.py          ← логика бота
storage.py      ← хранение номеров
.env            ← токен и ваш ID (создайте сами)
deploy/         ← Docker, Railway, инструкции (редко нужны)
```

## Быстрый старт (локально)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r deploy\requirements.txt
copy deploy\.env.example .env
# заполните .env, затем:
python bot.py
```

Облако: **[deploy/DEPLOY.md](deploy/DEPLOY.md)**

## Кнопки и команды

В боте есть меню с кнопками (📋 🔐 📱 🚫 🆔 🏠). Команды тоже работают.

**Владелец:** 📱 Регистрация (только +7…) · код **ровно 6 цифр** · 🚫 Отказаться

**Админ:** 📋 Владельцы · 🔐 Запросить код · ❌ Отменить · 🏠 Меню

## Правки кода

- Меняете **`bot.py`** и **`storage.py`**
- В облаке: `git push` → Railway обновит бот (~1–3 мин)
- Локально с автоперезапуском: `watchfiles "python bot.py" bot.py storage.py`
- Не запускайте бота на ПК и в Railway одновременно (один токен)

## Данные

`data/store.json` — номера владельцев (создаётся сам).
