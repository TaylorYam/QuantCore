"""持倉歸屬規則（#181）：做多的類股 ETF 依 Allocation 判斷 SR／SkewLS。"""
from strategy_owner import SKEWLS, SR, SR_AND_SKEWLS, etf_owner, includes

SKEWLS_ONLY = (0.0, 1.0)      # 目前三個帳戶都是這樣（SR 0%）
SR_ONLY = (0.8, 0.0)
BOTH = (0.5, 0.3)


def test_long_sector_etf_follows_the_allocation():
    assert etf_owner("XLC", 60, SKEWLS_ONLY) == SKEWLS
    assert etf_owner("XLC", 60, SR_ONLY) == SR
    assert etf_owner("XLC", 60, BOTH) == SR_AND_SKEWLS
    assert etf_owner("XLC", 60, (0.0, 0.0)) == SR          # 都關：殘留部位照舊算 SR


def test_unknown_allocation_keeps_the_old_rule():
    assert etf_owner("XLK", 10) == SR


def test_inverse_and_short_sector_etfs_are_always_skewls():
    for alloc in (None, SKEWLS_ONLY, SR_ONLY, BOTH):
        assert etf_owner("SIJ", 61, alloc) == SKEWLS
        assert etf_owner("XLC", -10, alloc) == SKEWLS


def test_non_etfs_are_left_to_the_caller():
    assert etf_owner("AAPL", 3, SKEWLS_ONLY) == ""


def test_filters_match_both_strategies_of_a_shared_holding():
    assert includes(SR_AND_SKEWLS, "SR") and includes(SR_AND_SKEWLS, "SkewLS")
    assert includes(SKEWLS, "SkewLS") and not includes(SKEWLS, "SR")


# ── 清單只有一個來源：策略設定（#187）────────────────────────────────

def test_etf_lists_come_from_the_strategy_configs():
    import strategy_owner
    from strategies.sector_rotation import config as sr_config
    from strategies.sector_skew_ls import config as skewls_config

    assert strategy_owner.SECTOR_ETFS == set(sr_config.SECTOR_TICKERS)
    assert strategy_owner.INVERSE_ETFS == set(skewls_config.INVERSE_TICKERS)
    assert strategy_owner.STRATEGY_ETFS == (strategy_owner.SECTOR_ETFS
                                            | strategy_owner.INVERSE_ETFS)


def test_ib_fetcher_names_are_the_same_lists():
    import ib_fetcher
    import strategy_owner

    assert ib_fetcher.SR_ETFS is strategy_owner.SECTOR_ETFS
    assert ib_fetcher.SKEWLS_INVERSE_TICKERS is strategy_owner.INVERSE_ETFS
    assert ib_fetcher.ETF_TICKERS is strategy_owner.STRATEGY_ETFS


def test_trade_owner_follows_the_allocation_without_a_position_direction():
    from strategy_owner import trade_owner

    assert trade_owner("XLK", SKEWLS_ONLY) == SKEWLS
    assert trade_owner("XLK", SR_ONLY) == SR
    assert trade_owner("XLK", BOTH) == SR_AND_SKEWLS
    assert trade_owner("XLK", None) == SR
    assert trade_owner("REW", SR_ONLY) == SKEWLS
    assert trade_owner("AAPL", BOTH) == ""
