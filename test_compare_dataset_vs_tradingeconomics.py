"""Tests (script autonome, sans framework, sans reseau) pour compare_dataset_vs_tradingeconomics.py.

Usage : python test_compare_dataset_vs_tradingeconomics.py
Sort avec le code 1 si une assertion echoue.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

import compare_aggregated_sources
from compare_clustered_vs_tradingeconomics import _compare_country
from compare_dataset_vs_tradingeconomics import (
    build_monthly,
    build_selection,
    currency_inventory,
    dataset_notes,
    load_selection_file,
    month_number,
    month_range,
    parse_period,
    te_in_compared_currency,
    to_usd_monthly,
)

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


def test_comparaison_en_devise_locale() -> None:
    """Dataset en CHF et serie TE en CHF : compare directement en CHF, sans conversion (le taux
    fictif x1.1 n'est pas applique). Un mois avec une 2e devise (EUR) passe en USD."""
    feb = dt.date(2025, 2, 1)
    raw = _raw([
        (JAN, "Export", "CHF", 100.0),
        (feb, "Export", "CHF", 100.0),
        (feb, "Export", "EUR", 5.0),  # pas de taux fictif pour EUR -> conversion echouee
    ])
    monthly = build_monthly(raw, "CHF").set_index("Month")
    assert monthly.loc[JAN, "compared_in"] == "CHF" and monthly.loc[JAN, "Total Value"] == 100.0, monthly
    assert monthly.loc[feb, "compared_in"] == "USD" and monthly.loc[feb, "dataset_currencies"] == "CHF, EUR", monthly
    assert pd.isna(monthly.loc[feb, "Total Value"]), "EUR sans taux : mois USD NaN (NON_COMPARABLE)"

    te = pd.DataFrame([
        {"Month": JAN, "Direction": "Export", "value_original": 101.0, "currency": "CHF", "value_usd": 111.1},
        {"Month": feb, "Direction": "Export", "value_original": 101.0, "currency": "CHF", "value_usd": 111.1},
    ])
    te_aligned = te_in_compared_currency(te, monthly.reset_index(), "CHF").set_index("Month")
    assert te_aligned.loc[JAN, "value_usd"] == 101.0, "mois CHF : valeur TE d'origine (CHF)"
    assert te_aligned.loc[feb, "value_usd"] == 111.1, "mois USD : valeur TE convertie en USD"
    result = _compare_country(monthly.reset_index(), te_aligned.reset_index(), tolerance=0.03).set_index("Month")
    assert result.loc[JAN, "status"] == "MATCH" and abs(result.loc[JAN, "pct_diff"] - 0.01) < 1e-9, result
    print("OK  test_comparaison_en_devise_locale")


def test_inventaire_et_notes_devises() -> None:
    """Inventaire des devises non nulles sur tout l'historique, et notes : plusieurs devises,
    devise manquante, devise TE absente du dataset."""
    feb = dt.date(2025, 2, 1)
    raw = _raw([
        (JAN, "Export", "USD", 10.0), (JAN, "Import", "USD", 10.0), (feb, "Export", "EUR", 3.0),
        (feb, "Import", "N/A", 7.0), (feb, "Import", "JPY", 0.0),  # JPY a zero : ignore
    ])
    inventory = currency_inventory(raw)
    assert inventory == {"USD": 2, "EUR": 1, "N/A": 1}, inventory
    empty = pd.DataFrame(columns=["Month", "Trade Direction", "conversion_errors", "flag"])
    notes = " | ".join(dataset_notes(inventory, "TND", empty))
    assert "plusieurs devises : USD (2 mois/sens), EUR (1 mois/sens)" in notes, notes
    assert "devise manquante (N/A)" in notes and "aucune valeur en TND" in notes, notes
    print("OK  test_inventaire_et_notes_devises")


def test_periodes_saisies_a_la_main() -> None:
    """--month AAAA-MM -> un mois, --year AAAA -> 12 mois ; sans periode -> tout l'historique."""
    assert parse_period("2025-03") == [dt.date(2025, 3, 1)]
    assert parse_period("2025") == [dt.date(2025, m, 1) for m in range(1, 13)]
    assert parse_period(2025.0) == parse_period("2025"), "annee lue comme nombre dans Excel"
    assert parse_period(pd.Timestamp("2025-03-17")) == [dt.date(2025, 3, 1)], "date Excel -> son mois"
    try:
        parse_period("mars")
        raise AssertionError("periode invalide acceptee")
    except ValueError:
        pass
    sel = build_selection(["ds"], ["2024", "2025-01"], "export")
    assert len(sel["ds"]) == 13 and {d for _, d in sel["ds"]} == {"Export"}, sel
    assert build_selection(["ds"], [])["ds"] is None

    # --from/--to, pour 2 ou 3 datasets : meme intervalle pour chacun.
    sel = build_selection(["ds1", "ds2", "ds3"], [], None, "2025-01", "2025-06")
    assert set(sel) == {"ds1", "ds2", "ds3"} and all(len(v) == 6 * 2 for v in sel.values()), sel
    assert month_range("2024", "2025") == [dt.date(y, m, 1) for y in (2024, 2025) for m in range(1, 13)]
    assert month_range("2025-11", "2026-02") == [dt.date(2025, 11, 1), dt.date(2025, 12, 1), dt.date(2026, 1, 1), dt.date(2026, 2, 1)]
    for bad in (("2025-06", "2025-01"),):
        try:
            month_range(*bad)
            raise AssertionError(f"intervalle inverse accepte : {bad}")
        except ValueError:
            pass
    try:
        build_selection(["ds"], [], None, None, "2025-06")
        raise AssertionError("--to sans --from accepte")
    except ValueError:
        pass
    assert [month_number(v) for v in (3, "03", 3.0, "March", "mars", "Mar", "fevrier", "Février", "dec.")] == [3, 3, 3, 3, 3, 3, 2, 2, 12]
    print("OK  test_periodes_saisies_a_la_main")


def test_selection_par_fichier() -> None:
    """Fichier : un mois, une annee pour un sens, et une ligne sans mois (= tout l'historique)."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "liste.csv")
        pd.DataFrame({
            "dataset": ["ds-a", "ds-a", "ds-b", "ds-b"],
            "Month": ["2025-03", "2024", "", "2025-01"],
            "Trade direction": ["", "Import", "", "Export"],
        }).to_csv(path, index=False)
        sel = load_selection_file(path)
    assert sel["ds-b"] is None, "ligne sans mois : tout l'historique, meme si un mois est aussi demande"
    assert (dt.date(2025, 3, 1), "Export") in sel["ds-a"] and (dt.date(2025, 3, 1), "Import") in sel["ds-a"]
    assert (dt.date(2024, 7, 1), "Import") in sel["ds-a"] and (dt.date(2024, 7, 1), "Export") not in sel["ds-a"]
    assert len(sel["ds-a"]) == 2 + 12, sel["ds-a"]

    # Colonnes separees Year / Month (Excel), et intervalle From / To.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "liste.xlsx")
        pd.DataFrame({
            "Dataset code": ["ds-a", "ds-b", "ds-c"],
            "Year": [2025, 2024, None],
            "Month": [3, None, None],
            "From": [None, None, "2025-11"],
            "To": [None, None, "2026-01"],
        }).to_excel(path, index=False)
        sel = load_selection_file(path)
        assert {m for m, _ in sel["ds-a"]} == {dt.date(2025, 3, 1)}, sel["ds-a"]
        assert {m for m, _ in sel["ds-b"]} == {dt.date(2024, m, 1) for m in range(1, 13)}, "Year seul : annee complete"
        assert {m for m, _ in sel["ds-c"]} == {dt.date(2025, 11, 1), dt.date(2025, 12, 1), dt.date(2026, 1, 1)}, sel["ds-c"]

        pd.DataFrame({"Dataset": ["ds-a"], "Month": [3]}).to_excel(path, index=False)
        try:
            load_selection_file(path)
            raise AssertionError("mois sans annee accepte")
        except SystemExit as exc:
            assert "sans annee" in str(exc), exc
    print("OK  test_selection_par_fichier")


if __name__ == "__main__":
    compare_aggregated_sources.get_monthly_usd_rate = _fake_rate
    tests = [
        test_conversion_et_somme_par_mois,
        test_conversion_echouee_non_comparable,
        test_match_apres_conversion,
        test_comparaison_en_devise_locale,
        test_inventaire_et_notes_devises,
        test_periodes_saisies_a_la_main,
        test_selection_par_fichier,
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
