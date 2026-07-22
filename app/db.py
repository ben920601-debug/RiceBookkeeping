"""
資料庫連線池（DBUtils PooledDB）與統一的 db_cursor context manager。
所有其他模組存取 MySQL 一律透過這裡的 db_cursor()，不要自己另外開連線。
"""
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor
from dbutils.pooled_db import PooledDB  # 🔌 連線池：取代每次手動開關連線，避免逾時被斷線與連線數暴增

from app.config import MYSQL_CONFIG

DB_POOL = None
DB_READY = False


def _init_pool():
    global DB_POOL
    DB_POOL = PooledDB(
        creator=pymysql,
        mincached=2,
        maxcached=5,
        maxconnections=20,
        blocking=True,
        ping=1,
        cursorclass=DictCursor,
        **MYSQL_CONFIG,
    )


def get_db_connection():
    """從連線池借用一條連線；用完呼叫 .close() 只是歸還給池子，不會真的斷線"""
    if DB_POOL is None:
        _init_pool()
    return DB_POOL.connection()


@contextmanager
def db_cursor():
    """統一管理連線與游標的 context manager，離開時自動歸還連線給連線池"""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def check_db_ready() -> bool:
    global DB_READY
    try:
        _init_pool()
        with db_cursor() as cur:
            cur.execute("SELECT 1")
        DB_READY = True
        print("🔥 [DATABASE] MySQL 連線池就位！", flush=True)
    except Exception as e:
        DB_READY = False
        print(f"❌ [DATABASE] MySQL 連線初始化異常: {e}", flush=True)
    return DB_READY


# 模組載入時就先檢查一次，跟原本 main.py 的行為一致
DB_READY = check_db_ready()


def is_db_ready() -> bool:
    """其他模組請呼叫這個函式，不要直接 import DB_READY 這個變數本身
    （import 進去的是當下的值快照，之後若有變動不會自動同步）"""
    return DB_READY

