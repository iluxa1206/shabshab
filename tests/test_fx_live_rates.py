"""Живой FX: любая валюта номинала должна получить курс или честный no_fx."""


def test_cbr_parser_keeps_chf_and_normalises_nominal():
    """CHF не торговался в нашем TOM-наборе и раньше отбрасывался фильтром."""
    from services import fx

    raw = b'''<?xml version="1.0" encoding="windows-1251"?>
    <ValCurs>
      <Valute ID="R01235"><CharCode>USD</CharCode><Nominal>1</Nominal><Value>78,5000</Value></Valute>
      <Valute ID="R01775"><CharCode>CHF</CharCode><Nominal>1</Nominal><Value>99,2500</Value></Valute>
      <Valute ID="R01375"><CharCode>CNY</CharCode><Nominal>10</Nominal><Value>111,5000</Value></Valute>
    </ValCurs>'''

    assert fx._parse_cbr_xml(raw) == {"USD": 78.5, "CHF": 99.25, "CNY": 11.15}
    assert fx._parse_cbr_ids(raw) == {"USD": "R01235", "CHF": "R01775", "CNY": "R01375"}


def test_history_backfill_targets_live_face_units_without_ruble_noise():
    from services import fx

    assert fx._history_ccys({"RUB", "SUR", "CHF", "CNH"}) == {"USD", "EUR", "CNY", "CHF"}


def test_archived_face_units_uses_only_isins_present_in_ticks(monkeypatch):
    import asyncio
    from services import trades_archive as ta

    monkeypatch.setattr(ta, "face_units", lambda: asyncio.sleep(0, result={
        "RU000RUB001": "SUR", "RU000CHF001": "CHF", "UNUSED": "AED",
    }))

    class Cursor:
        def __iter__(self):
            return iter([("RU000RUB001",), ("RU000CHF001",)])

    class Conn:
        def execute(self, _sql): return Cursor()
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    monkeypatch.setattr(ta, "_connect", lambda: Conn())
    assert asyncio.run(ta.archived_face_units()) == {"SUR", "CHF"}


def test_chf_face_tick_has_a_reliable_rouble_value(monkeypatch):
    """Курс ЦБ CHF делает порог сигнала рублёвым, а не отключает alert."""
    from services import trades_stream as ts

    monkeypatch.setattr(ts, "_faces", {
        "at": 0.0, "map": {"RU000CHF001": 1_000.0}, "unit": {"RU000CHF001": "CHF"},
    })
    monkeypatch.setattr(ts, "_fx", {"at": 0.0, "rates": {"CHF": 99.25}})

    value, fx_ok = ts._tick_value("RU000CHF001", 100.0, 10)

    assert value == 992_500.0
    assert fx_ok is True
