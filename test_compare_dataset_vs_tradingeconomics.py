"""Tests (script autonome, sans framework, sans reseau) pour compare_dataset_vs_tradingeconomics.py.

Usage : python test_compare_dataset_vs_tradingeconomics.py
Sort avec le code 1 si une assertion echoue.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

import compare_aggregated_sources
from compare_clustered_vs_tradingeconomics import _compare_country
from compare_dataset_vs_tradingeconomics import to_usd_monthly

JAN = dt.date(2025, 1, 1)


def _fake_rate(currency: str, year: int, month: int) -> dict:
    if currency == "CHF":
        return {"rate": 1.1}
    raise compare_aggregated_sources.ExchangeRateError(f"pas de taux pour {currency}")


def _raw(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["Month", "Trade Direction", "Currency", "Total Value"])


def test_conversion_et_somme_par_mois() -> None:
    """CHF converti (x1.1), USD garde tel quel, DEXP/REEX ramenes a Export, flux TRANSIT ignore."""
    raw = _raw([
        (JAN, "DEXP", "CHF", 100.0),
        (JAN, "REEX", "USD", 10.0),
        (JAN, "Import", "CHF", 200.0),
        (JAN, "TRANSIT", "CHF", 999.0),
    ])
    got = to_usd_monthly(raw).set_index("Trade Direction")["Total Value"].to_dict()
    assert abs(got["Export"] - 120.0) < 1e-9 and abs(got["Import"] - 220.0) < 1e-9, got
    assert set(got) == {"Export", "Import"}, got
    print("OK  test_conversion_et_somme_par_mois")


def test_conversion_echouee_non_comparable() -> None:
    """Une devise sans taux (valeur non nulle) rend le mois NaN, puis NON_COMPARABLE face a TE."""
    raw = _raw([(JAN, "Export", "CHF", 100.0), (JAN, "Export", "XXX", 50.0)])
    monthly = to_usd_monthly(raw)
    assert monthly["Total Value"].isna().all() and "XXX" in monthly["conversion_errors"].iloc[0], monthly
    te = pd.DataFrame([{"Month": JAN, "Direction": "Export", "value_usd": 110.0}])
    result = _compare_country(monthly, te, tolerance=0.03)
    got = list(zip(result["status"], result["reason"]))
    assert got == [("NON_COMPARABLE", "source_conversion_echouee")], got
    print("OK  test_conversion_echouee_non_comparable")


def test_match_apres_conversion() -> None:
    raw = _raw([(JAN, "Export", "CHF", 100.0)])
    te = pd.DataFrame([{"Month": JAN, "Direction": "Export", "value_usd": 111.0}])
    result = _compare_country(to_usd_monthly(raw), te, tolerance=0.03)
    assert result["status"].tolist() == ["MATCH"], result
    print("OK  test_match_apres_conversion")


if __name__ == "__main__":
    compare_aggregated_sources.get_monthly_usd_rate = _fake_rate
    tests = [test_conversion_et_somme_par_mois, test_conversion_echouee_non_comparable, test_match_apres_conversion]
    failures = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append(test.__name__)
            print(f"FAIL {test.__name__}: {exc}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} tests passes")
    if failures:
        raise SystemExit(1)
