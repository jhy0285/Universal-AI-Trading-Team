from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from trading_team.replay import demo_case
from trading_team.settings import Settings
from trading_team.storage import Database


def pytest_addoption(parser):
    parser.addoption(
        "--postgres",
        action="store_true",
        help="Run DB tests in isolated schemas of UAT_TEST_DATABASE_URL",
    )


@pytest.fixture
def db(tmp_path, request):
    admin = None
    schema = None
    if request.config.getoption("--postgres"):
        url = os.environ.get("UAT_TEST_DATABASE_URL")
        if not url:
            pytest.fail("--postgres requires an explicit UAT_TEST_DATABASE_URL")
        schema = "uat_test_" + uuid.uuid4().hex
        admin = create_engine(url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        database_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"})
        database = Database(database_url.render_as_string(hide_password=False))
    else:
        database = Database(f"sqlite:///{tmp_path}/test.sqlite3")
    database.initialize()
    database.create_account("paper", "fixture")
    yield database
    database.engine.dispose()
    if admin:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for role in ("CHIEF", "SCAN", "VET", "BOOK", "SIZE", "FILLS", "RISK"):
        for suffix in ("MODEL", "PROVIDER", "API_KEY"):
            monkeypatch.delenv(f"{role}_{suffix}", raising=False)
    return Settings(
        _env_file=None,
        llm_provider="ollama",
        llm_model="fixture-model",
        state_dir=tmp_path / "state",
    )


@pytest.fixture
def case():
    return demo_case()


@pytest.fixture
def snapshot(case):
    return case.events[0].snapshots[0]
