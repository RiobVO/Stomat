# Self-service инкремент 3 — визуал/UX админ-консоли (план реализации)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Привести админ-карточки, меню и промпты `admin_console.py` к hero-стилю пациентского UI (emoji-якоря на полях, пустые строки между блоками, статус-бейджи 🟢/⚪, emoji на кнопках) — паритет с пациентским визуалом.

**Architecture:** Чисто презентационный инкремент: меняются ТОЛЬКО строки рендера и лейблы кнопок в `src/navbat/telegram/admin_console.py`. Ноль изменений поведения, callback-схемы (`adm:*`), pending-видов, БД, миграций, `onboard.py`, репозиториев, воркера. Один хелпер `_status_badge` для консистентности статуса.

**Tech Stack:** Python, Telegram Bot API (parse_mode=HTML), pytest против реального postgres (:5434). Админ-ответы уже шлются с HTML — `<b>`/эмодзи рендерятся.

**Спека:** `docs/superpowers/specs/2026-06-14-admin-console-visual-design.md`

**Соглашение по коммитам:** автор `dejavuu <95645082+RiobVO@users.noreply.github.com>`; без упоминаний ассистента/AI/Co-Authored-By; префиксы `feat`/`fix`/`refactor`/`docs`/`style`.

**Грабля:** перед полным `pytest` глушить живого бота (TRUNCATE-фикстуры рушат его FK). Никаких голых `<` в HTML-строках (урок stats-бага — `&lt;` для литерала).

---

## Task 1: Хелпер `_status_badge` (TDD)

**Files:**
- Modify: `src/navbat/telegram/admin_console.py` (добавить функцию модульного уровня рядом с `_fmt_sum`/`_esc`)
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_admin_console.py`:

```python
def test_status_badge():
    assert ac._status_badge(True, female=True) == "🟢 Активна"
    assert ac._status_badge(True, female=False) == "🟢 Активен"
    assert ac._status_badge(False, female=True) == "⚪ Скрыта"
    assert ac._status_badge(False, female=False) == "⚪ Скрыт"
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py::test_status_badge -q`
Expected: FAIL (`_status_badge` не существует).

- [ ] **Step 3: Реализовать** (рядом с `_esc` в `admin_console.py`)

```python
def _status_badge(is_active: bool, female: bool) -> str:
    """Статус-бейдж сущности: 🟢 активна / ⚪ скрыта (род по female)."""
    if is_active:
        return "🟢 Активна" if female else "🟢 Активен"
    return "⚪ Скрыта" if female else "⚪ Скрыт"
```

- [ ] **Step 4: Запустить — зелено**

Run: `python -m pytest tests/test_admin_console.py::test_status_badge -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): status badge helper for card rendering"
```

---

## Task 2: Hero-карточки услуги и врача + style-lock тесты

**Files:**
- Modify: `src/navbat/telegram/admin_console.py` (`_service_card`, `_doctor_card`, лейблы кнопок)
- Test: `tests/test_admin_console.py`

- [ ] **Step 1: Написать падающие style-lock тесты**

```python
def test_service_card_hero_style(app_session_factory, admin_engine, clinic_a,
                                 service_cleaning):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, "adm:svc:cleaning")
    body = (api.edited[-1][2] if api.edited else last_to(api, ADMIN_CHAT))
    assert "💰 Цена:" in body
    assert "⏱ Длительность:" in body
    assert "🟢 Активна" in body


def test_doctor_card_hero_style(app_session_factory, admin_engine, clinic_a,
                                doctor_a):
    worker, api, _ = make_worker(app_session_factory, clinic_a, [],
                                 admin_chat_id=ADMIN_CHAT)
    click(worker, app_session_factory, clinic_a, f"adm:doc:{doctor_a}")
    body = (api.edited[-1][2] if api.edited else last_to(api, ADMIN_CHAT))
    assert "⏲ Буфер:" in body
    assert "📆 Календарь:" in body
    assert "🟢 Активен" in body
    assert "📅" in body  # блок графика
```

- [ ] **Step 2: Запустить — падает**

Run: `python -m pytest tests/test_admin_console.py -k "hero_style" -q`
Expected: FAIL (нет emoji-якорей/бейджа в текущих карточках).

- [ ] **Step 3: Реализовать `_service_card` тело + кнопки**

Заменить блок построения `text` и `btn_rows_list` в `_service_card` (строки ~336–360):

```python
        emoji = SERVICE_EMOJI.get(key, "")
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        price_txt = _fmt_sum(row.price) + " сум" if row.price else "не задана"
        head = f"{notice}\n\n" if notice else ""
        text = (f"{head}{emoji} <b>{_esc(label)}</b>\n\n"
                f"💰 Цена: {price_txt}\n"
                f"⏱ Длительность: {row.duration_min} мин\n"
                f"{_status_badge(row.is_active, female=True)}")
        toggle_btn = (
            Button("⛔ Скрыть", f"adm:svc:{key}:deact")
            if row.is_active else
            Button("✅ Показать", f"adm:svc:{key}:act")
        )
        btn_rows_list = [
            (Button("💰 Изм. цену", f"adm:svc:{key}:price"),
             Button("⏱ Изм. длит.", f"adm:svc:{key}:dur")),
            (toggle_btn,),
        ]
        if not row.is_active and refs == 0:
            btn_rows_list.append((Button("🗑 Удалить совсем", f"adm:svc:{key}:del"),))
        btn_rows_list.append((Button("◀ Услуги", "adm:services"),))
        return Reply(text, button_rows=tuple(btn_rows_list))
```

(Удаляются старые `dur_txt`/`status` локали — заменены инлайном/бейджем.)

- [ ] **Step 4: Реализовать `_doctor_card` тело + кнопки**

Заменить блок `text`/`btn_rows_list` в `_doctor_card` (строки ~594–616):

```python
        name = doc.name or "(без имени)"
        sch = _format_schedule(doc.working_intervals or {})
        cal = ("привязан" if doc.gcal_calendar_id else "не привязан")
        head = f"{notice}\n\n" if notice else ""
        text = (f"{head}🧑‍⚕️ <b>{_esc(name)}</b>\n\n"
                f"⏲ Буфер: {doc.buffer_min} мин\n"
                f"📆 Календарь: {cal}\n"
                f"{_status_badge(doc.is_active, female=False)}\n\n"
                f"📅 <b>График</b>\n{_esc(sch)}")
        toggle_btn = (
            Button("⛔ Скрыть", f"adm:doc:{doc_id}:deact")
            if doc.is_active else
            Button("✅ Показать", f"adm:doc:{doc_id}:act")
        )
        btn_rows_list = [
            (Button("👤 Имя", f"adm:doc:{doc_id}:name"),
             Button("⏲ Буфер", f"adm:doc:{doc_id}:buf")),
            (Button("📅 Расписание", f"adm:doc:{doc_id}:sched"),),
            (toggle_btn,),
        ]
        if not doc.is_active and refs == 0:
            btn_rows_list.append((Button("🗑 Удалить совсем", f"adm:doc:{doc_id}:del"),))
        btn_rows_list.append((Button("◀ Врачи", "adm:doctors"),))
        return Reply(text, button_rows=tuple(btn_rows_list))
```

(Старая `status = "активен"...` строка удаляется — заменена бейджем.)

- [ ] **Step 5: Запустить style-lock + раздел услуг/врачей**

Run: `python -m pytest tests/test_admin_console.py -k "hero_style or service_card or doctor_card or service_add or schedule or doctor_add or delete_gating" -q`
Expected: PASS. Если упал текстовый ассерт старого формата — обновить его под новый словарь (поведенческие ассерты actions/DB не трогать).

- [ ] **Step 6: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): hero-style service and doctor cards"
```

---

## Task 3: Меню, промпты, empty-states — стиль и подсказки

**Files:**
- Modify: `src/navbat/telegram/admin_console.py`
- Test: `tests/test_admin_console.py` (прогон существующих)

Презентационная причёска. Точечные замены строк:

- [ ] **Step 1: Меню-заголовки — добавить подсказку 👇**

`main_menu` (строка ~240):
```python
        return Reply(f"{head}🛠 <b>Админ-консоль</b>\nВыберите раздел 👇", menu=rows)
```
`_services_menu` (строка ~283–285):
```python
        return Reply(
            f"{head}💊 <b>Услуги</b>\nВыберите услугу 👇",
            button_rows=tuple(rows))
```
`_doctors_menu` (строка ~535–537):
```python
        return Reply(
            f"{head}🧑‍⚕️ <b>Врачи</b>\nВыберите врача 👇",
            button_rows=tuple(rows))
```
`_faq_menu` (строка ~484):
```python
        return Reply(f"{head}🏥 <b>О клинике</b>\nВыберите поле 👇", button_rows=rows)
```

- [ ] **Step 2: Промпты ввода — hero-заголовок поля**

`_begin_dname` (строки ~621–623):
```python
        reply = Reply(
            "👤 <b>Имя врача</b>\n\nВведите имя (до 80 символов):",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
```
`_begin_dbuf` (строки ~643–645):
```python
        reply = Reply(
            "⏲ <b>Буфер</b>\n\nВведите буфер в минутах (0–120), например 10:",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
```
`_begin_docadd` (строки ~665–667):
```python
        reply = Reply(
            "🧑‍⚕️ <b>Новый врач</b>\n\nВведите имя:",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
```

- [ ] **Step 3: Расписание — подсказки 👇**

`_sched_entry` (строки ~688–690):
```python
        r = Reply(
            "📅 <b>Расписание</b>\nВыберите шаблон или задайте свой 👇",
            button_rows=tuple(rows))
```
`_sched_custom_days` — заголовок «Отметьте рабочие дни» дополнить 👇 (найти строку `Reply("📅 Отметьте рабочие дни:"` в `_sched_custom_days` и заменить на):
```python
            "📅 <b>Свой график</b>\nОтметьте рабочие дни 👇",
```

- [ ] **Step 4: Empty-states**

`_dayoff_menu` — ветка пустого списка (строка с `Закрытых дней впереди нет.`):
```python
            intro = "📅 Закрытых дней нет — клиника работает по графику."
```
(оставить остальной рендер `_dayoff_menu` как есть; intro подставляется в тело.)

- [ ] **Step 5: Прогон затронутого + полного раздела**

Run: `python -m pytest tests/test_admin_console.py -q`
Expected: PASS. Текстовые ассерты на старые заголовки (если есть «Выберите услугу» без 👇 — substring «Услуги» сохраняется; «О клинике»/«Врачи» сохраняются) переживут; что сломалось по формату — обновить.

- [ ] **Step 6: Commit**

```bash
git add src/navbat/telegram/admin_console.py tests/test_admin_console.py
git commit -m "feat(admin): polish menus, prompts, empty-states to hero style"
```

---

## Task 4: Финальная верификация

**Files:** —

- [ ] **Step 1: Полный сьют (бот заглушён!)**

Если живой бот запущен — остановить (иначе TRUNCATE рушит его FK).
Run: `python -m pytest -q`
Expected: всё зелёное.

- [ ] **Step 2: Преддемо-чек**

Run: `python -m navbat.onboard --demo` затем `python -m navbat --check`
Expected: все `[OK]`, ревизия миграций = head.

- [ ] **Step 3: Живое подтверждение рендера (опц., по наличию окружения)**

Восстановить @qqevaaa в админы (`python live_poke/admin_setup.py`), запустить бота (`python -m navbat --real`), прогнать `python live_poke/admin_probe.py`, прочитать `live_poke/transcript_admin.md` — карточки/меню должны быть в hero-стиле (💰/⏱/🟢, заголовки с 👇). По окончании — остановить бота, убрать @qqevaaa из админов, восстановить демо.
Expected: транскрипт показывает новый стиль, ноль `[STUCK]`-багов бота.

---

## Self-Review (выполнено при написании плана)

**Покрытие спеки:**
- Визуальный словарь (бейдж/якоря/заголовки/промпты/empty-states) → Tasks 1–3.
- Hero-карточки услуги/врача → Task 2.
- Меню + подсказки + промпты + расписание + empty-states → Task 3.
- Кнопки с emoji (💰/⏱/👤/⏲) → Task 2 (карточки).
- Тесты-замки стиля (`_status_badge` + карточки) → Tasks 1–2.
- Полный сьют + `--check` + живое подтверждение → Task 4.
- Границы (worker не трогаем; пациентский UI не трогаем; ноль поведения) — соблюдены: все правки в `admin_console.py`, только строки/лейблы.

**Плейсхолдеры:** нет. Все before→after — конкретный код с номерами строк-ориентирами (номера приблизительны — искать по сигнатуре метода).

**Согласованность:** `_status_badge(is_active, female)` — одна сигнатура, используется в `_service_card` (female=True) и `_doctor_card` (female=False). callback-схема `adm:*` и pending-виды НЕ меняются (только видимые лейблы/тексты). Поведенческие тесты (actions/DB/pending) переживают; ломаются только презентационные текст-ассерты — обновляются под словарь.
