from datetime import date

import pytest

from ties import repo
from ties.db import Vault

TODAY = date(2026, 11, 9)


@pytest.fixture
def vault(tmp_path):
    v = Vault(tmp_path / "test.vault")
    repo.init_vault(v, "Test Owner")
    yield v
    v.close()


@pytest.fixture
def today():
    return TODAY
