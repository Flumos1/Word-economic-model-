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

### C. Через SMTP с app-password (без публикации пароля)
1. Создать app-password Google для `flumos@gmail.com` (2FA → App passwords).
2. В PowerShell (пароль не сохраняется в истории, если задать через Read-Host):
```powershell
$env:SMTP_USER="flumos@gmail.com"
$env:SMTP_PASSWORD=(Read-Host "Google app password" -AsSecureString | ConvertFrom-SecureString -AsPlainText)
python tools/send_email_smtp.py --eml outreach/legat/email.eml --confirm
Remove-Item Env:SMTP_PASSWORD
```
Скрипт без `--confirm` делает только dry-run. Пароль не логируется.

## После отправки

- В `marketing/crm_pipeline.csv.tmpl` (или рабочей копии `.csv`) перевести `LEGAT`/аккаунт в
  `CONTACTED`, поставить дату и next action.
- Если ответа нет — follow-up через 7 дней (см. `marketing/outreach.md`).

## Честность

В письме нет выдуманных фактов о компании, персональных данных или обещаний точности; указано
`RESEARCH_ONLY`, `admission=false`. Вложение — one-pager с контактом `flumos@gmail.com`.
