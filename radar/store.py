import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    """为文件版本、幂等和并发控制提供 SQLite 事务连接。"""

    def __init__(self, path="data/radar.sqlite3"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()
