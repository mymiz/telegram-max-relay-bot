# Запуск бота в облаке (Railway)

## Структура проекта

| Папка / файл | Назначение |
| --- | --- |
| `bot.py`, `storage.py` | Код бота (редактируете чаще всего) |
| `.env` | Секреты (в корне, не в git) |
| `deploy/` | Docker, облако, зависимости — редко трогаете |

## Шаг 1. GitHub

```powershell
cd "c:\Users\Пользователь\Desktop\Димон\it\cursor code\telegram-max-relay-bot"
git add -A
git commit -m "update"
git push
```

На GitHub в корне должны быть `bot.py` и папка `deploy/` с `Dockerfile`.

### «Failed to fetch repository files»

- Репозиторий на GitHub не пустой
- Railway → Root Directory: **пусто**
- Ветка: **main**

## Шаг 2. Railway

1. [railway.app](https://railway.app) → **Deploy from GitHub repo**
2. Dockerfile подхватится из `railway.toml` (`deploy/Dockerfile`)
3. **Variables**: `TELEGRAM_BOT_TOKEN`, `ADMIN_USER_IDS`, `DATA_DIR`=`/data`
4. **Volumes** → mount `/data`

## Шаг 3. Проверка

Логи: `Бот запущен`, `Start polling`. В Telegram: `/start`.

## Без GitHub

```powershell
cd "...\telegram-max-relay-bot"
npm install -g @railway/cli
railway login
railway init
railway up
```

## Обновления

Правите `bot.py` / `storage.py` → `git push` → Railway пересоберёт сам.
