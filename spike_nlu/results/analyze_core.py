"""Разбор raw-файла: core-5 (без language) по моделям и source.

Запуск: python results/analyze_core.py results/raw_<ts>.jsonl [--fails MODEL]
--fails MODEL — полный список core-5-фейлов модели с диффами text/expected/got.
"""
import argparse
import json
from collections import defaultdict

CORE = ("intent", "service", "doctor", "date_ref", "time_ref")


def ok(gold: dict, got: dict, f: str) -> bool:
    if f != "doctor":
        return gold[f] == got[f]
    if gold[f] is None or got[f] is None:
        return gold[f] is None and got[f] is None
    g, p = gold[f].lower(), got[f].lower()
    return g in p or p in g


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("raw")
    parser.add_argument("--fails", help="модель, для которой выдать полный список core-фейлов")
    parser.add_argument("--source", help="фильтр source для списка фейлов (например real)")
    args = parser.parse_args()

    rows = [json.loads(line) for line in open(args.raw, encoding="utf-8")]
    for r in rows:
        r["core_ok"] = r["got"] is not None and all(ok(r["gold"], r["got"], f) for f in CORE)

    by_model = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)

    for model, rs in by_model.items():
        n = len(rs)
        core_n = sum(r["core_ok"] for r in rs)
        lang_n = sum(r["got"] is not None and ok(r["gold"], r["got"], "language") for r in rs)
        err_n = sum(r["got"] is None for r in rs)
        print(f"\n{model}: core-5 {core_n}/{n} = {core_n/n:.1%} | language {lang_n/n:.1%} "
              f"| ошибок прогона {err_n}")
        by_src = defaultdict(lambda: [0, 0, 0, 0])  # n, core_ok, med_labeled, med_ok
        for r in rs:
            s = by_src[r["source"]]
            s[0] += 1
            s[1] += r["core_ok"]
            if r["gold"].get("is_medical") is not None:
                s[2] += 1
                s[3] += r["got"] is not None and r["got"]["is_medical"] == r["gold"]["is_medical"]
        for src, (total, good, med_n, med_ok) in sorted(by_src.items()):
            med = f" | is_medical {med_ok}/{med_n} = {med_ok/med_n:.1%}" if med_n else ""
            print(f"  {src:18} core {good}/{total} = {good/total:.1%}{med}")
        # по полям
        for f in CORE:
            f_ok = sum(r["got"] is not None and ok(r["gold"], r["got"], f) for r in rs)
            print(f"  поле {f:9} {f_ok/n:.1%}")

    if args.fails:
        rs = by_model.get(args.fails)
        if not rs:
            raise SystemExit(f"нет результатов для модели {args.fails}")
        fails = [r for r in rs if not r["core_ok"]]
        if args.source:
            fails = [r for r in fails if r["source"] == args.source]
        print(f"\n=== core-5 фейлы {args.fails}"
              f"{f' (source={args.source})' if args.source else ''}: {len(fails)} ===")
        for r in fails:
            print(f"\n[{r['id']}] ({r['source']}) «{r['text']}»")
            if r["got"] is None:
                print(f"  ОШИБКА ПРОГОНА: {r['error']}")
                continue
            for f in CORE:
                if not ok(r["gold"], r["got"], f):
                    print(f"  {f}: expected {r['gold'][f]!r} | got {r['got'][f]!r}")


if __name__ == "__main__":
    main()
