# C-5 Бэкапы + проверенный restore — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** WAL-архивирование + базовый бэкап раз в 2 ч + restore-процедура, прогнанная руками локально с измеренным RTO. Пятый инкремент по спеке `docs/superpowers/specs/2026-06-10-group-c-deploy-ops-design.md`.

**Architecture:** PITR-классика: postgres пишет WAL в отдельный volume (`archive_command` с защитой от перезаписи), sidecar раз в 2 ч снимает `pg_basebackup -Ft -z` (самодостаточный tar) и ротирует старые. Restore = чистый pgdata ← base.tar.gz + `restore_command` из WAL-архива + `recovery.signal` (PITR через `recovery_target_time`). S3-выгрузка — хук в скрипте (env `NAVBAT_BACKUP_RCLONE_REMOTE`), по умолчанию выключена: хранилища у пользователя ещё нет. Python-кода ноль → pytest-тестов ноль; верификация — живой restore-прогон с [OK]-выводом и записанным RTO.

**Tech Stack:** postgres:16-alpine (pg_basebackup в образе), shell, compose volumes.

**Контекст:**
- `deploy/docker-compose.prod.yml` — postgres-сервис (healthcheck, initdb-каталог уже смонтирован), volumes: pgdata, letsencrypt, certbot-webroot.
- `deploy/initdb/01-app-role.sh` — initdb-скрипты выполняются ТОЛЬКО при чистой инициализации тома; pg_basebackup ходит replication-протоколом — дефолтный pg_hba initdb разрешает replication только локально → нужна запись `host replication all all scram-sha-256`.
- `docs/OPERATIONS.md` — раздел «Восстановление из бэкапа» сейчас заглушка «появится в C-5».
- Грабля Windows-хоста: пути в `docker run -v` через git-bash ломаются — все восстановительные операции делаем через `docker compose run/exec` и named volumes, без bind-mount временных каталогов.

---

### Task 1: WAL-архив + replication-доступ + backup-sidecar

**Files:**
- Create: `deploy/scripts/backup-loop.sh`, `deploy/initdb/02-replication-hba.sh`
- Modify: `deploy/docker-compose.prod.yml`, `deploy/.env.example`

- [ ] **Step 1: `deploy/initdb/02-replication-hba.sh`** (выполнится при чистой инициализации тома):

```sh
#!/bin/sh
# pg_basebackup из backup-sidecar ходит replication-протоколом; дефолтный
# pg_hba разрешает replication только с localhost. Сеть compose внутренняя,
# наружу порт БД не публикуется.
set -e
echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf"
```

- [ ] **Step 2: `deploy/scripts/backup-loop.sh`**:

```sh
#!/bin/sh
# Базовый бэкап раз в NAVBAT_BACKUP_INTERVAL_SEC (деф. 7200 = 2 ч):
# pg_basebackup -Ft -z — самодостаточный архив (base.tar.gz + pg_wal.tar.gz).
# Ротация: храним последние NAVBAT_BACKUP_KEEP (деф. 12 = сутки).
# RPO: ≤ 2 ч базовым бэкапом; с WAL-архивом (/wal_archive) — минуты (PITR).
# S3-хук: NAVBAT_BACKUP_RCLONE_REMOTE + rclone в образе — выгрузка наружу;
# без них шаг тихо пропускается (хранилища пока нет — решение 10.06.2026).
set -e
INTERVAL="${NAVBAT_BACKUP_INTERVAL_SEC:-7200}"
KEEP="${NAVBAT_BACKUP_KEEP:-12}"

while :; do
    STAMP=$(date -u +%Y%m%d-%H%M%S)
    DEST="/backups/$STAMP"
    echo "[backup] $STAMP: pg_basebackup -> $DEST"
    if pg_basebackup -h postgres -U postgres -D "$DEST" -Ft -z -Xs; then
        echo "[backup] $STAMP: OK ($(du -sh "$DEST" | cut -f1))"
    else
        echo "[backup] $STAMP: FAIL — каталог удаляю, жду следующего цикла" >&2
        rm -rf "$DEST"
    fi
    # ротация: лишние (старейшие) каталоги под нож
    ls -1d /backups/*/ 2>/dev/null | sort | head -n -"$KEEP" \
        | while read -r OLD; do
        echo "[backup] ротация: rm $OLD"
        rm -rf "$OLD"
    done
    if [ -n "$NAVBAT_BACKUP_RCLONE_REMOTE" ] && command -v rclone >/dev/null; then
        rclone copy /backups "$NAVBAT_BACKUP_RCLONE_REMOTE" --max-age 3h
    fi
    sleep "$INTERVAL"
done
```

- [ ] **Step 3: compose** — postgres получает WAL-параметры и volume; новый сервис backup:

К `postgres` добавить:

```yaml
    command:
      - postgres
      - -c
      - wal_level=replica
      - -c
      - archive_mode=on
      - -c
      - archive_command=test ! -f /wal_archive/%f && cp %p /wal_archive/%f
    # + volume в списке volumes сервиса:
      - wal_archive:/wal_archive
```

Новый сервис (после certbot):

```yaml
  backup:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      PGPASSWORD: ${NAVBAT_PG_PASSWORD}
    volumes:
      - ./scripts/backup-loop.sh:/backup-loop.sh:ro
      - backups:/backups
    entrypoint: ["/bin/sh", "/backup-loop.sh"]
    depends_on:
      postgres:
        condition: service_healthy
```

И `backups:`, `wal_archive:` в общий список volumes. ВАЖНО: postgres должен писать в /wal_archive (контейнерный пользователь postgres) — named volume докер отдаёт root'ом; у официального образа entrypoint выполняется от root и chown'ит только PGDATA. Решение: задать右 права в command? Проще: archive_command упадёт с permission → postgres health деградирует. Лечится `--user` нет; используем подкаталог тома, который создаст initdb-скрипт `03-wal-dir.sh` с chown… НО initdb-скрипты работают от postgres? Официальный образ запускает их от postgres после chown PGDATA — у него нет прав chown на /wal_archive. Реальное решение: volume монтируется и в backup-сервис нет; добавить к postgres-сервису `user:`? Не трогаем. Берём проверенный приём: одноразовый init-контейнер не нужен — добавить в command postgres'а нельзя. ПРОВЕРИТЬ НА SMOKE: docker named volume по умолчанию наследует права каталога образа в точке монтирования; точка /wal_archive в образе не существует → root:root 755 → postgres писать НЕ сможет. Фикс заранее: смонтировать тот же volume в backup-сервис и выполнить там `chown 70:70` нельзя (backup стартует позже архивации первой WAL)… Простейший надёжный фикс: монтировать WAL-архив ПОД PGDATA-родителя: `wal_archive:/var/lib/postgresql/wal_archive` — официальный entrypoint от root делает `chown -R postgres /var/lib/postgresql` ТОЛЬКО для PGDATA. Тоже нет. Решение без гаданий: добавить лёгкий init-сервис:

```yaml
  wal-perms:
    image: postgres:16-alpine
    user: root
    entrypoint: ["/bin/sh", "-c", "chown postgres:postgres /wal_archive && chmod 700 /wal_archive"]
    volumes:
      - wal_archive:/wal_archive
    restart: "no"
```

и `postgres.depends_on: wal-perms: condition: service_completed_successfully`.

- [ ] **Step 4: `.env.example`** — секция бэкапов:

```sh
# ── Бэкапы ───────────────────────────────────────────────────────────────
# NAVBAT_BACKUP_INTERVAL_SEC=7200
# NAVBAT_BACKUP_KEEP=12
# S3-совместимая выгрузка (включится, когда появится хранилище):
# образ backup-сервиса должен содержать rclone, remote настроен заранее
# NAVBAT_BACKUP_RCLONE_REMOTE=
```

- [ ] **Step 5: Verify** `docker compose -f deploy/docker-compose.prod.yml config --quiet && echo CONFIG-OK` → CONFIG-OK

- [ ] **Step 6: Commit** `feat(deploy): WAL archiving + 2h pg_basebackup sidecar with rotation`

---

### Task 2: живой прогон бэкапа и restore + измеренный RTO

Прогон на прод-стенде локально, с чистого тома (initdb-скрипты должны выполниться).

- [ ] **Step 1: поднять стенд с данными**

```bash
docker compose -f deploy/docker-compose.prod.yml down -v
docker compose -f deploy/docker-compose.prod.yml up -d postgres
docker compose -f deploy/docker-compose.prod.yml run --rm --entrypoint sh app -c "alembic upgrade head && python -m navbat.onboard --demo"
```
Expected: [OK] демо-клиника.

- [ ] **Step 2: первый бэкап** — поднять backup-сервис, дождаться архива:

```bash
docker compose -f deploy/docker-compose.prod.yml up -d backup
docker compose -f deploy/docker-compose.prod.yml logs backup
docker compose -f deploy/docker-compose.prod.yml exec backup ls /backups
```
Expected: `[backup] ... OK`, каталог `YYYYMMDD-HHMMSS` с base.tar.gz.

- [ ] **Step 3: маркер после бэкапа** (проверка WAL-доезда):

```bash
docker compose ... exec postgres psql -U postgres -d navbat -c "INSERT INTO holiday (clinic_id, date, reason) SELECT id, '2099-01-01', 'restore-marker' FROM clinic LIMIT 1; SELECT pg_switch_wal();"
```
(switch_wal заставляет заархивировать сегмент с маркером.)

- [ ] **Step 4: катастрофа + restore** — зафиксировать время старта:

```bash
docker compose ... stop postgres backup app  (часы пошли)
docker volume rm deploy_pgdata               # диск умер
```
Восстановление (всё через compose run от root, без bind-mount):

```bash
docker compose ... run --rm --user root --entrypoint sh -v deploy_backups:/backups -v deploy_wal_archive:/wal_archive postgres -c "
  LATEST=\$(ls -1d /backups/*/ | sort | tail -1)
  mkdir -p /var/lib/postgresql/data && cd /var/lib/postgresql/data
  tar xzf \$LATEST/base.tar.gz
  mkdir -p pg_wal && tar xzf \$LATEST/pg_wal.tar.gz -C pg_wal
  echo \"restore_command = 'cp /wal_archive/%f %p'\" >> postgresql.auto.conf
  touch recovery.signal
  chown -R postgres:postgres /var/lib/postgresql/data && chmod 700 /var/lib/postgresql/data"
```
(точный синтаксис монтирования тома в `compose run` уточнить по факту: `docker compose run` не принимает -v у некоторых версий → запасной путь `docker run --rm -v deploy_pgdata:/var/lib/postgresql/data -v deploy_backups:/backups -v deploy_wal_archive:/wal_archive postgres:16-alpine sh -c ...` — named volumes в docker run на Windows безопасны, ломаются только path-bind'ы.)

```bash
docker compose ... up -d postgres   # recovery по WAL → promote
docker compose ... exec postgres psql -U postgres -d navbat -tAc "SELECT reason FROM holiday WHERE date='2099-01-01'"
```
Expected: `restore-marker` — данные ПОСЛЕ бэкапа доехали из WAL. Часы остановить → RTO.

- [ ] **Step 5: добить стенд** — `up -d app` → `--check` изнутри [OK]; удалить маркер-строку; `down` стенда.

- [ ] **Step 6: записать процедуру + фактический RTO в docs/OPERATIONS.md** (заменить заглушку разделом с шагами из Step 4, включая PITR-вариант с `recovery_target_time` и предупреждение про NAVBAT_ENC_KEY: бэкап без ключа шифрования бесполезен).

- [ ] **Step 7: Commit** `docs(ops): tested restore runbook with measured RTO` + push.

## Definition of Done (C-5)

- [ ] Стенд: WAL-архив пишется, бэкап снимается, ротация работает.
- [ ] Restore прогнан руками: маркер из WAL доехал, RTO измерен и записан.
- [ ] OPERATIONS.md содержит полную процедуру восстановления + PITR.
- [ ] Compose-конфиг валиден; всё в origin; дев-демо восстановлено.
