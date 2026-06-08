# Узбекские тексты бота — проверка носителем

Все фразы, которые бот Navbat говорит пациенту по-узбекски. Тексты писались
как черновик — нужна проверка живым носителем до пилота (чеклист v1.0,
пункт D.4). Источник: `src/navbat/dialog/replies.py` — все 67 пар строк
бота живут только там, вне файла узбекского текста в коде нет (проверено).

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
