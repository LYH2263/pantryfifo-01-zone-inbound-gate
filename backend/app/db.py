import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect(tx: bool = False):
    """tx=True 时进入 autocommit 模式并立即 BEGIN IMMEDIATE:
    读改写全程持有写锁,落层确认 / 消费 / 下架彼此串行,不会看到中间态。
    调用方须自行 commit()/rollback() 并 close()。"""
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    if tx:
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
    return c
