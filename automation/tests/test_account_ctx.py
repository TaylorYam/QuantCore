"""
Unit tests for AccountCtx dataclass + from_name factory.

Coverage:
  - Field mapping from accounts.json + config.json merge
  - Frozen dataclass (mutation raises)
  - leverage = alloc_sr + alloc_sp500 (computed property)
  - client_id uniqueness per (account, role) pair
  - Per-account alloc override (Yvonne has different allocs in fake data)
"""

from __future__ import annotations

import pytest
from dataclasses import FrozenInstanceError


def test_from_name_taylor(fake_config):
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    ctx = AccountCtx.from_name("Taylor", ROLE_REBALANCE)

    assert ctx.name == "Taylor"
    assert ctx.account == "U17800779"
    assert ctx.port == 4001
    assert ctx.host == "127.0.0.1"
    assert ctx.alloc_sr == 0.80
    assert ctx.alloc_sp500 == 0.50
    assert ctx.dry_run is True
    assert ctx.order_type == "MOO"
    assert ctx.tg_token == "FAKE_TOKEN"
    assert ctx.tg_chat_id == "FAKE_CHAT"


def test_from_name_rosa(fake_config):
    from account_ctx import AccountCtx
    from account_utils import ROLE_REPORT

    ctx = AccountCtx.from_name("Rosa", ROLE_REPORT)
    assert ctx.account == "U12770188"
    assert ctx.port == 4002


def test_from_name_yvonne_has_override_allocs(fake_config):
    """Yvonne has alloc_sr=0.75 / alloc_sp500=0.45 in fake_accounts,
    overriding the base config values (0.80 / 0.50)."""
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    ctx = AccountCtx.from_name("Yvonne", ROLE_REBALANCE)
    assert ctx.alloc_sr == 0.75
    assert ctx.alloc_sp500 == 0.45


def test_leverage_is_sum_of_allocs(fake_config):
    """`leverage` is a computed property = alloc_sr + alloc_sp500.
    Prevents display-vs-execution drift (the bug we fixed in S2)."""
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    taylor = AccountCtx.from_name("Taylor", ROLE_REBALANCE)
    assert taylor.leverage == pytest.approx(1.30, abs=1e-9)

    yvonne = AccountCtx.from_name("Yvonne", ROLE_REBALANCE)
    assert yvonne.leverage == pytest.approx(0.75 + 0.45, abs=1e-9)


def test_frozen_cannot_mutate(fake_config):
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    ctx = AccountCtx.from_name("Taylor", ROLE_REBALANCE)
    with pytest.raises(FrozenInstanceError):
        ctx.port = 9999  # type: ignore[misc]


def test_frozen_replace_creates_new_object(fake_config):
    """dataclasses.replace should work on frozen ctx — used by S10 sync_position
    to build a LMT+outsideRTH variant without mutating the original."""
    from dataclasses import replace
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    original = AccountCtx.from_name("Taylor", ROLE_REBALANCE)
    modified = replace(original, order_type="LMT", outside_rth=True)

    assert modified.order_type == "LMT"
    assert modified.outside_rth is True
    # Original untouched
    assert original.order_type == "MOO"
    assert original.outside_rth is False
    # Other fields preserved
    assert modified.port == original.port
    assert modified.tg_token == original.tg_token


def test_client_id_unique_per_role(fake_config):
    """Each (account, role) pair must get a unique client_id to avoid IB
    Gateway clientId collisions."""
    from account_ctx import AccountCtx
    from account_utils import (
        ROLE_REBALANCE, ROLE_REPORT, ROLE_MOO_CHECK, ROLE_PANEL, ROLE_MONITOR,
    )

    roles = [ROLE_REBALANCE, ROLE_REPORT, ROLE_MOO_CHECK, ROLE_PANEL, ROLE_MONITOR]
    cids = {
        AccountCtx.from_name("Taylor", r).client_id for r in roles
    }
    assert len(cids) == 5, f"Taylor client_ids not unique across 5 roles: {cids}"


def test_client_id_unique_across_accounts(fake_config):
    """Different accounts at the same role get different client_ids."""
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    cids = {
        AccountCtx.from_name(name, ROLE_REBALANCE).client_id
        for name in ("Taylor", "Rosa", "Yvonne")
    }
    assert len(cids) == 3, f"3 accounts share client_id: {cids}"


def test_from_name_unknown_account_raises(fake_config):
    from account_ctx import AccountCtx
    from account_utils import ROLE_REBALANCE

    with pytest.raises(ValueError, match="not found"):
        AccountCtx.from_name("Nonexistent", ROLE_REBALANCE)
