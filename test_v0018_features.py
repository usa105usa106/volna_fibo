from io import BytesIO
from pathlib import Path

from PIL import Image

from config import Settings
from core_models import WaveState
from core_symbols import WALK_MAJOR_BASES, excluded_from_crypto_top
from services_formatter import _fmt, render_table_png, technical_report_text


ROOT = Path(__file__).resolve().parent


def sample_state(symbol="DOGSUSDT", rating=9.1, control=False):
    state = WaveState(
        symbol=symbol,
        exchange="mexc_futures",
        wave_type="W3-(2)",
        status="CONFIRMED",
        origin=0.00004001,
        impulse_high=0.00006001,
        working_low=0.00004901,
        strict_origin=0.00004001,
        retrace_depth=0.78,
        fibs={"0.618": 0.00005201},
        fib_status="> .618 · 3/3 C4H",
        targets=[0.00006901, 0.00008137, 0.00010137],
        base_zone=(0.00004701, 0.00004901),
        deep_zone=(0.00004301, 0.00004601),
        current_price=0.00004912,
        growth_from_low_pct=0.22,
        strict_distance_pct=22.5,
        rating=rating,
        last_event="CONFIRMED",
        is_control=control,
    )
    return state


def test_v0018_version_is_default():
    assert Settings(BOT_TOKEN="test", _env_file=None).bot_version == "0026"


def test_commodity_duplicates_are_excluded_from_crypto_top():
    assert excluded_from_crypto_top("XAUT") is True
    assert excluded_from_crypto_top("UKOIL") is True
    assert excluded_from_crypto_top("JUP") is False


def test_walk_is_fixed_to_exact_ten_majors():
    assert WALK_MAJOR_BASES == ("BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LINK", "LTC", "BCH")
    source = (ROOT / "services_scanner.py").read_text()
    assert "symbols = [normalize_symbol(exchange, base) for base in WALK_MAJOR_BASES]" in source
    assert "await self._map(one_asset, symbols)" in source


def test_png_table_is_high_resolution_vertical_column_table_and_preserves_tiny_prices():
    data = render_table_png(
        [sample_state()],
        title="ПОИСК W2 / W3-(2)",
        subtitle="MEXC Futures · Top-300",
        crypto_label="TOP-10 CRYPTO",
        version="0020",
    )
    image = Image.open(BytesIO(data))
    assert image.width >= 2400
    assert image.height >= 400
    assert data.startswith(b"\x89PNG")
    assert _fmt(4.901e-05) == "0.00004901"
    assert "e" not in _fmt(1e-20).lower()


def test_txt_report_contains_current_table_data_and_technical_state():
    text = technical_report_text(
        [sample_state()],
        heading=["SENIOR WAVE BOT v0018", "TEST"],
        errors=["ABC: DATA_INCOMPLETE"],
    )
    assert "DOGS" in text
    assert "0.00004901" in text
    assert "fib_status" not in text  # human-facing labels, not a Python dump
    assert "fibs:" in text
    assert "ОШИБКИ / НЕПОЛНЫЕ ДАННЫЕ" in text


def test_runtime_declares_pillow_and_dejavu_font():
    requirements = (ROOT / "requirements.txt").read_text()
    lock = (ROOT / "requirements.lock.txt").read_text()
    docker = (ROOT / "Dockerfile").read_text()
    assert "Pillow==12.3.0" in requirements
    assert "Pillow==12.3.0" in lock
    assert "fonts-dejavu-core" in docker
    assert 'org.opencontainers.image.version="0026"' in docker


def test_v0018_highlighting_is_sparse_and_only_marks_strong_properties():
    from services_formatter import _row_values_flags

    strong = sample_state(rating=9.1)
    _, flags = _row_values_flags(strong, 1)
    # rating, very-fresh growth and >=30% T1 asymmetry are good here.
    # Asset/raw price/wave/low are deliberately NOT bold; .618 reclaim is not strong enough.
    assert flags == [False, False, True, False, False, False, False, True, False, False, True]

    ordinary = sample_state(rating=7.8)
    ordinary.growth_from_low_pct = 4.0
    ordinary.fib_status = "> .618 · 3/3 C4H"
    ordinary.targets = [ordinary.current_price * 1.20]
    _, ordinary_flags = _row_values_flags(ordinary, 1)
    assert ordinary_flags == [False] * 11

    confirmed = sample_state(rating=8.1)
    confirmed.growth_from_low_pct = 4.0
    confirmed.fib_status = ">R.500 · 3 C4H"
    confirmed.targets = [confirmed.current_price * 1.20]
    _, confirmed_flags = _row_values_flags(confirmed, 1)
    assert confirmed_flags[6] is True
    assert sum(confirmed_flags) == 1
