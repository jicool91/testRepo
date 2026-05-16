#!/usr/bin/env python3
"""Poizon API Client — реверс-инжиниринг мобильного API"""

import hashlib
import time
import json
from typing import Optional

import requests

BASE_URL = "https://app.poizon.com/api/v1/h5"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 12; 2201123C Build/SP1A.210812.003) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/96.0.4664.104 Mobile Safari/537.36",
    "appConfig": "pcm",
    "accept-encoding": "gzip",
    "content-type": "application/json; charset=utf-8",
    "x-country": "",
    "accessToken": "",
}


def make_ts() -> str:
    return str(int(time.time() * 1000))


def make_nonce() -> str:
    return str(int(time.time() * 1000)) + str(hash(time.time()))[-6:]


def make_sign(ts: str, secret_key: str) -> str:
    return hashlib.md5(f"{ts}|{secret_key}".encode()).hexdigest()


def build_headers(secret_key: str) -> dict:
    ts = make_ts()
    h = dict(HEADERS)
    h["timestamp"] = ts
    h["nonce"] = make_nonce()
    h["sign"] = make_sign(ts, secret_key)
    return h


def search_spu(keyword: str, page: int = 1, limit: int = 20, timeout: int = 15, secret_key: str = "") -> Optional[dict]:
    url = f"{BASE_URL}/search/fire/search/list"
    params = {
        "title": keyword,
        "page": page,
        "limit": limit,
        "sortMode": "",
        "sortType": "",
        "showHot": -1,
    }
    try:
        resp = requests.get(url, params=params, headers=build_headers(secret_key), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if data.get("code") == 200 else None
    except Exception as e:
        print(f"[ERROR] search: {e}")
        return None


def get_spu_detail(spu_id: str, timeout: int = 15, secret_key: str = "") -> Optional[dict]:
    url = f"{BASE_URL}/index/fire/flow/product/detail"
    body = {
        "spuId": spu_id,
        "productSourceName": "",
        "propertyValueId": "0",
    }
    try:
        resp = requests.post(url, json=body, headers=build_headers(secret_key), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data if data.get("code") == 200 else None
    except Exception as e:
        print(f"[ERROR] detail: {e}")
        return None


def parse_spu_list(data: dict) -> list[dict]:
    products = []
    if not data or "result" not in data:
        return products
    result = data["result"]
    items = result.get("items", []) if isinstance(result, dict) else []
    for item in items:
        try:
            products.append({
                "spuId": item.get("spuId", ""),
                "title": item.get("title", ""),
                "brand": item.get("brandName", ""),
                "category": item.get("categoryName", ""),
                "price": item.get("price", item.get("salePrice", 0)),
                "salePrice": item.get("salePrice", 0),
                "titleImg": (item.get("imageList") or [""])[0],
                "images": item.get("imageList", []),
                "url": f"https://m.poizon.com/product/{item.get('spuId', '')}",
            })
        except:
            continue
    return products


def format_product_card(product: dict) -> str:
    return (
        f"👟 <b>{product['title']}</b>\n"
        f"🏢 {product['brand']}\n"
        f"📂 {product['category']}\n"
        f"💰 <b>¥{product['salePrice']}</b>"
    )
