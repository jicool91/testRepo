#!/usr/bin/env python3
"""
Poizon Open Platform API Client v2
Основан на документации OAuth2 Authorization Code Grant + API подписи.

FLOW:
1. OAuth2 Authorization Code → access_token + refresh_token
2. Каждый API запрос содержит в query params:
   - app_key (client_id)
   - access_token
   - timestamp (ms)
   - language + timeZone
   - sign (подпись всех параметров)
   - business params

Использование:
  export POIZON_CLIENT_ID="твой_appKey"
  export POIZON_CLIENT_SECRET="твой_appSecret"
  export POIZON_ACCESS_TOKEN="..."  # после auth
  
  python3 open_platform_client_v2.py auth_url
  python3 open_platform_client_v2.py auth "https://...?code=XXXXX"
  python3 open_platform_client_v2.py search "Nike Air Force 1"
  python3 open_platform_client_v2.py refresh
"""

import os
import sys
import json
import time
import hashlib
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests


# === Конфигурация ===
OPEN_API_BASE = "https://open.poizon.com"
TOKEN_URL = f"{OPEN_API_BASE}/api/v1/h5/passport/v1/oauth2/token"
REFRESH_URL = f"{OPEN_API_BASE}/api/v1/h5/passport/v1/oauth2/refresh_token"


def make_sign(params: dict, secret: str) -> str:
    """
    Подпись запроса Open Platform.
    
    Из доки Poizon:
    - app_key + access_token + timestamp + остальные параметры → подписываются
    - По аналогии с order list: sign от всех параметров
    
    Алгоритм (предполагаемый, по образу мобильного API):
    - Сортируем параметры по ключам
    - Конкатенируем: k1=v1&k2=v2&... + secret
    - MD5
    """
    sorted_keys = sorted(params.keys())
    sign_base = "&".join(f"{k}={params[k]}" for k in sorted_keys)
    sign_base += secret
    return hashlib.md5(sign_base.encode("utf-8")).hexdigest()


class PoizonAPIClient:
    """Клиент Poizon Open Platform"""

    def __init__(self):
        self.client_id = os.environ.get("POIZON_CLIENT_ID", "")
        self.client_secret = os.environ.get("POIZON_CLIENT_SECRET", "")
        self.access_token = os.environ.get("POIZON_ACCESS_TOKEN", "")
        self.refresh_token = os.environ.get("POIZON_REFRESH_TOKEN", "")
        
        if not self.client_id or not self.client_secret:
            print("⚠️  Укажи POIZON_CLIENT_ID и POIZON_CLIENT_SECRET")
            print("   (появятся после аппрува приложения на open.poizon.com)")
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
            "Origin": OPEN_API_BASE,
            "Referer": f"{OPEN_API_BASE}/",
        })

    # ==================== OAuth2 Flow ====================

    def get_authorization_url(self, redirect_uri: str = "https://localhost:666/redict") -> str:
        """Сформировать URL для авторизации (шаг 1)"""
        from urllib.parse import quote
        
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": quote(redirect_uri),
            "scope": "all",
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{OPEN_API_BASE}/authorize?{query}"

    def exchange_code_for_token(self, authorization_code: str) -> Optional[dict]:
        """Обменять authorization_code на access_token + refresh_token (шаг 2)"""
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "authorization_code": authorization_code,
        }
        
        headers = {"Content-Type": "application/json"}
        
        print(f"🔄 Обмен code на token...")
        
        try:
            resp = self.session.post(TOKEN_URL, json=payload, headers=headers, timeout=15)
            data = resp.json()
            
            print(f"   Статус: {resp.status_code}")
            
            if data.get("code") == 200 or data.get("status") == 200:
                token_data = data.get("data", {})
                self.access_token = token_data.get("access_token", "")
                self.refresh_token = token_data.get("refresh_token", "")
                expires_in = token_data.get("access_token_expires_in", 0)
                
                print(f"✅ Токен получен!")
                print(f"   access_token:  {self.access_token[:40]}...")
                print(f"   refresh_token: {self.refresh_token[:40]}...")
                print(f"   срок: {expires_in} сек ({expires_in/86400:.0f} дней)")
                print(f"   open_id: {token_data.get('open_id', 'N/A')}")
                
                self._save_tokens(token_data)
                return token_data
            else:
                print(f"❌ Ошибка: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}")
                return None
                
        except Exception as e:
            print(f"❌ Ошибка запроса: {e}")
            return None

    def refresh_access_token(self) -> Optional[dict]:
        """Обновить access_token через refresh_token (шаг 3)"""
        if not self.refresh_token:
            print("❌ Нет refresh_token. Укажи POIZON_REFRESH_TOKEN")
            return None
        
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }
        
        headers = {"Content-Type": "application/json"}
        
        print(f"🔄 Обновление токена...")
        
        try:
            resp = self.session.post(REFRESH_URL, json=payload, headers=headers, timeout=15)
            data = resp.json()
            
            if data.get("code") == 200 or data.get("status") == 200:
                token_data = data.get("data", {})
                self.access_token = token_data.get("access_token", "")
                self.refresh_token = token_data.get("refresh_token", "")
                
                print(f"✅ Токен обновлён!")
                print(f"   access_token:  {self.access_token[:40]}...")
                
                self._save_tokens(token_data)
                return token_data
            else:
                print(f"❌ Ошибка: {json.dumps(data, indent=2, ensure_ascii=False)[:500]}")
                return None
                
        except Exception as e:
            print(f"❌ Ошибка запроса: {e}")
            return None

    def _save_tokens(self, token_data: dict):
        """Сохранить токены в .env файл"""
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        existing = {}
        
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        existing[k] = v
        
        if token_data.get("access_token"):
            existing["POIZON_ACCESS_TOKEN"] = token_data["access_token"]
        if token_data.get("refresh_token"):
            existing["POIZON_REFRESH_TOKEN"] = token_data["refresh_token"]
        
        with open(env_path, "w") as f:
            f.write("# Poizon API Tokens (auto-generated)\n")
            for k, v in sorted(existing.items()):
                f.write(f"{k}={v}\n")
        
        os.chmod(env_path, 0o600)
        print(f"💾 Токены сохранены в {env_path}")

    # ==================== API Endpoints (с подписью) ====================

    def _build_api_params(self, business_params: dict = None) -> dict:
        """
        Собрать query-параметры для API запроса.
        
        Формат из доки order list:
          app_key         — обязательный
          access_token    — обязательный (для ERP/ISV)
          timestamp       — мс
          language        — "zh"
          timeZone        — "Asia/Shanghai"
          sign            — подпись (вычисляется)
          + business params
        """
        params = {}
        
        # Стандартные параметры
        params["app_key"] = self.client_id
        params["timestamp"] = str(int(time.time() * 1000))
        params["language"] = "zh"
        params["timeZone"] = "Asia/Shanghai"
        
        if self.access_token:
            params["access_token"] = self.access_token
        
        # Бизнес-параметры
        if business_params:
            for k, v in business_params.items():
                params[k] = v
        
        # Подпись
        params["sign"] = make_sign(params, self.client_secret)
        
        return params

    def _api_request(self, endpoint: str, business_params: dict = None, method: str = "GET", body: dict = None) -> Optional[dict]:
        """Базовый запрос к API с авторизацией через query params"""
        url = f"{OPEN_API_BASE}{endpoint}"
        params = self._build_api_params(business_params)
        
        try:
            if method == "GET":
                # GET: параметры в URL + sign
                resp = self.session.get(url, params=params, timeout=15)
            elif method == "POST":
                # POST: параметры в URL, body в JSON
                resp = self.session.post(url, params=params, json=body, timeout=15)
            else:
                resp = self.session.request(method, url, params=params, json=body, timeout=15)
            
            # Пробуем JSON
            try:
                result = resp.json()
            except json.JSONDecodeError:
                print(f"⚠️  Не JSON ответ: {resp.status_code} {resp.text[:300]}")
                return {"_raw": resp.text, "_status": resp.status_code}
            
            if "code" in result and result.get("code") != 200 and result.get("code") != "200":
                code = result.get("code")
                msg = result.get("msg", result.get("message", "?"))
                if code in (401, "401"):
                    print(f"❌ 401 — токен истёк или невалидный. Сделай refresh.")
                elif code in (404, "404"):
                    print(f"❌ 404 — endpoint не найден: {endpoint}")
                else:
                    print(f"⚠️  API ошибка [{code}]: {msg}")
                print(f"   Body: {json.dumps(result, ensure_ascii=False)[:300]}")
                return result
            
            return result
            
        except requests.exceptions.Timeout:
            print(f"❌ Таймаут: {url}")
            return None
        except requests.exceptions.ConnectionError:
            print(f"❌ Ошибка соединения: {url}")
            return None
        except Exception as e:
            print(f"❌ {type(e).__name__}: {e}")
            return None

    # --- Поиск товаров ---
    def search(self, keyword: str, page: int = 1, limit: int = 20) -> Optional[dict]:
        """Поиск товаров"""
        endpoint = "/api/v1/h5/search/fire/search/list"
        biz = {
            "title": keyword,
            "page": page,
            "limit": limit,
            "showHot": -1,
            "sortMode": -1,
            "sortType": "default",
            "spuSearchType": 0,
        }
        return self._api_request(endpoint, biz, method="GET")

    # --- Категории ---
    def categories(self) -> Optional[dict]:
        """Дерево категорий"""
        endpoint = "/api/commodity/get-category-tree"
        biz = {"showType": 0}
        return self._api_request(endpoint, biz, method="GET")

    # --- Детали товара ---
    def detail(self, spu_id: str) -> Optional[dict]:
        """Детали товара по spuId"""
        endpoint = "/api/commodity/product/detail"
        biz = {"spuId": spu_id}
        return self._api_request(endpoint, biz, method="GET")

    # --- Список товаров по категории ---
    def product_list(self, category_id: int, page: int = 1, limit: int = 20) -> Optional[dict]:
        """Список товаров по категории"""
        endpoint = "/api/commodity/list"
        biz = {
            "categoryId": category_id,
            "page": page,
            "limit": limit,
        }
        return self._api_request(endpoint, biz, method="GET")

    # --- Список заказов (как в доке) ---
    def order_list(self, page: int = 1, page_size: int = 20, **filters) -> Optional[dict]:
        """Список заказов (референс из документации Poizon)"""
        endpoint = "/api/v1/h5/order/list"
        biz = {
            "page_no": page,
            "page_size": page_size,
        }
        biz.update(filters)
        return self._api_request(endpoint, biz, method="GET")

    # --- Инфо о пользователе ---
    def me(self) -> Optional[dict]:
        """Информация о пользователе/приложении"""
        endpoint = "/api/v1/h5/passport/v1/user/info"
        return self._api_request(endpoint, method="GET")


# ==================== Utils ====================

def parse_code_from_url(url: str) -> Optional[str]:
    """Извлечь code из callback URL"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return qs.get("code", [None])[0]


def print_response(data: dict, max_len: int = 3000):
    """Красиво напечатать ответ"""
    if data:
        text = json.dumps(data, indent=2, ensure_ascii=False)
        print(text[:max_len])
        if len(text) > max_len:
            print(f"\n... (ещё {len(text) - max_len} символов)")
    else:
        print("(пусто)")


def main():
    client = PoizonAPIClient()
    
    if len(sys.argv) < 2:
        print("Использование:")
        print("  🔐 Авторизация:")
        print("    auth_url [redirect_uri]    — получить ссылку для OAuth")
        print("    auth <callback_url>        — обменять code на токены")
        print("    refresh                    — обновить access_token")
        print()
        print("  🔍 API:")
        print("    search <keyword> [page]    — поиск товаров")
        print("    categories                 — дерево категорий")
        print("    detail <spuId>             — детали товара")
        print("    list <categoryId> [page]   — товары категории")
        print("    me                         — профиль")
        print()
        print("  📋 Переменные окружения:")
        print("    POIZON_CLIENT_ID           — appKey")
        print("    POIZON_CLIENT_SECRET       — appSecret")
        print("    POIZON_ACCESS_TOKEN        — токен (после auth)")
        print("    POIZON_REFRESH_TOKEN       — refresh токен")
        sys.exit(1)
    
    cmd = sys.argv[1]
    
    if cmd == "auth_url":
        redirect_uri = sys.argv[2] if len(sys.argv) > 2 else "https://localhost:666/redict"
        url = client.get_authorization_url(redirect_uri)
        print(f"\n🔗 Открой в браузере:\n{url}\n")
        print("👆 После авторизации вставь полный URL редиректа:\n")
        print(f"   python3 {sys.argv[0]} auth \"<полный_url>\"")
    
    elif cmd == "auth":
        if len(sys.argv) < 3:
            print("❌ Укажи callback URL с code")
            sys.exit(1)
        callback_url = sys.argv[2]
        code = parse_code_from_url(callback_url)
        
        if not code:
            print(f"❌ Не удалось извлечь code из URL: {callback_url}")
            print("   Ожидается: ?code=XXXXX")
            sys.exit(1)
        
        print(f"🔑 Code: {code}")
        client.exchange_code_for_token(code)
    
    elif cmd == "refresh":
        client.refresh_access_token()
    
    elif cmd == "search":
        if len(sys.argv) < 3:
            print("❌ Укажи поисковый запрос")
            sys.exit(1)
        keyword = sys.argv[2]
        page = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        
        print(f"🔍 Поиск '{keyword}' (стр. {page})...")
        data = client.search(keyword, page=page)
        
        if data:
            # Попробуем распарсить товары из разных форматов ответа
            payload = data.get("result") or data.get("data") or data
            items = payload.get("items") or payload.get("list") or payload.get("records") or []
            if not items and isinstance(payload, list):
                items = payload
            
            print_response(data)
            
            if items:
                print(f"\n📦 Найдено: {len(items)} товаров")
                for i, item in enumerate(items[:10], 1):
                    title = item.get("title") or item.get("spuName") or item.get("name", "?")
                    price = item.get("salePrice") or item.get("price") or item.get("originalPrice", "?")
                    spu = item.get("spuId") or item.get("id", "?")
                    print(f"  {i}. [{spu}] {title} — ¥{price}")
    
    elif cmd == "categories":
        print("📂 Категории...")
        data = client.categories()
        print_response(data)
    
    elif cmd == "detail":
        if len(sys.argv) < 3:
            print("❌ Укажи spuId")
            sys.exit(1)
        spu_id = sys.argv[2]
        print(f"📄 Детали {spu_id}...")
        data = client.detail(spu_id)
        print_response(data)
        
        if data and (data.get("code") == 200 or data.get("status") == 200):
            product = data.get("result") or data.get("data") or {}
            if product:
                print(f"\n📦 {product.get('title', '?')}")
                print(f"💰 ¥{product.get('salePrice', '?')}")
    
    elif cmd == "list":
        if len(sys.argv) < 3:
            print("❌ Укажи categoryId")
            sys.exit(1)
        cat_id = int(sys.argv[2])
        page = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        print(f"📂 Товары категории {cat_id} (стр. {page})...")
        data = client.product_list(cat_id, page=page)
        print_response(data)
    
    elif cmd == "me":
        print("👤 Профиль...")
        data = client.me()
        print_response(data)
    
    else:
        print(f"❌ Неизвестная команда: {cmd}")


if __name__ == "__main__":
    main()
