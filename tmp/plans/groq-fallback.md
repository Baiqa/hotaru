# Groq как третий уровень fallback для group_claims/synthesize

## Контекст

Gemini free-tier (500 generate_content/день, общий на MODEL+FALLBACK_MODEL) —
постоянное узкое место: `group_claims`/`synthesize` упираются в него каждый
день на много часов. Пользователь решил не переходить на платный Gemini
(остаёмся бесплатными, см. память `project_hotaru_decisions_2026_11`), а
вместо этого добавить **ещё один бесплатный провайдер** как третий уровень
fallback — когда и основная, и фолбэк-модель Gemini отвечают 429, пробовать
Groq перед тем как сдаваться.

Поиск актуальных (ноябрь 2026) бесплатных тарифов показал: Groq — самый
конкретный и живой вариант (~1000 запросов/день без карты, OpenAI-совместимый
REST API с JSON-режимом вывода); OpenRouter (явно отклонён 2026-09-27) и
Cerebras/Mistral — хуже или неопределённее. Пользователь подтвердил: Groq,
только в `group_claims`/`synthesize` (где реально упирается сейчас), не в
`extract_claims`/`cluster_events`.

**Важная оговорка, которую нужно сказать пользователю явно:** я не могу
протестировать реальный Groq-вызов сам — нужен `GROQ_API_KEY`, который
пользователь должен получить сам (регистрация на console.groq.com — это
создание аккаунта, не то, что я могу сделать за него). Код пишется так, что
без ключа он просто не использует Groq (откатывается к текущему поведению) —
это не блокирует написание и структурную проверку кода, но живой e2e-тест
фолбэка возможен только после того, как пользователь впишет ключ в
`worker/.env`.

## Архитектура — третий tier в уже существующей цепочке fallback

И `claim_grouping._classify_relation`, и `synthesis._call_model` уже делают
`try MODEL except -> try FALLBACK_MODEL` (этот же паттерн, только на уровне
моделей одного провайдера). Groq становится третьим `except` в той же цепочке,
не новой архитектурой.

**Новый `worker/src/hotaru_worker/groq_client.py`** — без своего retry (это
последний тир, решение "сдаваться или нет" принимает вызывающий код, как и
везде в проекте):
```python
import httpx

# Нужна живая проверка по console.groq.com на момент настройки — бесплатные
# модели/лимиты Groq меняются; на ноябрь 2026 gpt-oss-120b — в бесплатном тарифе.
MODEL = "openai/gpt-oss-120b"
BASE_URL = "https://api.groq.com/openai/v1/chat/completions"


def call_json(api_key: str, prompt: str, timeout: float = 30.0) -> str:
    resp = httpx.post(
        BASE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    resp.raise_for_status()  # 429/5xx -> httpx.HTTPStatusError
    return resp.json()["choices"][0]["message"]["content"]
```

**`config.py`**: добавить `groq_api_key: str | None = os.environ.get("GROQ_API_KEY")`
— **без** `RuntimeError`, если не задан (в отличие от `DATABASE_URL`/
`CONTACT_EMAIL`): это опциональный фолбэк, код должен работать и без ключа,
просто не используя Groq (откат к текущему поведению до сегодняшнего фикса).

### `claim_grouping.py` — `_classify_relation`

Третий `except` после `FALLBACK_MODEL`. Промпт `claim_relation_classify.txt`
просит "одним словом" (под Gemini enum-режим) — для Groq, у которого нет
схемной привязки, нужна **отдельная JSON-инструкция**, добавляемая в коде при
вызове Groq (не трогаем сам файл промпта, он работает для Gemini):
```python
def _classify_relation_groq(candidate, new_claim) -> Relation:
    prompt = _PROMPT_TEMPLATE.format(...) + (
        '\n\nОтветь в формате JSON: {"relation": "confirms"} '
        '(или "contradicts", или "different").'
    )
    content = groq_client.call_json(os.environ["GROQ_API_KEY"], prompt)
    return Relation(json.loads(content)["relation"])
```
`_classify_relation`: после второго `except` (FALLBACK_MODEL) — если
`os.environ.get("GROQ_API_KEY")` задан, пробовать `_classify_relation_groq`;
иначе `raise` как сейчас.

### `synthesis.py` — `_call_model`

Третий `except`, аналогично. Промпты синтеза (`synthesize_summary_30s.txt`/
`synthesize_full_breakdown.txt`) **уже** просят JSON (нужно для
`response_format: json_object` у Groq без 400 — OpenAI-совместимые API
требуют слово "json" в промпте, оно там уже есть), но описывают форму как
список предложений, а наш `_SynthesisResponse` — объект `{"sentences": [...]}`
(это работает у Gemini только потому что `response_schema` жёстко навязывает
форму независимо от буквального текста промпта). Для Groq парсинг должен быть
терпимым к обеим формам:
```python
def _parse_synthesis_json(raw: str) -> _SynthesisResponse:
    data = json.loads(raw)
    if isinstance(data, list):
        data = {"sentences": data}
    return _SynthesisResponse.model_validate(data)
```
Использовать этот парсер и для Gemini-ветки (просто `model_validate_json` ->
`model_validate` после `json.loads`, без функционального отличия), и для
Groq-ветки — не дублировать логику парсинга.

### `group_claims.py` / `synthesize.py` — исключения и смоук-тест

Сейчас `except (genai_errors.APIError, httpx.TransportError)` — расширить до
`(genai_errors.APIError, httpx.TransportError, httpx.HTTPStatusError)`, т.к.
финальная (после Groq) ошибка может прилететь как `HTTPStatusError`.
`is_quota_wall`-проверка — учитывать оба варианта:
```python
is_quota_wall = (
    (isinstance(exc, genai_errors.APIError) and exc.code == 429)
    or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429)
)
```
Это корректно само по себе: "оборвать весь прогон" теперь означает "умерли
*все* провайдеры в цепочке", а не только Gemini — ровно то поведение, которое
нужно.

**Смоук-тест (`quota.has_quota`) перед батч-циклом** — сейчас при провале
сразу завершает прогон. С Groq это неверно: Gemini может быть мёртв, а Groq
жив. Фикс: если `GROQ_API_KEY` задан, **не делать** ранний выход по одному
Gemini-смоук-тесту — входить в цикл всегда, пусть пер-claim/пер-событие
fallback-цепочка решает. Без ключа — поведение не меняется (текущий фикс от
2026-11-05/06 остаётся как есть).

## Файлы

Новый: `worker/src/hotaru_worker/groq_client.py`.
Изменённые: `worker/src/hotaru_worker/config.py` (+`groq_api_key`),
`worker/src/hotaru_worker/claim_grouping.py` (+`_classify_relation_groq`),
`worker/src/hotaru_worker/synthesis.py` (+Groq-ветка в `_call_model`,
общий `_parse_synthesis_json`), `worker/src/hotaru_worker/group_claims.py`
и `synthesize.py` (исключения + смоук-тест-гейт).

## Проверка

- Без `GROQ_API_KEY` в `worker/.env` — прогнать `group_claims`/`synthesize`,
  убедиться, что поведение идентично текущему (моментальный выход при мёртвой
  квоте, как сейчас) — фолбэк не должен ничего ломать для тех, кто его не
  настроил.
- **Пользователь должен сам получить ключ** на console.groq.com и вписать
  `GROQ_API_KEY=...` в `worker/.env` — без этого шага живой e2e-тест
  невозможен.
- С ключом — прогнать `group_claims`/`synthesize` при текущей (подтверждённо
  мёртвой) квоте Gemini: ожидаем, что прогон реально обрабатывает claims/
  события через Groq, а не выходит мгновенно со `skipped_quota_exhausted`.
  Проверить `claim_group_members`/`renders` реально растут.
- Проверить, что `relation`/`_SynthesisResponse`, полученные от Groq,
  проходят ту же пост-проверку, что и у Gemini (claim_id-цитирование, числа,
  шинглы в synthesis.py — код там общий, не трогается).
