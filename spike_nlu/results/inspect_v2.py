"""Одноразовая инспекция второго сета: схемы, словари значений, дубли."""
import json
import sys
from collections import Counter, defaultdict

old, new, bad = [], [], []
for lineno, line in enumerate(open(sys.argv[1], encoding="utf-8"), 1):
    if not line.strip():
        continue
    try:
        rec = json.loads(line)
    except json.JSONDecodeError as e:
        bad.append((lineno, str(e)))
        continue
    (old if "gold" in rec else new).append(rec)

print(f"старых (gold): {len(old)}, новых (expected): {len(new)}, битых JSON: {len(bad)}")
for lineno, err in bad[:10]:
    print(f"  битая строка {lineno}: {err}")

prefixes = Counter(r["id"].rsplit("_", 1)[0] for r in new)
print(f"префиксы id новых: {dict(prefixes)}")

ids = Counter(r["id"] for r in old + new)
dup_ids = {k: v for k, v in ids.items() if v > 1}
print(f"дубли id: {dup_ids if dup_ids else 'нет'}")

keys = Counter(tuple(sorted(r.keys())) for r in new)
print(f"наборы ключей новых: {list(keys.items())}")
ekeys = Counter(tuple(sorted(r["expected"].keys())) for r in new)
print(f"наборы ключей expected: {list(ekeys.items())}")

for field in ("intent", "language", "service_canonical", "date_ref", "time_ref"):
    vals = Counter(str(r["expected"].get(field)) for r in new)
    print(f"{field}: {dict(vals.most_common())}")

doctors = Counter(str(r["expected"].get("doctor")) for r in new if r["expected"].get("doctor"))
print(f"doctor (не-null): {dict(doctors)}")

texts = Counter(r["text"].strip().lower() for r in old + new)
dups = {t: c for t, c in texts.items() if c > 1}
print(f"точных текст-дублей: {len(dups)}")
for t, c in list(dups.items())[:20]:
    print(f"  ×{c}: {t[:80]}")
