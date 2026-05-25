# Запуск бота в облаке (Railway)

Облако решает проблему блокировки `api.telegram.org` на вашем ПК — сервер за рубежом видит Telegram API.

Рекомендуем **[Railway](https://railway.app)** (есть бесплатные кредиты, деплой за 10 минут).

## Что понадобится

- Аккаунт [GitHub](https://github.com)
- Аккаунт [Railway](https://railway.app) (вход через GitHub)
- Токен бота и ваш `ADMIN_USER_IDS` из `.env`

## Шаг 1. Загрузить код на GitHub

В PowerShell:

```powershell
cd "c:\Users\Пользователь\Desktop\Димон\it\cursor code\telegram-max-relay-bot"

git init
git add bot.py storage.py requirements.txt Dockerfile railway.toml render.yaml .env.example .gitignore README.md DEPLOY.md
git commit -m "Telegram MAX relay bot"
```

На GitHub: **New repository** → имя, например `telegram-max-relay-bot` → без README.

```powershell
git remote add origin https://github.com/ВАШ_ЛОГИН/telegram-max-relay-bot.git
git branch -M main
git push -u origin main
```

Файл `.env` в git **не попадает** (в `.gitignore`) — это правильно.

## Шаг 2. Проект в Railway

1. Откройте [railway.app](https://railway.app) → **New Project**
2. **Deploy from GitHub repo** → выберите `telegram-max-relay-bot`
3. Railway соберёт Docker-образ автоматически

## Шаг 3. Переменные окружения

В проекте Railway: сервис → **Variables** → **Add variables**:

| Переменная | Значение |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | токен от @BotFather |
| `ADMIN_USER_IDS` | ваш Telegram ID (из `/myid`) |
| `DATA_DIR` | `/data` |

Опционально: `OWNER_USER_IDS` — если нужен whitelist владельцев.

**Deploy** перезапустится сам.

## Шаг 4. Диск для данных (номера владельцев)

Без диска при перезапуске пропадут зарегистрированные номера.

1. В сервисе → **Volumes** → **Add Volume**
2. Mount path: `/data`
3. Redeploy

## Шаг 5. Проверка

1. **Deployments** → логи: должны быть строки `Бот запущен` и `Start polling`
2. В Telegram напишите боту `/start`

## Альтернатива: Render

1. [render.com](https://render.com) → **New** → **Background Worker**
2. Подключите GitHub-репозиторий
3. Runtime: **Docker**
4. Те же переменные: `TELEGRAM_BOT_TOKEN`, `ADMIN_USER_IDS`, `DATA_DIR=/data`
5. На бесплатном тарифе воркер может «засыпать» — для постоянной работы лучше Railway или платный план

## Обновление бота

После изменений в коде:

```powershell
git add -A
git commit -m "update"
git push
```

Railway/Render пересоберут и перезапустят сервис.

## Стоимость

- Railway: ~$5 бесплатных кредитов в месяц (хватает на лёгкий бот)
- Render Free: ограничения по uptime

## Локальный запуск не нужен

После деплоя в облако `python bot.py` на ПК можно не запускать — бот работает на сервере 24/7.
