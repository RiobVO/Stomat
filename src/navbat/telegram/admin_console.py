"""Админ-консоль на кнопках: владелец клиники правит услуги, врачей
и FAQ-поля прямо из админ-чата.

Инкремент 2 (Услуги P-2): список услуг с is_active, добавление,
дезактивация/удаление; цена и длительность.
Инкремент 3 (Врачи P-3): список врачей, имя/буфер/расписание,
добавление/дезактивация/удаление.
"""
from __future__ import annotations

import html
import json
import re
import uuid as _uuid_mod

from navbat import onboard
from navbat.db.base import tenant_transaction
from navbat.dialog import clinic_repo, doctors_repo, services_repo
from navbat.dialog.conversation import load_conversation, save_conversation
from navbat.dialog.replies import SERVICE_EMOJI, SERVICE_LABELS, Button, Reply
from navbat.scheduling.calendar_rules import WEEKDAY_KEYS

# верхнее меню — reply-клавиатура
BTN_SERVICES = "💊 Услуги"
BTN_DOCTORS = "🧑‍⚕️ Врачи"
BTN_ABOUT = "🏥 О клинике"
BTN_DAYOFF = "📅 Выходные"
BTN_STATS = "📊 Статистика"
BTN_PAUSE = "⏸ Пауза"
BTN_RESUME = "▶️ Возобновить"
_MENU_LABELS = {BTN_SERVICES, BTN_DOCTORS, BTN_ABOUT, BTN_DAYOFF, BTN_STATS,
                BTN_PAUSE, BTN_RESUME}

CANCEL_WORDS = {"отмена", "cancel"}

PRICE_MAX = 1_000_000_000
FAQ_MAX = 500
DUR_MIN, DUR_MAX = 5, 480
BUF_MIN, BUF_MAX = 0, 120
NAME_MAX = 80

_FAQ_TITLES = {
    "address": "Адрес",
    "payment": "Условия оплаты",
    "phone": "Телефон",
}
_FAQ_READERS = {
    "address": clinic_repo.clinic_address,
    "payment": clinic_repo.clinic_payment_info,
    "phone": clinic_repo.clinic_phone,
}
_FAQ_WRITERS = {
    "address": onboard.set_clinic_address,
    "payment": onboard.set_clinic_payment,
    "phone": onboard.set_clinic_phone,
}

# Шаблоны расписания
_SCHEDULE_TEMPLATES = [
    ("Пн–Пт 09–18", {
        d: [["09:00", "18:00"]]
        for d in ("mon", "tue", "wed", "thu", "fri")
    }),
    ("Пн–Сб 09–13 / 14–18", {
        d: [["09:00", "13:00"], ["14:00", "18:00"]]
        for d in ("mon", "tue", "wed", "thu", "fri", "sat")
    }),
    ("Пн–Пт 10:00–19:00", {
        d: [["10:00", "19:00"]]
        for d in ("mon", "tue", "wed", "thu", "fri")
    }),
]

_WEEKDAY_RU = {
    "mon": "Пн", "tue": "Вт", "wed": "Ср",
    "thu": "Чт", "fri": "Пт", "sat": "Сб",
    "sun": "Вс",
}


def _fmt_sum(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _esc(value: str) -> str:
    return html.escape(str(value), quote=False)


def _format_schedule(wi: dict) -> str:
    """Группирует последовательные дни с одинаковыми сменами в диапазон."""
    order = list(WEEKDAY_KEYS)
    groups: list = []  # (first_day, last_day, shifts_json)
    for day in order:
        shifts = wi.get(day)
        if not shifts:
            continue
        key = json.dumps(shifts, separators=(",", ":"))
        if (groups and groups[-1][2] == key
                and order.index(day) == order.index(groups[-1][1]) + 1):
            groups[-1] = (groups[-1][0], day, key)
        else:
            groups.append((day, day, key))
    lines = []
    for first, last, shifts_key in groups:
        shifts = json.loads(shifts_key)
        spans = " / ".join(f"{s[0]}–{s[1]}" for s in shifts)
        if first == last:
            lines.append(f"{_WEEKDAY_RU[first]} {spans}")
        else:
            lines.append(f"{_WEEKDAY_RU[first]}–{_WEEKDAY_RU[last]} {spans}")
    return "\n".join(lines) if lines else "выходной всю неделю"


def _parse_shifts(raw: str) -> list[list[str]] | None:
    """Парсит "09:00-13:00, 14:00-18:00" -> [["09:00","13:00"],...].

    Возвращает None при любой ошибке формата, включая часы >23 / минуты >59.
    """
    parts = [p.strip() for p in raw.split(",")]
    result = []
    pattern = re.compile(r"^(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})$")
    for part in parts:
        m = pattern.match(part)
        if not m:
            return None
        start_raw, end_raw = m.group(1), m.group(2)
        try:
            # onboard._parse_hhmm поднимает ValueError при h>=24 или m>=60
            sh, sm = onboard._parse_hhmm(start_raw)
            eh, em = onboard._parse_hhmm(end_raw)
        except ValueError:
            return None
        start = f"{sh:02d}:{sm:02d}"
        end = f"{eh:02d}:{em:02d}"
        if start >= end:
            return None
        result.append([start, end])
    return result if result else None


class AdminConsole:
    """Кнопочная админ-поверхность.

    Воркер дёргает три публичных метода: handle_text, handle_callback, main_menu.
    """

    def __init__(self, session_factory, clinic_id, api, worker) -> None:
        self._sf = session_factory
        self._cid = clinic_id
        self._api = api
        self._worker = worker

    # -- Публичная поверхность -------------------------------------------------

    def handle_text(self, chat_id: int, text: str) -> Reply:
        stripped = text.strip()
        pending = self._get_pending(chat_id)
        if stripped in _MENU_LABELS:
            if pending:
                self._clear_pending(chat_id)
            return self._menu_action(chat_id, stripped)
        if pending and stripped.lower() in CANCEL_WORDS:
            self._clear_pending(chat_id)
            return self.main_menu()
        if pending:
            kind, _, arg = pending.partition(":")
            if kind == "price":
                return self._apply_price(chat_id, arg, stripped)
            if kind == "dur":
                return self._apply_duration(chat_id, arg, stripped)
            if kind == "faq":
                return self._apply_faq(chat_id, arg, stripped)
            if kind == "svcadd":
                return self._apply_svcadd(chat_id, arg, stripped)
            if kind == "dname":
                return self._apply_dname(chat_id, arg, stripped)
            if kind == "dbuf":
                return self._apply_dbuf(chat_id, arg, stripped)
            if kind == "dadd":
                return self._apply_dadd(chat_id, stripped)
            if kind == "sched":
                return self._apply_sched_shifts(chat_id, arg, stripped)
            if kind == "dayoff":
                return self._apply_dayoff(chat_id, stripped)
        return self.main_menu()

    def handle_callback(self, callback: dict, chat_id: int, data: str) -> None:
        self._api.answer_callback_query(callback["id"])
        message_id = callback["message"].get("message_id")
        body = data[len("adm:"):]
        if body in ("home", "cancel"):
            self._clear_pending(chat_id)
            self._worker._send(chat_id, self.main_menu())
            return
        kind, _, arg = body.partition(":")
        if body == "services" or kind == "services":
            r = self._services_menu()
            self._edit_or_send(chat_id, message_id, r)
            return
        if kind == "svc":
            self._service_callback(chat_id, message_id, arg)
            return
        if body == "svcadd":
            # adm:svcadd (без ключа) → показать каталог добавляемых услуг
            r = self._svcadd_catalog_menu()
            self._edit_or_send(chat_id, message_id, r)
            return
        if kind == "svcadd":
            # adm:svcadd:<key> → начать добавление конкретной услуги
            self._begin_svcadd(chat_id, arg, message_id)
            return
        if kind == "doctors":
            r = self._doctors_menu()
            self._edit_or_send(chat_id, message_id, r)
            return
        if kind == "doc":
            self._doctor_callback(chat_id, message_id, arg)
            return
        if kind == "docadd":
            self._begin_docadd(chat_id, message_id)
            return
        if kind == "sched":
            self._sched_callback(chat_id, message_id, arg)
            return
        if kind == "price":
            self._begin_price_edit(chat_id, arg, message_id)
            return
        if kind == "dayoff":
            self._handle_dayoff_callback(chat_id, message_id, arg)
            return
        if kind == "faq":
            self._begin_faq_edit(chat_id, arg, message_id)

    def main_menu(self) -> Reply:
        paused = self._worker._bot_paused()
        pause_btn = BTN_RESUME if paused else BTN_PAUSE
        rows = ((BTN_SERVICES, BTN_DOCTORS), (BTN_ABOUT, BTN_DAYOFF),
                (BTN_STATS,), (pause_btn,))
        head = "⏸ <i>Бот на паузе.</i>\n\n" if paused else ""
        return Reply(f"{head}🛠 <b>Админ-консоль</b>\nВыберите раздел:", menu=rows)

    # -- Роутинг ---------------------------------------------------------------

    def _menu_action(self, chat_id: int, label: str) -> Reply:
        if label == BTN_SERVICES:
            return self._services_menu()
        if label == BTN_DOCTORS:
            return self._doctors_menu()
        if label == BTN_ABOUT:
            return self._faq_menu()
        if label == BTN_DAYOFF:
            return self._dayoff_menu()
        if label == BTN_STATS:
            return self._worker._stats_reply()
        if label in (BTN_PAUSE, BTN_RESUME):
            return self._toggle_pause()
        return self.main_menu()

    def _toggle_pause(self) -> Reply:
        if self._worker._bot_paused():
            conf = self._worker._resume_reply()
        else:
            conf = self._worker._pause_reply("/pause")
        return Reply(conf.text, menu=self.main_menu().menu)

    # -- Раздел Услуги (P-2) -------------------------------------------------

    def _services_menu(self, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = services_repo.service_list_all(session)
        rows = []
        for row in rows_data:
            emoji = SERVICE_EMOJI.get(row.name, "")
            label = SERVICE_LABELS.get(row.name, {}).get("ru", row.name)
            if row.is_active:
                btn_text = f"{emoji} {label}".strip()
            else:
                btn_text = f"⚪ {label} (скрыта)"
            rows.append((Button(btn_text, f"adm:svc:{row.name}"),))
        rows.append((Button("+ Добавить услугу", "adm:svcadd"),))
        rows.append((Button("◀ Меню", "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(
            f"{head}💊 <b>Услуги</b>\nВыберите услугу:",
            button_rows=tuple(rows))

    def _service_callback(self, chat_id: int, message_id: int | None, arg: str) -> None:
        parts = arg.split(":", 1)
        key = parts[0]
        action = parts[1] if len(parts) > 1 else None
        if action == "price":
            self._begin_price_edit(chat_id, key, message_id)
        elif action == "dur":
            self._begin_dur_edit(chat_id, key, message_id)
        elif action == "deact":
            onboard.deactivate_service(self._sf, self._cid, key)
            r = self._services_menu(notice="⏸ Услуга поставлена на паузу")
            self._edit_or_send(chat_id, message_id, r)
        elif action == "act":
            onboard.activate_service(self._sf, self._cid, key)
            r = self._services_menu(notice="✅ Услуга активирована")
            self._edit_or_send(chat_id, message_id, r)
        elif action == "del":
            try:
                onboard.delete_service(self._sf, self._cid, key)
                r = self._services_menu(notice="✅ Услуга удалена")
            except ValueError as e:
                r = self._service_card(key, notice=f"⚠️ {_esc(str(e))}")
            self._edit_or_send(chat_id, message_id, r)
        else:
            r = self._service_card(key)
            self._edit_or_send(chat_id, message_id, r)

    @staticmethod
    def _service_refs(session, key: str) -> int:
        """Число ссылок на услугу в appointment + waitlist (по имени).
        Кнопка удаления не должна показываться при refs > 0."""
        from sqlalchemy import text as _text
        return session.execute(
            _text("SELECT"
                  " (SELECT count(*) FROM appointment a"
                  "  JOIN service s ON s.id = a.service_id WHERE s.name = :n)"
                  "+"
                  "(SELECT count(*) FROM waitlist w"
                  "  JOIN service s ON s.id = w.service_id WHERE s.name = :n)"),
            {"n": key},
        ).scalar_one()

    def _service_card(self, key: str, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = services_repo.service_list_all(session)
            row = next((r for r in rows_data if r.name == key), None)
            refs = self._service_refs(session, key) if row is not None else 0
        if row is None:
            return self._services_menu()
        emoji = SERVICE_EMOJI.get(key, "")
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        price_txt = _fmt_sum(row.price) + " сум" if row.price else "не задана"
        dur_txt = f"{row.duration_min} мин"
        status = "активна" if row.is_active else "⚪ скрыта"
        head = f"{notice}\n\n" if notice else ""
        text = (f"{head}{emoji} <b>{_esc(label)}</b>\n"
                f"Цена: {price_txt}\n"
                f"Длительность: {dur_txt}\n"
                f"Статус: {status}")
        toggle_btn = (
            Button("⏸ Поставить на паузу", f"adm:svc:{key}:deact")
            if row.is_active else
            Button("▶️ Активировать", f"adm:svc:{key}:act")
        )
        btn_rows_list = [
            (Button("Изм. цену", f"adm:svc:{key}:price"),
             Button("Изм. длит.", f"adm:svc:{key}:dur")),
            (toggle_btn,),
        ]
        # Кнопка физического удаления — только деактивированной и без ссылок
        if not row.is_active and refs == 0:
            btn_rows_list.append((Button("🗑 Удалить совсем", f"adm:svc:{key}:del"),))
        btn_rows_list.append((Button("◀ Услуги", "adm:services"),))
        return Reply(text, button_rows=tuple(btn_rows_list))

    def _begin_dur_edit(self, chat_id: int, key: str, message_id: int | None) -> None:
        self._set_pending(chat_id, f"dur:{key}")
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = services_repo.service_list_all(session)
        row = next((r for r in rows_data if r.name == key), None)
        cur_txt = f"{row.duration_min} мин" if row else "неизвестно"
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        reply = Reply(
            f"⏱ <b>{_esc(label)}</b>\nТекущая длительность: {cur_txt}\n\n"
            f"Введите длительность в минутах (5–480), например 30.",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_duration(self, chat_id: int, key: str, raw: str) -> Reply:
        if not raw.isdigit() or not DUR_MIN <= int(raw) <= DUR_MAX:
            return Reply(
                f"⚠️ Длительность — целое число от {DUR_MIN} до {DUR_MAX} минут.",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        dur = int(raw)
        onboard.set_service_duration(self._sf, self._cid, key, dur)
        self._clear_pending(chat_id)
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        return self._service_card(
            key, notice=f"✅ Длительность «{_esc(label)}»: {dur} мин")

    def _svcadd_catalog_menu(self, notice: str = "") -> Reply:
        """Каталог услуг, ещё не добавленных в клинику."""
        with tenant_transaction(self._sf, self._cid) as session:
            existing = {r.name for r in services_repo.service_list_all(session)}
        from navbat.nlu.schema import SERVICE_KEYS
        missing = [k for k in SERVICE_KEYS if k not in existing]
        if not missing:
            return Reply(
                "✅ Все услуги из каталога уже добавлены.",
                button_rows=((Button("◀ Назад", "adm:services"),),))
        rows = []
        for k in missing:
            emoji = SERVICE_EMOJI.get(k, "")
            label = SERVICE_LABELS.get(k, {}).get("ru", k)
            rows.append((Button(f"{emoji} {label}".strip(), f"adm:svcadd:{k}"),))
        rows.append((Button("◀ Назад", "adm:services"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(
            f"{head}Добавить услугу из каталога:",
            button_rows=tuple(rows))

    def _begin_svcadd(self, chat_id: int, key: str, message_id: int | None) -> None:
        """Задать pending svcadd:<key> и попросить длительность."""
        from navbat.nlu.schema import SERVICE_KEYS
        if key not in SERVICE_KEYS:
            self._edit_or_send(chat_id, message_id, self._svcadd_catalog_menu())
            return
        self._set_pending(chat_id, f"svcadd:{key}")
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        reply = Reply(
            f"➕ <b>{_esc(label)}</b>\nВведите длительность приёма в минутах "
            f"({DUR_MIN}–{DUR_MAX}), например 30.",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_svcadd(self, chat_id: int, key: str, raw: str) -> Reply:
        if not raw.isdigit() or not DUR_MIN <= int(raw) <= DUR_MAX:
            return Reply(
                f"Введите длительность в минутах ({DUR_MIN}–{DUR_MAX}):",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        dur = int(raw)
        # add_service создаёт новую строку; set_service_duration работает только
        # для уже существующих — не использовать здесь
        try:
            onboard.add_service(self._sf, self._cid, key, dur)
        except ValueError as exc:
            self._clear_pending(chat_id)
            return self._services_menu(notice=f"⚠️ {_esc(str(exc))}")
        self._clear_pending(chat_id)
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        return self._services_menu(
            notice=f"✅ Услуга «{_esc(label)}» добавлена, {dur} мин")

    # -- Цены (backward compat) -----------------------------------------------

    def _begin_price_edit(self, chat_id: int, key: str, message_id: int | None) -> None:
        self._set_pending(chat_id, f"price:{key}")
        with tenant_transaction(self._sf, self._cid) as session:
            # service_price фильтрует is_active — для деактивированной услуги
            # вернёт None даже если цена задана; service_list_all даёт честную цену
            rows_all = services_repo.service_list_all(session)
        row = next((r for r in rows_all if r.name == key), None)
        current = row.price if row is not None else None
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        cur_txt = f"{_fmt_sum(current)} сум" if current is not None else "не задана"
        reply = Reply(
            f"💰 <b>{_esc(label)}</b>\nТекущая цена: {cur_txt}\n\n"
            f"Введите новую цену в сумах, например 400000.",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_price(self, chat_id: int, key: str, raw: str) -> Reply:
        value = raw.strip()
        if not value.isdigit() or not 0 < int(value) <= PRICE_MAX:
            return Reply(
                "⚠️ Цена — целое число сум больше нуля, например 400000.\n"
                "Введите ещё раз или нажмите «Отмена».",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        price = int(value)
        onboard.set_service_price(self._sf, self._cid, key, price)
        self._clear_pending(chat_id)
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        return self._service_card(
            key, notice=f"✅ Цена «{_esc(label)}»: {_fmt_sum(price)} сум")

    # -- FAQ О клинике ---------------------------------------------------------

    def _faq_menu(self, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            values = {field: reader(session) for field, reader in _FAQ_READERS.items()}
        rows = (
            (Button(self._faq_btn("📍 Адрес", values["address"]), "adm:faq:address"),),
            (Button(self._faq_btn("💳 Оплата", values["payment"]), "adm:faq:payment"),),
            (Button(self._faq_btn("📞 Телефон", values["phone"]), "adm:faq:phone"),),
            (Button("◀ Меню", "adm:home"),),
        )
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}🏥 <b>О клинике</b>\nВыберите поле:", button_rows=rows)

    @staticmethod
    def _faq_btn(label: str, value: str | None) -> str:
        if not value:
            return f"{label}: не задано"
        short = value if len(value) <= 30 else value[:29] + "…"
        return f"{label}: {short}"

    def _begin_faq_edit(self, chat_id: int, field: str, message_id: int | None) -> None:
        if field not in _FAQ_READERS:
            return
        self._set_pending(chat_id, f"faq:{field}")
        with tenant_transaction(self._sf, self._cid) as session:
            current = _FAQ_READERS[field](session)
        cur_txt = _esc(current) if current else "не задано"
        reply = Reply(
            f"🏥 <b>{_FAQ_TITLES[field]}</b>\nТекущее значение: {cur_txt}\n\n"
            f"Введите новое значение или нажмите «Отмена».",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_faq(self, chat_id: int, field: str, raw: str) -> Reply:
        if field not in _FAQ_WRITERS:
            self._clear_pending(chat_id)
            return self.main_menu()
        value = raw.strip()
        if not value or len(value) > FAQ_MAX:
            return Reply(
                f"⚠️ Введите непустой текст до {FAQ_MAX} символов.",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        _FAQ_WRITERS[field](self._sf, self._cid, value)
        self._clear_pending(chat_id)
        return self._faq_menu(notice=f"✅ {_FAQ_TITLES[field]} обновлено")

    # -- Врачи (P-3) ----------------------------------------------------------

    def _doctors_menu(self, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            docs = doctors_repo.doctor_list_all(session)
        rows = []
        for doc in docs:
            name = doc.name or f"[врач {str(doc.id)[:8]}]"
            if doc.is_active:
                btn_text = f"🧑‍⚕️ {name}"
            else:
                btn_text = f"⚪ {name} (скрыт)"
            rows.append((Button(btn_text, f"adm:doc:{doc.id}"),))
        rows.append((Button("+ Добавить врача", "adm:docadd:"),))
        rows.append((Button("◀ Меню", "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(
            f"{head}🧑‍⚕️ <b>Врачи</b>\nВыберите врача:",
            button_rows=tuple(rows))

    def _doctor_callback(self, chat_id: int, message_id: int | None, arg: str) -> None:
        parts = arg.split(":", 1)
        doc_id_str = parts[0]
        action = parts[1] if len(parts) > 1 else None
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._edit_or_send(chat_id, message_id, self._doctors_menu())
            return
        if action == "name":
            self._begin_dname(chat_id, doc_id_str, message_id)
        elif action == "buf":
            self._begin_dbuf(chat_id, doc_id_str, message_id)
        elif action == "sched":
            self._sched_entry(chat_id, doc_id_str, message_id)
        elif action == "deact":
            onboard.deactivate_doctor(self._sf, self._cid, doc_id)
            r = self._doctors_menu(notice="⏸ Врач поставлен на паузу")
            self._edit_or_send(chat_id, message_id, r)
        elif action == "act":
            onboard.activate_doctor(self._sf, self._cid, doc_id)
            r = self._doctors_menu(notice="✅ Врач активирован")
            self._edit_or_send(chat_id, message_id, r)
        elif action == "del":
            try:
                onboard.delete_doctor(self._sf, self._cid, doc_id)
                r = self._doctors_menu(notice="✅ Врач удалён")
            except ValueError as e:
                r = self._doctor_card(doc_id_str, notice=f"⚠️ {_esc(str(e))}")
            self._edit_or_send(chat_id, message_id, r)
        else:
            r = self._doctor_card(doc_id_str)
            self._edit_or_send(chat_id, message_id, r)

    @staticmethod
    def _doctor_refs(session, doctor_id) -> int:
        """Число ссылок на врача в appointment (для гейтинга кнопки удаления).
        Физическое удаление при наличии записей заблокировано FK RESTRICT."""
        from sqlalchemy import text as _text
        return session.execute(
            _text("SELECT count(*) FROM appointment WHERE doctor_id = :d"),
            {"d": str(doctor_id)},
        ).scalar_one()

    def _doctor_card(self, doc_id_str: str, notice: str = "") -> Reply:
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            return self._doctors_menu()
        with tenant_transaction(self._sf, self._cid) as session:
            docs = doctors_repo.doctor_list_all(session)
            doc = next((d for d in docs if d.id == doc_id), None)
            refs = self._doctor_refs(session, doc_id) if doc is not None else 0
        if doc is None:
            return self._doctors_menu()
        name = doc.name or "(без имени)"
        status = "активен" if doc.is_active else "⚪ скрыт"
        sch = _format_schedule(doc.working_intervals or {})
        head = f"{notice}\n\n" if notice else ""
        text = (f"{head}🧑‍⚕️ <b>{_esc(name)}</b>\n"
                f"Буфер: {doc.buffer_min} мин\n"
                f"Статус: {status}\n"
                f"Расписание:\n{_esc(sch)}")
        toggle_btn = (
            Button("⏸ Пауза", f"adm:doc:{doc_id}:deact")
            if doc.is_active else
            Button("▶️ Активировать", f"adm:doc:{doc_id}:act")
        )
        btn_rows_list = [
            (Button("Имя", f"adm:doc:{doc_id}:name"),
             Button("Буфер", f"adm:doc:{doc_id}:buf")),
            (Button("📅 Расписание", f"adm:doc:{doc_id}:sched"),),
            (toggle_btn,),
        ]
        # Кнопка физического удаления — только деактивированного и без записей
        if not doc.is_active and refs == 0:
            btn_rows_list.append((Button("🗑 Удалить совсем", f"adm:doc:{doc_id}:del"),))
        btn_rows_list.append((Button("◀ Врачи", "adm:doctors"),))
        return Reply(text, button_rows=tuple(btn_rows_list))

    def _begin_dname(self, chat_id: int, doc_id_str: str, message_id: int | None) -> None:
        self._set_pending(chat_id, f"dname:{doc_id_str}")
        reply = Reply(
            "Введите имя врача (80 символов максимум):",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_dname(self, chat_id: int, doc_id_str: str, raw: str) -> Reply:
        name = raw.strip()
        if not name or len(name) > NAME_MAX:
            return Reply(
                f"Имя — непустая строка до {NAME_MAX} символов.",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._clear_pending(chat_id)
            return self._doctors_menu()
        onboard.rename_doctor(self._sf, self._cid, doc_id, name)
        self._clear_pending(chat_id)
        return self._doctor_card(doc_id_str, notice=f"✅ Имя обновлено: {_esc(name)}")

    def _begin_dbuf(self, chat_id: int, doc_id_str: str, message_id: int | None) -> None:
        self._set_pending(chat_id, f"dbuf:{doc_id_str}")
        reply = Reply(
            f"Введите буфер в минутах (0–120), например 10:",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_dbuf(self, chat_id: int, doc_id_str: str, raw: str) -> Reply:
        if not raw.isdigit() or not BUF_MIN <= int(raw) <= BUF_MAX:
            return Reply(
                f"Буфер — целое число от {BUF_MIN} до {BUF_MAX}.",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        buf = int(raw)
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._clear_pending(chat_id)
            return self._doctors_menu()
        onboard.set_doctor_buffer(self._sf, self._cid, doc_id, buf)
        self._clear_pending(chat_id)
        return self._doctor_card(doc_id_str, notice=f"✅ Буфер: {buf} мин")

    def _begin_docadd(self, chat_id: int, message_id: int | None) -> None:
        self._set_pending(chat_id, "dadd:")
        reply = Reply(
            "Введите имя нового врача:",
            button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_dadd(self, chat_id: int, raw: str) -> Reply:
        name = raw.strip()
        if not name or len(name) > NAME_MAX:
            return Reply(
                f"Имя — непустая строка до {NAME_MAX} символов.",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        onboard.add_doctor(self._sf, self._cid, name)
        self._clear_pending(chat_id)
        return self._doctors_menu(notice=f"✅ Врач «{_esc(name)}» добавлен")

    # -- Расписание -----------------------------------------------------------

    def _sched_entry(self, chat_id: int, doc_id_str: str, message_id: int | None) -> None:
        rows = []
        for i, (tpl_name, _wi) in enumerate(_SCHEDULE_TEMPLATES):
            rows.append((Button(tpl_name, f"adm:sched:tpl:{doc_id_str}:{i}"),))
        rows.append((Button("📝 Свой график", f"adm:sched:custom:{doc_id_str}"),))
        rows.append((Button("◀ Назад", f"adm:doc:{doc_id_str}"),))
        r = Reply(
            "📅 <b>Расписание</b>\nВыберите шаблон или задайте свой:",
            button_rows=tuple(rows))
        self._edit_or_send(chat_id, message_id, r)

    def _sched_callback(self, chat_id: int, message_id: int | None, arg: str) -> None:
        parts = arg.split(":", 2)
        action = parts[0]
        if action == "tpl" and len(parts) >= 3:
            doc_id_str, tpl_idx_str = parts[1], parts[2]
            try:
                tpl_idx = int(tpl_idx_str)
                wi = _SCHEDULE_TEMPLATES[tpl_idx][1]
                doc_id = _uuid_mod.UUID(doc_id_str)
            except (ValueError, IndexError):
                self._edit_or_send(chat_id, message_id, self._doctors_menu())
                return
            onboard.set_doctor_schedule(self._sf, self._cid, doc_id, wi)
            r = self._doctor_card(doc_id_str, notice="✅ Расписание задано")
            self._edit_or_send(chat_id, message_id, r)
        elif action == "custom" and len(parts) >= 2:
            doc_id_str = parts[1]
            # выбор дней начинаем с чистого листа: незавершённый выбор для
            # другого врача иначе протёк бы в этот график (C1)
            self._set_sched_days(chat_id, set())
            self._sched_custom_days(chat_id, doc_id_str, message_id, selected=set())
        elif action == "day" and len(parts) >= 3:
            doc_id_str = parts[1]
            day = parts[2]
            selected = self._get_sched_days(chat_id)
            if day in selected:
                selected.discard(day)
            else:
                selected.add(day)
            self._set_sched_days(chat_id, selected)
            self._sched_custom_days(chat_id, doc_id_str, message_id, selected)
        elif action == "next" and len(parts) >= 2:
            doc_id_str = parts[1]
            selected = self._get_sched_days(chat_id)
            if not selected:
                r = Reply(
                    "Выберите хотя бы один рабочий день.",
                    button_rows=((Button("◀ Назад", f"adm:doc:{doc_id_str}:sched"),),))
                self._edit_or_send(chat_id, message_id, r)
                return
            self._set_pending(chat_id, f"sched:{doc_id_str}")
            days_txt = ", ".join(_WEEKDAY_RU[d] for d in WEEKDAY_KEYS if d in selected)
            r = Reply(
                f"Дни: {days_txt}\n\n"
                "Введите смены через запятую, например:\n"
                "<code>09:00-13:00, 14:00-18:00</code>",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
            self._edit_or_send(chat_id, message_id, r)

    def _sched_custom_days(self, chat_id: int, doc_id_str: str,
                           message_id: int | None, selected: set) -> None:
        rows = []
        for day in WEEKDAY_KEYS:
            mark = "✅ " if day in selected else ""
            rows.append((Button(f"{mark}{_WEEKDAY_RU[day]}",
                                f"adm:sched:day:{doc_id_str}:{day}"),))
        rows.append((Button("Далее →", f"adm:sched:next:{doc_id_str}"),))
        rows.append((Button("◀ Назад", f"adm:doc:{doc_id_str}:sched"),))
        r = Reply(
            "📅 Отметьте рабочие дни:",
            button_rows=tuple(rows))
        self._edit_or_send(chat_id, message_id, r)

    def _apply_sched_shifts(self, chat_id: int, doc_id_str: str, raw: str) -> Reply:
        shifts = _parse_shifts(raw)
        if shifts is None:
            return Reply(
                "⚠️ Формат: <code>09:00-13:00, 14:00-18:00</code>\nВведите ещё раз:",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        selected = self._get_sched_days(chat_id)
        wi = {d: shifts for d in selected}
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._clear_pending(chat_id)
            self._clear_sched_days(chat_id)
            return self._doctors_menu()
        onboard.set_doctor_schedule(self._sf, self._cid, doc_id, wi)
        self._clear_pending(chat_id)
        self._clear_sched_days(chat_id)
        return self._doctor_card(doc_id_str, notice="✅ Расписание задано")

    # -- раздел «Выходные» ----------------------------------------------------

    def _dayoff_menu(self, notice: str = "") -> Reply:
        from sqlalchemy import text as _text

        today = self._worker._clinic_today()
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = session.execute(
                _text("SELECT date, reason FROM holiday WHERE date >= :t "
                      "ORDER BY date LIMIT 20"), {"t": today},
            ).all()
        rows = []
        for r in rows_data:
            label = f"{r.date:%d.%m.%Y}" + (f" ({r.reason})" if r.reason else "")
            rows.append((Button(f"{label} ✖",
                                f"adm:dayoff:open:{r.date.isoformat()}"),))
        rows.append((Button("➕ Закрыть день", "adm:dayoff:add"),))
        rows.append((Button("◀ Меню", "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        intro = ("Ближайшие закрытые дни (тап — снова открыть):"
                 if rows_data else "Закрытых дней впереди нет.")
        return Reply(f"{head}📅 <b>Выходные</b>\n{intro}", button_rows=tuple(rows))

    def _handle_dayoff_callback(self, chat_id: int, message_id: int | None,
                               arg: str) -> None:
        from datetime import date as _date

        from sqlalchemy import text as _text

        sub, _, rest = arg.partition(":")
        if sub == "add":
            self._set_pending(chat_id, "dayoff")
            self._edit_or_send(chat_id, message_id, Reply(
                "📅 Введите дату и (по желанию) причину:\n"
                "<code>21.03 Навруз</code>",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),)))
            return
        if sub == "open":
            try:
                target = _date.fromisoformat(rest)
            except ValueError:
                self._edit_or_send(chat_id, message_id, self._dayoff_menu())
                return
            with tenant_transaction(self._sf, self._cid) as session:
                session.execute(_text("DELETE FROM holiday WHERE date = :d"),
                                {"d": target})
            self._worker._send(chat_id,
                               self._dayoff_menu(notice="✅ День снова рабочий"))
            return
        # body был просто «dayoff» — показать меню
        self._edit_or_send(chat_id, message_id, self._dayoff_menu())

    def _apply_dayoff(self, chat_id: int, raw: str) -> Reply:
        from sqlalchemy import text as _text

        parts = raw.split(maxsplit=1)
        today = self._worker._clinic_today()
        target = self._worker._parse_ddmm(parts[0], today) if parts else None
        if target is None:
            return Reply(
                "⚠️ Формат: <code>21.03 причина</code> (день.месяц). "
                "Повторите или нажмите «Отмена».",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        reason = parts[1].strip() if len(parts) > 1 else None
        with tenant_transaction(self._sf, self._cid) as session:
            exists = session.execute(
                _text("SELECT 1 FROM holiday WHERE date = :d"), {"d": target},
            ).scalar_one_or_none()
            if not exists:
                session.execute(
                    _text("INSERT INTO holiday (clinic_id, date, reason) VALUES "
                          "(current_setting('app.clinic_id')::uuid, :d, :r)"),
                    {"d": target, "r": reason})
        self._clear_pending(chat_id)
        return self._dayoff_menu(notice=f"✅ {target:%d.%m.%Y} — выходной")

    # -- extras: sched days ---------------------------------------------------

    def _get_sched_days(self, chat_id: int) -> set:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
        return set(conv.context.extras.get("adm_sch_days", []))

    def _set_sched_days(self, chat_id: int, days: set) -> None:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
            conv.context.extras["adm_sch_days"] = sorted(days)
            save_conversation(session, conv)

    def _clear_sched_days(self, chat_id: int) -> None:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
            if conv.context.extras.pop("adm_sch_days", None) is not None:
                save_conversation(session, conv)

    # -- pending-ввод ---------------------------------------------------------

    def _get_pending(self, chat_id: int) -> str | None:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
        return conv.context.extras.get("adm_pending")

    def _set_pending(self, chat_id: int, value: str) -> None:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
            conv.context.extras["adm_pending"] = value
            save_conversation(session, conv)

    def _clear_pending(self, chat_id: int) -> None:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
            if conv.context.extras.pop("adm_pending", None) is not None:
                save_conversation(session, conv)

    # -- отправка/редактирование ----------------------------------------------

    def _edit_or_send(self, chat_id: int, message_id: int | None,
                      reply: Reply) -> None:
        if message_id is not None:
            self._worker._edit(chat_id, message_id, reply)
        else:
            self._worker._send(chat_id, reply)
