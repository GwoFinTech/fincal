"""Regression test for Issue #21: db_cursor connection leak on cursor() failure."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_db_cursor_returns_connection_when_cursor_fails():
    """When conn.cursor() raises, the connection must still be returned to the pool."""
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    # Simulate conn.cursor() raising OperationalError
    mock_conn.cursor.side_effect = Exception("connection closed")

    with patch("app.db._get_pool", return_value=mock_pool):
        from app.db import db_cursor
        try:
            with db_cursor():
                pass
        except Exception:
            pass

    # Connection must be returned even when cursor() fails
    mock_pool.putconn.assert_called_once_with(mock_conn)


def test_db_cursor_returns_connection_on_normal_exit():
    """Normal path: cursor closed, connection returned."""
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_pool.getconn.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    with patch("app.db._get_pool", return_value=mock_pool):
        from app.db import db_cursor
        with db_cursor() as cur:
            pass

    mock_cursor.close.assert_called_once()
    mock_conn.commit.assert_called_once()
    mock_pool.putconn.assert_called_once_with(mock_conn)


def test_db_cursor_rolls_back_and_returns_on_error():
    """Error path: rollback called, connection returned."""
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_pool.getconn.return_value = mock_conn
    mock_conn.cursor.return_value = mock_cursor

    with patch("app.db._get_pool", return_value=mock_pool):
        from app.db import db_cursor
        try:
            with db_cursor():
                raise ValueError("query failed")
        except ValueError:
            pass

    mock_conn.rollback.assert_called_once()
    mock_cursor.close.assert_called_once()
    mock_pool.putconn.assert_called_once_with(mock_conn)
