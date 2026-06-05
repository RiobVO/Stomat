# spike_nlu — eval-харнесс гипотезы №1 (Ф0)

Проверяет, тянет ли дешёвый GPT извлечение слотов из узбекских/русских/смешанных
сообщений пациентов (BRIEF.md, разд. 5 «Гипотеза №1», kill-критерий ≥ 85%).
Одноразовый спайк, не прод.

## Запуск

```powershell
pip install -r requirements.txt
$env:OPENAI_API_KEY = "sk-..."

python eval.py --dry-run                       # валидация датасета, без API
python eval.py --limit 5 --models gpt-5-nano   # smoke (~$0.001)
python eval.py                                 # полный: 130 сообщ × 3 модели, < $0.10
```

Флаги: `--models` (список через запятую), `--data`, `--limit`, `--concurrency`,
`--reasoning-effort minimal|low|medium|high` (только gpt-5-*).

Отчёт: консоль + `results/report_<ts>.md`; сырые ответы: `results/raw_<ts>.jsonl`.

## Формат data/messages.jsonl

```json
{"id":"m001","text":"ertaga tish tozalashga yozilsam bo'ladimi?","source":"synthetic","category":"uz_latin","gold":{"intent":"book","service":"cleaning","doctor":null,"date_ref":"tomorrow","time_ref":null,"language":"uz"}}
```

- `source`: `synthetic` | `real` — отчёт даёт разрез отдельно.
- `category`: `uz_latin | uz_cyrillic | ru | mixed | voice_artifact | typo | elderly`.
- `gold`: схема `Extraction` из `eval.py` (intent, service, doctor, date_ref,
  time_ref, language). Кривой gold валит запуск, не молчит.

## Как добавить реальные сообщения

1. Дописывай строки в `data/messages.jsonl` с `"source":"real"` (PII убрать руками:
   имена пациентов заменить, телефоны выкинуть — текст уходит в OpenAI).
2. `python eval.py --dry-run` — проверка разметки.
3. Полный прогон. **Вердикт по гипотезе №1 считается только по разрезу `real`** —
   синтетика валидирует харнесс, не гипотезу (бриф прямо запрещает «зелёный на
   курированных 50 сообщениях»).

## Что измеряется

- accuracy по каждому полю + **strict** (все 6 полей верны) — это и есть «понимает»;
- разрезы по category/source, intent-confusion, список фейлов;
- токены, стоимость прогона, экстраполяция на месяц (сверка с cost model брифа);
- latency p50/p95, количество JSON-repair.

## Известные ограничения

- Синтетический узбекский не проверен носителем — показать носителю до того,
  как верить цифрам по uz-категориям.
- Цены моделей и курс UZS захардкожены в `eval.py` (сверены 2026-06-05).
- Извлекается одна услуга на сообщение; «чистка и отбеливание» в одном сообщении
  схема не покрывает (в датасете таких нет).
