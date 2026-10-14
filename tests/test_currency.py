from services.currency import is_ruble, normalize_ccy


def test_normalize():
    assert [normalize_ccy(x) for x in (None, "", "sur", "RUR", "RUB", " rub ")] == ["RUB"] * 6
    assert normalize_ccy("cnh") == "CNY" and normalize_ccy("USD") == "USD" and normalize_ccy("chf") == "CHF"
    assert is_ruble("SUR") and not is_ruble("EUR")
