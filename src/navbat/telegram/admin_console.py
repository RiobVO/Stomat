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
import logging
import re
import uuid as _uuid_mod

from navbat import onboard
from navbat.db.base import tenant_transaction
from navbat.dialog import clinic_repo, doctors_repo, services_repo
from navbat.dialog.conversation import load_conversation, save_conversation
from navbat.dialog.replies import SERVICE_EMOJI, SERVICE_LABELS, Button, Reply
from navbat.scheduling.calendar_rules import WEEKDAY_KEYS
from navbat.telegram.admin_texts import (CANCEL_WORDS, DEFAULT_LANG, LANGS,
                                         TEMPLATES, at, menu_key)

log = logging.getLogger("navbat.telegram.admin")

# верхнее меню — reply-клавиатура. Константы = русские подписи: их знают
# тесты и онбординг-руководство, узбекские приходят из словаря по языку чата
BTN_SERVICES = TEMPLATES["btn_services"]["ru"]
BTN_DOCTORS = TEMPLATES["btn_doctors"]["ru"]
BTN_ABOUT = TEMPLATES["btn_about"]["ru"]
BTN_DAYOFF = TEMPLATES["btn_dayoff"]["ru"]
BTN_STATS = TEMPLATES["btn_stats"]["ru"]
BTN_PAUSE = TEMPLATES["btn_pause"]["ru"]
BTN_RESUME = TEMPLATES["btn_resume"]["ru"]

PRICE_MAX = 1_000_000_000
FAQ_MAX = 500
DUR_MIN, DUR_MAX = 5, 480
# Интервалы повторного визита: не свободный ввод, а три ходовых срока
# (гигиена — полгода, осмотр — год). Владелец выбирает одним тапом
RECALL_CHOICES = (3, 6, 12)
BUF_MIN, BUF_MAX = 0, 120
NAME_MAX = 80

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

# Шаблоны расписания: ключ подписи + сам график
_SCHEDULE_TEMPLATES = [
    ("sched_tpl_workweek", {
        d: [["09:00", "18:00"]]
        for d in ("mon", "tue", "wed", "thu", "fri")
    }),
    ("sched_tpl_six_days", {
        d: [["09:00", "13:00"], ["14:00", "18:00"]]
        for d in ("mon", "tue", "wed", "thu", "fri", "sat")
    }),
    ("sched_tpl_late", {
        d: [["10:00", "19:00"]]
        for d in ("mon", "tue", "wed", "thu", "fri")
    }),
]


def _fmt_sum(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _esc(value: str) -> str:
    return html.escape(str(value), quote=False)


def _weekday(day: str, lang: str) -> str:
    return at(f"weekday_{day}", lang)


def _status_badge(is_active: bool, female: bool, lang: str = DEFAULT_LANG) -> str:
    """Статус-бейдж сущности: 🟢 активна / ⚪ скрыта (род по female)."""
    gender = "f" if female else "m"
    return at(f"badge_{'active' if is_active else 'hidden'}_{gender}", lang)


def _format_schedule(wi: dict, lang: str = DEFAULT_LANG) -> str:
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
            lines.append(f"{_weekday(first, lang)} {spans}")
        else:
            lines.append(f"{_weekday(first, lang)}–{_weekday(last, lang)} {spans}")
    return "\n".join(lines) if lines else at("week_off", lang)


def booked_warning(session, day, lang: str = DEFAULT_LANG) -> str:
    """Предупреждение о живых приёмах закрываемого дня (карта, №7) — пустая
    строка, когда их нет.

    Решение владельца: закрытие дня НЕ отменяет записи (договорённость с
    пациентом бот не рвёт) — но и закрывать день вслепую не даёт: раньше
    владелец не узнавал о приёмах, а пациенту в этот день ещё и приходило
    напоминание «Ждём вас!»."""
    from zoneinfo import ZoneInfo

    from sqlalchemy import text as _text

    tz_name = session.execute(_text(
        "SELECT timezone FROM clinic "
        "WHERE id = current_setting('app.clinic_id')::uuid")).scalar_one()
    tz = ZoneInfo(tz_name)
    rows = session.execute(
        _text("SELECT lower(time_range) AS start FROM appointment "
              "WHERE status = 'booked' AND lower(time_range) > now() "
              "AND (lower(time_range) AT TIME ZONE :tz)::date = :d "
              "ORDER BY lower(time_range)"),
        {"tz": tz_name, "d": day}).all()
    if not rows:
        return ""
    shown = ", ".join(f"{r.start.astimezone(tz):%H:%M}" for r in rows[:5])
    if len(rows) > 5:
        shown += at("dayoff_warning_more", lang, count=len(rows) - 5)
    return at("dayoff_booked_warning", lang, count=len(rows), times=shown)


def _failure_key(error: Exception, kind: str) -> str:
    """Ключ строки отказа по ТИПУ ошибки слоя данных: причины разные
    («ещё доступна пациентам» против «на неё ссылаются записи»), а разбирать
    русский текст исключения нельзя (ревью волны C)."""
    if isinstance(error, onboard.EntityActive):
        return f"{kind}_still_active"
    if isinstance(error, onboard.EntityMissing):
        return f"{kind}_missing"
    return f"{kind}_in_use"


def _day_hours(shifts, lang: str = DEFAULT_LANG) -> str:
    """Часы одного дня для подписи кнопки: «09:00–13:00 / 14:00–18:00»."""
    if not shifts:
        return at("day_off", lang)
    return " / ".join(f"{s[0]}–{s[1]}" for s in shifts)


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
        key = menu_key(stripped)
        if key is not None:
            if pending:
                self._clear_pending(chat_id)
            return self._menu_action(chat_id, key)
        if pending and stripped.lower() in CANCEL_WORDS:
            self._clear_pending(chat_id)
            return self.main_menu(chat_id)
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
        return self.main_menu(chat_id)

    def handle_callback(self, callback: dict, chat_id: int, data: str) -> None:
        self._api.answer_callback_query(callback["id"])
        message_id = callback["message"].get("message_id")
        body = data[len("adm:"):]
        if body in ("home", "cancel"):
            self._clear_pending(chat_id)
            self._worker._send(chat_id, self.main_menu(chat_id))
            return
        if body == "today":
            # «Обновить» перерисовывает день на месте: экран висит в чате
            # часами, а записи за это время приходят и отменяются
            self._edit_or_send(chat_id, message_id,
                               self._worker._today_reply(lang=self._lang(chat_id)))
            return
        kind, _, arg = body.partition(":")
        if body == "services" or kind == "services":
            r = self._services_menu(self._lang(chat_id))
            self._edit_or_send(chat_id, message_id, r)
            return
        if kind == "svc":
            self._service_callback(chat_id, message_id, arg)
            return
        if body == "svcadd":
            # adm:svcadd (без ключа) → показать каталог добавляемых услуг
            r = self._svcadd_catalog_menu(self._lang(chat_id))
            self._edit_or_send(chat_id, message_id, r)
            return
        if kind == "svcadd":
            # adm:svcadd:<key> → начать добавление конкретной услуги
            self._begin_svcadd(chat_id, arg, message_id)
            return
        if kind == "doctors":
            r = self._doctors_menu(self._lang(chat_id))
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
        if kind == "preview":
            self._edit_or_send(chat_id, message_id,
                               self._preview(self._lang(chat_id), arg))
            return
        if kind == "faq":
            self._begin_faq_edit(chat_id, arg, message_id)

    def main_menu(self, chat_id: int | None = None) -> Reply:
        lang = self._lang(chat_id) if chat_id is not None else DEFAULT_LANG
        paused = self._worker._bot_paused()
        pause_btn = at("btn_resume" if paused else "btn_pause", lang)
        rows = ((at("btn_services", lang), at("btn_doctors", lang)),
                (at("btn_about", lang), at("btn_dayoff", lang)),
                (at("btn_today_list", lang), at("btn_stats", lang)),
                (at("btn_preview", lang), at("btn_lang", lang)),
                (pause_btn,))
        head = at("console_paused", lang) if paused else ""
        return Reply(f"{head}{at('console_title', lang)}", menu=rows)

    # -- Роутинг ---------------------------------------------------------------

    def _menu_action(self, chat_id: int, key: str) -> Reply:
        lang = self._lang(chat_id)
        if key == "btn_services":
            return self._services_menu(lang)
        if key == "btn_doctors":
            return self._doctors_menu(lang)
        if key == "btn_about":
            return self._faq_menu(lang)
        if key == "btn_dayoff":
            return self._dayoff_menu(lang)
        if key == "btn_today_list":
            return self._worker._today_reply(lang=lang)
        if key == "btn_stats":
            return self._worker._stats_reply(lang=lang)
        if key == "btn_lang":
            return self._switch_lang(chat_id, lang)
        if key == "btn_preview":
            return self._preview(lang, DEFAULT_LANG)
        if key in ("btn_pause", "btn_resume"):
            return self._toggle_pause(chat_id)
        return self.main_menu(chat_id)

    def _switch_lang(self, chat_id: int, lang: str) -> Reply:
        """Тумблер ru↔uz: языков два, отдельный экран выбора был бы лишним."""
        new_lang = LANGS[(LANGS.index(lang) + 1) % len(LANGS)]
        self._set_lang(chat_id, new_lang)
        menu = self.main_menu(chat_id)
        return Reply(at("lang_switched", new_lang), menu=menu.menu)

    def _toggle_pause(self, chat_id: int | None = None) -> Reply:
        lang = self._lang(chat_id) if chat_id is not None else DEFAULT_LANG
        if self._worker._bot_paused():
            conf = self._worker._resume_reply(lang)
        else:
            conf = self._worker._pause_reply("/pause", lang)
        return Reply(conf.text, menu=self.main_menu(chat_id).menu)

    # -- Превью «глазами пациента» (карта, №14) --------------------------------

    def _preview(self, lang: str, patient_lang: str) -> Reply:
        """Экраны пациента одним сообщением админ-чата.

        Пациентское меню — reply-клавиатура: показать её здесь значит
        затереть админскую, поэтому кнопки пациента рисуются текстом.
        Язык превью живёт в самом callback'е — он про пациента, а не про
        консоль, и не должен переключать язык владельца."""
        if patient_lang not in LANGS:
            patient_lang = DEFAULT_LANG
        screens = self._worker._dialog.preview_screens(patient_lang)
        blocks = []
        for reply in screens:
            block = reply.text
            if reply.menu:
                buttons = " · ".join(label for row in reply.menu for label in row)
                block += at("preview_menu", lang, buttons=buttons)
            blocks.append(block)
        head = at("preview_head", lang,
                  language=at(f"preview_lang_{patient_lang}", lang))
        other = "uz" if patient_lang == "ru" else "ru"
        return Reply(
            head + "\n\n— — —\n\n".join(blocks),
            button_rows=((Button(at(f"btn_preview_{other}", lang),
                                 f"adm:preview:{other}"),),
                         (Button(at("btn_home", lang), "adm:home"),)))

    # -- Раздел Услуги (P-2) -------------------------------------------------

    def _services_menu(self, lang: str = DEFAULT_LANG, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = services_repo.service_list_all(session)
        rows = []
        for row in rows_data:
            emoji = SERVICE_EMOJI.get(row.name, "")
            label = self._service_label(row.name, lang)
            if row.is_active:
                btn_text = f"{emoji} {label}".strip()
            else:
                btn_text = at("svc_hidden_item", lang, name=label)
            rows.append((Button(btn_text, f"adm:svc:{row.name}"),))
        rows.append((Button(at("btn_svc_add", lang), "adm:svcadd"),))
        rows.append((Button(at("btn_home", lang), "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}{at('services_title', lang)}",
                     button_rows=tuple(rows))

    @staticmethod
    def _service_label(key: str, lang: str) -> str:
        return SERVICE_LABELS.get(key, {}).get(lang, key)

    def _service_callback(self, chat_id: int, message_id: int | None, arg: str) -> None:
        lang = self._lang(chat_id)
        parts = arg.split(":", 1)
        key = parts[0]
        action = parts[1] if len(parts) > 1 else None
        if action == "price":
            self._begin_price_edit(chat_id, key, message_id)
        elif action == "dur":
            self._begin_dur_edit(chat_id, key, message_id)
        elif action == "deact":
            onboard.deactivate_service(self._sf, self._cid, key)
            r = self._services_menu(lang, notice=at("svc_hidden_notice", lang))
            self._edit_or_send(chat_id, message_id, r)
        elif action == "act":
            onboard.activate_service(self._sf, self._cid, key)
            r = self._services_menu(lang, notice=at("svc_shown_notice", lang))
            self._edit_or_send(chat_id, message_id, r)
        elif action is not None and action.startswith("recall:"):
            self._apply_recall(chat_id, message_id, key,
                               action[len("recall:"):])
        elif action == "del":
            r = self._service_card(key, lang,
                                   notice=at("svc_delete_confirm", lang),
                                   confirm_delete=True)
            self._edit_or_send(chat_id, message_id, r)
        elif action == "delyes":
            try:
                onboard.delete_service(self._sf, self._cid, key)
                r = self._services_menu(lang, notice=at("svc_deleted", lang))
            except ValueError as e:
                # причина различается (ещё активна / есть записи), а текст
                # исключения только русский — ключ выбираем по ТИПУ ошибки
                log.warning("удаление услуги %s отклонено: %s", key, e)
                r = self._service_card(key, lang,
                                       notice=at(_failure_key(e, "svc"), lang))
            self._edit_or_send(chat_id, message_id, r)
        else:
            r = self._service_card(key, lang)
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

    def _service_card(self, key: str, lang: str = DEFAULT_LANG, notice: str = "",
                      confirm_delete: bool = False) -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = services_repo.service_list_all(session)
            row = next((r for r in rows_data if r.name == key), None)
            refs = self._service_refs(session, key) if row is not None else 0
        if row is None:
            return self._services_menu(lang)
        price_txt = (at("sum", lang, value=_fmt_sum(row.price)) if row.price
                     else at("price_unset", lang))
        head = f"{notice}\n\n" if notice else ""
        text = head + at("svc_card", lang,
                         emoji=SERVICE_EMOJI.get(key, ""),
                         name=self._service_label(key, lang),
                         price=price_txt,
                         duration=at("minutes", lang, value=row.duration_min),
                         recall=self._recall_value(row.recall_months, lang),
                         badge=_status_badge(row.is_active, True, lang))
        toggle_btn = (
            Button(at("btn_hide", lang), f"adm:svc:{key}:deact")
            if row.is_active else
            Button(at("btn_show", lang), f"adm:svc:{key}:act")
        )
        if confirm_delete:
            return Reply(text, button_rows=(
                (Button(at("btn_delete_yes", lang), f"adm:svc:{key}:delyes"),),
                (Button(at("btn_cancel", lang), f"adm:svc:{key}"),)))
        btn_rows_list = [
            (Button(at("btn_price_edit", lang), f"adm:svc:{key}:price"),
             Button(at("btn_dur_edit", lang), f"adm:svc:{key}:dur")),
            # повторный визит — один тап без ввода: значение сразу на кнопке
            (Button(at("btn_recall_off", lang), f"adm:svc:{key}:recall:off"),)
            + tuple(Button(at("recall_value", lang, value=months),
                           f"adm:svc:{key}:recall:{months}")
                    for months in RECALL_CHOICES),
            (toggle_btn,),
        ]
        # Кнопка физического удаления — только деактивированной и без ссылок
        if not row.is_active and refs == 0:
            btn_rows_list.append(
                (Button(at("btn_delete", lang), f"adm:svc:{key}:del"),))
        btn_rows_list.append(
            (Button(at("btn_services_back", lang), "adm:services"),))
        return Reply(text, button_rows=tuple(btn_rows_list))

    @staticmethod
    def _recall_value(months: int | None, lang: str) -> str:
        """Интервал повторного визита словами: «6 мес» или «выкл» (NULL)."""
        if not months:
            return at("recall_off", lang)
        return at("recall_value", lang, value=months)

    def _apply_recall(self, chat_id: int, message_id: int | None, key: str,
                      raw: str) -> None:
        """`adm:svc:<ключ>:recall:off|3|6|12` — интервал повторного визита."""
        lang = self._lang(chat_id)
        if raw == "off":
            months = None
        elif raw.isdigit() and int(raw) in RECALL_CHOICES:
            months = int(raw)
        else:
            # значения приходят с наших же кнопок; чужой callback ничего не
            # меняет и просто перерисовывает карточку
            self._edit_or_send(chat_id, message_id,
                               self._service_card(key, lang))
            return
        onboard.set_service_recall(self._sf, self._cid, key, months)
        self._edit_or_send(chat_id, message_id, self._service_card(
            key, lang, notice=at("recall_saved", lang,
                                 label=self._service_label(key, lang),
                                 value=self._recall_value(months, lang))))

    def _begin_dur_edit(self, chat_id: int, key: str, message_id: int | None) -> None:
        lang = self._lang(chat_id)
        self._set_pending(chat_id, f"dur:{key}")
        with tenant_transaction(self._sf, self._cid) as session:
            rows_data = services_repo.service_list_all(session)
        row = next((r for r in rows_data if r.name == key), None)
        cur_txt = (at("minutes", lang, value=row.duration_min) if row
                   else at("dur_unknown", lang))
        reply = Reply(
            at("dur_prompt", lang, label=self._service_label(key, lang),
               current=cur_txt, lo=DUR_MIN, hi=DUR_MAX),
            button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_duration(self, chat_id: int, key: str, raw: str) -> Reply:
        lang = self._lang(chat_id)
        if not raw.isdigit() or not DUR_MIN <= int(raw) <= DUR_MAX:
            return Reply(
                at("dur_invalid", lang, lo=DUR_MIN, hi=DUR_MAX),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        dur = int(raw)
        onboard.set_service_duration(self._sf, self._cid, key, dur)
        self._clear_pending(chat_id)
        return self._service_card(
            key, lang, notice=at("dur_saved", lang,
                                 label=self._service_label(key, lang),
                                 duration=at("minutes", lang, value=dur)))

    def _svcadd_catalog_menu(self, lang: str = DEFAULT_LANG,
                             notice: str = "") -> Reply:
        """Каталог услуг, ещё не добавленных в клинику."""
        with tenant_transaction(self._sf, self._cid) as session:
            existing = {r.name for r in services_repo.service_list_all(session)}
        from navbat.nlu.schema import SERVICE_KEYS
        missing = [k for k in SERVICE_KEYS if k not in existing]
        if not missing:
            return Reply(
                at("svcadd_all_present", lang),
                button_rows=((Button(at("btn_back", lang), "adm:services"),),))
        rows = []
        for k in missing:
            emoji = SERVICE_EMOJI.get(k, "")
            label = self._service_label(k, lang)
            rows.append((Button(f"{emoji} {label}".strip(), f"adm:svcadd:{k}"),))
        rows.append((Button(at("btn_back", lang), "adm:services"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}{at('svcadd_catalog', lang)}",
                     button_rows=tuple(rows))

    def _begin_svcadd(self, chat_id: int, key: str, message_id: int | None) -> None:
        """Задать pending svcadd:<key> и попросить длительность."""
        from navbat.nlu.schema import SERVICE_KEYS
        lang = self._lang(chat_id)
        if key not in SERVICE_KEYS:
            self._edit_or_send(chat_id, message_id,
                               self._svcadd_catalog_menu(lang))
            return
        self._set_pending(chat_id, f"svcadd:{key}")
        reply = Reply(
            at("svcadd_prompt", lang, label=self._service_label(key, lang),
               lo=DUR_MIN, hi=DUR_MAX),
            button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_svcadd(self, chat_id: int, key: str, raw: str) -> Reply:
        lang = self._lang(chat_id)
        if not raw.isdigit() or not DUR_MIN <= int(raw) <= DUR_MAX:
            return Reply(
                at("svcadd_retry", lang, lo=DUR_MIN, hi=DUR_MAX),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        dur = int(raw)
        # add_service создаёт новую строку; set_service_duration работает только
        # для уже существующих — не использовать здесь
        try:
            onboard.add_service(self._sf, self._cid, key, dur)
        except ValueError as exc:
            log.warning("добавление услуги %s отклонено: %s", key, exc)
            self._clear_pending(chat_id)
            return self._services_menu(lang, notice=at("svc_exists", lang))
        self._clear_pending(chat_id)
        return self._services_menu(
            lang, notice=at("svcadd_done", lang,
                            label=self._service_label(key, lang),
                            duration=at("minutes", lang, value=dur)))

    # -- Цены (backward compat) -----------------------------------------------

    def _begin_price_edit(self, chat_id: int, key: str, message_id: int | None) -> None:
        lang = self._lang(chat_id)
        self._set_pending(chat_id, f"price:{key}")
        with tenant_transaction(self._sf, self._cid) as session:
            # service_price фильтрует is_active — для деактивированной услуги
            # вернёт None даже если цена задана; service_list_all даёт честную цену
            rows_all = services_repo.service_list_all(session)
        row = next((r for r in rows_all if r.name == key), None)
        current = row.price if row is not None else None
        cur_txt = (at("sum", lang, value=_fmt_sum(current))
                   if current is not None else at("price_unset", lang))
        reply = Reply(
            at("price_prompt", lang, label=self._service_label(key, lang),
               current=cur_txt),
            button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_price(self, chat_id: int, key: str, raw: str) -> Reply:
        lang = self._lang(chat_id)
        value = raw.strip()
        if not value.isdigit() or not 0 < int(value) <= PRICE_MAX:
            return Reply(
                at("price_invalid", lang),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        price = int(value)
        onboard.set_service_price(self._sf, self._cid, key, price)
        self._clear_pending(chat_id)
        return self._service_card(
            key, lang, notice=at("price_saved", lang,
                                 label=self._service_label(key, lang),
                                 price=at("sum", lang, value=_fmt_sum(price))))

    # -- FAQ О клинике ---------------------------------------------------------

    def _faq_menu(self, lang: str = DEFAULT_LANG, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            values = {field: reader(session) for field, reader in _FAQ_READERS.items()}
        rows = tuple(
            (Button(self._faq_btn(at(f"faq_btn_{field}", lang),
                                  values[field], lang), f"adm:faq:{field}"),)
            for field in _FAQ_READERS
        ) + ((Button(at("btn_home", lang), "adm:home"),),)
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}{at('faq_title', lang)}", button_rows=rows)

    @staticmethod
    def _faq_btn(label: str, value: str | None, lang: str) -> str:
        if not value:
            return f"{label}: {at('faq_unset', lang)}"
        short = value if len(value) <= 30 else value[:29] + "…"
        return f"{label}: {short}"

    def _begin_faq_edit(self, chat_id: int, field: str, message_id: int | None) -> None:
        if field not in _FAQ_READERS:
            return
        lang = self._lang(chat_id)
        self._set_pending(chat_id, f"faq:{field}")
        with tenant_transaction(self._sf, self._cid) as session:
            current = _FAQ_READERS[field](session)
        reply = Reply(
            at("faq_prompt", lang, title=at(f"faq_name_{field}", lang),
               current=current if current else at("faq_unset", lang)),
            button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_faq(self, chat_id: int, field: str, raw: str) -> Reply:
        lang = self._lang(chat_id)
        if field not in _FAQ_WRITERS:
            self._clear_pending(chat_id)
            return self.main_menu(chat_id)
        value = raw.strip()
        if not value or len(value) > FAQ_MAX:
            return Reply(
                at("faq_invalid", lang, limit=FAQ_MAX),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        _FAQ_WRITERS[field](self._sf, self._cid, value)
        self._clear_pending(chat_id)
        return self._faq_menu(
            lang, notice=at("faq_saved", lang,
                            title=at(f"faq_name_{field}", lang)))

    # -- Врачи (P-3) ----------------------------------------------------------

    def _doctors_menu(self, lang: str = DEFAULT_LANG, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            docs = doctors_repo.doctor_list_all(session)
        rows = []
        for doc in docs:
            name = doc.name or at("doc_placeholder", lang,
                                  short_id=str(doc.id)[:8])
            if doc.is_active:
                btn_text = f"🧑‍⚕️ {name}"
            else:
                btn_text = at("doc_hidden_item", lang, name=name)
            rows.append((Button(btn_text, f"adm:doc:{doc.id}"),))
        rows.append((Button(at("btn_doc_add", lang), "adm:docadd:"),))
        rows.append((Button(at("btn_home", lang), "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}{at('doctors_title', lang)}",
                     button_rows=tuple(rows))

    def _doctor_callback(self, chat_id: int, message_id: int | None, arg: str) -> None:
        lang = self._lang(chat_id)
        parts = arg.split(":", 1)
        doc_id_str = parts[0]
        action = parts[1] if len(parts) > 1 else None
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._edit_or_send(chat_id, message_id, self._doctors_menu(lang))
            return
        if action == "name":
            self._begin_dname(chat_id, doc_id_str, message_id)
        elif action == "buf":
            self._begin_dbuf(chat_id, doc_id_str, message_id)
        elif action == "sched":
            self._sched_entry(chat_id, doc_id_str, message_id)
        elif action == "deact":
            # будущие записи скрытие НЕ отменяет — владелец обязан это узнать
            # сразу, иначе пациенты придут к «убранному» врачу (карта, №11)
            with tenant_transaction(self._sf, self._cid) as session:
                upcoming = self._future_bookings(session, doc_id)
            onboard.deactivate_doctor(self._sf, self._cid, doc_id)
            notice = at("doc_hidden_notice", lang)
            if upcoming:
                notice += at("doc_hidden_bookings", lang, count=upcoming)
            r = self._doctors_menu(lang, notice=notice)
            self._edit_or_send(chat_id, message_id, r)
        elif action == "act":
            onboard.activate_doctor(self._sf, self._cid, doc_id)
            r = self._doctors_menu(lang, notice=at("doc_shown_notice", lang))
            self._edit_or_send(chat_id, message_id, r)
        elif action == "del":
            # первый тап только спрашивает: удаление физическое и необратимое
            r = self._doctor_card(doc_id_str, lang,
                                  notice=at("doc_delete_confirm", lang),
                                  confirm_delete=True)
            self._edit_or_send(chat_id, message_id, r)
        elif action == "delyes":
            try:
                onboard.delete_doctor(self._sf, self._cid, doc_id)
                r = self._doctors_menu(lang, notice=at("doc_deleted", lang))
            except ValueError as e:
                log.warning("удаление врача %s отклонено: %s", doc_id_str, e)
                r = self._doctor_card(doc_id_str, lang,
                                      notice=at(_failure_key(e, "doc"), lang))
            self._edit_or_send(chat_id, message_id, r)
        else:
            r = self._doctor_card(doc_id_str, lang)
            self._edit_or_send(chat_id, message_id, r)

    @staticmethod
    def _future_bookings(session, doctor_id) -> int:
        """Живые записи врача впереди: скрытие их не трогает (карта, №11)."""
        from sqlalchemy import text as _text
        return session.execute(
            _text("SELECT count(*) FROM appointment "
                  "WHERE doctor_id = :d AND status = 'booked' "
                  "AND lower(time_range) > now()"),
            {"d": str(doctor_id)},
        ).scalar_one()

    @staticmethod
    def _doctor_refs(session, doctor_id) -> int:
        """Число ссылок на врача в appointment (для гейтинга кнопки удаления).
        Физическое удаление при наличии записей заблокировано FK RESTRICT."""
        from sqlalchemy import text as _text
        return session.execute(
            _text("SELECT count(*) FROM appointment WHERE doctor_id = :d"),
            {"d": str(doctor_id)},
        ).scalar_one()

    def _doctor_card(self, doc_id_str: str, lang: str = DEFAULT_LANG,
                     notice: str = "", confirm_delete: bool = False) -> Reply:
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            return self._doctors_menu(lang)
        with tenant_transaction(self._sf, self._cid) as session:
            docs = doctors_repo.doctor_list_all(session)
            doc = next((d for d in docs if d.id == doc_id), None)
            refs = self._doctor_refs(session, doc_id) if doc is not None else 0
        if doc is None:
            return self._doctors_menu(lang)
        head = f"{notice}\n\n" if notice else ""
        text = head + at(
            "doc_card", lang,
            name=doc.name or at("doc_noname", lang),
            buffer=at("minutes", lang, value=doc.buffer_min),
            calendar=at("cal_linked" if doc.gcal_calendar_id
                        else "cal_unlinked", lang),
            badge=_status_badge(doc.is_active, False, lang),
            schedule=_format_schedule(doc.working_intervals or {}, lang))
        toggle_btn = (
            Button(at("btn_hide", lang), f"adm:doc:{doc_id}:deact")
            if doc.is_active else
            Button(at("btn_show", lang), f"adm:doc:{doc_id}:act")
        )
        if confirm_delete:
            # экран подтверждения: только «да» и «нет», чтобы не промахнуться
            return Reply(text, button_rows=(
                (Button(at("btn_delete_yes", lang), f"adm:doc:{doc_id}:delyes"),),
                (Button(at("btn_cancel", lang), f"adm:doc:{doc_id}"),)))
        btn_rows_list = [
            (Button(at("btn_doc_name", lang), f"adm:doc:{doc_id}:name"),
             Button(at("btn_doc_buffer", lang), f"adm:doc:{doc_id}:buf")),
            (Button(at("btn_doc_sched", lang), f"adm:doc:{doc_id}:sched"),),
            (toggle_btn,),
        ]
        # Кнопка физического удаления — только деактивированного и без записей
        if not doc.is_active and refs == 0:
            btn_rows_list.append(
                (Button(at("btn_delete", lang), f"adm:doc:{doc_id}:del"),))
        btn_rows_list.append(
            (Button(at("btn_doctors_back", lang), "adm:doctors"),))
        return Reply(text, button_rows=tuple(btn_rows_list))

    def _begin_dname(self, chat_id: int, doc_id_str: str, message_id: int | None) -> None:
        lang = self._lang(chat_id)
        self._set_pending(chat_id, f"dname:{doc_id_str}")
        reply = Reply(
            at("dname_prompt", lang, limit=NAME_MAX),
            button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_dname(self, chat_id: int, doc_id_str: str, raw: str) -> Reply:
        lang = self._lang(chat_id)
        name = raw.strip()
        if not name or len(name) > NAME_MAX:
            return Reply(
                at("dname_invalid", lang, limit=NAME_MAX),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._clear_pending(chat_id)
            return self._doctors_menu(lang)
        onboard.rename_doctor(self._sf, self._cid, doc_id, name)
        self._clear_pending(chat_id)
        return self._doctor_card(doc_id_str, lang,
                                 notice=at("dname_saved", lang, name=name))

    def _begin_dbuf(self, chat_id: int, doc_id_str: str, message_id: int | None) -> None:
        lang = self._lang(chat_id)
        self._set_pending(chat_id, f"dbuf:{doc_id_str}")
        reply = Reply(
            at("dbuf_prompt", lang, lo=BUF_MIN, hi=BUF_MAX),
            button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_dbuf(self, chat_id: int, doc_id_str: str, raw: str) -> Reply:
        lang = self._lang(chat_id)
        if not raw.isdigit() or not BUF_MIN <= int(raw) <= BUF_MAX:
            return Reply(
                at("dbuf_invalid", lang, lo=BUF_MIN, hi=BUF_MAX),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        buf = int(raw)
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._clear_pending(chat_id)
            return self._doctors_menu(lang)
        onboard.set_doctor_buffer(self._sf, self._cid, doc_id, buf)
        self._clear_pending(chat_id)
        return self._doctor_card(
            doc_id_str, lang,
            notice=at("dbuf_saved", lang,
                      buffer=at("minutes", lang, value=buf)))

    def _begin_docadd(self, chat_id: int, message_id: int | None) -> None:
        lang = self._lang(chat_id)
        self._set_pending(chat_id, "dadd:")
        reply = Reply(
            at("docadd_prompt", lang),
            button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        self._edit_or_send(chat_id, message_id, reply)

    def _apply_dadd(self, chat_id: int, raw: str) -> Reply:
        lang = self._lang(chat_id)
        name = raw.strip()
        if not name or len(name) > NAME_MAX:
            return Reply(
                at("dname_invalid", lang, limit=NAME_MAX),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        onboard.add_doctor(self._sf, self._cid, name)
        self._clear_pending(chat_id)
        return self._doctors_menu(lang,
                                  notice=at("doc_added", lang, name=name))

    # -- Расписание -----------------------------------------------------------

    def _sched_entry(self, chat_id: int, doc_id_str: str, message_id: int | None) -> None:
        lang = self._lang(chat_id)
        rows = []
        for i, (tpl_key, _wi) in enumerate(_SCHEDULE_TEMPLATES):
            rows.append((Button(at(tpl_key, lang),
                                f"adm:sched:tpl:{doc_id_str}:{i}"),))
        rows.append((Button(at("btn_sched_custom", lang),
                            f"adm:sched:custom:{doc_id_str}"),))
        rows.append((Button(at("btn_back", lang), f"adm:doc:{doc_id_str}"),))
        r = Reply(at("sched_title", lang), button_rows=tuple(rows))
        self._edit_or_send(chat_id, message_id, r)

    def _sched_callback(self, chat_id: int, message_id: int | None, arg: str) -> None:
        lang = self._lang(chat_id)
        parts = arg.split(":", 2)
        action = parts[0]
        if action == "tpl" and len(parts) >= 3:
            doc_id_str, tpl_idx_str = parts[1], parts[2]
            try:
                tpl_idx = int(tpl_idx_str)
                wi = _SCHEDULE_TEMPLATES[tpl_idx][1]
                doc_id = _uuid_mod.UUID(doc_id_str)
            except (ValueError, IndexError):
                self._edit_or_send(chat_id, message_id, self._doctors_menu(lang))
                return
            onboard.set_doctor_schedule(self._sf, self._cid, doc_id, wi)
            r = self._doctor_card(doc_id_str, lang,
                                  notice=at("sched_saved", lang))
            self._edit_or_send(chat_id, message_id, r)
        elif action == "custom" and len(parts) >= 2:
            doc_id_str = parts[1]
            # выбор дней начинаем с чистого листа: незавершённый выбор для
            # другого врача иначе протёк бы в этот график (C1)
            self._set_sched_days(chat_id, doc_id_str, set())
            self._sched_custom_days(chat_id, doc_id_str, message_id, selected=set())
        elif action == "day" and len(parts) >= 3:
            doc_id_str = parts[1]
            day = parts[2]
            selected = self._get_sched_days(chat_id, doc_id_str)
            if day in selected:
                selected.discard(day)
            else:
                selected.add(day)
            self._set_sched_days(chat_id, doc_id_str, selected)
            self._sched_custom_days(chat_id, doc_id_str, message_id, selected)
        elif action == "next" and len(parts) >= 2:
            doc_id_str = parts[1]
            selected = self._get_sched_days(chat_id, doc_id_str)
            if not selected:
                r = Reply(
                    at("sched_pick_day", lang),
                    button_rows=((Button(at("btn_back", lang),
                                         f"adm:doc:{doc_id_str}:sched"),),))
                self._edit_or_send(chat_id, message_id, r)
                return
            self._set_pending(chat_id, f"sched:{doc_id_str}")
            days_txt = ", ".join(_weekday(d, lang) for d in WEEKDAY_KEYS
                                 if d in selected)
            r = Reply(
                at("sched_shifts_prompt", lang, days=days_txt),
                button_rows=((Button(at("btn_sched_dayoff", lang),
                                     f"adm:sched:off:{doc_id_str}"),),
                             (Button(at("btn_cancel", lang), "adm:cancel"),)))
            self._edit_or_send(chat_id, message_id, r)
        elif action == "off" and len(parts) >= 2:
            self._sched_make_dayoff(chat_id, message_id, parts[1])

    def _sched_make_dayoff(self, chat_id: int, message_id: int | None,
                           doc_id_str: str) -> None:
        """Выбранные дни — выходные. Пара к слиянию: без неё день, однажды
        ставший рабочим, уже нельзя было бы освободить."""
        lang = self._lang(chat_id)
        selected = self._get_sched_days(chat_id, doc_id_str)
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._edit_or_send(chat_id, message_id, self._doctors_menu(lang))
            return
        if not selected:
            self._edit_or_send(chat_id, message_id, Reply(
                at("sched_pick_any_day", lang),
                button_rows=((Button(at("btn_back", lang),
                                     f"adm:doc:{doc_id_str}:sched"),),)))
            return
        try:
            onboard.set_doctor_schedule_days(self._sf, self._cid, doc_id,
                                             selected, None)
        except ValueError as error:
            # дни приходят с наших же кнопок, но врача мог удалить второй
            # администратор — причину выбираем по типу ошибки
            key = ("doc_missing" if isinstance(error, onboard.EntityMissing)
                   else "sched_dayoff_last")
            self._edit_or_send(chat_id, message_id, Reply(
                at(key, lang),
                button_rows=((Button(at("btn_back", lang),
                                     f"adm:doc:{doc_id_str}"),),)))
            return
        self._clear_pending(chat_id)
        self._clear_sched_days(chat_id)
        self._edit_or_send(
            chat_id, message_id,
            self._doctor_card(doc_id_str, lang,
                              notice=at("sched_dayoff_saved", lang)))

    def _sched_custom_days(self, chat_id: int, doc_id_str: str,
                           message_id: int | None, selected: set) -> None:
        # текущие часы прямо на кнопках: иначе владелец правит день вслепую
        # и не видит, что именно заменит
        lang = self._lang(chat_id)
        wi = self._doctor_intervals(doc_id_str)
        rows = []
        for day in WEEKDAY_KEYS:
            mark = "✅ " if day in selected else ""
            hours = _day_hours(wi.get(day), lang)
            rows.append((Button(f"{mark}{_weekday(day, lang)} · {hours}",
                                f"adm:sched:day:{doc_id_str}:{day}"),))
        rows.append((Button(at("btn_sched_next", lang),
                            f"adm:sched:next:{doc_id_str}"),))
        rows.append((Button(at("btn_back", lang),
                            f"adm:doc:{doc_id_str}:sched"),))
        r = Reply(at("sched_days_title", lang), button_rows=tuple(rows))
        self._edit_or_send(chat_id, message_id, r)

    def _doctor_intervals(self, doc_id_str: str) -> dict:
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            return {}
        with tenant_transaction(self._sf, self._cid) as session:
            doc = next((d for d in doctors_repo.doctor_list_all(session)
                        if d.id == doc_id), None)
        return (doc.working_intervals or {}) if doc is not None else {}

    def _apply_sched_shifts(self, chat_id: int, doc_id_str: str, raw: str) -> Reply:
        lang = self._lang(chat_id)
        shifts = _parse_shifts(raw)
        if shifts is None:
            return Reply(
                at("sched_shifts_invalid", lang),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
        selected = self._get_sched_days(chat_id, doc_id_str)
        try:
            doc_id = _uuid_mod.UUID(doc_id_str)
        except ValueError:
            self._clear_pending(chat_id)
            self._clear_sched_days(chat_id)
            return self._doctors_menu(lang)
        # только выбранные дни: прежняя перезапись целиком стирала остальные
        try:
            onboard.set_doctor_schedule_days(self._sf, self._cid, doc_id,
                                             selected, shifts)
        except ValueError as error:
            # врача мог удалить второй администратор, пока владелец набирал
            # смены: это ответ владельцу, а не исключение в очередь
            log.warning("график врача %s не сохранён: %s", doc_id_str, error)
            self._clear_pending(chat_id)
            self._clear_sched_days(chat_id)
            return self._doctors_menu(lang,
                                      notice=at(_failure_key(error, "doc"), lang))
        self._clear_pending(chat_id)
        self._clear_sched_days(chat_id)
        return self._doctor_card(doc_id_str, lang,
                                 notice=at("sched_saved", lang))

    # -- раздел «Выходные» ----------------------------------------------------

    def _dayoff_menu(self, lang: str = DEFAULT_LANG, notice: str = "") -> Reply:
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
        rows.append((Button(at("btn_dayoff_add", lang), "adm:dayoff:add"),))
        rows.append((Button(at("btn_home", lang), "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        intro = at("dayoff_intro" if rows_data else "dayoff_empty", lang)
        return Reply(f"{head}{at('dayoff_title', lang)}{intro}",
                     button_rows=tuple(rows))

    def _handle_dayoff_callback(self, chat_id: int, message_id: int | None,
                               arg: str) -> None:
        from datetime import date as _date

        from sqlalchemy import text as _text

        lang = self._lang(chat_id)
        sub, _, rest = arg.partition(":")
        if sub == "add":
            self._set_pending(chat_id, "dayoff")
            self._edit_or_send(chat_id, message_id, Reply(
                at("dayoff_prompt", lang),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),)))
            return
        if sub == "open":
            try:
                target = _date.fromisoformat(rest)
            except ValueError:
                self._edit_or_send(chat_id, message_id, self._dayoff_menu(lang))
                return
            with tenant_transaction(self._sf, self._cid) as session:
                session.execute(_text("DELETE FROM holiday WHERE date = :d"),
                                {"d": target})
            self._worker._send(
                chat_id,
                self._dayoff_menu(lang, notice=at("dayoff_reopened", lang)))
            return
        # body был просто «dayoff» — показать меню
        self._edit_or_send(chat_id, message_id, self._dayoff_menu(lang))

    def _apply_dayoff(self, chat_id: int, raw: str) -> Reply:
        from sqlalchemy import text as _text

        lang = self._lang(chat_id)
        parts = raw.split(maxsplit=1)
        today = self._worker._clinic_today()
        target = self._worker._parse_ddmm(parts[0], today) if parts else None
        if target is None:
            return Reply(
                at("dayoff_invalid", lang),
                button_rows=((Button(at("btn_cancel", lang), "adm:cancel"),),))
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
            warning = booked_warning(session, target, lang)
        self._clear_pending(chat_id)
        notice = at("dayoff_closed", lang, date=f"{target:%d.%m.%Y}") + warning
        return self._dayoff_menu(lang, notice=notice)

    # -- extras: язык консоли -------------------------------------------------

    def _lang(self, chat_id: int) -> str:
        """Язык админ-чата. Живёт в диалоге, а не в клинике: у каждого
        администратора он свой, и миграция ради флага не нужна. Чистка
        диалогов старше retention сбросит его на русский — переключается
        одной кнопкой."""
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
        lang = conv.context.extras.get("adm_lang")
        return lang if lang in LANGS else DEFAULT_LANG

    def _set_lang(self, chat_id: int, lang: str) -> None:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
            conv.context.extras["adm_lang"] = lang
            save_conversation(session, conv)

    # -- extras: sched days ---------------------------------------------------

    def _get_sched_days(self, chat_id: int, doc_id_str: str) -> set:
        """Дни, отмеченные ДЛЯ ЭТОГО врача.

        Врач в ключе обязателен: сообщения консоли живут в чате долго, и тап
        по «Далее» в старом сообщении другого врача применял бы чужой выбор."""
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
        picked = conv.context.extras.get("adm_sch_days") or {}
        if not isinstance(picked, dict) or picked.get("doctor") != doc_id_str:
            return set()
        return set(picked.get("days", ()))

    def _set_sched_days(self, chat_id: int, doc_id_str: str, days: set) -> None:
        with tenant_transaction(self._sf, self._cid) as session:
            conv = load_conversation(session, chat_id)
            conv.context.extras["adm_sch_days"] = {"doctor": doc_id_str,
                                                   "days": sorted(days)}
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
