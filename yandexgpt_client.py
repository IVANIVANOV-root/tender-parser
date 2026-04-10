# -*- coding: utf-8 -*-
"""YandexGPT Foundation Models API client — synchronous completion"""

import json
import re
import requests
from typing import Optional

YANDEX_LLM_API = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


def chat(api_key: str, folder_id: str, messages: list,
         temperature: float = 0.3, max_tokens: int = 1024) -> str:
    """Send messages to YandexGPT Lite and return the response text."""
    yandex_messages = [
        {"role": m["role"], "text": m["content"]}
        for m in messages
    ]
    resp = requests.post(
        YANDEX_LLM_API,
        headers={
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "modelUri": f"gpt://{folder_id}/yandexgpt-lite",
            "completionOptions": {
                "stream": False,
                "temperature": temperature,
                "maxTokens": str(max_tokens),
            },
            "messages": yandex_messages,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["result"]["alternatives"][0]["message"]["text"].strip()


def normalize_table_to_items(api_key: str, folder_id: str, raw_content: str) -> list:
    """
    Send raw table content to YandexGPT.
    Returns structured list of tender items with descriptions.
    """
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
- description: ВСЕ характеристики из заявки — ГОСТ, тип, размер, артикул, материал, мощность и т.д.

Верни ТОЛЬКО JSON-массив, без пояснений, без markdown-блоков."""

    try:
        result = chat(api_key, folder_id, [{"role": "user", "content": prompt}],
                      temperature=0.1, max_tokens=4096)
        result = result.strip()
        if result.startswith("```"):
            lines = result.split("\n")
            result = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        items = json.loads(result)
        if isinstance(items, list):
            return items
    except Exception as e:
        print(f"[YandexGPT normalize] Error: {e}")
    return []


def generate_search_queries(api_key: str, folder_id: str, name: str, description: str) -> list:
    """Generate 3 search queries for a tender item, using characteristics and GOST."""
    prompt = (
        f"Товар для закупки по тендеру:\n"
        f"Наименование: {name}\n"
        f"Характеристики/требования: {description}\n\n"
        f"Сгенерируй 3 поисковых запроса для поиска этого товара в российских интернет-магазинах.\n"
        f"Правила:\n"
        f"- Первый запрос: общее название товара (3-5 слов)\n"
        f"- Второй запрос: с ключевыми техническими характеристиками или размерами\n"
        f"- Третий запрос: с ГОСТ, артикулом или специфическим типом (если указан)\n"
        f"- Если ГОСТ не указан — используй другой вариант названия\n"
        f"- Запросы должны быть краткими (3-7 слов), без лишних слов\n"
        f'Верни только JSON-массив строк без пояснений: ["запрос 1", "запрос 2", "запрос 3"]'
    )
    try:
        result = chat(api_key, folder_id, [{"role": "user", "content": prompt}],
                      temperature=0.2, max_tokens=256)
        match = re.search(r"\[.*?\]", result, re.DOTALL)
        if match:
            queries = json.loads(match.group())
            if isinstance(queries, list) and queries:
                return [str(q) for q in queries[:3]]
    except Exception as e:
        print(f"[YandexGPT queries] Error: {e}")
    return [f"{name} купить"]


def extract_price_from_snippets(api_key: str, folder_id: str, item_name: str,
                                 max_price: float, results: list) -> list:
    """Ask YandexGPT to extract prices from search snippets."""
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
        f'[{{"supplier": "домен", "price": число, "url": "ссылка", "title": "название", "quantity_available": "в наличии или пусто"}}]\n'
        f"price=0 если цена не указана явно. Только реальные магазины. Только JSON."
    )
    try:
        result = chat(api_key, folder_id, [{"role": "user", "content": prompt}],
                      temperature=0.1, max_tokens=800)
        match = re.search(r"\[.*?\]", result, re.DOTALL)
        if match:
            offers = json.loads(match.group())
            if isinstance(offers, list):
                return offers
    except Exception as e:
        print(f"[YandexGPT extract] Error: {e}")
    return []
