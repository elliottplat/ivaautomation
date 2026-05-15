import os
import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DATABASE_URL", "")

import app as _app_module


@pytest.fixture
def app():
    _app_module.app.config["TESTING"] = True
    _app_module.app.config["WTF_CSRF_ENABLED"] = False
    yield _app_module.app


@pytest.fixture
def client(app):
    return app.test_client()
