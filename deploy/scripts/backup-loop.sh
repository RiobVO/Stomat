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
