# Эксплуатация Navbat: рубильники и аварийные процедуры

Адресат — владелец системы (и обученный админ клиники). Все команды
админ-чата работают только из чатов в `clinic.tg_admin_chat_ids`.

## Рубильники (kill-switch, C-4)

### Пауза бота клиники — `/pause` / `/resume`

Когда: ремонт, форс-мажор, клиника просит «выключите на день».

- `/pause [причина]` в админ-чате — бот перестаёт вести диалоги.
  Пациент на любое сообщение/кнопку получает вежливое
  «запись временно приостановлена, позвоните в клинику» (ru/uz).
- Что ПРОДОЛЖАЕТ работать: напоминания о существующих записях,
  все админ-команды (/stats, /release, /resume и пр.), очередь
  (сообщения не теряются, эскалации не плодятся).
- `/resume` — бот снова принимает запись. Рестарт процесса не нужен.

### LLM выключен, кнопки живут — `/llm off` / `/llm on`

Когда: дрифт NLU, подозрительный расход токенов, деградация модели.

- `/llm off` — свободный текст пациентов больше не уходит в LLM:
  бот отвечает «запись принимается через кнопки меню» + меню.
  Кнопочные сценарии (Записаться/Перенести/Отменить/Цены/Язык)
  работают ПОЛНОСТЬЮ — они не используют NLU.
- Это режим, не сбой: счётчик сбоев не растёт, эскалаций нет.
- `/llm on` — вернуть. Рестарт не нужен (флаг читается на каждый вызов).

### Глобальный рубильник LLM — env

Когда: инцидент на стороне провайдера/бюджета, выключить ВСЕ клиники.

1. В `deploy/.env` поставить `NAVBAT_LLM_DISABLED=1`.
2. `docker compose -f docker-compose.prod.yml restart app`
   (каждому app-контейнеру, если клиник несколько).

Поведение пациента — то же, что при `/llm off`. Вернуть: убрать
переменную и снова restart.

### Полная остановка бота

```bash
docker compose -f docker-compose.prod.yml stop app
```

Очередь durable (Postgres): апдейты Telegram доживут до старта —
webhook-доставку Telegram повторяет, polling дочитает с offset.
Поднять обратно: `docker compose -f docker-compose.prod.yml up -d app`.

## Симптом → действие

| Симптом | Действие |
|---|---|
| Клиника просит выключить бота | `/pause причина` в админ-чате |
| NLU несёт чушь / дрифт-алерт | `/llm off`, разбираться без давления |
| Алерт «дневной лимит токенов исчерпан» | штатно: бот эскалирует до конца дня; при подозрении на спам — `/pause` |
| Расход токенов аномальный на всех клиниках | `NAVBAT_LLM_DISABLED=1` + restart app |
| «Системный алерт: cert истекает» | проверить certbot-контейнер: `docker compose logs certbot` |
| «Системный алерт: синк календаря не работает» | `docker compose exec app python -m navbat --check`; протух токен → `python -m navbat.calendar.auth` |
| Апдейт в dead letter | смотреть JSON-логи app на ERROR вокруг update_id из алерта |

## Восстановление из бэкапа

Процедура прогнана руками 10.06.2026 на локальном прод-стенде:
**RTO = 24 секунды** (от старта восстановления до проверенных данных;
данные, записанные ПОСЛЕ базового бэкапа, доехали из WAL-архива).

Как устроено (C-5):
- postgres пишет каждый закрытый WAL-сегмент в volume `wal_archive`
  (`archive_command` с защитой от перезаписи);
- сервис `backup` раз в 2 ч (`NAVBAT_BACKUP_INTERVAL_SEC`) снимает
  `pg_basebackup -Ft -z` в volume `backups`, хранит последние 12
  (`NAVBAT_BACKUP_KEEP`); см. `deploy/scripts/backup-loop.sh`;
- restore = свежий том ← base.tar.gz + доигрывание WAL из архива.

ВАЖНО: бэкап БЕЗ `NAVBAT_ENC_KEY` бесполезен — имена пациентов и токены
ботов зашифрованы этим ключом. Храни копию ключа (и всего `deploy/.env`)
вне сервера. S3-выгрузка бэкапов включается переменной
`NAVBAT_BACKUP_RCLONE_REMOTE` (нужен rclone в образе backup-сервиса);
пока хранилища нет — тома живут на той же машине, что и БД.

### Процедура (все команды из `deploy/`)

1. Остановить и удалить контейнеры БД, убить повреждённый том
   (часы RTO пошли):

```bash
docker compose -f docker-compose.prod.yml rm -f -s app postgres backup
docker volume rm deploy_pgdata
```

(`rm -f -s`, не `stop`: остановленный контейнер держит том,
`volume rm` иначе откажет с «volume is in use».)

2. Развернуть последний бэкап в свежий том + настроить recovery
   (named volumes безопасны на Windows-хосте, path-bind — нет):

```bash
docker run --rm \
  -v deploy_pgdata:/var/lib/postgresql/data \
  -v deploy_backups:/backups \
  -v deploy_wal_archive:/wal_archive \
  postgres:16-alpine sh -c "
set -e
LATEST=\$(ls -1d /backups/*/ | sort | tail -1)
echo restoring from \$LATEST
cd /var/lib/postgresql/data
tar xzf \$LATEST/base.tar.gz
mkdir -p pg_wal && tar xzf \$LATEST/pg_wal.tar.gz -C pg_wal
echo \"restore_command = 'cp /wal_archive/%f %p'\" >> postgresql.auto.conf
touch recovery.signal
chown -R postgres:postgres /var/lib/postgresql/data
chmod 700 /var/lib/postgresql/data"
```

Из PowerShell вложенные кавычки ломаются — передавать скрипт
одинарно-квотированной строкой, одинарная кавычка внутри printf
как `\047` (проверено на прогоне).

3. Поднять БД — она сама доиграет WAL и сделает promote:

```bash
docker compose -f docker-compose.prod.yml up -d postgres
# дождаться healthy, затем убедиться что recovery завершён:
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U postgres -tAc "SELECT pg_is_in_recovery()"   # → f
```

Предупреждение compose «volume already exists but was not created by
Docker Compose» безвредно: том создан `docker run` на шаге 2.

4. Поднять приложение и проверить:

```bash
docker compose -f docker-compose.prod.yml up -d app
docker compose -f docker-compose.prod.yml exec app python -m navbat --check
```

Все строки `[OK]` → восстановление завершено, часы RTO остановить.

### PITR — откат на момент времени

Когда: данные испорчены логически (ошибочная массовая отмена, кривой
скрипт) — восстановиться на момент ДО инцидента, а не на конец WAL.
На шаге 2 после строки `restore_command` добавить:

```bash
echo \"recovery_target_time = '2026-06-10 16:45:00+00'\" >> postgresql.auto.conf
echo \"recovery_target_action = 'promote'\" >> postgresql.auto.conf
```

Время — UTC, момент до инцидента, но после снятия базового бэкапа
(бэкап старше цели взять из `/backups`, каталоги именованы
`YYYYMMDD-HHMMSS` в UTC). Остальные шаги без изменений. Всё, что
записано после цели, будет потеряно — это и есть смысл операции.
