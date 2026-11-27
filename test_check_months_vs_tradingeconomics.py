"""Tests (script autonome, sans framework, sans reseau) pour check_months_vs_tradingeconomics.py.

Usage : python test_check_months_vs_tradingeconomics.py
Sort avec le code 1 si une assertion echoue.
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd

from check_months_vs_tradingeconomics import compare_checks, load_checks, parse_month

SEP, AUG = pd.Timestamp("2026-09-01"), pd.Timestamp("2026-08-01")
DS = "dnexr-cpt-all-twn-tf-customs-bilateral-monthly"


def _variant(points: dict, condition: str = "Export", source: str = "taiwan/exports") -> dict:
    """Variante TE : {mois: valeur USD}."""
    series = pd.DataFrame({"Month": list(points), "value_usd": list(points.values())}, columns=["Month", "value_usd"])
    return {"source": source, "condition": condition, "series": series}


def _checks(rows: list[tuple[pd.Timestamp, str]]) -> pd.DataFrame:
    return pd.DataFrame([{"Dataset": DS, "Month": m, "Trade Direction": d} for m, d in rows])


def test_format_mois() -> None:
    assert parse_month("2026-09") == SEP and parse_month("2026-09-01") == SEP
    assert parse_month("2026-09-01 00:00:00") == SEP, "Date Excel lue en texte"
    print("OK  test_format_mois")


def test_lecture_excel() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "checks.xlsx")
        pd.DataFrame({
            "Dataset": [DS, DS, DS, None],
            "Month": ["2026-09", "2026-09", "2026-08", None],
            "Trade direction": ["Export", "export", "Import", None],
        }).to_excel(path, index=False)
        checks = load_checks(path)
        assert list(checks.columns) == ["Dataset", "Month", "Trade Direction"]
        assert len(checks) == 2, "Ligne vide ignoree, doublon (casse de la direction) retire"
        assert checks.loc[0, "Month"] == SEP and checks.loc[0, "Trade Direction"] == "Export"

        pd.DataFrame({"Dataset": [DS], "Month": ["2026-09"]}).to_excel(path, index=False)
        try:
            load_checks(path)
        except ValueError:
            pass
        else:
            raise AssertionError("Colonne 'Trade direction' manquante : doit lever ValueError")
    print("OK  test_lecture_excel")


def test_verifications() -> None:
    clustered = pd.DataFrame([
        {"Month": SEP, "Trade Direction": "Export", "Total Value": 87_190_850.0},
        {"Month": SEP, "Trade Direction": "Import", "Total Value": 60e9},
    ])
    variants = {
        "Export": [_variant({SEP: 87_222_200_000.0})],
        "Import": [_variant({SEP: 60.6e9}, "Import", "taiwan/imports")],
    }
    checks = _checks([(SEP, "Export"), (SEP, "Import"), (AUG, "Export")])
    result = compare_checks(checks, clustered, variants).set_index(["Month", "Trade Direction"])
    assert len(result) == 3, "Seulement les (mois, direction) demandes"

    sep_export = result.loc[(SEP.date(), "Export")]
    assert sep_export["status"] == "MISMATCH" and round(sep_export["ratio_te_clustered"]) == 1000, "Ecart d'echelle x1000 visible"
    assert result.loc[(SEP.date(), "Import"), "status"] == "MATCH", "1 % d'ecart : dans la tolerance de 3 %"
    assert result.loc[(AUG.date(), "Export"), "reason"] == "absent_clustered"
    assert result.loc[(SEP.date(), "Import"), "te_source"] == "taiwan/imports"
    print("OK  test_verifications")


def test_mois_absent_te() -> None:
    clustered = pd.DataFrame([{"Month": SEP, "Trade Direction": "Export", "Total Value": 1.0}])
    row = compare_checks(_checks([(SEP, "Export")]), clustered, {"Export": [_variant({})]}).iloc[0]
    assert row["status"] == "NON_COMPARABLE" and row["reason"] == "absent_te"
    print("OK  test_mois_absent_te")


def test_variantes_arabie_saoudite() -> None:
    # Clustered 8,55 Md : "exports" (avec petrole) et "non-oil-exports" (sans re-exportations) ne
    # correspondent pas, "exports - oil-exports" (non-petrole avec re-exportations) oui.
    clustered = pd.DataFrame([{"Month": SEP, "Trade Direction": "Export", "Total Value": 8.55e9}])
    variants = {"Export": [
        _variant({SEP: 26.98e9}, "Export", "saudi-arabia/exports"),
        _variant({SEP: 5.09e9}, "Export non oil petroleum (hors re-exportations)", "saudi-arabia/non-oil-exports"),
        _variant({SEP: 8.48e9}, "Export non oil petroleum (avec re-exportations)",
                 "saudi-arabia/exports - saudi-arabia/oil-exports"),
    ]}
    row = compare_checks(_checks([(SEP, "Export")]), clustered, variants).iloc[0]
    assert row["status"] == "MATCH", row.to_dict()
    assert row["te_source"] == "saudi-arabia/exports - saudi-arabia/oil-exports"
    assert row["te_condition"] == "Export non oil petroleum (avec re-exportations)"

    # Aucune variante ne correspond : resultat de base garde, variantes listees dans reason.
    variants["Export"][2] = _variant({SEP: 1e9}, "Export non oil petroleum (avec re-exportations)")
    row = compare_checks(_checks([(SEP, "Export")]), clustered, variants).iloc[0]
    assert row["status"] == "MISMATCH" and row["te_source"] == "saudi-arabia/exports"
    assert row["reason"].startswith("aucune variante ne correspond"), row["reason"]
    print("OK  test_variantes_arabie_saoudite")


if __name__ == "__main__":
    tests = [test_format_mois, test_lecture_excel, test_verifications, test_mois_absent_te, test_variantes_arabie_saoudite]
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
