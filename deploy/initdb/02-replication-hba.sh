#!/bin/sh
# pg_basebackup из backup-sidecar ходит replication-протоколом; дефолтный
# pg_hba разрешает replication только с localhost. Сеть compose внутренняя,
# наружу порт БД не публикуется.
set -e
echo "host replication all all scram-sha-256" >> "$PGDATA/pg_hba.conf"
