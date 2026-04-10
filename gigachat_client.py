# -*- coding: utf-8 -*-
"""GigaChat API client — token management + chat completions"""

import time
import uuid
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GIGACHAT_OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API   = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
GIGACHAT_MODEL = "GigaChat"

# Per-token cache: {auth_key: (access_token, expires_at)}
_token_cache: dict = {}


def _get_access_token(auth_key: str) -> str:
    cached = _token_cache.get(auth_key)
    if cached and time.time() < cached[1] - 60:
        return cached[0]
    resp = requests.post(
        GIGACHAT_OAUTH,
        headers={
            "Authorization": f"Basic {auth_key}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"scope": "GIGACHAT_API_PERS"},
        verify=False,
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    access = data["access_token"]
    expires = data.get("expires_at", (time.time() + 1800) * 1000) / 1000
    _token_cache[auth_key] = (access, expires)
    return access


def chat(auth_key: str, messages: list, temperature: float = 0.3, max_tokens: int = 1024) -> str:
    token = _get_access_token(auth_key)
    resp = requests.post(
        GIGACHAT_API,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "model": GIGACHAT_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        verify=False,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def normalize_table_to_items(auth_key: str, raw_content: str) -> list:
    """
    Send raw table content (any format) to GigaChat.
    Returns structured list of tender items.
    """
    # Truncate to avoid token limit
    content = raw_content[:6000] if len(raw_content) > 6000 else raw_content

    prompt = f"""Перед тобой тендерная заявка (спецификация товаров) в виде таблицы или текста:

{content}

Извлеки ВСЕ позиции закупки и верни JSON-массив. Каждая позиция:
{{"num": 1, "name": "точное наименование товара", "qty": 10.0, "unit": "шт.", "max_price": 1500.0, "description": "технические характеристики"}}

Правила:
- num: порядковый номер позиции (целое число, 0 если нет)
- name: наименование товара БЕЗ лишних символов
- qty: количество (число, 0 если не указано)
- unit: единица измерения (шт., кг, м, л, компл. и т.д.)
- max_price: максимальная цена за единицу (число, 0 если не указана)
- description: характеристики, ГОСТ, артикул — всё что поможет найти товар

Верни ТОЛЬКО JSON-массив, без пояснений, без markdown-блоков."""

    try:
        result = chat(auth_key, [{"role": "user", "content": prompt}], temperature=0.1, max_tokens=4096)
        # Strip possible markdown
        result = result.strip()
        if result.startswith("```"):
            lines = result.split("\n")
            result = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        import json
        items = json.loads(result)
        if isinstance(items, list):
            return items
    except Exception as e:
        print(f"[GigaChat normalize] Error: {e}")
    return []


def generate_search_queries(auth_key: str, name: str, description: str) -> list:
    """Generate 3 search queries for a tender item."""
    prompt = (
        f"Товар для закупки:\n"
        f"Наименование: {name}\n"
        f"Характеристики: {description}\n\n"
        f"Сгенерируй 3 поисковых запроса для поиска этого товара в российских интернет-магазинах.\n"
        f"Запросы должны быть краткими (3-7 слов), содержать ключевые параметры товара.\n"
        f'Верни только JSON-массив строк без пояснений: ["запрос 1", "запрос 2", "запрос 3"]'
    )
    try:
        import json, re
        result = chat(auth_key, [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=256)
        match = re.search(r"\[.*?\]", result, re.DOTALL)
        if match:
            queries = json.loads(match.group())
            if isinstance(queries, list) and queries:
                return [str(q) for q in queries[:3]]
    except Exception as e:
        print(f"[GigaChat queries] Error: {e}")
    return [f"{name} купить"]


def extract_price_from_snippets(auth_key: str, item_name: str, max_price: float, results: list) -> list:
    """Ask GigaChat to extract prices from search snippets when offer_info has no price."""
    if not results:
        return []
    text_parts = []
    for r in results[:6]:
        text_parts.append(
            f"URL: {r['url']}\nЗаголовок: {r['title']}\nСниппет: {r['snippet'][:300]}\n"
        )
    results_text = "\n".join(text_parts)
    prompt = (
        f"Товар: {item_name}\n"
        f"Максимальная цена по тендеру: {max_price:.0f} руб.\n\n"
        f"Результаты поиска:\n{results_text}\n"
        f"Извлеки предложения о продаже этого товара с ценами.\n"
        f"Верни JSON-массив (до 5 штук), отсортированный по цене (по возрастанию):\n"
        f'[{{"supplier": "домен", "price": число, "url": "ссылка", "title": "название", "quantity_available": "в наличии/количество или пусто"}}]\n'
        f"price=0 если цена не указана явно. Только реальные магазины. Только JSON."
    )
    try:
        import json, re
        result = chat(auth_key, [{"role": "user", "content": prompt}], temperature=0.1, max_tokens=800)
        match = re.search(r"\[.*?\]", result, re.DOTALL)
        if match:
            offers = json.loads(match.group())
            if isinstance(offers, list):
                return offers
    except Exception as e:
        print(f"[GigaChat extract] Error: {e}")
    return []
