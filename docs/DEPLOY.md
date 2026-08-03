# Деплой Navbat: голый VPS → работающая клиника

Runbook самодостаточен: по нему систему разворачивает человек, не видевший
исходников. Эксплуатация после деплоя — `docs/OPERATIONS.md` (рубильники,
восстановление из бэкапа, ротация ключа).

Время на полный проход: ~1 час (без ожидания DNS и верификации Google).

## 0. Что нужно до начала

| Ресурс | Где взять | Зачем |
|---|---|---|
| VPS: 2 vCPU / 2 ГБ RAM / 20 ГБ SSD, Ubuntu 22.04+ | предпочтительно Ташкент (меньше latency до клиник) | вся система |
| Домен с A-записью на IP VPS | любой регистратор | HTTPS-webhook |
| Telegram-токен бота | @BotFather → /newbot | канал клиники |
| chat_id админ-чата клиники | @userinfobot в нужном чате/группе | эскалации, /stats |
| chat_id владельца системы (свой) | @userinfobot | системные алерты |
| Google OAuth client (Desktop) | console.cloud.google.com, опционально | календарь врача |

S3-хранилище для бэкапов — необязательно на старте, но «подключить внешнее
хранилище» — ПЕРВЫЙ шаг после деплоя: бэкапы на том же диске не защищают
от смерти диска.

## 1. VPS: Docker + репозиторий

```bash
ssh root@<IP>
apt-get update && apt-get install -y git curl
curl -fsSL https://get.docker.com | sh        # docker + compose-плагин
git clone https://github.com/RiobVO/Stomat.git /opt/navbat
cd /opt/navbat/deploy
```

## 2. Конфигурация: deploy/.env

```bash
cp .env.example .env
```

Заполнить (комментарии в файле):

- `NAVBAT_PG_PASSWORD`, `NAVBAT_APP_PASSWORD` — сгенерировать:
  `python3 -c "import secrets; print(secrets.token_urlsafe(24))"` (дважды).
- `NAVBAT_ENC_KEY` — `python3 -c "import os,base64;
  print(base64.b64encode(os.urandom(32)).decode())"`.
  **КОПИЮ — В МЕНЕДЖЕР ПАРОЛЕЙ ВНЕ СЕРВЕРА.** Без ключа бэкап БД
  не расшифровать (имена пациентов, токены ботов) — потеря ключа
  равна потере данных.
- `NAVBAT_DOMAIN` — домен из шага 0; `NAVBAT_WEBHOOK_URL=https://<домен>`.
- `NAVBAT_OWNER_CHAT_ID` — свой chat_id (алерты: cert, бэкапы, синк, дрифт).
- `OPENAI_API_KEY` (+ `GEMINI_API_KEY` для fallback-каскада);
  `NAVBAT_REAL=1` — боевой NLU.
- `NAVBAT_CLINIC_ID` — пока пусто, появится на шаге 4.
- `NAVBAT_IMAGE_TAG` — пока пусто (latest), см. шаг 8.

## 3. БД + миграции + первый smoke

```bash
docker compose -f docker-compose.prod.yml build app
docker compose -f docker-compose.prod.yml up -d postgres
docker compose -f docker-compose.prod.yml run --rm --entrypoint sh app \
    -c "alembic upgrade head && echo MIGRATIONS-OK"
```

Ожидаемо: `MIGRATIONS-OK`. Ошибка подключения → проверить пароли в `.env`.

## 4. Онбординг клиники

Все команды — через контейнер app (alias для удобства):

```bash
alias navbat='docker compose -f docker-compose.prod.yml run --rm --entrypoint python app -m'

navbat navbat.onboard --new-clinic "Стоматология Шифо"      # → UUID клиники
navbat navbat.onboard --clinic <UUID> --add-doctor "Азиза Каримова" \
    --schedule-json '{"mon":[["09:00","13:00"],["14:00","18:00"]],
                      "tue":[["09:00","13:00"],["14:00","18:00"]],
                      "wed":[["09:00","13:00"],["14:00","18:00"]],
                      "thu":[["09:00","13:00"],["14:00","18:00"]],
                      "fri":[["09:00","13:00"],["14:00","18:00"]],
                      "sat":[["09:00","14:00"]]}'
navbat navbat.onboard --clinic <UUID> --add-service cleaning --duration 40 --price 300000
# ... остальные услуги: extraction filling crown implant checkup xray braces whitening
navbat navbat.onboard --clinic <UUID> --tg-token "<токен @BotFather>" --admin-chat <chat_id>
navbat navbat.onboard --clinic <UUID> --list                # проверка
```

`NAVBAT_CLINIC_ID=<UUID>` — вписать в `.env`.

## 5. HTTPS: certbot + nginx (профиль web)

Первый выпуск cert — ДО старта nginx (ему нужен существующий файл):

```bash
docker compose -f docker-compose.prod.yml run --rm -p 80:80 \
    --entrypoint certbot certbot certonly --standalone \
    -d "$NAVBAT_DOMAIN" --agree-tos -m <свой email> --non-interactive
docker compose -f docker-compose.prod.yml --profile web up -d
```

Дальше certbot-контейнер продлевает cert сам (renew каждые 12 ч);
дни до истечения видны в /health, алерт — за 14 дней.

## 6. Запуск всей системы

```bash
docker compose -f docker-compose.prod.yml --profile web up -d
docker compose -f docker-compose.prod.yml ps    # app → healthy (~30 с)
docker compose -f docker-compose.prod.yml exec app sh -c \
    "wget -qO- http://127.0.0.1:8080/health"     # status: ok
navbat navbat --check                            # все [OK]
```

Контрольный диалог: написать боту /start с обычного аккаунта, пройти
запись кнопками до подтверждения. Чеклист 5 тест-диалогов — `docs/DEMO.md`.

## 7. Google Calendar (опционально, можно позже)

OAuth-авторизация интерактивна (браузер) — удобнее с локальной машины,
затем токен переносится онбордингом:

```bash
python -m navbat.calendar.auth          # локально; client_id/secret в env
navbat navbat.onboard --clinic <UUID> --doctor <doctor-UUID> --calendar <calendar-id>
navbat navbat.onboard --clinic <UUID> --import-calendar   # существующие записи
```

**Верификация Google-приложения — критично для прода.** Пока OAuth consent
screen в testing-режиме, refresh-токен живёт 7 дней — потом синк падает
с алертом «нужна переавторизация». Подать на верификацию (consent screen →
Publish app; ~неделя) сразу после первого подключения календаря.
Watch-каналы (push вместо поллинга) включаются автоматически — публичный
HTTPS уже есть.

## 8. Обновления и откат

Деплой новой версии — с тегом по git SHA, чтобы был путь назад:

```bash
cd /opt/navbat && git pull
TAG=$(git rev-parse --short HEAD)
NAVBAT_IMAGE_TAG=$TAG docker compose -f deploy/docker-compose.prod.yml build app
sed -i "s/^NAVBAT_IMAGE_TAG=.*/NAVBAT_IMAGE_TAG=$TAG/" deploy/.env
docker compose -f deploy/docker-compose.prod.yml --profile web up -d app
```

Откат = вернуть прежний тег в `.env` и снова `up -d app` (образ уже на
диске; миграции вперёд-совместимы, вниз не откатываются — откатывать
только код). Список локальных образов: `docker images navbat`.

## 9. Финальный чеклист

- [ ] `navbat navbat --check` — все [OK] (бот, админ-чат, календарь/без).
- [ ] `/health` изнутри — `"status": "ok"`, в checks есть `backups: ok`
      (первый бэкап появляется в течение пары минут после старта).
- [ ] Контрольная запись через бота прошла; алерт эскалации доходит
      в админ-чат; /stats отвечает.
- [ ] Копия `NAVBAT_ENC_KEY` (лучше — всего `.env`) сохранена вне сервера.
- [ ] Заявка на верификацию Google подана (если календарь подключён).
- [ ] В календаре (своём) — напоминание про ежемесячные restore-учения
      (`docs/OPERATIONS.md`, раздел «Восстановление из бэкапа»).
- [ ] Следующий шаг после стабилизации: внешнее S3-хранилище бэкапов
      (`NAVBAT_BACKUP_RCLONE_REMOTE`).
