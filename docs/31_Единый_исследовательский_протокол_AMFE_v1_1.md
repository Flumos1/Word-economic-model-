# AMFE — единый исследовательский протокол, проект v1.1
Дата: 2026-11-11. Статус: нормативная спецификация-кандидат, НЕ исполняемый программный модуль.

## Назначение и границы
Объединяет docs/09, 12, 15, 17–21, 24–26, 29. Не заменяет историю версий. Разделяет научную оценку, право использования и утверждение новизны. Исследовательская методология — завершение предыдущего этапа; следующий главный этап — экономико-математическая формализация.

## Схема исследовательской записи
ResearchCase = {case_id, question, context, objectives, claim_ids, hypothesis_set, null_and_rivals, source_ids, evidence_ids, assumptions, data_vintages, test_plan, preregistration_hash, metrics, baseline_ids, independence_groups, conflicts, decision_log, next_review_trigger, version}.
Claim = {claim_id, proposition, epistemic_type, scope, units, assumptions, evidence_links, derivation_links, scientific_status, warrant_status, use_permissions, novelty_status, provenance, revision_history}.
Evidence = {evidence_id, source_id, acquisition_time, observation_time, data_vintage, independence_group, quality_checks, leakage_flags, limitations}.
Все записи версионированы, изменение статуса не удаляет прежнее состояние.

## Устранение конфликта словарей статусов
Ранее docs/12 и 29 использовали разные статусные системы. В v1.1 вводятся **независимые оси**, не сводимые в одну лестницу:
1. workflow_status: REGISTERED, DESIGNING, TESTING, REVIEW, CLOSED, REOPENED.
2. scientific_status: UNASSESSED, SUPPORTED_WITH_SCOPE, CHALLENGED, REFUTED_WITH_SCOPE, INCONCLUSIVE. «Гипотеза» — тип утверждения, а не степень истинности.
3. warrant_status: NOT_ASSESSED, PROVISIONAL, CONTEXTUALLY_WARRANTED, UNDER_REVIEW.
4. use_permissions: отдельное решение Admit(H,E,c,a)∈{YES,NO,DEFER} для каждого применения a (exploration, forecast, causal_claim, public_discovery_claim). Нет глобального разрешения на все применения.
5. novelty_status: NOT_ASSESSED, PRIOR_ART_FOUND, POTENTIALLY_NOVEL, NOVEL_WITH_SCOPE, INCONCLUSIVE.
Отображение прежних меток: docs/12 REGISTERED→workflow REGISTERED; UNDER_REVIEW→workflow REVIEW или warrant UNDER_REVIEW (уточнить основание); SUPPORTED_WITH_SCOPE→scientific SUPPORTED_WITH_SCOPE; CHALLENGED→scientific CHALLENGED; REFUTED_WITH_SCOPE→scientific REFUTED_WITH_SCOPE; INCONCLUSIVE→scientific INCONCLUSIVE; DEFERRED→workflow CLOSED с причиной deferred (не эквивалент опровержению). docs/29 RESEARCH_HYPOTHESIS→тип claim + warrant NOT_ASSESSED; PROVISIONALLY_SUPPORTED→warrant PROVISIONAL; CONTEXTUALLY_WARRANTED→warrant CONTEXTUALLY_WARRANTED; UNDER_REVIEW→warrant UNDER_REVIEW. Отображение является проектным уточнением, не тождеством всех старых смыслов.

## Ворота и условия переходов
G0 QUESTION→REGISTERED: однозначный вопрос, контекст, измеряемый объект, границы; иначе DEFER.
G1 REGISTERED→DESIGNING: альтернативы включая нулевую, известные источники, предпосылки, различающие следствия; если тест невозможен — зафиксировать NONIDENTIFIABLE/INCONCLUSIVE, не подменять проверку нарративом.
G2 DESIGNING→TESTING: заранее определены метрики, baselines, данные/винтажи, независимые тестовые разделы, план множественных проверок, критерии остановки и нарушения; при критических пробелах NO/DEFER.
G3 TESTING→REVIEW: воспроизводимые результаты и журналы, контроль утечки и независимости, аудит формальных ограничений; критическая утечка→SUSPEND_USE и повторный тест.
G4 REVIEW→DECISION: альтернативы рассмотрены, область применимости и остаточная неопределённость указаны; разделены прогнозный, причинный и научно-новаторский допуски; спорные результаты не объявляются доказанными.
G5 DECISION→MONITORING: сохраняются зависимости, дата/триггеры ревизии, rollback и версии; при новых противоречиях REOPENED с обходом зависимых выводов.
Каждые ворота сохраняют: inputs, rule_version, reviewer_id, evidence_ids, outcome, rationale, timestamp. Переходы с отсутствующим обязательным полем запрещены либо явно откладываются; неизвестное не приравнивается к ложному.

## Независимость
Разные агенты не равны независимым источникам. Доказательства с общей линией происхождения маркируются independence_group; повторное использование того же набора данных не считается независимой репликацией. Независимый арбитр не вправе менять заранее объявленные критерии после просмотра контрольного результата.

## Конфликты и пересмотр
При логическом конфликте проверить контекст, единицы, масштабы и предпосылки; локализовать минимальные несовместимые подмножества там, где формализация позволяет. При ошибке исходной посылки все зависящие утверждения получают NEEDS_REVALIDATION в отдельном поле dependency_check; ранее разрешённое использование временно DEFER/NO в зависимости от риска. Историю не стирать.

## Инварианты протокола
I1: NO public_discovery_claim без независимой проверки prior art и результатов.
I2: YES forecast не подразумевает YES causal_claim.
I3: источник не становится независимым от числа пересказавших его агентов.
I4: критическая утечка данных запрещает подтверждающее использование испорченного теста.
I5: при смене области применимости допуск пересматривается.
I6: история утверждений и причин пересмотра неизменяема в журнале.
I7: логическая непротиворечивость и бухгалтерская согласованность не доказывают причинности.

## Проверяемые выходы
CASE_RECORD, CLAIM_RECORD, EVIDENCE_RECORD, GATE_DECISION, CONFLICT_REPORT, DEPENDENCY_REVALIDATION, AUDIT_TRAIL, NEXT_EXPERIMENT.
Неопределённость и воздержание от вывода — допустимые результаты.

## Ограничения
Это спецификация, не формально верифицированный автомат и не доказательство полноты методологии. Остаются задачи: типизированные схемы данных, формальная логика переходов, распределение прав доступа, политика разрешения конфликтов, машинная проверка инвариантов. Их можно уточнять при математическом проектировании.
