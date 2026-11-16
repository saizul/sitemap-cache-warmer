from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakecache import FakeCache


@pytest.fixture
def cache() -> FakeCache:
    return FakeCache()
