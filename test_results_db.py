"""Tests (script autonome, sans framework) pour results_db.py.

Les deux premiers tests n'ont besoin de rien ; le dernier ecrit une table temporaire dans tempdb de
l'instance SQL Server locale (results_db.SQL_SERVER) et la supprime, et est saute si l'instance est
injoignable.

Usage : python test_results_db.py
Sort avec le code 1 si une assertion echoue.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pandas as pd
import pyodbc

from results_db import _sql_type, connect, save_to_db, to_db_frame

RUN_AT = dt.datetime(2026, 11, 27, 10, 0, 0)


def test_ecarts_en_pourcentage_positif() -> None:
    df = pd.DataFrame([
        {"Dataset": "ds", "Month": dt.date(2026, 9, 1), "pct_diff": 0.105, "median_signed_gap": -0.2},
        {"Dataset": "ds", "Month": dt.date(2026, 8, 1), "pct_diff": None, "median_signed_gap": 0.05},
    ])
    out = to_db_frame(df, RUN_AT)
    assert list(out.columns)[0] == "run_at"
    assert out.loc[0, "pct_diff"] == 10.5 and out.loc[0, "median_signed_gap"] == 20.0, "Ecart signe stocke positif"
    assert pd.isna(out.loc[1, "pct_diff"]) and out.loc[1, "median_signed_gap"] == 5.0
    aggregated = to_db_frame(pd.DataFrame([{"pct_diff_value": -0.031, "pct_diff_quantity": 0.5, "value": 0.2}]), RUN_AT)
    assert aggregated.loc[0, "pct_diff_value"] == 3.1 and aggregated.loc[0, "pct_diff_quantity"] == 50.0
    assert aggregated.loc[0, "value"] == 0.2, "Seules les colonnes d'ecart sont converties"
    print("OK  test_ecarts_en_pourcentage_positif")


def test_types_sql() -> None:
    assert _sql_type(pd.Series([dt.date(2026, 9, 1), None])) == "DATE"
    assert _sql_type(pd.Series([RUN_AT])) == "DATETIME2(0)"
    assert _sql_type(pd.Series([1.5, None])) == "FLOAT"
    assert _sql_type(pd.Series([3, 4])) == "BIGINT"
    assert _sql_type(pd.Series(["MATCH", None])) == "NVARCHAR(MAX)"
    assert _sql_type(pd.Series([None, None])) == "NVARCHAR(MAX)"
    print("OK  test_types_sql")


def test_ecriture_sql_server() -> None:
    try:
        conn = connect("tempdb")
    except pyodbc.Error as exc:
        print(f"SKIP test_ecriture_sql_server (SQL Server injoignable : {str(exc)[:80]})")
        return
    table = f"test_results_{uuid.uuid4().hex[:8]}"
    try:
        save_to_db(pd.DataFrame([{"Dataset": "a", "Month": dt.date(2026, 9, 1), "pct_diff": 0.105}]), table, RUN_AT, conn)
        later = RUN_AT + dt.timedelta(hours=1)
        save_to_db(pd.DataFrame([{"Dataset": "b", "Month": dt.date(2026, 8, 1), "pct_diff": float("nan"), "flag": "x"}]), table, later, conn)
        assert save_to_db(pd.DataFrame(), table, later, conn) == 0, "Resultat vide : rien d'ecrit"
        # Colonne texte aux valeurs mixtes (nombre et libelle) : ecrite en texte.
        mixed = pd.DataFrame([{"Dataset": "c", "Month": dt.date(2026, 7, 1), "pct_diff": 0.0, "flag": 3}, {"Dataset": "d", "flag": "y"}])
        save_to_db(mixed, table, later + dt.timedelta(hours=1), conn)
        rows = conn.execute(f"SELECT run_at, Dataset, Month, pct_diff, flag FROM dbo.[{table}] ORDER BY run_at, Dataset").fetchall()
        assert [tuple(r) for r in rows][:2] == [
            (RUN_AT, "a", dt.date(2026, 9, 1), 10.5, None),
            (later, "b", dt.date(2026, 8, 1), None, "x"),
        ], rows
        assert [r.flag for r in rows[2:]] == ["3", "y"], rows
    finally:
        conn.execute(f"DROP TABLE IF EXISTS dbo.[{table}]")
        conn.commit()
        conn.close()
    print("OK  test_ecriture_sql_server")


if __name__ == "__main__":
    tests = [test_ecarts_en_pourcentage_positif, test_types_sql, test_ecriture_sql_server]
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
