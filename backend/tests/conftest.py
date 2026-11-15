import os

import pytest
from sqlalchemy import create_engine

from scity.models import Base, make_session_factory
from scity.settings import DEMO_PRICING
from scity.spaces import SpaceService
from scity.wallet import WalletService


@pytest.fixture
def database_url(tmp_path):
    """默认用临时 SQLite；设了 SCITY_TEST_DATABASE_URL 就用它（每个测试前清空），用来在 PostgreSQL 上跑同一套测试。"""
    url = os.environ.get("SCITY_TEST_DATABASE_URL")
    if not url:
        return f"sqlite:///{tmp_path}/t.db"
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    engine.dispose()
    return url


@pytest.fixture
def sessions(database_url):
    return make_session_factory(database_url, create=True)


@pytest.fixture
def spaces(sessions):
    return SpaceService(sessions)


@pytest.fixture
def wallets(sessions):
    return WalletService(sessions, DEMO_PRICING, {"default": {"input_per_1k": 0.001, "output_per_1k": 0.002}})
