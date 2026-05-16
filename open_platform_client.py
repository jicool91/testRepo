#!/usr/bin/env python3
"""
Poizon Open Platform API Client
Официальное API: https://open.poizon.com

Использование:
    export POIZON_OPEN_KEY="твой_ключ"
    python3 open_platform_client.py search "Nike Air Force 1"
"""

import os
import sys
import json
import hashlib
import time
import hmac
from typing import Optional
from urllib.parse import urlencode, quote

import requests


# Open Platform base URL (все эндпоинты идут отсюда)
OPEN_API_BASE = "https://open.poizon.com/api"


class PoizonOpenClient:
    """Клиент для Poizon Open Platform API"""

    def __init__(self, open_key: str = None):
        self.open_key = open_key or os.environ.get("POIZON_OPEN_KEY", "")
        if not self.open_key:
            raise ValueError("openKey не указан! Укажи openKey или выставь POIZON_OPEN_KEY")
        
        self.base_url = OPEN_API_BASE
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Origin": "https://open.poizon.com",
            "Referer": "https://open.poizon.com/",
        })

    def _sign(self, params: dict) -> str:
        """
        Алгоритм подписи для Open Platform.
        Обычно: сортировка params по ключам -> конкатенация key=value -> +secret -> md5/hmac
        
        Надо будет уточнить по документации после получения openKey.
        Для начала попробуем:
          - params + "&key=" + openKey -> MD5
        """
        # Сортируем параметры по ключам
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        # Собираем строку для подписи
        sign_str = "&".join(f"{k}={v}" for k, v in sorted_params)
        # Добавляем openKey
        sign_str += f"&key={self.open_key}"
        # MD5 хэш
        return hashlib.md5(sign_str.encode()).hexdigest()

    def _request(self, endpoint: str, params: dict = None, method: str = "GET") -> Optional[dict]:
        """Выполнить запрос к Open Platform API"""
        url = f"{self.base_url}{endpoint}"
        
        if params is None:
            params = {}
        
        # Добавляем openKey в параметры
        params["openKey"] = self.open_key
        
        # Добавляем timestamp
        params["timestamp"] = str(int(time.time() * 1000))
        
        # Подписываем
        params["sign"] = self._sign(params)
        
        try:
            if method == "GET":
                resp = self.session.get(url, params=params, timeout=15)
            else:
                resp = self.session.post(url, json=params, timeout=15)
            
            print(f"[{resp.status_code}] {method} {url}")
            
            if resp.status_code != 200:
                print(f"[WARN] Статус: {resp.status_code}")
                print(f"[WARN] Тело: {resp.text[:500]}")
                return None
                
            data = resp.json()
            return data
            
        except requests.exceptions.Timeout:
            print(f"[ERROR] Таймаут: {url}")
            return None
        except requests.exceptions.ConnectionError as e:
            print(f"[ERROR] Ошибка соединения: {e}")
            return None
        except json.JSONDecodeError:
            print(f"[ERROR] Не JSON ответ: {resp.text[:200]}")
            return None
        except Exception as e:
            print(f"[ERROR] {e}")
            return None

    # === Эндпоинты ===

    def search(self, keyword: str, page: int = 1, limit: int = 20) -> Optional[dict]:
        """
        Поиск товаров по ключевому слову.
        Эндпоинт: /api/v1/h5/search/fire/search/list (из Open Platform)
        """
        endpoint = "/api/v1/h5/search/fire/search/list"
        params = {
            "title": keyword,
            "page": page,
            "limit": limit,
            "showHot": -1,
        }
        return self._request(endpoint, params)

    def get_category_tree(self) -> Optional[dict]:
        """Получить дерево категорий (Nike -> кроссовки и т.д.)"""
        endpoint = "/api/commodity/get-category-tree"
        return self._request(endpoint)

    def get_product_detail(self, spu_id: str) -> Optional[dict]:
        """Детальная информация о товаре"""
        endpoint = "/api/commodity/product/detail"
        params = {"spuId": spu_id}
        return self._request(endpoint)

    def get_commodity_list(self, category_id: str, page: int = 1) -> Optional[dict]:
        """Список товаров по категории"""
        endpoint = "/api/commodity/list"
        params = {
            "categoryId": category_id,
            "page": page,
            "pageSize": 20,
        }
        return self._request(endpoint)

    def search_v2(self, keyword: str) -> Optional[dict]:
        """
        Международный поиск (intl)
        Эндпоинт из чанков: /api/v1/h5/bigger/intl/commodity
        """
        # Возможные эндпоинты для intl поиска
        candidates = [
            "/api/v1/h5/bigger/intl/commodity/get-index-spu-detail",
            "/api/intl/commodity/list",
            "/api/v1/h5/bigger/intl/search/list",
            "/api/intl/search/spu",
        ]
        
        results = {}
        for endpoint in candidates:
            params = {
                "keyword": keyword,
                "pageNum": 1,
                "pageSize": 20,
            }
            data = self._request(endpoint, params)
            results[endpoint] = data
            
        return results


def parse_products(data: dict) -> list[dict]:
    """Распарсить список товаров из ответа API"""
    products = []
    
    if not data:
        return products
    
    # Open Platform обычно возвращает data.result.items или data.data
    result = data.get("result") or data.get("data") or {}
    items = result.get("items") or result.get("list") or []
    
    if not items and isinstance(result, list):
        items = result
    
    for item in items:
        try:
            products.append({
                "spuId": item.get("spuId", ""),
                "title": item.get("title", "") or item.get("spuName", ""),
                "brand": item.get("brandName", ""),
                "category": item.get("categoryName", ""),
                "price": item.get("price", item.get("salePrice", 0)),
                "salePrice": item.get("salePrice", item.get("price", 0)),
                "titleImg": (item.get("imageList") or item.get("images") or [""])[0],
                "images": item.get("imageList", []) or item.get("images", []),
                "url": f"https://www.poizon.com/product/{item.get('spuId', '')}",
            })
        except Exception as e:
            print(f"[WARN] Ошибка парсинга товара: {e}")
            continue
    
    return products


def main():
    if len(sys.argv) < 2:
        print("Использование:")
        print("  python3 open_platform_client.py search <keyword> [page]")
        print("  python3 open_platform_client.py categories")
        print("  python3 open_platform_client.py detail <spuId>")
        print("  python3 open_platform_client.py test <keyword>")
        print("")
        print("Переменная окружения: POIZON_OPEN_KEY")
        sys.exit(1)
    
    try:
        client = PoizonOpenClient()
    except ValueError as e:
        print(f"❌ {e}")
        print("\n💡 Установи ключ: export POIZON_OPEN_KEY='твой_ключ'")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "search":
        if len(sys.argv) < 3:
            print("Укажи поисковый запрос")
            sys.exit(1)
        keyword = sys.argv[2]
        page = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        
        print(f"🔍 Поиск: '{keyword}' (стр. {page})...")
        data = client.search(keyword, page=page)
        
        if data:
            print(f"\n📦 Ответ API:")
            print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])
            products = parse_products(data)
            if products:
                print(f"\n✅ Найдено товаров: {len(products)}")
                for p in products[:5]:
                    print(f"  👟 {p['title']} — ¥{p['salePrice']}")
            else:
                print("⚠️ Товары не распарсились. Нужно настроить парсинг под структуру ответа.")
        else:
            print("❌ Не удалось получить данные")
    
    elif command == "categories":
        print("📂 Получение категорий...")
        data = client.get_category_tree()
        if data:
            print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])
        else:
            print("❌ Не удалось получить категории")
    
    elif command == "detail":
        if len(sys.argv) < 3:
            print("Укажи spuId")
            sys.exit(1)
        spu_id = sys.argv[2]
        print(f"📄 Детали товара: {spu_id}")
        data = client.get_product_detail(spu_id)
        if data:
            print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])
        else:
            print("❌ Не удалось получить детали")
    
    elif command == "test":
        if len(sys.argv) < 3:
            print("Укажи поисковый запрос")
            sys.exit(1)
        keyword = sys.argv[2]
        
        print(f"🧪 Тестируем разные эндпоинты для '{keyword}'...")
        results = client.search_v2(keyword)
        
        for endpoint, data in results.items():
            status = "✅" if data else "❌"
            print(f"\n{status} {endpoint}")
            if data:
                print(json.dumps(data, indent=2, ensure_ascii=False)[:500])
    else:
        print(f"Неизвестная команда: {command}")


if __name__ == "__main__":
    main()
