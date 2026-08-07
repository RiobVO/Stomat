"""Админ-консоль на кнопках: владелец клиники правит цены и FAQ-поля прямо
из своего админ-чата, без CLI и без слэш-команд.

Решение пользователя (11.06.2026): админ-чат — ЧИСТАЯ консоль. Как только
chat_id входит в clinic.tg_admin_chat_ids, воркер маршрутизирует сообщение
сюда (worker._handle), а пациентский DialogEngine для этого чата не вызывается
вовсе. Поэтому владелец всегда видит АДМИН-меню, а не пациентское
«Записаться/Перенести/...». Тестировать запись владелец может со второго
аккаунта.

Многошаговый ввод (нажал «изменить цену» → бот ждёт число) хранит pending в
conversation.context.extras['adm_pending'] = "price:cleaning" | "faq:address".
fsm_state админ-чата остаётся idle — FSM эту строку не читает (uniqueness по
(clinic_id, tg_chat_id), а админ-чат теперь чисто админский — конфликта нет).

Язык — только русский, как и существующие админ-команды. Подстановки
пользовательского текста (адрес/оплата/телефон) экранируются html.escape:
ответы уходят с parse_mode=HTML (П-7), «<&>» не должны ломать парсер Telegram.
Лейблы inline-кнопок Telegram показывает буквально — их не экранируем.
"""
from __future__ import annotations

import html

from navbat import onboard
from navbat.db.base import tenant_transaction
from navbat.dialog import clinic_repo, services_repo
from navbat.dialog.conversation import load_conversation, save_conversation
from navbat.dialog.replies import SERVICE_EMOJI, SERVICE_LABELS, Button, Reply

# верхнее меню — reply-клавиатура; нажатие приходит ТЕКСТОМ, матчим точно
BTN_PRICES = "💰 Цены"
BTN_ABOUT = "🏥 О клинике"
BTN_STATS = "📊 Статистика"
BTN_PAUSE = "⏸ Пауза"
BTN_RESUME = "▶️ Возобновить"
_MENU_LABELS = {BTN_PRICES, BTN_ABOUT, BTN_STATS, BTN_PAUSE, BTN_RESUME}

# текстовый синоним кнопки «Отмена» (на случай если владелец напечатает руками)
CANCEL_WORDS = {"отмена", "cancel"}

PRICE_MAX = 1_000_000_000  # защита показа/ввода от случайного мусора
FAQ_MAX = 500              # адрес/реквизиты — одна-две строки

_FAQ_TITLES = {"address": "Адрес", "payment": "Условия оплаты", "phone": "Телефон"}
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


def _fmt_sum(value: int) -> str:
    """123456 → «123 456» (узкий неразрывный разделитель тысяч не нужен —
    рендер совпадает с show_clinic/_stats)."""
    return f"{value:,}".replace(",", " ")


def _esc(value: str) -> str:
    return html.escape(str(value), quote=False)


class AdminConsole:
    """Кнопочная админ-поверхность. Воркер дёргает три публичных метода;
    переиспускает свои _stats_reply/_pause_reply/_resume_reply/_bot_paused/
    _send/_edit через переданную ссылку на себя (worker)."""

    def __init__(self, session_factory, clinic_id, api, worker) -> None:
        self._sf = session_factory
        self._cid = clinic_id
        self._api = api
        self._worker = worker

    # ── Публичная поверхность ────────────────────────────────────────────

    def handle_text(self, chat_id: int, text: str) -> Reply:
        """Текст из админ-чата: label меню / pending-ввод / дефолт (меню)."""
        stripped = text.strip()
        pending = self._get_pending(chat_id)
        # label меню имеет приоритет над pending: нажатие reply-кнопки в режиме
        # ввода = выход из ввода, а не значение поля
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
            if kind == "faq":
                return self._apply_faq(chat_id, arg, stripped)
        # /start, свободный текст, мусор → главное меню
        return self.main_menu()

    def handle_callback(self, callback: dict, chat_id: int, data: str) -> None:
        """adm:-callback'и (сырой префикс, мимо tg_actions-map). По образцу
        worker._handle_stats_callback."""
        self._api.answer_callback_query(callback["id"])
        message_id = callback["message"].get("message_id")
        body = data[len("adm:"):]
        if body in ("home", "cancel"):
            self._clear_pending(chat_id)
            self._worker._send(chat_id, self.main_menu())
            return
        kind, _, arg = body.partition(":")
        if kind == "price":
            self._begin_price_edit(chat_id, arg, message_id)
            return
        if kind == "faq":
            self._begin_faq_edit(chat_id, arg, message_id)

    def main_menu(self) -> Reply:
        paused = self._worker._bot_paused()
        pause_btn = BTN_RESUME if paused else BTN_PAUSE
        rows = ((BTN_PRICES, BTN_ABOUT), (BTN_STATS,), (pause_btn,))
        head = "⏸ <i>Бот на паузе.</i>\n\n" if paused else ""
        return Reply(f"{head}🛠 <b>Админ-консоль</b>\nВыберите раздел:", menu=rows)

    # ── Маршрутизация верхнего меню ──────────────────────────────────────

    def _menu_action(self, chat_id: int, label: str) -> Reply:
        if label == BTN_PRICES:
            return self._prices_menu()
        if label == BTN_ABOUT:
            return self._faq_menu()
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
        # перерисовать меню новым состоянием (label паузы ↔ возобновления)
        return Reply(conf.text, menu=self.main_menu().menu)

    # ── Раздел цен ───────────────────────────────────────────────────────

    def _prices_menu(self, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            prices = services_repo.price_list(session)
        rows = []
        for row in prices:
            emoji = SERVICE_EMOJI.get(row.name, "")
            label = SERVICE_LABELS.get(row.name, {}).get("ru", row.name)
            price_txt = f"{_fmt_sum(row.price)} сум" if row.price is not None \
                else "цена не задана"
            rows.append((Button(f"{emoji} {label} — {price_txt}".strip(),
                                 f"adm:price:{row.name}"),))
        rows.append((Button("◀ Меню", "adm:home"),))
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}💰 <b>Цены услуг</b>\n"
                     f"Выберите услугу, чтобы изменить цену:",
                     button_rows=tuple(rows))

    def _begin_price_edit(self, chat_id: int, key: str,
                          message_id: int | None) -> None:
        self._set_pending(chat_id, f"price:{key}")
        with tenant_transaction(self._sf, self._cid) as session:
            current = services_repo.service_price(session, key)
        label = SERVICE_LABELS.get(key, {}).get("ru", key)
        cur_txt = f"{_fmt_sum(current)} сум" if current is not None else "не задана"
        reply = Reply(
            f"💰 <b>{_esc(label)}</b>\nТекущая цена: {cur_txt}\n\n"
            f"Введите новую цену в сумах (целое число), например 400000.",
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
        return self._prices_menu(
            notice=f"✅ Цена «{_esc(label)}»: {_fmt_sum(price)} сум")

    # ── Раздел FAQ «О клинике» ───────────────────────────────────────────

    def _faq_menu(self, notice: str = "") -> Reply:
        with tenant_transaction(self._sf, self._cid) as session:
            values = {field: reader(session)
                      for field, reader in _FAQ_READERS.items()}
        rows = (
            (Button(self._faq_btn("📍 Адрес", values["address"]),
                    "adm:faq:address"),),
            (Button(self._faq_btn("💳 Оплата", values["payment"]),
                    "adm:faq:payment"),),
            (Button(self._faq_btn("📞 Телефон", values["phone"]),
                    "adm:faq:phone"),),
            (Button("◀ Меню", "adm:home"),),
        )
        head = f"{notice}\n\n" if notice else ""
        return Reply(f"{head}🏥 <b>О клинике</b>\n"
                     f"Выберите поле, чтобы изменить:", button_rows=rows)

    @staticmethod
    def _faq_btn(label: str, value: str | None) -> str:
        if not value:
            return f"{label}: не задано"
        short = value if len(value) <= 30 else value[:29] + "…"
        return f"{label}: {short}"

    def _begin_faq_edit(self, chat_id: int, field: str,
                        message_id: int | None) -> None:
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
                f"⚠️ Введите непустой текст до {FAQ_MAX} символов "
                f"или нажмите «Отмена».",
                button_rows=((Button("✖ Отмена", "adm:cancel"),),))
        _FAQ_WRITERS[field](self._sf, self._cid, value)
        self._clear_pending(chat_id)
        return self._faq_menu(notice=f"✅ {_FAQ_TITLES[field]} обновлено")

    # ── pending-ввод (conversation.extras) ───────────────────────────────

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

    # ── отправка/редактирование ──────────────────────────────────────────

    def _edit_or_send(self, chat_id: int, message_id: int | None,
                      reply: Reply) -> None:
        if message_id is not None:
            self._worker._edit(chat_id, message_id, reply)
        else:
            self._worker._send(chat_id, reply)
