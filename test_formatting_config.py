from decimal import Decimal
import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from config import Settings
from services_formatter import (
    _fmt,
    split_html,
    split_plain,
    telegram_table_messages,
)
from conftest import state


@pytest.mark.parametrize(
    "value", [0.00004901, 1.49e-24, 1.23456789e-25, 9.87654321e-31, 5e-324, 1e308]
)
def test_fixed_decimal_preserves_significant_digits_at_all_float_magnitudes(value):
    rendered = _fmt(value)
    assert "e" not in rendered.lower()
    assert abs(Decimal(rendered) / Decimal(str(value)) - 1) < Decimal("1e-9")


@pytest.mark.parametrize("case", ["row", "title", "empty", "emoji"])
def test_html_messages_are_bounded_and_tags_entities_remain_valid(case):
    row = state()
    title = "T"
    rows = [row]
    if case == "row":
        row.fib_status = "<broken & unsafe>" * 600
    if case == "title":
        title = "long & title " * 600
    if case == "empty":
        title = "empty title " * 600
        rows = []
    if case == "emoji":
        row.fib_status = "🧪" * 3000
    messages = telegram_table_messages(rows, title)
    assert len(messages) > 1
    for text in messages:
        root = ET.fromstring("<root>" + text + "</root>")
        plain = "".join(root.itertext())
        assert 0 < len(plain.encode("utf-16-le")) // 2 <= 3500
        assert all(node.tag in {"root", "b"} for node in root.iter())


def test_split_does_not_drop_or_inject_characters():
    text = "<b>A &amp; B 🧪</b> &lt;x&gt;" * 1000
    chunks = split_html(text, 100)

    def parsed(x):
        return "".join(ET.fromstring("<root>" + x + "</root>").itertext())

    assert "".join(map(parsed, chunks)) == parsed(text)
    plain = "a & <x> 🧪" * 1000
    assert "".join(split_plain(plain)) == plain


@pytest.mark.parametrize(
    "key,value",
    [
        ("HTTP_CONCURRENCY", 0),
        ("HTTP_CONCURRENCY", -1),
        ("HTTP_TIMEOUT_SECONDS", 0),
        ("API_RETRY_BASE_DELAY_SECONDS", -1),
        ("TELEGRAM_RETRY_BASE_DELAY_SECONDS", float("nan")),
        ("LOOKBACK_1H_DAYS", 0),
        ("LOOKBACK_1D_DAYS", -1),
        ("WALK_HISTORY_DAYS", 0),
        ("WALK_HORIZON_DAYS", -1),
        ("MIN_RATING", float("inf")),
        ("ACTION_COOLDOWN_SECONDS", -1),
    ],
)
def test_invalid_runtime_configuration_fails_at_startup(key, value):
    with pytest.raises(ValidationError):
        Settings(BOT_TOKEN="test", _env_file=None, **{key: value})
