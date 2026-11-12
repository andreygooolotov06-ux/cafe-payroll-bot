# Безкоштовний хостинг 24/7 (Neon + Render + cron-job.org)

Ця інструкція дозволяє перенести бота на безкоштовний цілодобовий хостинг без прив'язки до ноутбука:
- **БД**: PostgreSQL на **Neon** (безкоштовно)
- **Сервер бота + Mini App**: **Render** Free Web Service (Docker + webhook-режим + постійний HTTPS)
- **Прогрів / Uptime**: **cron-job.org** (безкоштовний пінг `/health` раз на 10 хв)

## 1. База даних у хмарі (Neon)
1. Зареєструйтесь на [neon.tech](https://neon.tech) і створіть безкоштовний проєкт.
2. Скопіюйте рядок підключення **Connection string** (`postgresql://...`).
3. Перенесіть усі поточні локальні дані з `data/cafe_payroll.db` у Neon:
   ```powershell
   .\.venv\Scripts\python.exe -m app.database.migrate --source data/cafe_payroll.db --target "postgresql://..."
   ```

## 2. Репозиторій GitHub
Завантажте папку проєкту в **приватний** репозиторій GitHub.
Файл `.gitignore` вже налаштовано так, щоб `.env` (з токеном бота) і локальна папка `data/` не потрапили в Git.

## 3. Запуск на Render
1. Зайдіть на [render.com](https://render.com) -> **New** -> **Blueprint** (підхопить готовий `render.yaml`) або **Web Service** (середовище **Docker**).
2. Підключіть свій приватний репозиторій GitHub.
3. У розділі **Environment** вкажіть змінні:
   - `BOT_TOKEN` — токен з `.env`
   - `DATABASE_URL` — рядок підключення Neon (`postgresql://...`)
   - `OWNER_IDS` та/або `OWNER_USERNAMES` — як у `.env`
   - (`TIMEZONE=Europe/Kyiv`, `BOT_MODE=webhook`, `MINIAPP_HOST=0.0.0.0`, `MINIAPP_AUTO_TUNNEL=false` уже прописані в `render.yaml`)
4. Після деплою Render видасть постійну HTTPS-адресу виду `https://cafe-payroll-bot.onrender.com`, сам активує Telegram Webhook і одразу обслуговуватиме Mini App без жодних тунелів.

> Важливо: перед фінальним запуском у хмарі зупиніть бота на ноутбуці (`Ctrl+C`), щоб лише хмарний сервер приймав повідомлення.

## 4. Захист від сну на Render Free (cron-job.org)
На безкоштовному тарифі Render сервіс засинає через 15 хвилин без зовнішніх запитів:
1. Зареєструйтесь на [cron-job.org](https://cron-job.org).
2. Створіть безкоштовний job з URL `https://<ваш-сервіс>.onrender.com/health` та інтервалом **кожні 10 хвилин**.
3. Маршрут `/health` не звертається до PostgreSQL, тому Render залишатиметься активним 24/7, а Neon при цьому спокійно «засинатиме» між реальними натисканнями користувачів і не витрачатиме ліміт годин.
