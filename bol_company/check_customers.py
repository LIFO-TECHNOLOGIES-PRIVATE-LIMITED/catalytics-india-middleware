from db import Database
from config import config

db = Database(config.SQLITE_DB_PATH)

total = db.query('SELECT COUNT(*) as c FROM customers')['c']
synced = db.query('SELECT COUNT(*) as c FROM customers WHERE is_synced=1')['c']
not_synced = db.query('SELECT COUNT(*) as c FROM customers WHERE is_synced=0 OR is_synced IS NULL')['c']

print(f'Total customers in SQLite : {total}')
print(f'Synced (is_synced=1)      : {synced}')
print(f'Not synced                : {not_synced}')

# Sample errors
rows = db.query_all(
    "SELECT name, tally_company, last_sync_error FROM customers "
    "WHERE (is_synced=0 OR is_synced IS NULL) "
    "AND last_sync_error IS NOT NULL AND last_sync_error != '' "
    "LIMIT 15"
)
print()
print('Sample errors from unsynced customers:')
for r in rows:
    err = str(r.get('last_sync_error', '') or '')[:150]
    name = str(r.get('name', ''))[:30]
    comp = str(r.get('tally_company', ''))[:15]
    print(f'  [{comp}] {name}: {err}')

# Error pattern summary
print()
print('Error patterns (grouped):')
all_errors = db.query_all(
    "SELECT last_sync_error, COUNT(*) as cnt FROM customers "
    "WHERE (is_synced=0 OR is_synced IS NULL) "
    "AND last_sync_error IS NOT NULL AND last_sync_error != '' "
    "GROUP BY last_sync_error ORDER BY cnt DESC LIMIT 10"
)
for r in all_errors:
    err = str(r.get('last_sync_error', '') or '')[:120]
    print(f'  [{r.get("cnt")}x] {err}')

db.close()
