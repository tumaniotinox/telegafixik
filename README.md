# Автономный Telegram News Bot

Готовый бот, который:
- парсит новости с популярных сайтов по RSS;
- парсит посты из Telegram-каналов (через Telethon);
- генерирует посты (через OpenAI, либо fallback без ИИ);
- публикует в Telegram-канал автоматически каждые 5 минут (или другой интервал).

## 1) Что нужно от вас

Только значения в `.env`:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHANNEL_ID`
- `OPENAI_API_KEY` (необязательно, но рекомендуется для красивых постов)
- Для парсинга Telegram-каналов:
  - `TG_API_ID`
  - `TG_API_HASH`
  - `TG_PHONE`

## 2) Быстрый запуск

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py
```

## 3) Настройка Telegram

1. Создайте бота через [@BotFather](https://t.me/BotFather) и получите токен.
2. Создайте канал или используйте существующий.
3. Добавьте бота в админы канала с правом публикации.
4. В `.env`:
   - `TELEGRAM_CHANNEL_ID=@username_канала`  
   или ID вида `-100xxxxxxxxxx`.

## 4) Настройка источников

Список источников лежит в `sources.json`.  
Туда можно добавлять/удалять RSS-ссылки.

Список Telegram-каналов лежит в `tg_sources.json`.  
Поддерживаются форматы `@channel_name` и `https://t.me/channel_name`.

## 5) Первая авторизация Telethon

Для чтения Telegram-каналов нужен аккаунт Telegram (не бот):
1. Получите `api_id` и `api_hash` на [my.telegram.org](https://my.telegram.org).
2. Заполните `TG_API_ID`, `TG_API_HASH`, `TG_PHONE` в `.env`.
3. При первом запуске Telegram пришлет код.
4. Впишите код в `.env` как `TG_CODE=12345` и перезапустите `python main.py`.
5. Если включена 2FA, добавьте `TG_PASSWORD=...`.
6. После успешной авторизации можно удалить `TG_CODE` из `.env`.

## 6) Как работает

- При старте бот сразу делает 1 публикацию, если есть новая новость.
- Далее каждые `PUBLISH_INTERVAL_MINUTES` минут:
  - собирает свежие записи из RSS и Telegram-каналов;
  - выбирает первую непубликованную;
  - генерирует текст поста;
  - публикует в канал;
  - сохраняет ссылку в `state.json`, чтобы не дублировать.

## 7) Автозапуск на Windows (опционально)

Чтобы бот работал постоянно:
- запустите через `Task Scheduler` (Планировщик задач),
- либо как сервис через NSSM.

Простой вариант через `.bat`:

```bat
@echo off
cd /d C:\Users\admin\Desktop\telega
call .venv\Scripts\activate
python main.py
```

Сохраните как `run_bot.bat` и добавьте в Планировщик задач "При входе в систему".

## 8) Примечания

- Если `OPENAI_API_KEY` не указан, бот все равно будет публиковать посты (более простым шаблоном).
- Для стабильности держите 5-15 надежных RSS-источников.

## 9) Бесплатный запуск без ПК (GitHub Actions)

Можно запускать бот бесплатно каждые 5 минут без вашего компьютера.

### Шаги

1. Создайте приватный репозиторий на GitHub и загрузите туда проект.
2. В репозитории откройте `Settings -> Secrets and variables -> Actions -> New repository secret`.
3. Добавьте секреты:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHANNEL_ID`
   - `TG_API_ID`
   - `TG_API_HASH`
   - `TG_PHONE`
   - `TG_PASSWORD` (если 2FA включена)
   - `OPENAI_API_KEY` (опционально)
   - `OPENAI_MODEL` (опционально, например `gpt-4o-mini`)
4. Включите GitHub Actions в репозитории.
5. Запустите workflow `Telegram News Bot` вручную один раз (`Run workflow`), дальше он будет идти по расписанию каждые 5 минут.

### Как это работает

- Workflow запускает `python main.py` в режиме одного цикла (`RUN_ONCE=true`).
- Состояние (`state.json`) и Telethon-сессия (`tg_news_session.session`) сохраняются в artifacts и подтягиваются в следующий запуск.
- Благодаря этому бот помнит, что уже публиковал, и не просит авторизацию каждый раз.
