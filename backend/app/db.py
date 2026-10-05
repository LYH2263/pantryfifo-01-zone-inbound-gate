import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    # isolation_level=None：自动提交模式，写路径自行发出
    # BEGIN IMMEDIATE/COMMIT，使「确认落层 / 消费 / 下架」串行化，
    # 杜绝半上架与幽灵批。
    c = sqlite3.connect(db_path(), isolation_level=None)
    c.row_factory = sqlite3.Row
    return c
