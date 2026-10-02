"""Billing test kit fixtures: the full schema with the payment and billing tables and ``users.wallet_minor``.

Until integration registers ``svbg.billing.tables`` / ``svbg.payments.tables`` in ``TABLE_MODULES`` (and a
migration adds ``users.wallet_minor``), the tables are attached to the shared metadata only while a billing
test runs (``tests/e2e/test_migrations.py`` in the same xdist worker must not see tables no migration
creates), and the column is added by ``ALTER TABLE`` in the fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from tests.billing.kit import BillingEnv, attach_tables, build_billing_env, detach_tables


@pytest.fixture
def billing_schema() -> Iterator[None]:
    attach_tables()
    try:
        yield
    finally:
        detach_tables()


@pytest.fixture
async def env(pg_dsn: str, billing_schema: None) -> AsyncIterator[BillingEnv]:
    async with build_billing_env(pg_dsn) as environment:
        yield environment
