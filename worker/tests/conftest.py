"""Общая настройка тестов: путь к вспомогательным модулям и подключение к БД."""

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv()

from hotaru_worker import db  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL")


@pytest.fixture
def conn():
    """Соединение с БД внутри транзакции, которая откатывается. Без DATABASE_URL — пропуск
    (CI тесты на БД не запускает)."""
    if not DATABASE_URL:
        pytest.skip("нужен DATABASE_URL")
    with db.connect(DATABASE_URL) as connection:
        connection.autocommit = False
        try:
            yield connection
        finally:
            connection.rollback()
