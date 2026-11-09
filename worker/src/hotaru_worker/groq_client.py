import httpx

# Третий уровень fallback после Gemini MODEL/FALLBACK_MODEL (2026-11-08) —
# Gemini free-tier (500 generate_content/день) постоянно упирается в потолок,
# пользователь решил остаться на бесплатных тарифах и добавить второй
# бесплатный провайдер вместо перехода на платный Gemini. Нужна живая проверка
# по console.groq.com на момент настройки — бесплатные модели/лимиты Groq
# меняются; на ноябрь 2026 gpt-oss-120b — в бесплатном тарифе без карты.
MODEL = "openai/gpt-oss-120b"
BASE_URL = "https://api.groq.com/openai/v1/chat/completions"


def call_json(api_key: str, prompt: str, timeout: float = 30.0) -> str:
    """Последний тир в цепочке fallback — без собственного retry, решение
    "сдаваться или нет" принимает вызывающий код (как и везде в проекте).
    Поднимает httpx.HTTPStatusError на 4xx/5xx (включая 429 — .response.
    status_code) или httpx.TransportError на сетевой сбой."""
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
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
