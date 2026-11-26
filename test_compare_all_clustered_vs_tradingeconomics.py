"""Tests (script autonome, sans framework, sans reseau) pour compare_all_clustered_vs_tradingeconomics.py.

Usage : python test_compare_all_clustered_vs_tradingeconomics.py
Sort avec le code 1 si une assertion echoue.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from compare_all_clustered_vs_tradingeconomics import _is_te_monthly, _summarize_pair
from compare_clustered_vs_tradingeconomics import _compare_country, resolve_te_slug
from te_scraper import trade_indicator_slugs


def _te(months: list[dt.date], direction: str = "Export", value: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame([{"Month": m, "Direction": direction, "value_usd": value} for m in months])


def test_serie_mensuelle_detectee() -> None:
    months = [dt.date(2025, m, 1) for m in range(1, 13)]
    te = pd.concat([_te(months, "Export"), _te(months, "Import")])
    assert _is_te_monthly(te), "Une serie de 12 mois consecutifs doit etre reconnue mensuelle"
    print("OK  test_serie_mensuelle_detectee")


def test_serie_trimestrielle_rejetee() -> None:
    quarters = [dt.date(2024, m, 1) for m in (3, 6, 9, 12)] + [dt.date(2025, m, 1) for m in (3, 6)]
    assert not _is_te_monthly(_te(quarters)), "Une serie trimestrielle ne doit pas etre comparee mois par mois"
    assert not _is_te_monthly(_te([dt.date(2025, 1, 1)])), "Un seul point ne suffit pas a etablir une serie mensuelle"
    assert not _is_te_monthly(pd.DataFrame(columns=["Month", "Direction", "value_usd"])), "Serie vide : non mensuelle"
    print("OK  test_serie_trimestrielle_rejetee")


def test_mapping_pays() -> None:
    expected = {
        "FIN": "finland",
        "KOSOV": "kosovo",  # code non ISO (CODE_SLUG_OVERRIDES)
        "TUR": "turkey",  # nom ISO "Turkiye", TE garde "Turkey"
        "COD": "congo",  # TE nomme la RD Congo "Congo"
        "COG": "republic-of-the-congo",  # et non "congo", qui est la RD Congo chez TE
    }
    for code, slug in expected.items():
        assert resolve_te_slug(code) == (slug, ""), f"{code} -> {slug} attendu, obtenu {resolve_te_slug(code)}"
    slug, reason = resolve_te_slug("ASEAN")
    assert slug is None and reason, "Un regroupement (ASEAN) n'a pas de page pays Trading Economics : non mappe"
    print("OK  test_mapping_pays")


def test_resume_couple() -> None:
    """Ecart signe = (TE - Clustered) / Clustered : +2 % et +10 % -> mediane +6 %, 1 MATCH / 1 MISMATCH."""
    clustered = pd.DataFrame(
        [
            {"Month": dt.date(2025, 1, 1), "Trade Direction": "Export", "Total Value": 100.0},
            {"Month": dt.date(2025, 2, 1), "Trade Direction": "Export", "Total Value": 100.0},
        ]
    )
    te = pd.DataFrame(
        [
            {"Month": dt.date(2025, 1, 1), "Direction": "Export", "value_usd": 102.0},
            {"Month": dt.date(2025, 2, 1), "Direction": "Export", "value_usd": 110.0},
        ]
    )
    summary = _summarize_pair(_compare_country(clustered, te, tolerance=0.03))
    assert summary["rows_compared"] == 2 and summary["match"] == 1 and summary["mismatch"] == 1, summary
    assert abs(summary["median_signed_gap"] - 0.06) < 1e-9, f"Ecart median signe attendu +6 %, obtenu {summary['median_signed_gap']}"
    assert summary["window_start"] == dt.date(2025, 1, 1) and summary["window_end"] == dt.date(2025, 2, 1)
    print("OK  test_resume_couple")


def test_conversion_te_manquante_non_comparable() -> None:
    """Une valeur TE non convertie en USD (NaN, ex: KES avant 2024) est NON_COMPARABLE
    "te_conversion_echouee", et non additionnee a 0 (faux ecart de -100 %)."""
    clustered = pd.DataFrame(
        [
            {"Month": dt.date(2020, 1, 1), "Trade Direction": "Export", "Total Value": 100.0},
            {"Month": dt.date(2025, 1, 1), "Trade Direction": "Export", "Total Value": 100.0},
        ]
    )
    te = pd.DataFrame(
        [
            {"Month": dt.date(2020, 1, 1), "Direction": "Export", "value_usd": float("nan")},
            {"Month": dt.date(2025, 1, 1), "Direction": "Export", "value_usd": 101.0},
        ]
    )
    result = _compare_country(clustered, te, tolerance=0.03).sort_values("Month")
    got = list(zip(result["status"], result["reason"].fillna("")))
    assert got == [("NON_COMPARABLE", "te_conversion_echouee"), ("MATCH", "")], got
    summary = _summarize_pair(result)
    assert summary["median_signed_gap"] == 0.01, f"Le mois non converti ne doit pas compter : {summary}"
    print("OK  test_conversion_te_manquante_non_comparable")


def test_etats_unis_biens_seulement() -> None:
    # Les pages "exports"/"imports" TE des Etats-Unis incluent les services : on lit les biens.
    assert trade_indicator_slugs("united-states") == {"Export": "goods-exports", "Import": "goods-imports"}
    assert trade_indicator_slugs("finland") == {"Export": "exports", "Import": "imports"}
    print("OK  test_etats_unis_biens_seulement")


if __name__ == "__main__":
    tests = [
        test_serie_mensuelle_detectee,
        test_serie_trimestrielle_rejetee,
        test_mapping_pays,
        test_resume_couple,
        test_conversion_te_manquante_non_comparable,
        test_etats_unis_biens_seulement,
    ]
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
