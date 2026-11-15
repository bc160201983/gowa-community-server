#!/bin/sh
# GoW Ascension community server: nightly copy of the accounts database (sqlite online backup), 14 kept.
set -e
D=/opt/gowa/backups; mkdir -p "$D"
python3 -I -c "import sqlite3,sys; s=sqlite3.connect('/opt/gowa/data/accounts.db'); d=sqlite3.connect(sys.argv[1]); s.backup(d); d.close()" "$D/accounts-$(date +%Y%m%d).db"
cp /opt/gowa/data/*.key "$D/" 2>/dev/null || true
chmod 600 "$D"/*; chown -R gowa:gowa "$D"
find "$D" -name 'accounts-*.db' -mtime +14 -delete
