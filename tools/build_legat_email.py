#!/usr/bin/env python3
"""Build the ready-to-send .eml for TPO LEGAT (does not send anything).

The .eml can be opened in Outlook/Thunderbird or imported into Gmail.  Sending requires a mailbox
and credentials/channel controlled by the human; this script only assembles MIME.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outreach" / "legat"
PDF = ROOT / "build" / "marketing" / "AMFE_oil_gas_one_pager.pdf"

SUBJECT = "ТПО «ЛЕГАТ»: сценарная аналитика по нефти и газу — 20 минут"
BODY = """Уважаемый Виктор Иванович,

Меня зовут Flumos. Пишу по профилю вашей компании: если тема не ваша, буду признателен за подсказку, кому её передать.

Мы делаем сценарную аналитику для нефтегазовых предприятий: показываем, как шоки цены нефти и газа, логистики и санкций влияют на выручку, бюджет, CAPEX и закупки. Это не «чёрный ящик» и не прогноз: расчёт с явными допущениями, диапазонами и проверяемыми хешами.

Пример: при закрытии Ормуза на 6 месяцев базовый сценарий даёт Brent около 169 USD/bbl (диапазон 139–269), дизель около 6,6 USD/gal, CPI +3,9 п.п.; в стресс-сценарии — дизель около 8,0, керосин около 6,9, CPI +5,6 п.п.

Для ТПО «ЛЕГАТ» могу за 20 минут показать:
— сценарии цены и бюджета под ваши горизонты;
— TCO поставщиков и бюджет закупок (FLAKS);
— газ/СПГ и геополитические риски, если это релевантно.

Если полезно, предлагаю короткий пилот на 4 недели: 1–2 решения, ваши данные под NDA (можно обезличить), результат — Excel/PDF/JSON и воркшоп. Стоимость — от 600 тыс. руб., критерии успеха фиксируем до старта.

Во вложении — одностраничник. Удобно созвониться на следующей неделе? Предложите 2–3 слота, подстроюсь.

С уважением,
Flumos
flumos@gmail.com

P.S. Продукт в статусе RESEARCH_ONLY, admission=false: это сценарная аналитика, не прогноз и не инвестиционная рекомендация.
"""


def build() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = "Flumos <flumos@gmail.com>"
    msg["To"] = "tpolegat@gmail.com"
    msg["Subject"] = SUBJECT
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg.set_content(BODY)
    if PDF.exists():
        msg.add_attachment(PDF.read_bytes(), maintype="application", subtype="pdf",
                           filename="AMFE_oil_gas_one_pager.pdf")
    path = OUT / "email.eml"
    path.write_bytes(msg.as_bytes())
    (OUT / "email.md").write_text(
        f"**Кому:** tpolegat@gmail.com  \n**От:** Flumos <flumos@gmail.com>  \n"
        f"**Тема:** {SUBJECT}\n\n---\n\n{BODY}\n\n---\n\n"
        f"Вложение: `{PDF.name}` (если файл существует на момент сборки).\n",
        encoding="utf-8", newline="\n")
    return path


if __name__ == "__main__":
    path = build()
    print({"eml": str(path), "bytes": path.stat().st_size, "attachment": PDF.exists()})
