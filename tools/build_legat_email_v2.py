#!/usr/bin/env python3
"""Build the revised USD email for TPO LEGAT (does not send anything)."""
from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outreach" / "legat"
ATTACHMENTS = [
    ROOT / "build" / "marketing" / "AMFE_oil_gas_one_pager.pdf",
    OUT / "LEGAT_offer_USD.pdf",
]
SUBJECT = "ТПО «ЛЕГАТ»: программа исследований и стоимость в USD"
BODY = """Уважаемый Виктор Иванович,

В дополнение к письму от 20 ноября — уточняю предложение: цены в USD и более подробная программа исследований.

AMFE — сценарная аналитика для нефтегаза: показываем, как шоки цены нефти и газа, логистики и санкций влияют на выручку, бюджет, CAPEX и закупки. Это не «чёрный ящик» и не прогноз: расчёт с явными допущениями, диапазонами и проверяемыми хешами (RESEARCH_ONLY, admission=false).

Что можем исследовать:
1. Цена нефти и Ормуз → выручка, бюджет, CAPEX, триггеры.
2. Закупки → TCO поставщиков, бюджет, CAP/FLOOR, риск страны.
3. Газ/СПГ → TTF/JKM, шок трубы, зима, переток, чувствительность проекта.
4. Геополитика/логистика → санкции, фрахт, маршруты, асимметрия.
5. Страны/отрасли → где шок бьёт сильнее.
6. API/интеграция → встроить сценарии в ваш контур.
7. Валидация/бэктест → насколько числа устойчивы на истории.
8. Воркшоп → научить команду читать сценарии.

Стоимость (USD, ориентир):
— диагностика — 0;
— пилот по одному направлению — 7 500–10 000 (4–6 недель);
— газ/СПГ — 6 000–9 000;
— закупки/TCO — 7 500–10 000;
— страны/отрасли — 8 000–12 000;
— API-интеграция — 15 000–25 000 + подписка;
— валидация/бэктест — 10 000–15 000;
— полный сценарий — 20 000–30 000;
— подписка/API — 25 000–80 000 в год.
Цены фиксируются в USD; налоги/курс — по договору. В пилот не входит покупка сторонних данных (Platts/ICE/Consensus).

Вопросы, которые вы можете задать:
— «Насколько точны прогнозы?» Это не прогноз и не вероятность; диапазон при допущениях, admission=false.
— «Чем отличаетесь от WoodMac/S&P?» Не заменяем данные; делаем кастомный сценарий под решение, с закупками, газом и прозрачными допущениями.
— «Откуда данные?» Публичные агрегаты; данные клиента — по NDA, обезличенные, не перепродаются.
— «Конфиденциальность?» NDA, обезличивание, on-prem, удаление по запросу.
— «Сроки?» Диагностика — 1 неделя, пилот — 4–6 недель, полный — 8–12 недель.
— «Что получим?» PDF/DOCX, Excel, JSON, SHA-256, воркшоп; для закупок — CFO-отчёт.
— «Есть референсы?» Публичных кейсов пока нет; покажем демо на открытых сценариях и методологию.
— «Что если не совпадёт?» Критерии успеха фиксируем до старта; отрицательный результат — тоже результат, гарантий экономии нет.
— «Это допущенная модель?» Нет: RESEARCH_ONLY, admission=false, не для регуляторных решений.

Если удобно, начнём с бесплатной диагностики 20–30 минут: выберите 1–2 вопроса, я покажу рамку и что получится на выходе. Предложите 2–3 слота на следующую неделю — подстроюсь.

Во вложении: программа с ценами и one-pager. NDA-шаблон и демо — по запросу.

С уважением,
Flumos
flumos@gmail.com

P.S. Это сценарная аналитика, не прогноз и не инвестиционная рекомендация.
"""


def build() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    msg = EmailMessage()
    msg["From"] = "Flumos <flumos@gmail.com>"
    msg["To"] = "tpolegat@gmail.com"
    msg["Subject"] = SUBJECT
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg.set_content(BODY)
    for path in ATTACHMENTS:
        if path.exists():
            msg.add_attachment(path.read_bytes(), maintype="application", subtype="pdf",
                               filename=path.name)
    eml = OUT / "email_v2.eml"
    eml.write_bytes(msg.as_bytes())
    (OUT / "email_v2.md").write_text(
        f"**Кому:** tpolegat@gmail.com  \n**От:** Flumos <flumos@gmail.com>  \n**Тема:** {SUBJECT}\n\n---\n\n{BODY}\n\n---\n\n"
        + "Вложения: " + ", ".join(p.name for p in ATTACHMENTS if p.exists()) + "\n",
        encoding="utf-8", newline="\n")
    return eml


if __name__ == "__main__":
    path = build()
    print({"eml": str(path), "bytes": path.stat().st_size,
           "attachments": [p.name for p in ATTACHMENTS if p.exists()]})
