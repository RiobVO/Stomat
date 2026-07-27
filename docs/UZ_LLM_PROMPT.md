# ПРОМПТ ДЛЯ ПРОВЕРКИ УЗБЕКСКИХ ТЕКСТОВ — скопируй всё содержимое файла в чат модели

Ты — редактор узбекского языка (ташкентская норма, латиница). Проверь
фразы телеграм-бота стоматологической клиники в Ташкенте. Бот записывает
пациентов на приём. Аудитория — обычные пациенты, любого возраста.

ЗАДАЧА: для каждой фразы реши, звучит ли узбекский текст естественно —
так, как написал бы вежливый администратор клиники в мессенджере живому
человеку. Русский текст дан как эталон смысла: узбекский должен передавать
тот же смысл и тон, дословность не нужна.

ПРАВИЛА:
1. Слова в фигурных скобках {вот_такие} — подстановки, их НЕ переводить
   и НЕ удалять. Можно менять их место во фразе и аффиксы вокруг них.
   Что подставляется: {date} и {asked} — дата «08.06»; {when}, {old},
   {new} — дата-время «08.06 15:30»; {service} — название услуги
   («Tish tozalash»); {doctor} — пусто ЛИБО «, Dilshod Karimov»;
   {price} — «150 000»; {clinic} — название клиники.
2. Эмодзи (📅 🔄 ❌ 💰 🌐 📱 ✓ •) и знак \n (перенос строки) — сохранить.
3. Тон: вежливое «siz», тёпло и просто, без канцелярита и без излишней
   цветистости. Кнопки (помечены btn_) должны остаться короткими.
4. Обращай внимание на аффиксы вокруг подстановок: «{when} ga», «{asked}
   kuni», «{when} kungi» — корректны ли они, если вместо подстановки
   встанет «08.06 15:30».

ФОРМАТ ОТВЕТА — строго такой, ничего больше:
- Верни ТОЛЬКО фразы, которые нужно исправить, по одной на строку:
  `ключ: исправленный узбекский текст целиком`
- Если фраза хороша — НЕ включай её в ответ.
- После списка правок ответь коротко на 6 ВОПРОСОВ в конце документа.
- Никаких пояснений к каждой правке, пересказов и похвал.

---

## ФРАЗЫ (ключ → контекст → RU-эталон → UZ-проверяемый)

greeting | приветствие при первом сообщении
RU: Здравствуйте! Я виртуальный администратор клиники «{clinic}»: помогу записаться, перенести или отменить приём. По медицинским вопросам ответит врач.
UZ: Assalomu alaykum! Men «{clinic}» klinikasining virtual administratoriman: qabulga yozilish, ko'chirish yoki bekor qilishda yordam beraman. Tibbiy savollarga shifokor javob beradi.

menu_hint | подсказка под главным меню
RU: Выберите действие или напишите своими словами:
UZ: Amalni tanlang yoki o'z so'zlaringiz bilan yozing:

lang_changed | подтверждение смены языка (показывается уже НА узбекском — поэтому говорит про узбекский, это не ошибка)
RU: (русская версия говорит про русский)
UZ: Til o'zbek tiliga o'zgartirildi.

medical_disclaimer | дисклеймер при медицинском вопросе
RU: Я виртуальный администратор и не даю медицинских советов — точный ответ даст врач на приёме.
UZ: Men virtual administratorman, tibbiy maslahat bera olmayman — aniq javobni shifokor qabulda beradi.

ask_service | вопрос об услуге
RU: На какую услугу вас записать?
UZ: Qaysi xizmatga yozib qo'yay?

ask_date | вопрос о дне
RU: На какой день вам удобно?
UZ: Qaysi kun sizga qulay?

offer_slots | свободное время на запрошенный день
RU: Свободное время на {date}:
UZ: {date} kuni bo'sh vaqtlar:

offer_slots_other_day | на запрошенный день мест нет
RU: На {asked} свободного времени нет. Ближайшее — {date}:
UZ: {asked} kuni bo'sh vaqt yo'q. Eng yaqini — {date}:

closed_now_slots | клиника сейчас закрыта (запрос «на сегодня» ночью)
RU: Сейчас клиника закрыта.\nБлижайшее свободное время — {date}:
UZ: Hozir klinika yopiq.\nEng yaqin bo'sh vaqt — {date}:

no_slots_at_all | нет мест две недели вперёд
RU: В ближайшие две недели свободного времени нет — передаю администратору.
UZ: Yaqin ikki haftada bo'sh vaqt yo'q — administratorga uzataman.

doctor_not_found | врач с таким именем не найден
RU: Врача с таким именем не нашёл, показываю всё свободное время.
UZ: Bunday ismli shifokor topilmadi, barcha bo'sh vaqtlarni ko'rsataman.

slot_taken | время заняли, пока пациент выбирал
RU: Это время только что заняли. Вот свежие варианты:
UZ: Bu vaqt hozirgina band bo'ldi. Mana yangi variantlar:

hold_expired | бронь выбранного времени истекла
RU: Бронь на выбранное время истекла. Вот свежие варианты:
UZ: Tanlangan vaqt broni tugadi. Mana yangi variantlar:

ask_name | вопрос об имени
RU: Как вас зовут?
UZ: Ismingiz nima?

ask_phone | просьба отправить номер кнопкой
RU: Нажмите кнопку ниже — она отправит ваш номер телефона:
UZ: Pastdagi tugmani bosing — u telefon raqamingizni yuboradi:

press_contact_button | пациент написал номер текстом вместо кнопки
RU: Чтобы оставить номер, нажмите кнопку ниже:
UZ: Raqam qoldirish uchun pastdagi tugmani bosing:

foreign_contact | пациент отправил чужой контакт
RU: Это контакт другого человека. Нажмите кнопку — она отправит ваш собственный номер:
UZ: Bu boshqa odamning kontakti. Tugmani bosing — u o'zingizning raqamingizni yuboradi:

booked | запись оформлена (пример: «Yozib qo'ydim: Tish tozalash, 08.06 15:30, Dilshod Karimov. Sizni kutamiz!»)
RU: Записал: {service}, {when}{doctor}. Ждём вас!
UZ: Yozib qo'ydim: {service}, {when}{doctor}. Sizni kutamiz!

cancel_confirm_q | подтверждение отмены (пример: «08.06 15:30 kungi yozuvingizni bekor qilaymi?»)
RU: Отменить вашу запись на {when}?
UZ: {when} kungi yozuvingizni bekor qilaymi?

cancel_done | запись отменена
RU: Запись отменена. Будем рады записать вас снова.
UZ: Yozuv bekor qilindi. Sizni yana yozishdan xursand bo'lamiz.

cancel_kept | пациент передумал отменять
RU: Хорошо, запись остаётся в силе.
UZ: Yaxshi, yozuv o'z kuchida qoladi.

cancel_none | отменять нечего
RU: Активной записи не нашёл. Хотите записаться?
UZ: Faol yozuv topilmadi. Yozilishni xohlaysizmi?

resched_none | переносить нечего
RU: Активной записи для переноса не нашёл. Хотите записаться?
UZ: Ko'chirish uchun faol yozuv topilmadi. Yozilishni xohlaysizmi?

resched_done | запись перенесена (пример: «Yozuvingizni 08.06 15:30 ga ko'chirdim...»)
RU: Перенёс вашу запись на {when}. Ждём вас!
UZ: Yozuvingizni {when} ga ko'chirdim. Sizni kutamiz!

reminder | напоминание о приёме (пример: «Eslatamiz: siz Tish tozalash uchun 08.06 15:30 ga yozilgansiz...»)
RU: Напоминаем: вы записаны на {service} {when}. Ждём вас!
UZ: Eslatamiz: siz {service} uchun {when} ga yozilgansiz. Sizni kutamiz!

attend_ok | пациент подтвердил, что придёт
RU: Отлично, ждём вас!
UZ: Ajoyib, sizni kutamiz!

conflict_moved | время стало недоступно, бот перенёс запись
RU: К сожалению, время {old} стало недоступно — перенёс вашу запись на {new}. Если не подходит, выберите другое:
UZ: Afsuski, {old} vaqti band bo'lib qoldi — yozuvingizni {new} ga ko'chirdim. To'g'ri kelmasa, boshqasini tanlang:

conflict_cancelled | время стало недоступно, заменить нечем
RU: К сожалению, время {old} стало недоступно, а свободного времени в ближайшие дни нет — запись отменена. Напишите, и подберём новое.
UZ: Afsuski, {old} vaqti band bo'lib qoldi, yaqin kunlarda bo'sh vaqt yo'q — yozuv bekor qilindi. Yozing, yangisini topamiz.

price_answer | цена услуги
RU: «{service}» — {price} сум.
UZ: «{service}» — {price} so'm.

price_unknown | цены нет в базе
RU: Цену на «{service}» уточнит администратор.
UZ: «{service}» narxini administrator aniqlashtiradi.

price_header | заголовок прайса
RU: Наши цены:
UZ: Narxlarimiz:

price_line | строка прайса
RU: • {service} — {price} сум
UZ: • {service} — {price} so'm

price_line_unknown | строка прайса без цены
RU: • {service} — цену уточнит администратор
UZ: • {service} — narxini administrator aniqlashtiradi

price_empty | прайс пуст
RU: Прайс уточнит администратор.
UZ: Narxlarni administrator aniqlashtiradi.

reask | бот не понял сообщение (фраза в кавычках — образец того, как пишет обычный пациент, должна звучать разговорно)
RU: Не понял вас. Напишите, пожалуйста, иначе — например: «запись на чистку завтра».
UZ: Tushunmadim. Boshqacha yozib ko'ring — masalan: «ertaga tish tozalashga yozilmoqchiman».

escalated | бот передаёт диалог администратору
RU: Передаю администратору — он ответит вам здесь в ближайшее время.
UZ: Administratorga uzatdim — u tez orada shu yerda javob beradi.

other_fallback | сообщение не про запись
RU: Я помогу записаться на приём: напишите услугу и удобный день.
UZ: Qabulga yozilishga yordam beraman: xizmat va qulay kunni yozing.

faq_fallback | вопрос, на который бот не знает ответ
RU: Это уточнит администратор — я передал ему ваш вопрос.
UZ: Buni administrator aniqlashtiradi — savolingizni unga uzatdim.

rate_limited | слишком много сообщений подряд
RU: Слишком много сообщений подряд — сделайте небольшую паузу, и я отвечу.
UZ: Juda ko'p xabar yubordingiz — biroz kuting, javob beraman.

text_only | пациент прислал фото/голос/стикер
RU: Пока я понимаю только текст — напишите, пожалуйста, словами.
UZ: Hozircha faqat matnni tushunaman — iltimos, so'z bilan yozing.

stale_button | нажата устаревшая кнопка
RU: Эта кнопка устарела.
UZ: Bu tugma eskirgan.

btn_menu_book | кнопка главного меню
RU: 📅 Записаться
UZ: 📅 Yozilish

btn_menu_resched | кнопка главного меню
RU: 🔄 Перенести
UZ: 🔄 Ko'chirish

btn_menu_cancel | кнопка главного меню
RU: ❌ Отменить
UZ: ❌ Bekor qilish

btn_menu_prices | кнопка главного меню
RU: 💰 Цены
UZ: 💰 Narxlar

btn_today | кнопка выбора дня
RU: Сегодня
UZ: Bugun

btn_tomorrow | кнопка выбора дня
RU: Завтра
UZ: Ertaga

btn_after_tomorrow | кнопка выбора дня
RU: Послезавтра
UZ: Indinga

btn_other_time | кнопка под списком слотов
RU: Другое время
UZ: Boshqa vaqt

btn_share_contact | кнопка отправки контакта
RU: 📱 Отправить мой номер
UZ: 📱 Raqamimni yuborish

btn_yes | кнопка подтверждения отмены
RU: Да, отменить
UZ: Ha, bekor qilish

btn_no | кнопка отказа от отмены
RU: Нет, оставить
UZ: Yo'q, qoldirish

btn_attend | кнопка в напоминании
RU: ✓ Приду
UZ: ✓ Kelaman

btn_remind_cancel | кнопка в напоминании
RU: Отменить запись
UZ: Yozuvni bekor qilish

service_cleaning | название услуги (кнопка и прайс)
RU: Чистка
UZ: Tish tozalash

service_filling | название услуги
RU: Пломба
UZ: Plomba

service_extraction | название услуги
RU: Удаление
UZ: Tish oldirish

service_implant | название услуги
RU: Имплант
UZ: Implant

service_crown | название услуги
RU: Коронка
UZ: Koronka

service_whitening | название услуги
RU: Отбеливание
UZ: Oqartirish

service_braces | название услуги
RU: Брекеты
UZ: Breket

service_checkup | название услуги
RU: Осмотр
UZ: Ko'rik

service_xray | название услуги
RU: Снимок
UZ: Rentgen

---

## 6 ВОПРОСОВ (ответь коротко после списка правок)

1. Апостроф: в текстах ASCII-знак ' (bo'sh, o'zbek). Правильнее ли для
   пациентов окина ʻ (boʻsh, oʻzbek), или ASCII привычнее в мессенджерах?
2. Аффиксы вокруг дат: «{when} ga ko'chirdim», «{asked} kuni», «{when}
   kungi yozuvingizni» — корректно ли это при подстановке «08.06 15:30»?
3. «Tish oldirish» для удаления зуба — понятный пациенту термин, или
   естественнее «Tish olib tashlash»?
4. «Indinga» (послезавтра) — общепонятно, или лучше «Ertadan keyin»?
5. Заимствования «Plomba», «Koronka», «Breket», «Rentgen» — так говорят
   пациенты, или есть более ходовые слова?
6. Короткие формы «Tushunmadim», «Ismingiz nima?» — достаточно вежливы,
   или нужно мягче?
