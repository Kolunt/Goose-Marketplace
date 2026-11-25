import pytest

from ozon_seller_mcp.db import connect, init_db


@pytest.fixture
def conn(tmp_path):
    connection = connect(tmp_path / "ozon.sqlite")
    init_db(connection)
    yield connection
    connection.close()
