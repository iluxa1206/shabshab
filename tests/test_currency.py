from services.currency import is_ruble, normalize_ccy


def test_normalize():
    assert [normalize_ccy(x) for x in (None, "", "sur", "RUR", "RUB", " rub ")] == ["RUB"] * 6
    assert normalize_ccy("cnh") == "CNY" and normalize_ccy("USD") == "USD" and normalize_ccy("chf") == "CHF"
    assert is_ruble("SUR") and not is_ruble("EUR")


def test_to_rub(monkeypatch):
    from services import fx
    from services.currency import to_rub
    monkeypatch.setattr(fx, "cached_rate", lambda u: {"CNY": 12.5}.get(u))
    assert to_rub(None, "CNY") is None
    assert to_rub(10.0, "SUR") == 10.0            # рубль — без курса
    assert to_rub(2.0, "CNH") == 25.0             # CNH → CNY
    assert to_rub(1.0, "CHF") is None             # курса нет — не чужая валюта
