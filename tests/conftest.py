# SPDX-License-Identifier: Apache-2.0
"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def vault_root() -> Path:
    return (Path(__file__).parent / "fixtures" / "sample_vault").resolve()
