#!/usr/bin/env python3
"""SQLite database for Poizon bot"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/workspace/poizon-bot/products.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            spu_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            brand TEXT DEFAULT '',
            price_cny REAL DEFAULT 0,
            sale_price_cny REAL DEFAULT 0,
            images TEXT DEFAULT '[]',
            category TEXT DEFAULT '',
            url TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            spu_id TEXT NOT NULL,
            price_cny REAL NOT NULL,
            recorded_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (spu_id) REFERENCES products(spu_id)
        );
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            results_count INTEGER DEFAULT 0,
            searched_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


def save_product(product: dict) -> bool:
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO products (spu_id, title, brand, price_cny, sale_price_cny, images, category, url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(spu_id) DO UPDATE SET
                price_cny=excluded.price_cny,
                sale_price_cny=excluded.sale_price_cny,
                updated_at=datetime('now')
        """, (
            product["spu_id"], product["title"], product["brand"],
            float(product.get("price", 0)), float(product.get("sale_price", 0)),
            json.dumps(product.get("images", [])), product.get("category", ""),
            product.get("url", "")
        ))
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB ERROR] save: {e}")
        return False
    finally:
        conn.close()


def save_search(query: str, count: int):
    conn = get_conn()
    conn.execute("INSERT INTO searches (query, results_count) VALUES (?, ?)", (query, count))
    conn.commit()
    conn.close()


def get_product(spu_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM products WHERE spu_id = ?", (spu_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_search_history(limit: int = 10) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM searches ORDER BY searched_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


import json
from typing import Optional
