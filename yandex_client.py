# -*- coding: utf-8 -*-
"""Yandex Cloud Search API v2 — sync + async (deferred) search"""

import base64
import json
import time
import requests
from typing import List, Dict
from xml.etree import ElementTree as ET

YANDEX_SYNC_URL  = "https://searchapi.api.cloud.yandex.net/v2/web/search"
YANDEX_ASYNC_URL = "https://searchapi.api.cloud.yandex.net/v2/web/searchAsync"
YANDEX_OPS_URL   = "https://operation.api.cloud.yandex.net/operations/{}"


def _parse_xml_response(raw_b64: str) -> List[Dict]:
    """Parse base64-encoded XML from Yandex Search API response."""
    xml_str = base64.b64decode(raw_b64).decode("utf-8")
    root = ET.fromstring(xml_str)
    results = []

    for doc in root.findall(".//doc"):
        url_val = doc.findtext("url") or ""
        domain  = doc.findtext("domain") or ""

        title_el = doc.find("title")
        title = "".join(title_el.itertext()).strip() if title_el is not None else ""

        passages = ["".join(p.itertext()) for p in doc.findall(".//passage")]
        snippet = " ".join(passages[:2])

        # Price from offer_info (Yandex embeds this for product pages)
        offer_raw = doc.findtext("properties/offer_info") or ""
        price = 0
        quantity_available = ""
        if offer_raw:
            try:
                oi = json.loads(offer_raw)
                price_info = oi.get("price", {})
                price = int(price_info.get("value", 0))
                # Try to get availability info
                avail = oi.get("availability") or oi.get("in_stock") or ""
                quantity_available = str(avail) if avail else ""
            except Exception:
                pass

        results.append({
            "url": url_val,
            "domain": domain,
            "title": title,
            "snippet": snippet,
            "price": price,
            "quantity_available": quantity_available,
        })

    return results


def search_sync(api_key: str, query: str, limit: int = 10) -> List[Dict]:
    """Synchronous Yandex search. Use for small batches or testing."""
    try:
        resp = requests.post(
            YANDEX_SYNC_URL,
            headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
            json={
                "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
                "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": limit, "docsInGroup": 1},
                "maxPassages": 2,
                "region": "225",
                "l10N": "LOCALIZATION_RU",
                "responseFormat": "FORMAT_XML",
            },
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"[Yandex sync] HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        raw_b64 = resp.json().get("rawData", "")
        return _parse_xml_response(raw_b64)
    except Exception as e:
        print(f"[Yandex sync] Error: {e}")
        return []


def submit_async_search(api_key: str, query: str, limit: int = 10) -> str:
    """Submit async (deferred) search. Returns operation_id."""
    resp = requests.post(
        YANDEX_ASYNC_URL,
        headers={"Authorization": f"Api-Key {api_key}", "Content-Type": "application/json"},
        json={
            "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
            "groupSpec": {"groupMode": "GROUP_MODE_FLAT", "groupsOnPage": limit, "docsInGroup": 1},
            "maxPassages": 2,
            "region": "225",
            "l10N": "LOCALIZATION_RU",
            "responseFormat": "FORMAT_XML",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def poll_operation(api_key: str, operation_id: str, timeout: int = 60) -> List[Dict]:
    """Poll until operation is done, return parsed results."""
    deadline = time.time() + timeout
    delay = 1.0
    while time.time() < deadline:
        try:
            resp = requests.get(
                YANDEX_OPS_URL.format(operation_id),
                headers={"Authorization": f"Api-Key {api_key}"},
                timeout=15,
            )
            if resp.status_code != 200:
                time.sleep(delay)
                continue
            data = resp.json()
            if data.get("done"):
                response = data.get("response", {})
                raw_b64 = response.get("rawData", "")
                if raw_b64:
                    return _parse_xml_response(raw_b64)
                return []
        except Exception as e:
            print(f"[Yandex poll] Error: {e}")
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)
    print(f"[Yandex poll] Timeout for operation {operation_id}")
    return []


def search_item(api_key: str, queries: List[str], results_per_item: int = 5) -> List[Dict]:
    """
    Submit all queries as deferred async operations (/v2/web/searchAsync),
    poll until done, merge & deduplicate. Sorted: best price first, unpriced last.
    """
    op_ids = []
    for q in queries:
        try:
            op_id = submit_async_search(api_key, q, limit=max(results_per_item, 5))
            op_ids.append((q, op_id))
        except Exception as e:
            print(f"[Yandex async submit] Error for '{q}': {e}")

    if not op_ids:
        return []

    # Brief initial wait for Yandex to process
    time.sleep(2)

    all_results = []
    seen_urls: set = set()
    for q, op_id in op_ids:
        try:
            results = poll_operation(api_key, op_id, timeout=60)
            for r in results:
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    all_results.append(r)
        except Exception as e:
            print(f"[Yandex async poll] Error for op {op_id}: {e}")

    return _merge_results(all_results, results_per_item)


def _merge_results(results: List[Dict], limit: int) -> List[Dict]:
    """Sort: priced results first (price ascending), then unpriced. Return up to limit."""
    with_price    = sorted([r for r in results if r.get("price", 0) > 0], key=lambda x: x["price"])
    without_price = [r for r in results if not r.get("price", 0)]
    merged = with_price + without_price
    return merged[:limit] if limit else merged
