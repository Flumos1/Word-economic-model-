# Шаблон записи памяти

- id: mem-<role>-NNNN
- ts: YYYY-MM-DDTHH:MM:SSZ
- role: <role>
- task: task-NN | "" 
- kind: role | task | decision | lesson | artifact | open_question | status
- title: одна строка
- summary: 3–6 строк: что сделано/какие числа/что важно знать следующему
- artifacts: пути + SHA-256 (если файл)
- links: docs/протоколы/задачи
- tags: 2–5 тегов
- status: done | open

Правила: append-only; без секретов и сырых данных; если ошибка — новая запись, старую не удалять.
