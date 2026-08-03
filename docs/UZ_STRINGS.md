# Узбекские тексты бота — проверка носителем

Все фразы, которые Navbat говорит по-узбекски: пациенту (разделы 1–12, 14),
владельцу клиники в админ-консоли (раздел 15) и в алертах, которыми бот зовёт
человека или сообщает о сбое (раздел 16). Тексты писались как черновик — нужна
проверка живым носителем до пилота (чеклист v1.0, пункт D.4; карта готовности,
№20). Источники: `src/navbat/dialog/replies.py` и
`src/navbat/telegram/admin_texts.py` — весь узбекский код-базы живёт только
там, и `tests/test_replies_uz.py` не даёт добавить строку мимо этого файла.

> **Правьте смело: сломанная подстановка больше не гасит сообщение.** Фигурные
> скобки `{...}` по-прежнему не трогаем (см. «Как проверять»), но если правка
> всё же их повредит, бот доставит сигнал и запишет ошибку в лог, а не промолчит.
> Отдельная проверка следит, чтобы узбекский вариант нёс те же подстановки, что
> русский: потерянная скобка молча уносит из сообщения число или дату.

> **Раунд 1 (07.06.2026) ПРОВЕДЁН** кросс-проверкой LLM (Gemini + Claude,
> промпт docs/UZ_LLM_PROMPT.md): внесено 16 правок — терминология «qabul»
> вместо «yozuv», аффикс «dagi» вместо «kungi» при дате-времени, мягкий
> reask. Решено оставить: ASCII-апостроф `'`, «Tish oldirish», «Indinga»,
> заимствования (Plomba и пр.), «Ismingiz nima?». Тексты ниже —
> актуальные (после правок); контроль — tests/test_replies_uz.py.
>
> **Раунд 2 (09.06.2026) — веб-ресёрч живого узбекского** (не LLM-перевод):
> 1. **Апостроф — ВОПРОС ЗАКРЫТ: оставляем ASCII `'`.** Правильная окина `ʻ`
>    (U+02BB) — орфографически верна, но ASCII `'` — де-факто стандарт цифрового
>    узбекского: так пишут даже госсайты Узбекистана, `ʻ` отсутствует на
>    раскладках и во многих шрифтах. Менять не на что. (Wikipedia: Uzbek
>    alphabet; soglom-avlod.uz.)
> 2. **«qabulga yozilish» подтверждён** — именно так реальная узбекская
>    клиника называет запись на приём (soglom-avlod.uz/qabulga-yozilish).
> 3. **Правка консистентности:** `whitening` «Oqartirish» → «Tish oqartirish»
>    (как «Tish tozalash», «Tish oldirish» — услуги с уточнением «Tish»).
> 4. Остальное (тон, «Tish oldirish» vs «olib tashlash», «Indinga») — нюансы,
>    которые честно требуют ЖИВОГО носителя, не ещё одного машинного прохода;
>    угадывать = риск отгрузить пациенту худший текст. Документ под это готов.

## Как проверять

- **Русский текст — эталон смысла.** Узбекский должен передавать тот же
  смысл и тон, дословность не обязательна.
- **Тон**: вежливое обращение на «siz», дружелюбный администратор клиники,
  без канцелярита. Аудитория — пациенты стоматологии в Ташкенте.
- **Фигурные скобки `{...}` не трогать** — это подстановки, бот заменяет
  их на лету. Менять можно их положение во фразе и аффиксы вокруг них:

  | Подстановка | Что подставится | Пример |
  |---|---|---|
  | `{date}`, `{asked}` | дата | `08.06` |
  | `{when}`, `{old}`, `{new}` | дата и время | `08.06 15:30` |
  | `{service}` | название услуги из списка в конце документа | `Tish tozalash` |
  | `{doctor}` | пусто ЛИБО запятая + имя врача | `, Dilshod Karimov` |
  | `{price}` | сумма с пробелами | `150 000` |
  | `{clinic}` | название клиники | `Shifo Dent` |

- **Эмодзи в кнопках** (📅 🔄 ❌ 💰 🌐 📱 ✓) — оставить.
- Правки вписывайте прямо под фразой в строку «Правка:» (или отдельным
  списком «номер — исправленный текст»).

**Отдельный вопрос по всему документу — апостроф.** Сейчас в текстах
ASCII-апостроф `'` (`bo'sh`, `o'zbek`, `ko'chirish`). Правильный знак
узбекской латиницы — `ʻ` (okina: `boʻsh`, `oʻzbek`). Telegram отображает
оба. Скажите, какой вариант привычнее читается пациентами — заменим разом
по всем строкам.

---

## 1. Первый контакт

### 1.1 `greeting` — приветствие при первом сообщении
- RU: Здравствуйте! Я виртуальный администратор клиники «{clinic}»: помогу записаться, перенести или отменить приём. По медицинским вопросам ответит врач.
- UZ: **Assalomu alaykum! Men «{clinic}» klinikasining virtual administratoriman: qabulga yozilish, uni boshqa vaqtga ko'chirish yoki bekor qilishda yordam beraman. Tibbiy savollarga shifokor javob beradi.**
- Правка:

### 1.2 `choose_lang` — экран выбора языка
- Текст: **Tilni tanlang / Выберите язык:**
- Намеренно двуязычный (показывается до того, как язык известен) — проверить только написание.
- Правка:

### 1.3 `menu_hint` — подсказка под главным меню
- RU: Выберите действие или напишите своими словами:
- UZ: **Amalni tanlang yoki o'z so'zlaringiz bilan yozing:**
- Правка:

### 1.4 `lang_changed` — подтверждение смены языка
- UZ: **Til o'zbek tiliga o'zgartirildi.**
- Показывается уже НА новом языке, поэтому русская и узбекская версии говорят о разных языках — это не ошибка.
- Правка:

### 1.5 `MEDICAL_DISCLAIMER` — дисклеймер при медицинском вопросе
- RU: Я виртуальный администратор и не даю медицинских советов — точный ответ даст врач на приёме.
- UZ: **Men virtual administratorman, tibbiy maslahat bera olmayman — aniq javobni shifokor qabulda beradi.**
- Правка:

---

## 2. Запись на приём

### 2.1 `ask_service` — вопрос об услуге
- RU: На какую услугу вас записать?
- UZ: **Qaysi xizmatga yozib qo'yay?**
- Правка:

### 2.2 `ask_date` — вопрос о дне
- RU: На какой день вам удобно?
- UZ: **Qaysi kun sizga qulay?**
- Правка:

### 2.3 `offer_slots` — свободное время на запрошенный день
- RU: Свободное время на {date}:
- UZ: **{date} kuni bo'sh vaqtlar:**
- Пример: «08.06 kuni bo'sh vaqtlar:»
- Правка:

### 2.4 `offer_slots_other_day` — на запрошенный день мест нет
- RU: На {asked} свободного времени нет. Ближайшее — {date}:
- UZ: **{asked} kuni bo'sh vaqt yo'q. Eng yaqini — {date}:**
- Пример: «08.06 kuni bo'sh vaqt yo'q. Eng yaqini — 09.06:»
- Правка:

### 2.5 `closed_now_slots` — клиника сейчас закрыта (запрос «на сегодня» ночью)
- RU: Сейчас клиника закрыта.\nБлижайшее свободное время — {date}:
- UZ: **Hozir klinika yopiq.\nEng yaqin bo'sh vaqt — {date}:**
- `\n` — перенос строки, оставить.
- Правка:

### 2.6 `no_slots_at_all` — нет мест две недели вперёд
- RU: В ближайшие две недели свободного времени нет — передаю администратору.
- UZ: **Yaqin ikki haftada bo'sh vaqt yo'q — sizni administratorga ulayman.**
- Правка:

### 2.7 `doctor_not_found` — врач с таким именем не найден
- RU: Врача с таким именем не нашёл, показываю всё свободное время.
- UZ: **Bunday ismli shifokor topilmadi, barcha bo'sh vaqtlarni ko'rsataman.**
- Правка:

### 2.8 `slot_taken` — время заняли, пока пациент выбирал
- RU: Это время только что заняли. Вот свежие варианты:
- UZ: **Bu vaqt hozirgina band bo'ldi. Mana yangi variantlar:**
- Правка:

### 2.9 `hold_expired` — бронь истекла
- RU: Бронь на выбранное время истекла. Вот свежие варианты:
- UZ: **Tanlangan vaqtni band qilish muddati tugadi. Mana yangi variantlar:**
- Правка:

---

## 3. Имя и телефон

### 3.1 `ask_name` — вопрос об имени
- RU: Как вас зовут?
- UZ: **Ismingiz nima?**
- Правка:

### 3.2 `ask_phone` — просьба отправить номер кнопкой
- RU: Нажмите кнопку ниже — она отправит ваш номер телефона:
- UZ: **Pastdagi tugmani bosing — u telefon raqamingizni yuboradi:**
- Правка:

### 3.3 `press_contact_button` — пациент написал номер текстом вместо кнопки
- RU: Чтобы оставить номер, нажмите кнопку ниже:
- UZ: **Raqam qoldirish uchun pastdagi tugmani bosing:**
- Правка:

### 3.4 `foreign_contact` — пациент отправил чужой контакт
- RU: Это контакт другого человека. Нажмите кнопку — она отправит ваш собственный номер:
- UZ: **Bu boshqa odamning kontakti. Tugmani bosing — u o'zingizning raqamingizni yuboradi:**
- Правка:

---

## 4. Подтверждение записи

### 4.1 `booked` — запись оформлена
- RU: Записал: {service}, {when}{doctor}. Ждём вас!
- UZ: **Yozib qo'ydim: {service}, {when}{doctor}. Sizni kutamiz!**
- Пример с врачом: «Yozib qo'ydim: Tish tozalash, 08.06 15:30, Dilshod Karimov. Sizni kutamiz!»
- Пример без врача: «Yozib qo'ydim: Tish tozalash, 08.06 15:30. Sizni kutamiz!»
- Правка:

---

## 5. Отмена записи

### 5.1 `cancel_confirm_q` — подтверждение отмены
- RU: Отменить вашу запись на {when}?
- UZ: **{when} dagi qabulni bekor qilaymi?**
- Пример: «08.06 15:30 dagi qabulni bekor qilaymi?» («dagi» вместо «kungi» — решение раунда 1: время не является днём).
- Правка:

### 5.2 `cancel_done` — запись отменена
- RU: Запись отменена. Будем рады записать вас снова.
- UZ: **Qabul bekor qilindi. Sizni yana kutib qolamiz.**
- Правка:

### 5.3 `cancel_kept` — пациент передумал отменять
- RU: Хорошо, запись остаётся в силе.
- UZ: **Yaxshi, qabul o'z kuchida qoladi.**
- Правка:

### 5.4 `cancel_none` — отменять нечего
- RU: Активной записи не нашёл. Хотите записаться?
- UZ: **Faol qabul topilmadi. Yozilishni xohlaysizmi?**
- Правка:

---

## 6. Перенос записи

### 6.1 `resched_none` — переносить нечего
- RU: Активной записи для переноса не нашёл. Хотите записаться?
- UZ: **Boshqa vaqtga ko'chirish uchun faol qabul topilmadi. Yozilishni xohlaysizmi?**
- Правка:

### 6.2 `resched_done` — запись перенесена
- RU: Перенёс вашу запись на {when}. Ждём вас!
- UZ: **Qabulni {when} ga ko'chirdim. Sizni kutamiz!**
- Пример: «Qabulni 08.06 15:30 ga ko'chirdim. Sizni kutamiz!» (аффикс «ga» подтверждён раундом 1).
- Правка:

---

## 7. Напоминания о приёме

### 7.1 `reminder` — напоминание
- RU: Напоминаем: вы записаны на {service} {when}. Ждём вас!
- UZ: **Eslatamiz: siz {service} uchun {when} ga yozilgansiz. Sizni kutamiz!**
- Пример: «Eslatamiz: siz Tish tozalash uchun 08.06 15:30 ga yozilgansiz. Sizni kutamiz!»
- Правка:

### 7.2 `attend_ok` — пациент подтвердил, что придёт
- RU: Отлично, ждём вас!
- UZ: **Ajoyib, sizni kutamiz!**
- Правка:

---

## 8. Перенос по вине клиники (конфликт календаря)

### 8.1 `conflict_moved` — время заняли, бот перенёс запись
- RU: К сожалению, время {old} стало недоступно — перенёс вашу запись на {new}. Если не подходит, выберите другое:
- UZ: **Afsuski, {old} vaqti band bo'lib qoldi — qabulni {new} ga ko'chirdim. To'g'ri kelmasa, boshqasini tanlang:**
- Пример: «Afsuski, 08.06 15:30 vaqti band bo'lib qoldi — qabulni 08.06 16:30 ga ko'chirdim. ...»
- Правка:

### 8.2 `conflict_cancelled` — время заняли, заменить нечем
- RU: К сожалению, время {old} стало недоступно, а свободного времени в ближайшие дни нет — запись отменена. Напишите, и подберём новое.
- UZ: **Afsuski, {old} vaqti band bo'lib qoldi, yaqin kunlarda bo'sh vaqt yo'q — qabul bekor qilindi. Yozing, boshqa vaqt topamiz.**
- Правка:

---

## 9. Цены

### 9.1 `price_answer` — цена услуги
- RU: «{service}» — {price} сум.
- UZ: **«{service}» — {price} so'm.**
- Правка:

### 9.2 `price_unknown` — цены нет в базе
- RU: Цену на «{service}» уточнит администратор.
- UZ: **«{service}» narxini administrator aniqlashtiradi.**
- Правка:

### 9.3 `price_header` — заголовок прайса
- RU: Наши цены:
- UZ: **Narxlarimiz:**
- Правка:

### 9.4 `price_line` — строка прайса
- RU: • {service} — {price} сум
- UZ: **• {service} — {price} so'm**
- Правка:

### 9.5 `price_line_unknown` — строка прайса без цены
- RU: • {service} — цену уточнит администратор
- UZ: **• {service} — narxini administrator aniqlashtiradi**
- Правка:

### 9.6 `price_empty` — прайс пуст
- RU: Прайс уточнит администратор.
- UZ: **Narxlarni administrator aniqlashtiradi.**
- Правка:

---

## 10. Служебные ответы

### 10.1 `reask` — бот не понял сообщение
- RU: Не понял вас. Напишите, пожалуйста, иначе — например: «запись на чистку завтра».
- UZ: **Kechirasiz, tushunmadim. Boshqacha yozib ko'ring — masalan: «ertaga tish tozalashga yozilmoqchiman».**
- Пример в кавычках — образец фразы пациента, он должен звучать естественно, как пишет обычный человек.
- Правка:

### 10.2 `escalated` — бот передаёт диалог администратору
- RU: Передаю администратору — он ответит вам здесь в ближайшее время.
- UZ: **Administratorga ulab berdim — u tez orada shu yerda javob beradi.**
- Правка:

### 10.3 `other_fallback` — сообщение не про запись
- RU: Я помогу записаться на приём: напишите услугу и удобный день.
- UZ: **Qabulga yozilishga yordam beraman: xizmat va qulay kunni yozing.**
- Правка:

### 10.4 `faq_fallback` — вопрос, на который бот не знает ответ
- RU: Это уточнит администратор — я передал ему ваш вопрос.
- UZ: **Buni administrator aniqlashtiradi — savolingizni unga yubordim.**
- Правка:

### 10.5 `rate_limited` — слишком много сообщений подряд
- RU: Слишком много сообщений подряд — сделайте небольшую паузу, и я отвечу.
- UZ: **Juda ko'p xabar yubordingiz — biroz kuting, javob beraman.**
- Правка:

### 10.6 `text_only` — пациент прислал фото/голос/стикер
- RU: Пока я понимаю только текст — напишите, пожалуйста, словами.
- UZ: **Hozircha faqat matnni tushunaman — iltimos, so'z bilan yozing.**
- Правка:

### 10.7 `stale_button` — нажата устаревшая кнопка
- RU: Эта кнопка устарела.
- UZ: **Bu tugma endi faol emas.**
- Правка:

---

## 11. Кнопки

Короткие подписи, места мало — при правке желательно сохранять длину.

| Ключ | Где | RU | UZ (проверить) | Правка |
|---|---|---|---|---|
| `btn_menu_book` | главное меню | 📅 Записаться | **📅 Yozilish** | |
| `btn_menu_resched` | главное меню | 🔄 Перенести | **🔄 Ko'chirish** | |
| `btn_menu_cancel` | главное меню | ❌ Отменить | **❌ Bekor qilish** | |
| `btn_menu_prices` | главное меню | 💰 Цены | **💰 Narxlar** | |
| `btn_menu_lang` | главное меню | 🌐 Til / Язык | **🌐 Til / Язык** (двуязычная намеренно) | |
| `btn_lang_uz` | выбор языка | O'zbekcha | **O'zbekcha** | |
| `btn_lang_ru` | выбор языка | Русский | Русский | |
| `btn_today` | выбор дня | Сегодня | **Bugun** | |
| `btn_tomorrow` | выбор дня | Завтра | **Ertaga** | |
| `btn_after_tomorrow` | выбор дня | Послезавтра | **Indinga** | |
| `btn_other_time` | выбор слота | Другое время | **Boshqa vaqt** | |
| `ask_doctor` | выбор врача | 👨‍⚕️ Кто вам удобнее на {when}? | **👨‍⚕️ {when} ga kim qulay?** | шаг появляется, только если на это время свободны несколько врачей |
| `btn_any_doctor` | выбор врача | Любой | **Farqi yo'q** | «без разницы, к кому» |
| `doctor_taken` | выбор врача | К сожалению, этого врача только что заняли. | **Afsuski, bu shifokorning vaqti hozirgina band bo'ldi.** | врача заняли, пока пациент отвечал |
| `btn_share_contact` | шаг телефона | 📱 Отправить мой номер | **📱 Raqamimni yuborish** | |
| `btn_yes` | подтверждение отмены | Да, отменить | **Ha, bekor qilish** | |
| `btn_no` | подтверждение отмены | Нет, оставить | **Yo'q, qoldirish** | |
| `btn_attend` | напоминание | ✓ Приду | **✓ Kelaman** | |
| `btn_remind_cancel` | напоминание | Отменить запись | **Qabulni bekor qilish** | |

---

## 12. Названия услуг

Подставляются в `{service}` и показываются кнопками при выборе услуги
и строками в прайсе.

| Ключ | RU | UZ (проверить) | Правка |
|---|---|---|---|
| `cleaning` | Чистка | **Tish tozalash** | |
| `filling` | Пломба | **Plomba** | |
| `extraction` | Удаление | **Tish oldirish** | |
| `implant` | Имплант | **Implant** | |
| `crown` | Коронка | **Koronka** | |
| `whitening` | Отбеливание | **Tish oqartirish** | раунд 2: +«Tish» для консистентности |
| `braces` | Брекеты | **Breket** | |
| `checkup` | Осмотр | **Ko'rik** | |
| `xray` | Снимок | **Rentgen** | |

---

## 14. Пациентские строки, добавленные после раунда 2

Разделы 1–12 писались 07–09.06.2026. С тех пор появились лист ожидания,
выбор даты сеткой, FAQ-слой, кнопка администратора и пауза бота — эти
строки носитель ещё не видел.

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `no_slots_calendar` | В ближайшие две недели свободного времени нет — вот более дальние даты: | **Yaqin ikki haftada bo'sh vaqt yo'q — mana uzoqroq sanalar:** | |
| `no_slots_horizon` | Свободного времени не видно даже на три месяца вперёд — загляните позже или напишите «позовите администратора». | **Uch oy oldinga ham bo'sh vaqt ko'rinmayapti — keyinroq urinib ko'ring yoki «administratorni chaqiring» deb yozing.** | |
| `btn_pick_date` | 📅 Выбрать дату | **📅 Sanani tanlash** | |
| `btn_back_calendar` | ◀ К датам | **◀ Sanalarga** | |
| `btn_join_waitlist` | 🔔 Сообщить, когда освободится | **🔔 Bo'shaganda xabar bering** | |
| `waitlist_joined` | 🔔 Вы в очереди — пришлю первое освободившееся время. | **🔔 Siz navbatdasiz — bo'shagan birinchi vaqtni yuboraman.** | |
| `waitlist_already` | 🔔 Вы уже в очереди — как только освободится, сразу напишу. | **🔔 Siz allaqachon navbatdasiz — bo'shashi bilan yozaman.** | |
| `waitlist_taken_already` | ✅ Вы уже записаны на «{service}» — второй записи не нужно. | **✅ Siz «{service}» uchun allaqachon yozilgansiz — ikkinchisi shart emas.** | |
| `waitlist_left` | Хорошо, убрал вас из очереди ожидания. | **Mayli, sizni kutish navbatidan chiqardim.** | |
| `waitlist_slot_offer` | 🔔 Освободилось время на «{service}»: {when}. Записать вас? | **🔔 «{service}» uchun vaqt bo'shadi: {when}. Yozib qo'yaymi?** | |
| `btn_waitlist_leave` | Я больше не жду | **Endi kutmayman** | |
| `cal_no_free_days_month` | В этом месяце свободных дней нет | **Bu oyda bo'sh kunlar yo'q** | |
| `cal_no_slots` | Свободного времени нет | **Bo'sh vaqt yo'q** | |
| `cal_past_day` | Этот день уже прошёл | **Bu kun o'tib ketdi** | |
| `llm_off_menu` | Сейчас запись принимается через кнопки меню — выберите нужное действие. | **Hozir yozilish menyu tugmalari orqali qabul qilinadi — kerakli amalni tanlang.** | |
| `bot_paused` | Запись через бота временно приостановлена. Позвоните в клинику или загляните позже. | **Bot orqali yozilish vaqtincha to'xtatildi. Klinikaga qo'ng'iroq qiling yoki keyinroq urinib ko'ring.** | |
| `bot_paused_phone` | Запись через бота временно приостановлена. Позвоните в клинику: {phone} — или загляните позже. | **Bot orqali yozilish vaqtincha to'xtatildi. Klinikaga qo'ng'iroq qiling: {phone} — yoki keyinroq urinib ko'ring.** | |
| `outside_hours` | Клиника работает с {open} до {close}. | **Klinika {open} dan {close} gacha ishlaydi.** | |
| `hours_today` | 🕐 Сегодня клиника работает с {open} до {close}. | **🕐 Bugun klinika {open} dan {close} gacha ishlaydi.** | |
| `hours_next` | 🕐 Сегодня клиника не работает. Ближайший рабочий день — {date}: с {open} до {close}. | **🕐 Bugun klinika ishlamaydi. Eng yaqin ish kuni — {date}: {open} dan {close} gacha.** | |
| `clinic_address` | 📍 Наш адрес: {address} | **📍 Manzilimiz: {address}** | |
| `clinic_payment` | 💳 Оплата: {info} | **💳 To'lov: {info}** | |
| `clinic_phone` | 📞 Телефон: {phone} | **📞 Telefon: {phone}** | |
| `about_header` | ℹ️ <b>{clinic}</b> | **ℹ️ <b>{clinic}</b>** | |
| `not_understood` | 🤔 Я не понял. Помогу записаться, перенести или отменить приём — выберите действие в меню. Нужен человек — напишите «позовите администратора». | **🤔 Tushunmadim. Qabulga yozilish, uni ko'chirish yoki bekor qilishda yordam beraman — menyudan amalni tanlang. Administrator kerak bo'lsa — «administratorni chaqiring» deb yozing.** | |
| `btn_call_admin` | 👤 Позвать администратора | **👤 Administratorni chaqirish** | |
| `btn_back_to_bot` | ↩️ Вернуться к записи | **↩️ Yozilishga qaytish** | |
| `escalated_closed` | 👤 Передаю администратору. Клиника сейчас закрыта — он ответит вам здесь утром. | **👤 Administratorga uzataman. Klinika hozir yopiq — u sizga ertalab shu yerda javob beradi.** | |
| `relay_from_admin` | 👤 Администратор: {text} | **👤 Administrator: {text}** | |
| `confirm_retry` | Техническая заминка — подтвердить запись не получилось. Пожалуйста, выберите время ещё раз: | **Texnik nosozlik — qabulni tasdiqlab bo'lmadi. Iltimos, vaqtni yana tanlang:** | |
| `btn_menu_about` | ℹ️ О клинике | **ℹ️ Klinika haqida** | |

---

## 15. Админ-консоль (владелец клиники)

Кнопочная консоль в админ-чате, сводка владельца и ответы админ-команд: ими
пользуется не пациент, а владелец или администратор клиники. Тон другой —
короткие деловые формулировки, но то же обращение на «siz». Узбекский здесь
целиком черновик, вычитки не проходил.

### Верхнее меню и язык

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `btn_services` | 💊 Услуги | **💊 Xizmatlar** | |
| `btn_doctors` | 🧑‍⚕️ Врачи | **🧑‍⚕️ Shifokorlar** | |
| `btn_about` | 🏥 О клинике | **🏥 Klinika haqida** | |
| `btn_dayoff` | 📅 Выходные | **📅 Dam olish kunlari** | |
| `btn_stats` | 📊 Статистика | **📊 Statistika** | |
| `btn_pause` | ⏸ Пауза | **⏸ Pauza** | |
| `btn_resume` | ▶️ Возобновить | **▶️ Davom ettirish** | |
| `btn_lang` | 🌐 O'zbekcha | **🌐 Русский** | |
| `console_title` | 🛠 <b>Админ-консоль</b> ⏎ Выберите раздел 👇 | **🛠 <b>Admin-konsol</b> ⏎ Bo'limni tanlang 👇** | |
| `console_paused` | ⏸ <i>Бот на паузе.</i> ⏎  ⏎ | **⏸ <i>Bot pauzada.</i> ⏎  ⏎** | |
| `lang_switched` | ✅ Язык консоли: русский | **✅ Konsol tili: o'zbekcha** | |
| `btn_services_back` | ◀ Услуги | **◀ Xizmatlar** | |
| `btn_doctors_back` | ◀ Врачи | **◀ Shifokorlar** | |
| `btn_dayoff_add` | ➕ Закрыть день | **➕ Kunni yopish** | |
### Общие кнопки и статусы

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `btn_cancel` | ✖ Отмена | **✖ Bekor qilish** | |
| `btn_back` | ◀ Назад | **◀ Orqaga** | |
| `btn_home` | ◀ Меню | **◀ Menyu** | |
| `btn_hide` | ⛔ Скрыть | **⛔ Yashirish** | |
| `btn_show` | ✅ Показать | **✅ Ko'rsatish** | |
| `btn_delete` | 🗑 Удалить совсем | **🗑 Butunlay o'chirish** | |
| `btn_delete_yes` | 🗑 Да, удалить | **🗑 Ha, o'chirish** | |
| `badge_active_f` | 🟢 Активна | **🟢 Faol** | |
| `badge_active_m` | 🟢 Активен | **🟢 Faol** | |
| `badge_hidden_f` | ⚪ Скрыта | **⚪ Yashirilgan** | |
| `badge_hidden_m` | ⚪ Скрыт | **⚪ Yashirilgan** | |
### Услуги и цены

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `services_title` | 💊 <b>Услуги</b> ⏎ Выберите услугу 👇 | **💊 <b>Xizmatlar</b> ⏎ Xizmatni tanlang 👇** | |
| `btn_svc_add` | + Добавить услугу | **+ Xizmat qo'shish** | |
| `svc_hidden_item` | ⚪ {name} (скрыта) | **⚪ {name} (yashirilgan)** | |
| `svc_card` | {emoji} <b>{name}</b> ⏎  ⏎ 💰 Цена: {price} ⏎ ⏱ Длительность: {duration} ⏎ {badge} | **{emoji} <b>{name}</b> ⏎  ⏎ 💰 Narxi: {price} ⏎ ⏱ Davomiyligi: {duration} ⏎ {badge}** | |
| `price_unset` | не задана | **kiritilmagan** | |
| `sum` | {value} сум | **{value} so'm** | |
| `minutes` | {value} мин | **{value} daqiqa** | |
| `btn_price_edit` | 💰 Изм. цену | **💰 Narxi** | |
| `btn_dur_edit` | ⏱ Изм. длит. | **⏱ Davomiyligi** | |
| `svc_hidden_notice` | ⛔ Услуга скрыта | **⛔ Xizmat yashirildi** | |
| `svc_shown_notice` | ✅ Услуга снова доступна | **✅ Xizmat yana mavjud** | |
| `svc_delete_confirm` | 🗑 Удалить услугу навсегда? Отменить это будет нельзя. | **🗑 Xizmat butunlay o'chirilsinmi? Buni qaytarib bo'lmaydi.** | |
| `svc_deleted` | ✅ Услуга удалена | **✅ Xizmat o'chirildi** | |
| `dur_prompt` | ⏱ <b>{label}</b> ⏎ Текущая длительность: {current} ⏎  ⏎ Введите длительность в минутах ({lo}–{hi}), например 30. | **⏱ <b>{label}</b> ⏎ Hozirgi davomiyligi: {current} ⏎  ⏎ Davomiyligini daqiqada kiriting ({lo}–{hi}), masalan 30.** | |
| `dur_unknown` | неизвестно | **noma'lum** | |
| `dur_invalid` | ⚠️ Длительность — целое число от {lo} до {hi} минут. | **⚠️ Davomiyligi — {lo} dan {hi} gacha butun son (daqiqa).** | |
| `dur_saved` | ✅ Длительность «{label}»: {duration} | **✅ «{label}» davomiyligi: {duration}** | |
| `svcadd_all_present` | ✅ Все услуги из каталога уже добавлены. | **✅ Katalogdagi barcha xizmatlar allaqachon qo'shilgan.** | |
| `svcadd_catalog` | Добавить услугу из каталога: | **Katalogdan xizmat qo'shish:** | |
| `svcadd_prompt` | ➕ <b>{label}</b> ⏎ Введите длительность приёма в минутах ({lo}–{hi}), например 30. | **➕ <b>{label}</b> ⏎ Qabul davomiyligini daqiqada kiriting ({lo}–{hi}), masalan 30.** | |
| `svcadd_retry` | Введите длительность в минутах ({lo}–{hi}): | **Davomiyligini daqiqada kiriting ({lo}–{hi}):** | |
| `svcadd_done` | ✅ Услуга «{label}» добавлена, {duration} | **✅ «{label}» xizmati qo'shildi, {duration}** | |
| `price_prompt` | 💰 <b>{label}</b> ⏎ Текущая цена: {current} ⏎  ⏎ Введите новую цену в сумах, например 400000. | **💰 <b>{label}</b> ⏎ Hozirgi narxi: {current} ⏎  ⏎ Yangi narxni so'mda kiriting, masalan 400000.** | |
| `price_invalid` | ⚠️ Цена — целое число сум больше нуля, например 400000. ⏎ Введите ещё раз или нажмите «Отмена». | **⚠️ Narx — noldan katta butun son (so'm), masalan 400000. ⏎ Qaytadan kiriting yoki «Bekor qilish» tugmasini bosing.** | |
| `price_saved` | ✅ Цена «{label}»: {price} | **✅ «{label}» narxi: {price}** | |
| `svc_exists` | ⚠️ Такая услуга уже есть в клинике. | **⚠️ Bunday xizmat klinikada allaqachon bor.** | |
| `svc_still_active` | ⚠️ Услуга ещё доступна пациентам — сначала скройте её. | **⚠️ Xizmat hali bemorlarga ochiq — avval uni yashiring.** | |
| `svc_in_use` | ⚠️ Услугу нельзя удалить: на неё ссылаются записи. | **⚠️ Xizmatni o'chirib bo'lmaydi: unga yozuvlar bog'langan.** | |
### О клинике

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `faq_title` | 🏥 <b>О клинике</b> ⏎ Выберите поле 👇 | **🏥 <b>Klinika haqida</b> ⏎ Maydonni tanlang 👇** | |
| `faq_btn_address` | 📍 Адрес | **📍 Manzil** | |
| `faq_btn_payment` | 💳 Оплата | **💳 To'lov** | |
| `faq_btn_phone` | 📞 Телефон | **📞 Telefon** | |
| `faq_name_address` | Адрес | **Manzil** | |
| `faq_name_payment` | Условия оплаты | **To'lov shartlari** | |
| `faq_name_phone` | Телефон | **Telefon** | |
| `faq_unset` | не задано | **kiritilmagan** | |
| `faq_prompt` | 🏥 <b>{title}</b> ⏎ Текущее значение: {current} ⏎  ⏎ Введите новое значение или нажмите «Отмена». | **🏥 <b>{title}</b> ⏎ Hozirgi qiymati: {current} ⏎  ⏎ Yangi qiymatni kiriting yoki «Bekor qilish» tugmasini bosing.** | |
| `faq_invalid` | ⚠️ Введите непустой текст до {limit} символов. | **⚠️ {limit} belgigacha bo'sh bo'lmagan matn kiriting.** | |
| `faq_saved` | ✅ {title} обновлено | **✅ {title} yangilandi** | |
### Врачи

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `doctors_title` | 🧑‍⚕️ <b>Врачи</b> ⏎ Выберите врача 👇 | **🧑‍⚕️ <b>Shifokorlar</b> ⏎ Shifokorni tanlang 👇** | |
| `btn_doc_add` | + Добавить врача | **+ Shifokor qo'shish** | |
| `doc_noname` | (без имени) | **(ismsiz)** | |
| `doc_placeholder` | [врач {short_id}] | **[shifokor {short_id}]** | |
| `doc_hidden_item` | ⚪ {name} (скрыт) | **⚪ {name} (yashirilgan)** | |
| `doc_card` | 🧑‍⚕️ <b>{name}</b> ⏎  ⏎ ⏲ Буфер: {buffer} ⏎ 📆 Календарь: {calendar} ⏎ {badge} ⏎  ⏎ 📅 <b>График</b> ⏎ {schedule} | **🧑‍⚕️ <b>{name}</b> ⏎  ⏎ ⏲ Bufer: {buffer} ⏎ 📆 Kalendar: {calendar} ⏎ {badge} ⏎  ⏎ 📅 <b>Ish jadvali</b> ⏎ {schedule}** | |
| `cal_linked` | привязан | **ulangan** | |
| `cal_unlinked` | не привязан | **ulanmagan** | |
| `btn_doc_name` | 👤 Имя | **👤 Ismi** | |
| `btn_doc_buffer` | ⏲ Буфер | **⏲ Bufer** | |
| `btn_doc_sched` | 📅 Расписание | **📅 Ish jadvali** | |
| `dname_prompt` | 👤 <b>Имя врача</b> ⏎  ⏎ Введите имя (до {limit} символов): | **👤 <b>Shifokor ismi</b> ⏎  ⏎ Ismni kiriting ({limit} belgigacha):** | |
| `dname_invalid` | Имя — непустая строка до {limit} символов. | **Ism — {limit} belgigacha bo'sh bo'lmagan matn.** | |
| `dname_saved` | ✅ Имя обновлено: {name} | **✅ Ism yangilandi: {name}** | |
| `dbuf_prompt` | ⏲ <b>Буфер</b> ⏎  ⏎ Введите буфер в минутах ({lo}–{hi}), например 10: | **⏲ <b>Bufer</b> ⏎  ⏎ Buferni daqiqada kiriting ({lo}–{hi}), masalan 10:** | |
| `dbuf_invalid` | Буфер — целое число от {lo} до {hi}. | **Bufer — {lo} dan {hi} gacha butun son.** | |
| `dbuf_saved` | ✅ Буфер: {buffer} | **✅ Bufer: {buffer}** | |
| `docadd_prompt` | 🧑‍⚕️ <b>Новый врач</b> ⏎  ⏎ Введите имя: | **🧑‍⚕️ <b>Yangi shifokor</b> ⏎  ⏎ Ismni kiriting:** | |
| `doc_added` | ✅ Врач «{name}» добавлен | **✅ «{name}» shifokor qo'shildi** | |
| `doc_hidden_notice` | ⛔ Врач скрыт | **⛔ Shifokor yashirildi** | |
| `doc_hidden_bookings` | ⏎ ⚠️ Будущих записей к нему: {count} — они остаются в силе, перенесите их сами | **⏎ ⚠️ Unga kelgusi yozuvlar: {count} — ular bekor qilinmadi, o'zingiz ko'chiring** | |
| `doc_shown_notice` | ✅ Врач снова в записи | **✅ Shifokor yana yozuvda** | |
| `doc_delete_confirm` | 🗑 Удалить врача навсегда? Отменить это будет нельзя. | **🗑 Shifokor butunlay o'chirilsinmi? Buni qaytarib bo'lmaydi.** | |
| `doc_deleted` | ✅ Врач удалён | **✅ Shifokor o'chirildi** | |
| `doc_still_active` | ⚠️ Врач ещё доступен пациентам — сначала скройте его. | **⚠️ Shifokor hali bemorlarga ochiq — avval uni yashiring.** | |
| `doc_in_use` | ⚠️ Врача нельзя удалить: на него ссылаются записи. | **⚠️ Shifokorni o'chirib bo'lmaydi: unga yozuvlar bog'langan.** | |
### Расписание

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `sched_title` | 📅 <b>Расписание</b> ⏎ Выберите шаблон или задайте свой 👇 ⏎  ⏎ Шаблон задаёт неделю целиком, «Свой график» меняет только выбранные дни. | **📅 <b>Ish jadvali</b> ⏎ Shablonni tanlang yoki o'zingiznikini kiriting 👇 ⏎  ⏎ Shablon butun haftani belgilaydi, «O'z jadvali» faqat tanlangan kunlarni o'zgartiradi.** | |
| `btn_sched_custom` | 📝 Свой график | **📝 O'z jadvali** | |
| `sched_tpl_workweek` | Пн–Пт 09–18 | **Du–Ju 09–18** | |
| `sched_tpl_six_days` | Пн–Сб 09–13 / 14–18 | **Du–Sh 09–13 / 14–18** | |
| `sched_tpl_late` | Пн–Пт 10:00–19:00 | **Du–Ju 10:00–19:00** | |
| `sched_saved` | ✅ Расписание задано | **✅ Ish jadvali belgilandi** | |
| `sched_days_title` | 📅 <b>Свой график</b> ⏎ Отметьте дни, которые меняем 👇 ⏎  ⏎ Остальные дни останутся как есть. | **📅 <b>O'z jadvali</b> ⏎ O'zgartiriladigan kunlarni belgilang 👇 ⏎  ⏎ Qolgan kunlar o'z holicha qoladi.** | |
| `btn_sched_next` | Далее → | **Keyingi →** | |
| `sched_pick_day` | Выберите хотя бы один рабочий день. | **Kamida bitta ish kunini tanlang.** | |
| `sched_pick_any_day` | Выберите хотя бы один день. | **Kamida bitta kunni tanlang.** | |
| `sched_shifts_prompt` | Дни: {days} ⏎  ⏎ Введите смены через запятую, например: ⏎ <code>09:00-13:00, 14:00-18:00</code> ⏎  ⏎ Остальные дни недели останутся как есть. | **Kunlar: {days} ⏎  ⏎ Smenalarni vergul bilan kiriting, masalan: ⏎ <code>09:00-13:00, 14:00-18:00</code> ⏎  ⏎ Haftaning qolgan kunlari o'z holicha qoladi.** | |
| `sched_shifts_invalid` | ⚠️ Формат: <code>09:00-13:00, 14:00-18:00</code> ⏎ Введите ещё раз: | **⚠️ Format: <code>09:00-13:00, 14:00-18:00</code> ⏎ Qaytadan kiriting:** | |
| `btn_sched_dayoff` | 🚫 Сделать выходным | **🚫 Dam olish kuni qilish** | |
| `sched_dayoff_saved` | ✅ Дни сделаны выходными | **✅ Kunlar dam olish kuni qilindi** | |
| `sched_dayoff_last` | ⚠️ Так у врача не останется ни одного рабочего дня. ⏎  ⏎ Чтобы убрать врача из записи целиком, нажмите «⛔ Скрыть» в его карточке. | **⚠️ Unda shifokorda birorta ham ish kuni qolmaydi. ⏎  ⏎ Shifokorni yozuvdan butunlay olib tashlash uchun uning kartochkasida «⛔ Yashirish» tugmasini bosing.** | |
| `day_off` | выходной | **dam olish** | |
| `week_off` | выходной всю неделю | **butun hafta dam olish** | |
| `weekday_mon` | Пн | **Du** | |
| `weekday_tue` | Вт | **Se** | |
| `weekday_wed` | Ср | **Ch** | |
| `weekday_thu` | Чт | **Pa** | |
| `weekday_fri` | Пт | **Ju** | |
| `weekday_sat` | Сб | **Sh** | |
| `weekday_sun` | Вс | **Ya** | |
### Выходные дни

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `dayoff_title` | 📅 <b>Выходные</b> ⏎ | **📅 <b>Dam olish kunlari</b> ⏎** | |
| `dayoff_intro` | Ближайшие закрытые дни (тап — снова открыть): | **Yaqin yopiq kunlar (bosing — yana ochiladi):** | |
| `dayoff_empty` | 📅 Закрытых дней нет — клиника работает по графику. | **📅 Yopiq kunlar yo'q — klinika jadval bo'yicha ishlaydi.** | |
| `dayoff_prompt` | 📅 Введите дату и (по желанию) причину: ⏎ <code>21.03 Навруз</code> | **📅 Sanani va (xohlasangiz) sababni kiriting: ⏎ <code>21.03 Navro'z</code>** | |
| `dayoff_invalid` | ⚠️ Формат: <code>21.03 причина</code> (день.месяц). Повторите или нажмите «Отмена». | **⚠️ Format: <code>21.03 sabab</code> (kun.oy). Qaytaring yoki «Bekor qilish» tugmasini bosing.** | |
| `dayoff_closed` | ✅ {date} — выходной | **✅ {date} — dam olish kuni** | |
| `dayoff_reopened` | ✅ День снова рабочий | **✅ Kun yana ish kuni** | |
| `dayoff_booked_warning` | ⏎  ⏎ ⚠️ На этот день уже есть записи: {count} ({times}). ⏎ Бот их не отменяет и напоминания придут — перенесите или отмените их сами. | **⏎  ⏎ ⚠️ Bu kunga yozuvlar bor: {count} ({times}). ⏎ Bot ularni bekor qilmaydi va eslatmalar boradi — o'zingiz ko'chiring yoki bekor qiling.** | |
| `dayoff_warning_more` | и ещё {count} | **va yana {count}** | |
| `dayoff_ok` | [OK] {date} — выходной | **[OK] {date} — dam olish kuni** | |
| `dayoff_already` | {date} уже выходной. | **{date} allaqachon dam olish kuni.** | |
| `dayopen_ok` | [OK] {date} снова рабочий | **[OK] {date} yana ish kuni** | |
| `dayopen_already` | {date} и так рабочий. | **{date} allaqachon ish kuni.** | |
| `dayoff_usage` | Формат: /dayoff DD.MM [причина] — закрыть день, /dayopen DD.MM — снова открыть. ⏎ {upcoming} | **Format: /dayoff DD.MM [sabab] — kunni yopish, /dayopen DD.MM — yana ochish. ⏎ {upcoming}** | |
| `dayoff_upcoming` | Ближайшие выходные: {days} | **Yaqin dam olish kunlari: {days}** | |
| `dayoff_none_ahead` | Закрытых дней впереди нет. | **Oldinda yopiq kunlar yo'q.** | |
### Сводка владельца и вечерний дайджест

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `stats_header_day` | 📊 <b>Сводка за {date}</b> | **📊 <b>{date} kunlik hisobot</b>** | |
| `stats_header_range` | 📊 <b>Сводка за {days} дн. ({first}–{last})</b> | **📊 <b>{days} kunlik hisobot ({first}–{last})</b>** | |
| `stats_value_title` | 💰 Ценность | **💰 Qiymat** | |
| `stats_booked` | • записей подтверждено: {count}{trend}{after} | **• tasdiqlangan yozuvlar: {count}{trend}{after}** | |
| `stats_after_hours` | (из них {count} — вне рабочих часов) | **(shundan {count} tasi — ish vaqtidan tashqari)** | |
| `stats_prevented` | • предотвращено неявок: {count} (слотов на ≈ {money} сум освобождено заранее) | **• oldi olingan kelmasliklar: {count} (≈ {money} so'mlik vaqt oldindan bo'shatildi)** | |
| `stats_cancelled` | • отмен: {count}{trend} | **• bekor qilingan: {count}{trend}** | |
| `stats_escalated` | • эскалаций к администратору: {count} | **• administratorga murojaatlar: {count}** | |
| `stats_clients` | 👥 Клиенты ⏎ • новых: {new} · вернувшихся: {returning} | **👥 Mijozlar ⏎ • yangi: {new} · qaytgan: {returning}** | |
| `stats_top_doctors` | 👨‍⚕️ Топ врачей | **👨‍⚕️ Eng band shifokorlar** | |
| `stats_doctor_line` | • {name} — {count} зап. (≈ {money} сум) | **• {name} — {count} ta yozuv (≈ {money} so'm)** | |
| `stats_hit_service` | ✨ Хит-услуга ⏎ • {service} — {count} зап. | **✨ Eng ommabop xizmat ⏎ • {service} — {count} ta yozuv** | |
| `stats_waitlist` | 🔔 Очередь ожидания ⏎ • сейчас ждут слота: {count} | **🔔 Kutish navbati ⏎ • hozir vaqt kutayotganlar: {count}** | |
| `stats_tech` | ⚙️ Служебное ⏎ • напоминаний: {reminders} · LLM: {requests} запросов, {tokens} токенов, сбоев: {failures}, repair: {repairs} | **⚙️ Texnik ⏎ • eslatmalar: {reminders} · LLM: {requests} so'rov, {tokens} token, xato: {failures}, repair: {repairs}** | |
| `stats_p95` | · p95 ответа: {seconds} с (SLA &lt; 5 с) | **· javob p95: {seconds} s (SLA &lt; 5 s)** | |
| `digest_title` | 📊 <b>Итог дня</b> | **📊 <b>Kun yakuni</b>** | |
| `digest_booked` | • записей: {count}{after} | **• yozuvlar: {count}{after}** | |
| `digest_prevented` | • предотвращено неявок: {count} (≈ {money} сум) | **• oldi olingan kelmasliklar: {count} (≈ {money} so'm)** | |
| `digest_escalated` | • эскалаций: {count} | **• murojaatlar: {count}** | |
| `digest_waitlist` | ⏎ • 🔔 в очереди ожидания: {count} | **⏎ • 🔔 kutish navbatida: {count}** | |
| `digest_more` | 📊 Подробнее | **📊 Batafsil** | |
| `questions_title` | ❓ <b>Вопросы без ответа ({count})</b> | **❓ <b>Javobsiz savollar ({count})</b>** | |
| `questions_more` | ⏎ … и ещё {count} | **⏎ … va yana {count}** | |
| `stats_usage` | Формат: /stats — за сегодня, /stats 7 — за неделю или /stats 30 | **Format: /stats — bugun uchun, /stats 7 — hafta uchun yoki /stats 30** | |
| `btn_period` | {days} дней | **{days} kun** | |
| `btn_period_today` | 📅 День | **📅 Bugun** | |
### Лента записей, экран «Сегодня» и утренняя сводка

Карточка приходит владельцу в момент события (запись, отмена, перенос),
экран «Сегодня» показывает весь день списком, утренняя сводка приносит его
в 08:30 сама. Пометка 🌙 — запись сделана вне рабочих часов (бот работал,
пока клиника спала). `{patient}`, `{doctor}` — имена людей, `{phone}` —
номер цифрами, `{when}` — дата и время `08.06 15:30`, `{time}` — часы
`15:30`, `{date}` — дата `08.06`.

В списке дня перед временем стоит значок: ✅ — пациент нажал «Приду» в
напоминании, ⏳ — напоминание ушло, а ответа нет. Строка `today_call_hint`
считает таких молчунов и советует владельцу позвонить им до приёма.

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `feed_booked` | ✅ Запись: {patient}, {service}, {when}, {doctor}{night} | **✅ Yozuv: {patient}, {service}, {when}, {doctor}{night}** | |
| `feed_cancelled` | ❌ Отмена: {patient}, {service}, {when}, {doctor}{night} | **❌ Bekor qilindi: {patient}, {service}, {when}, {doctor}{night}** | |
| `feed_resched` | 🔄 Перенос: {patient}, {service} → {when}, {doctor}{night} | **🔄 Ko'chirildi: {patient}, {service} → {when}, {doctor}{night}** | |
| `feed_no_name` | без имени | **ismsiz** | |
| `feed_no_service` | приём | **qabul** | |
| `btn_today_list` | 📅 Сегодня | **📅 Bugun** | |
| `btn_today_refresh` | 🔄 Обновить | **🔄 Yangilash** | |
| `today_header` | 📅 <b>Сегодня, {date}</b> — приёмов: {count} | **📅 <b>Bugun, {date}</b> — qabullar: {count}** | |
| `today_line` | {time} {patient} ({phone}) — {service}, {doctor} | **{time} {patient} ({phone}) — {service}, {doctor}** | |
| `today_empty` | 📅 Сегодня записей нет | **📅 Bugun yozuvlar yo'q** | |
| `today_no_phone` | без телефона | **telefonsiz** | |
| `today_call_hint` | ⚠️ Без ответа после напоминания: {count} — стоит позвонить | **⚠️ Eslatmaga javob bermaganlar: {count} — qo'ng'iroq qilgan ma'qul** | |
| `morning_header` | ☀️ <b>Доброе утро!</b> | **☀️ <b>Xayrli tong!</b>** | |
### Команды администратора

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `paused_ok` | [OK] бот на паузе. Пациентам отвечаем «запись временно по телефону». Вернуть: /resume | **[OK] bot pauzada. Bemorlarga «yozuv vaqtincha telefon orqali» deb javob beramiz. Qaytarish: /resume** | |
| `paused_ok_reason` | [OK] бот на паузе ({reason}). Пациентам отвечаем «запись временно по телефону». Вернуть: /resume | **[OK] bot pauzada ({reason}). Bemorlarga «yozuv vaqtincha telefon orqali» deb javob beramiz. Qaytarish: /resume** | |
| `resumed_ok` | [OK] бот снова принимает запись | **[OK] bot yana yozuvni qabul qilmoqda** | |
| `llm_off_ok` | [OK] свободный текст выключен — работают только кнопки | **[OK] erkin matn o'chirildi — faqat tugmalar ishlaydi** | |
| `llm_on_ok` | [OK] свободный текст снова понимает NLU | **[OK] erkin matnni yana NLU tushunadi** | |
| `llm_usage` | Формат: /llm on\|off | **Format: /llm on\|off** | |
| `action_failed` | ⚠️ Не получилось: {reason} | **⚠️ Bajarilmadi: {reason}** | |
| `release_usage` | Формат: /release <chat_id> (число из алерта эскалации) | **Format: /release <chat_id> (murojaat xabaridagi raqam)** | |
| `release_not_found` | Чат {chat} не найден. | **{chat} chat topilmadi.** | |
| `release_not_escalated` | Чат {chat} не в эскалации (состояние: {state}). | **{chat} chat murojaatda emas (holati: {state}).** | |
| `release_ok` | [OK] эскалация снята: чат {chat} | **[OK] murojaat yopildi: chat {chat}** | |
| `relay_delivered` | ✅ Доставлено | **✅ Yetkazildi** | |
| `relay_failed` | ⚠️ Не доставлено: {error} | **⚠️ Yetkazilmadi: {error}** | |
| `relay_no_anchor` | Отвечайте реплаем на алерт эскалации или карточку 💬 | **Eskalatsiya ogohlantirishiga yoki 💬 kartochkaga javob (reply) qiling** | |
| `forget_usage` | Формат: /forget <chat_id> — анонимизировать пациента | **Format: /forget <chat_id> — bemor ma'lumotlarini o'chirish** | |
| `forget_not_found` | Чат {chat} не найден — данных нет. | **{chat} chat topilmadi — ma'lumot yo'q.** | |
| `forget_ok` | [OK] чат {chat}: пациент анонимизирован, диалог и сообщения удалены. Будущие записи не отменены — отмените отдельно, если пациент просил. | **[OK] chat {chat}: bemor ma'lumotlari o'chirildi, suhbat va xabarlar tozalandi. Kelgusi yozuvlar bekor qilinmadi — bemor so'ragan bo'lsa, alohida bekor qiling.** | |
### Прочее

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `reason_suffix` | ({reason}) | **({reason})** | |

### Превью «глазами пациента»

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `btn_preview` | 👁 Глазами пациента | **👁 Bemor ko'zi bilan** | |
| `preview_head` | 👁 <b>Так вас видит пациент</b> ⏎ Язык пациента: {language} ⏎ <i>Это картинка: записи не создаются.</i> ⏎  ⏎ | **👁 <b>Bemor sizni shunday ko'radi</b> ⏎ Bemor tili: {language} ⏎ <i>Bu ko'rinish: yozuv yaratilmaydi.</i> ⏎  ⏎** | |
| `preview_menu` | ⏎  ⏎ <i>Кнопки пациента:</i> {buttons} | **⏎  ⏎ <i>Bemor tugmalari:</i> {buttons}** | |
| `preview_lang_ru` | русский | **rus tili** | |
| `preview_lang_uz` | узбекский | **o'zbek tili** | |
| `btn_preview_ru` | 🇷🇺 По-русски | **🇷🇺 Rus tilida** | |
| `btn_preview_uz` | 🇺🇿 По-узбекски | **🇺🇿 O'zbek tilida** | |

### Алерты администратору

Уходят без HTML-разметки; `{reason}` подставляет причину, она пока приходит по-русски из служебных путей.

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `doc_missing` | ⚠️ Этого врача уже нет — возможно, его удалили в другом чате. | **⚠️ Bu shifokor endi yo'q — boshqa chatda o'chirilgan bo'lishi mumkin.** | |
| `svc_missing` | ⚠️ Этой услуги уже нет — возможно, её удалили в другом чате. | **⚠️ Bu xizmat endi yo'q — boshqa chatda o'chirilgan bo'lishi mumkin.** | |
| `alert_escalation` | Эскалация: чат {chat} ⏎ Причина: {reason} ⏎ Что хотел пациент: {context} ⏎ Снять: /release {chat} | **Murojaat: chat {chat} ⏎ Sababi: {reason} ⏎ Bemor nima xohlagan: {context} ⏎ Yopish: /release {chat}** | |
| `alert_fyi` | 🟡 К сведению: {reason} ⏎ Что хотел пациент: {context} | **🟡 Ma'lumot uchun: {reason} ⏎ Bemor nima xohlagan: {context}** | |
| `alert_ops` | ⚠ {reason} | **⚠ {reason}** | |
| `alert_system` | ⚠ Системный алерт ⏎ {reason} | **⚠ Tizim ogohlantirishi ⏎ {reason}** | |
| `relay_card` | 💬 Чат {chat}: {text} | **💬 Chat {chat}: {text}** | |

---

## 16. Причины внутри алертов владельцу

Текст, который бот подставляет в шапку алерта: почему он позвал человека, что
случилось с календарём, чего не смог. Каркас («Murojaat», «Sababi») уже был
переведён, а причина приходила по-русски — владелец видел половину экрана на
своём языке. Тон: короткое деловое сообщение администратору, без паники и без
технических деталей — их бот отправляет отдельно тому, кто чинит систему.

Системные причины (TLS-сертификат, бэкапы, webhook, дрейф модели) в этот
список НЕ входят осознанно: они уходят не клинике, а тому, кто обслуживает
систему, и переводить их некому.

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `reason_wants_human` | пациент просит администратора | **bemor administrator bilan gaplashmoqchi** | |
| `reason_confirm_failed` | сбой подтверждения записи | **yozuvni tasdiqlashda xatolik** | |
| `reason_no_slots` | нет слотов на 2 недели вперёд | **2 hafta oldinga bo'sh vaqt yo'q** | |
| `reason_no_slots_reschedule` | перенос: нет слотов на 2 недели вперёд | **ko'chirish: 2 hafta oldinga bo'sh vaqt yo'q** | |
| `reason_event_restored` | событие записи удалили в календаре — восстановил; правки записей — через бота | **kalendarda yozuv hodisasi o'chirilgan — tikladim; yozuvlarni bot orqali o'zgartiring** | |
| `reason_event_moved_back` | событие записи сдвинули в календаре — вернул; переносы — через бота | **kalendarda yozuv vaqti ko'chirilgan — qaytardim; ko'chirishni bot orqali qiling** | |
| `reason_relocated` | запись {old} вытеснена ручным событием — перенесена на {new} | **{old} yozuvi qo'lda kiritilgan hodisa tufayli {new} ga ko'chirildi** | |
| `reason_unrelocatable` | запись {old} вытеснена ручным событием, перенести некуда — отменена | **{old} yozuvi qo'lda kiritilgan hodisa tufayli bekor qilindi: ko'chirish uchun bo'sh vaqt yo'q** | |
| `reason_double_booking` | в календаре два приёма на одно время ({when}) — бот учитывает только первый, второй в записи не виден | **kalendarda bir vaqtga ikkita qabul ({when}) — bot faqat birinchisini hisobga oladi, ikkinchisi yozuvlarda ko'rinmaydi** | |
| `reason_gcal_auth_dead` | Google OAuth-токен мёртв — синхронизация календаря остановилась и сама не починится | **Google OAuth tokeni ishlamaydi — kalendar sinxronizatsiyasi to'xtadi va o'zi tuzalmaydi** | |
| `reason_sync_stuck` | синхронизация Google Calendar не работает {cycles} циклов подряд — проверьте доступ Google | **Google Calendar sinxronizatsiyasi {cycles} tsikl ketma-ket ishlamadi — Google ruxsatini tekshiring** | |
| `reason_sync_restored` | синхронизация Google Calendar восстановлена. | **Google Calendar sinxronizatsiyasi tiklandi.** | |
| `reason_llm_cap` | дневной лимит LLM-токенов ({cap}) исчерпан — бот эскалирует диалоги до конца дня | **kunlik LLM token limiti ({cap}) tugadi — bot kun oxirigacha suhbatlarni administratorga uzatadi** | |
| `reason_reminder_failed` | напоминание пациенту (чат {chat}) не доставлено после {attempts} попыток — свяжитесь с ним сами | **bemorga eslatma ({chat} chat) {attempts} urinishdan keyin yetib bormadi — u bilan o'zingiz bog'laning** | |
| `reason_update_failed` | сообщение пациента (чат {chat}) не удалось обработать — свяжитесь с ним сами | **bemor xabarini ({chat} chat) qayta ishlab bo'lmadi — u bilan o'zingiz bog'laning** | |

### Что хотел пациент (выжимка брони в том же алерте)

Подписи полей: бот перечисляет, что пациент успел выбрать до того, как позвал
человека. Формат — «подпись — значение», через точку с запятой.

| Ключ | Русский | Узбекский (черновик) | Правка |
|---|---|---|---|
| `ctx_service` | услуга — {value} | **xizmat — {value}** | |
| `ctx_day` | день — {value} | **kun — {value}** | |
| `ctx_time` | время — {value} | **vaqt — {value}** | |
| `ctx_slot` | выбранный слот — {value} | **tanlangan vaqt — {value}** | |
| `ctx_doctor` | врач — {value} | **shifokor — {value}** | |
| `ctx_cancel` | отмена записи на — {value} | **yozuvni bekor qilish — {value}** | |
| `ctx_empty` | пациент ещё ничего не выбрал | **bemor hali hech narsa tanlamagan** | |

Строки `reason_gcal_auth_dead` и `reason_sync_stuck` в коде длиннее: за
приведённой фразой идёт техническая подсказка (команда переавторизации и
предупреждение, что пока синк стоит, бот может записать пациента на занятое
врачом время). Их тоже нужно вычитать — полный текст в
`src/navbat/telegram/admin_texts.py`.

## 13. Отдельные вопросы носителю

1. **Апостроф**: ASCII `'` или окина `ʻ` (см. шапку) — что привычнее?
2. **Грамматика с подстановками**: даты и время вставляются в формате
   `08.06` / `08.06 15:30` — корректны ли аффиксы вокруг них
   («{when} ga ko'chirdim», «{asked} kuni», «{when} kungi yozuvingizni»)?
3. **`extraction` = «Tish oldirish»** — это понятный пациенту термин для
   удаления зуба, или естественнее «Tish olib tashlash»?
4. **`btn_after_tomorrow` = «Indinga»** — общепонятно ли (vs «Ertadan keyin»)?
5. **Заимствования** «Plomba», «Koronka», «Breket», «Rentgen» — так и
   говорят пациенты, или есть более ходовые варианты?
6. **Тон**: достаточно ли вежливо звучат короткие формы
   («Tushunmadim», «Ismingiz nima?») — или нужно мягче
   («Tushunolmadim», «Ismingizni yozib yuborasizmi?»)?
