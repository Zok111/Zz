"""The app must never be able to place trades or touch the account."""

import re
from pathlib import Path

import pytest

from app.marketdata.pocketoption import _ReadOnlyClient, check_ssid_format

APP_DIR = Path(__file__).resolve().parents[1] / "app"

FORBIDDEN = [
    "buy", "sell", "check_win", "open_pending_order", "cancel_pending_order", "cancel_pending_orders",
    "balance", "opened_deals", "closed_deals", "get_opened_deal", "get_closed_deal", "clear_closed_deals",
    "get_pending_deals", "send_raw", "send_raw_message", "subscribe_raw", "create_raw_handler",
    "create_raw_order", "create_raw_order_with_timeout", "create_raw_order_with_timeout_and_retry",
    "create_raw_iterator", "get_deal_end_time",
]


class _FakeLib:
    def __getattr__(self, name):
        return lambda *a, **k: name


@pytest.mark.parametrize("method", FORBIDDEN)
def test_proxy_blocks_trading_and_account_methods(method):
    client = _ReadOnlyClient(_FakeLib())
    with pytest.raises(PermissionError):
        getattr(client, method)


def test_proxy_allows_market_data_methods():
    client = _ReadOnlyClient(_FakeLib())
    assert client.active_assets() == "active_assets"
    assert client.subscribe_symbol("X") == "subscribe_symbol"


def test_whitelist_contains_no_forbidden_method():
    assert not (_ReadOnlyClient.ALLOWED & set(FORBIDDEN))


def test_no_source_file_calls_forbidden_methods():
    pattern = re.compile(r"\.(" + "|".join(map(re.escape, FORBIDDEN)) + r")\s*\(")
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []


def test_ssid_format_check():
    assert check_ssid_format('42["auth",{"session":"abcdefghijk","isDemo":1,"uid":1,"platform":2}]')
    assert not check_ssid_format("abcdef")
    assert not check_ssid_format('42["auth",{"isDemo":1}]')
    assert not check_ssid_format('42["auth",{broken')


def test_ssid_normalization():
    from app.marketdata.pocketoption import normalize_ssid
    assert check_ssid_format(normalize_ssid("  42['auth',{\"session\":\"abcdefghijk\",\"uid\":1}]\n"))
