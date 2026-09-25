import sqlite3
from pathlib import Path

import pytest

from mediavault import db as mv_db


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "mediavault.db"
    with mv_db.connect(path) as c:
        mv_db.init_db(c)
        yield c


def write_file(root: Path, rel_path: str, content: bytes) -> Path:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p
