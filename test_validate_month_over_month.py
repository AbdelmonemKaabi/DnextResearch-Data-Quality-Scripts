"""Tests (script autonome, sans framework, sans reseau) pour validate_month_over_month.py.

Usage : python test_validate_month_over_month.py
Sort avec le code 1 si une assertion echoue.
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd

from validate_month_over_month import (
    FAIL,
    NOT_ENOUGH_DATA,
    PASS,
    REFERENCE_ZERO,
    keep_last_months,
    load_config,
    validate_dataset,
    validate_series,
)


def _series(points: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame({"Month": [f"{m}-01" for m in points], "Value": list(points.values())})


def test_exemple_de_la_specification() -> None:
    """Exemple du cahier des charges : Jan->Fev, Fev->Mar, Mar->Avr, Avr (dernier mois)->Mar."""
    result = validate_series(_series({"2026-01": 100_000, "2026-02": 103_000, "2026-03": 105_000, "2026-04": 150_000}), 0.05)
    expected = [
        ("2026-01", "2026-02", "-2.91%", PASS),
        ("2026-02", "2026-03", "-1.90%", PASS),
        ("2026-03", "2026-04", "-30.00%", FAIL),
        ("2026-04", "2026-03", "+42.86%", FAIL),
    ]
    got = list(result[["Month", "Reference month", "Difference", "Status"]].itertuples(index=False, name=None))
    assert got == expected, f"Attendu {expected}, obtenu {got}"
    print("OK  test_exemple_de_la_specification")


def test_pas_de_mois_adjacent() -> None:
    """Un mois seul, ou deux mois separes par un trou, n'ont pas de mois adjacent."""
    assert list(validate_series(_series({"2026-01": 10.0}), 0.05)["Status"]) == [NOT_ENOUGH_DATA]
    gap = validate_series(_series({"2026-01": 10.0, "2026-03": 10.0}), 0.05)
    assert list(gap["Status"]) == [NOT_ENOUGH_DATA, NOT_ENOUGH_DATA], f"Janvier et mars ne sont pas adjacents : {list(gap['Status'])}"
    print("OK  test_pas_de_mois_adjacent")


def test_mois_precedent_si_pas_de_suivant() -> None:
    """Avec un trou apres mars, mars se compare a fevrier (precedent), pas a mai."""
    result = validate_series(_series({"2026-02": 100.0, "2026-03": 104.0, "2026-05": 500.0}), 0.05)
    march = result[result["Month"] == "2026-03"].iloc[0]
    assert march["Reference month"] == "2026-02" and march["Status"] == PASS, march.to_dict()
    print("OK  test_mois_precedent_si_pas_de_suivant")


def test_valeur_de_reference_nulle() -> None:
    """Reference a 0 : PASS si la valeur vaut aussi 0, sinon REFERENCE ZERO (pas de division)."""
    result = validate_series(_series({"2026-01": 0.0, "2026-02": 0.0, "2026-03": 50.0, "2026-04": 0.0}), 0.05)
    statuses = dict(zip(result["Month"], result["Status"]))
    assert statuses["2026-01"] == PASS, "0 compare a 0 : aucune variation"
    assert statuses["2026-03"] == REFERENCE_ZERO, "50 compare a 0 : variation infinie, signalee a part"
    assert result.loc[result["Month"] == "2026-03", "Difference"].iloc[0] == ""
    print("OK  test_valeur_de_reference_nulle")


def test_seuil_inclusif_et_configurable() -> None:
    """Un ecart d'exactement 5 % passe, 5.01 % echoue ; avec un seuil de 10 %, 8 % passe."""
    assert validate_series(_series({"2026-01": 105.0, "2026-02": 100.0}), 0.05)["Status"].iloc[0] == PASS
    assert validate_series(_series({"2026-01": 105.01, "2026-02": 100.0}), 0.05)["Status"].iloc[0] == FAIL
    assert validate_series(_series({"2026-01": 108.0, "2026-02": 100.0}), 0.10)["Status"].iloc[0] == PASS
    print("OK  test_seuil_inclusif_et_configurable")


def test_une_serie_par_groupe() -> None:
    """Les series sont validees separement par combinaison group_by (pas de melange Export/Import)."""
    df = pd.DataFrame({
        "Reporter": ["FIN"] * 4, "TradeFlow": ["Export", "Export", "Import", "Import"],
        "Month": ["2026-01-01", "2026-02-01", "2026-01-01", "2026-02-01"], "Value": [100.0, 101.0, 100.0, 200.0],
    })
    result = validate_dataset(df, ["Reporter", "TradeFlow"], 0.05)
    by_flow = result.groupby("TradeFlow")["Status"].apply(list).to_dict()
    assert by_flow == {"Export": [PASS, PASS], "Import": [FAIL, FAIL]}, by_flow
    print("OK  test_une_serie_par_groupe")


def test_dernier_mois_seulement() -> None:
    """start_month = latest : une ligne par serie, son dernier mois, compare au mois precedent.
    Une serie arretee plus tot garde son propre dernier mois (visible, pas ignoree)."""
    df = pd.DataFrame({
        "Reporter": ["FIN"] * 5, "TradeFlow": ["Export"] * 3 + ["Import"] * 2,
        "Month": ["2026-06-01", "2026-07-01", "2026-08-01", "2025-01-01", "2025-02-01"],
        "Value": [100.0, 100.0, 120.0, 50.0, 51.0],
    })
    result = keep_last_months(validate_dataset(df, ["Reporter", "TradeFlow"], 0.05), ["Reporter", "TradeFlow"], 1)
    got = list(result[["TradeFlow", "Month", "Reference month", "Difference", "Status"]].itertuples(index=False, name=None))
    assert got == [("Export", "2026-08", "2026-07", "+20.00%", FAIL), ("Import", "2025-02", "2025-01", "+2.00%", PASS)], got
    print("OK  test_dernier_mois_seulement")


def test_n_derniers_mois() -> None:
    """start_month = 3 : les 3 derniers mois calendaires (juin-aout) ; un mois manquant dans la
    fenetre (juillet) ne la decale pas. Juin garde sa reference normale (juillet absent -> mai,
    hors fenetre mais lu quand meme)."""
    df = pd.DataFrame({
        "Reporter": ["FIN"] * 5, "TradeFlow": ["Export"] * 5,
        "Month": ["2026-03-01", "2026-04-01", "2026-05-01", "2026-06-01", "2026-08-01"],
        "Value": [100.0, 100.0, 100.0, 103.0, 300.0],
    })
    result = keep_last_months(validate_dataset(df, ["Reporter", "TradeFlow"], 0.05), ["Reporter", "TradeFlow"], 3)
    # Mois de reference absent : NaN apres concatenation pandas (vide dans le CSV), compare comme None.
    got = [(m, None if pd.isna(r) else r, s) for m, r, s in result[["Month", "Reference month", "Status"]].itertuples(index=False, name=None)]
    assert got == [("2026-06", "2026-05", PASS), ("2026-08", None, NOT_ENOUGH_DATA)], got
    print("OK  test_n_derniers_mois")


def test_configuration_validee() -> None:
    """threshold en fraction (0.05) ; 5 (= 5 % ecrit en pourcentage) et un group_by inconnu sont refuses."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cfg.yaml")

        def write(text: str) -> None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

        write("threshold: 0.08\ndatasets: ds-1\nstart_month: '2024-03'\n")
        cfg = load_config(path)
        assert cfg["threshold"] == 0.08 and cfg["datasets"] == ["ds-1"] and cfg["start_month"] == "2024-03", cfg
        assert cfg["group_by"] == ["Reporter", "TradeFlow"], "group_by par defaut attendu"

        assert cfg["last_months"] is None
        for text, expected in (("Latest", (None, 1)), ("18", (None, 18)), ("", (None, None))):
            write(f"start_month: {text}\n")
            cfg = load_config(path)
            assert (cfg["start_month"], cfg["last_months"]) == expected, f"start_month: {text!r} -> {cfg}"

        for bad in ("threshold: 5\n", "threshold: 0.05\ngroup_by: [Country]\n", "start_month: 'mars'\n", "start_month: 0\n"):
            write(bad)
            try:
                load_config(path)
                raise AssertionError(f"Configuration invalide acceptee : {bad!r}")
            except SystemExit:
                pass
    print("OK  test_configuration_validee")


if __name__ == "__main__":
    tests = [
        test_exemple_de_la_specification,
        test_pas_de_mois_adjacent,
        test_mois_precedent_si_pas_de_suivant,
        test_valeur_de_reference_nulle,
        test_seuil_inclusif_et_configurable,
        test_une_serie_par_groupe,
        test_dernier_mois_seulement,
        test_n_derniers_mois,
        test_configuration_validee,
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
