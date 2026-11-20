## Статус: ОТПРАВЛЕНО 2026-11-20 08:10

Письмо принято SMTP Gmail. Детали и follow-up — [SENT.md](SENT.md). .smtp.env удалён.

# Письмо ТПО «ЛЕГАТ» — готово к отправке, но не отправлено

**Кому:** Еременко Виктор Иванович, генеральный директор ТПО «ЛЕГАТ» — `tpolegat@gmail.com`  
**От:** Flumos — `flumos@gmail.com`  
**Файлы:** `email.eml` (готовое письмо + вложение), `email.md` (текст для копирования).

## Почему не отправлено автоматически

В этой среде нет почтового канала: нет Outlook/Thunderbird, нет SMTP-креденшелов
`SMTP_USER`/`SMTP_PASSWORD`, нет доступа к ящику `flumos@gmail.com`. Отправить письмо «от
flumos@gmail.com» без авторизации невозможно; подделывать отправителя нельзя.

## Как отправить (выберите один способ)

### A. Через почтовый клиент (проще всего)
1. Дважды щёлкнуть `email.eml` — откроется Outlook/Thunderbird/Почта Windows.
2. Проверить, что From = `flumos@gmail.com`, To = `tpolegat@gmail.com`, тема и вложение на месте.
3. Нажать «Отправить».

### B. Через Gmail в браузере
1. Войти в `flumos@gmail.com`.
2. Создать письмо, скопировать текст из `email.md`, прикрепить
   `build/marketing/AMFE_oil_gas_one_pager.pdf`.
3. Отправить.

### C. Через SMTP с app-password (рекомендуемый способ, если отправляю я)
1. Включить 2FA и создать App Password для `flumos@gmail.com`:
   https://myaccount.google.com/apppasswords — это 16-символьный пароль, **не** пароль аккаунта.
2. Скопировать `.smtp.env.example` в `.smtp.env` (файл в `.gitignore`) и вписать app password:
```powershell
Copy-Item outreach/legat/.smtp.env.example outreach/legat/.smtp.env
notepad outreach/legat/.smtp.env
```
3. Сказать мне «доступ открыт» — я выполню:
```powershell
python tools/send_email_smtp.py --eml outreach/legat/email.eml `
  --env-file outreach/legat/.smtp.env --confirm
```
4. После отправки удалить `.smtp.env` и отозвать app password в Google.

Без `--confirm` скрипт делает только dry-run. Пароль не логируется и не печатается.

## После отправки

- Перевести контакт в CRM в `CONTACTED`, поставить дату и next action.
- Если ответа нет — follow-up через 7 дней (`marketing/outreach.md`).

## Честность

В письме нет выдуманных фактов о компании, персональных данных или обещаний точности; указано
`RESEARCH_ONLY`, `admission=false`. Вложение — one-pager с контактом `flumos@gmail.com`.
