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
    te = pd.DataFrame([
        {"Month": SEP.date(), "Direction": "Export", "value_usd": 87_222_200_000.0},
        {"Month": SEP.date(), "Direction": "Import", "value_usd": 60.6e9},
    ])
    checks = _checks([(SEP, "Export"), (SEP, "Import"), (AUG, "Export")])
    result = compare_checks(checks, clustered, te).set_index(["Month", "Trade Direction"])
    assert len(result) == 3, "Seulement les (mois, direction) demandes"

    sep_export = result.loc[(SEP.date(), "Export")]
    assert sep_export["status"] == "MISMATCH" and round(sep_export["ratio_te_clustered"]) == 1000, "Ecart d'echelle x1000 visible"
    assert result.loc[(SEP.date(), "Import"), "status"] == "MATCH", "1 % d'ecart : dans la tolerance de 3 %"
    assert result.loc[(AUG.date(), "Export"), "reason"] == "absent_clustered"
    print("OK  test_verifications")


def test_mois_absent_te() -> None:
    clustered = pd.DataFrame([{"Month": SEP, "Trade Direction": "Export", "Total Value": 1.0}])
    te = pd.DataFrame(columns=["Month", "Direction", "value_usd"])
    row = compare_checks(_checks([(SEP, "Export")]), clustered, te).iloc[0]
    assert row["status"] == "NON_COMPARABLE" and row["reason"] == "absent_te"
    print("OK  test_mois_absent_te")


if __name__ == "__main__":
    tests = [test_format_mois, test_lecture_excel, test_verifications, test_mois_absent_te]
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
