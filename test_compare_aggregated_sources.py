"""Tests (script autonome, sans framework) pour :
  - la regle d'exclusion silencieuse des lignes Snowflake en echec de conversion devise mais
    sans valeur reelle (voir _aggregate_snowflake dans compare_aggregated_sources.py) ;
  - le cas special MOP (pataca de Macao), converti via un peg fixe MOP->HKD (voir
    get_mop_to_usd_rate dans exchange_rate.py).

Usage : python test_compare_aggregated_sources.py
Sort avec le code 1 si une assertion echoue.

Note : test_get_mop_to_usd_rate_coherente fait un appel reseau reel vers l'API Frankfurter
(via HKD) et necessite donc un acces Internet ; les autres tests sont entierement locaux
(donnees synthetiques, get_mop_to_usd_rate mocke).
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pandas as pd

from compare_aggregated_sources import GROUP_KEYS, _aggregate_snowflake, compare_aggregated
from exchange_rate import get_mop_to_usd_rate

MONTH = pd.Timestamp("2026-01-01")


def _row(
    hs_chapter: str,
    currency: str,
    total_value: float | None,
    number_of_records: int = 1,
    quantity_unit: str = "KG",
    total_quantity: float = 100.0,
) -> dict:
    """Construit une ligne au format attendu par _aggregate_snowflake (sortie SNOWFLAKE_QUERY_TEMPLATE)."""
    return {
        "Month": MONTH,
        "Trade Direction": "Export",
        "HS Chapter": hs_chapter,
        "Quantity Unit": quantity_unit,
        "Quantity 2 Unit": "N/A",
        "Currency": currency,
        "Number of Records": number_of_records,
        "Total Quantity": total_quantity,
        "Total Quantity 2": 0.0,
        "Total Value": total_value,
    }


def _clustered_row(
    hs_chapter: str,
    total_value: float,
    number_of_records: int = 1,
    quantity_unit: str = "KG",
    total_quantity: float = 100.0,
) -> dict:
    """Construit une ligne au format attendu par _aggregate_clustered (sortie CLUSTERED_QUERY)."""
    return {
        "Month": MONTH,
        "Trade Direction": "Export",
        "HS Chapter": hs_chapter,
        "Quantity Unit": quantity_unit,
        "Quantity 2 Unit": "N/A",
        "Number of Records": number_of_records,
        "Total Quantity": total_quantity,
        "Total Quantity 2": 0.0,
        "Total Value": total_value,
    }


def _group(df: pd.DataFrame, hs_chapter: str) -> pd.Series:
    match = df[df["HS Chapter"] == hs_chapter]
    assert len(match) == 1, f"Groupe HS Chapter={hs_chapter!r} : attendu 1 ligne agregee, trouve {len(match)}"
    return match.iloc[0]


def test_devise_manquante_valeur_nulle_exclue_silencieusement() -> None:
    """Une ligne Currency='N/A' avec Total Value=0 ne doit pas contaminer le groupe :
    le reste du groupe (devise valide) doit rester comparable, et l'exclusion doit etre tracee.
    """
    df = pd.DataFrame(
        [
            _row(hs_chapter="08", currency="USD", total_value=1000.0, number_of_records=109),
            _row(hs_chapter="08", currency="N/A", total_value=0.0, number_of_records=1),
        ]
    )

    agg = _aggregate_snowflake(df)
    row = _group(agg, "08")

    assert not math.isnan(row["total_value_snowflake_usd"]), (
        "Le groupe devrait rester comparable (valeur non-NaN) malgre la ligne N/A a valeur nulle"
    )
    assert row["total_value_snowflake_usd"] == 1000.0, (
        f"Attendu 1000.0 (seule la ligne USD contribue), obtenu {row['total_value_snowflake_usd']}"
    )
    assert row["excluded_null_value_rows"] == 1, (
        f"1 enregistrement aurait du etre trace comme exclu silencieusement, obtenu {row['excluded_null_value_rows']}"
    )
    print("OK  test_devise_manquante_valeur_nulle_exclue_silencieusement")


def test_devise_manquante_valeur_reelle_contamine_le_groupe() -> None:
    """Une ligne Currency='N/A' avec une vraie valeur (non nulle) doit toujours faire echouer
    tout le groupe (comportement strict inchange), sans generer d'exclusion silencieuse.
    """
    df = pd.DataFrame(
        [
            _row(hs_chapter="09", currency="USD", total_value=1000.0, number_of_records=50),
            _row(hs_chapter="09", currency="N/A", total_value=500.0, number_of_records=3),
        ]
    )

    agg = _aggregate_snowflake(df)
    row = _group(agg, "09")

    assert math.isnan(row["total_value_snowflake_usd"]), (
        "Le groupe doit rester NON_COMPARABLE (NaN) : une vraie valeur avec devise inconnue "
        "ne peut pas etre ignoree sans fausser le total"
    )
    assert row["excluded_null_value_rows"] == 0, (
        f"Aucune ligne ne doit etre exclue silencieusement ici (la valeur N/A est reelle), "
        f"obtenu {row['excluded_null_value_rows']}"
    )
    print("OK  test_devise_manquante_valeur_reelle_contamine_le_groupe")


def test_groupes_independants() -> None:
    """Les deux groupes (HS 08 et HS 09) ci-dessus doivent etre traites independamment :
    verifie qu'on a bien 2 groupes distincts en sortie, sans interference entre eux.
    """
    df = pd.DataFrame(
        [
            _row(hs_chapter="08", currency="USD", total_value=1000.0, number_of_records=109),
            _row(hs_chapter="08", currency="N/A", total_value=0.0, number_of_records=1),
            _row(hs_chapter="09", currency="USD", total_value=1000.0, number_of_records=50),
            _row(hs_chapter="09", currency="N/A", total_value=500.0, number_of_records=3),
        ]
    )

    agg = _aggregate_snowflake(df)
    assert len(agg) == 2, f"Attendu 2 groupes (HS 08 et HS 09), obtenu {len(agg)}"
    assert set(GROUP_KEYS).issubset(agg.columns), "Colonnes de regroupement manquantes en sortie"
    print("OK  test_groupes_independants")


def test_quantity_zero_des_deux_cotes_est_un_match() -> None:
    """Quand Total Quantity vaut 0 des deux cotes (snowflake ET clustered) pour un groupe,
    c'est un accord parfait (0 = 0) : MATCH, pas NON_COMPARABLE (pas de division par zero
    a signaler ici puisqu'il n'y a aucun ecart reel entre les deux sources).
    """
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="11", currency="USD", total_value=1000.0, number_of_records=5, total_quantity=0.0)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="11", total_value=1000.0, number_of_records=5, total_quantity=0.0)]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    row = result[result["HS Chapter"] == "11"].iloc[0]

    assert row["status_quantity"] == "MATCH", (
        f"Attendu MATCH (0 = 0 des deux cotes), obtenu status={row['status_quantity']!r} "
        f"reason={row['reason_quantity']!r}"
    )
    assert row["pct_diff_quantity"] == 0.0, f"Attendu pct_diff=0.0, obtenu {row['pct_diff_quantity']}"
    assert row["reason_quantity"] == "", f"reason_quantity devrait etre vide, obtenu {row['reason_quantity']!r}"
    print("OK  test_quantity_zero_des_deux_cotes_est_un_match")


def test_quantity_zero_dun_seul_cote_reste_non_comparable() -> None:
    """Regression : quand seul le total clustered est a 0 (snowflake != 0), le comportement
    d'origine doit etre preserve : NON_COMPARABLE (division par zero non sensee), pas MATCH.
    """
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="12", currency="USD", total_value=1000.0, number_of_records=5, total_quantity=100.0)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="12", total_value=1000.0, number_of_records=5, total_quantity=0.0)]
    )

    result = compare_aggregated(df_snowflake, df_clustered)
    row = result[result["HS Chapter"] == "12"].iloc[0]

    assert row["status_quantity"] == "NON_COMPARABLE", (
        f"Attendu NON_COMPARABLE (clustered=0, snowflake!=0), obtenu status={row['status_quantity']!r}"
    )
    assert row["reason_quantity"] == "clustered_quantity_zero", (
        f"Attendu reason='clustered_quantity_zero', obtenu {row['reason_quantity']!r}"
    )
    print("OK  test_quantity_zero_dun_seul_cote_reste_non_comparable")


def test_get_mop_to_usd_rate_coherente() -> None:
    """Appel reseau reel (Frankfurter, via HKD) : verifie que get_mop_to_usd_rate(2026, 1)
    renvoie un taux MOP->USD coherent (MOP/USD tourne autour de 8, donc rate ~ 1/8 = 0.125)
    et un resultat structure incluant le chemin de peg utilise. Necessite un acces Internet.
    """
    result = get_mop_to_usd_rate(2026, 1)

    assert 0.10 <= result["rate"] <= 0.14, (
        f"Taux MOP->USD hors fourchette attendue (~1/8 = 0.125) : {result['rate']}"
    )
    assert result["days_used"] > 0, "Aucun jour ouvrable utilise pour le calcul du taux"
    assert result["via_peg"] == "MOP->HKD fixed, HKD->USD Frankfurter", (
        f"Chemin de peg inattendu : {result['via_peg']!r}"
    )
    print(f"OK  test_get_mop_to_usd_rate_coherente (rate={result['rate']:.5f})")


def test_conversion_mop_passe_de_non_comparable_a_match() -> None:
    """Cas synthetique : une ligne Snowflake en MOP avec une valeur reelle doit maintenant se
    convertir (via le peg MOP->HKD) et comparer normalement, au lieu de tomber en
    NON_COMPARABLE avec "conversion_echouee: MOP: Devise invalide ou inconnue pour
    Frankfurter". get_mop_to_usd_rate est mocke pour ne pas dependre du reseau ici.
    """
    df_snowflake = pd.DataFrame(
        [_row(hs_chapter="10", currency="MOP", total_value=800.0, number_of_records=20)]
    )
    df_clustered = pd.DataFrame(
        [_clustered_row(hs_chapter="10", total_value=100.0, number_of_records=20)]
    )

    mocked_rate = {
        "rate": 0.125,  # ~ 1 MOP = 0.125 USD (MOP/USD ~ 8)
        "days_used": 20,
        "dates": [],
        "via_peg": "MOP->HKD fixed, HKD->USD Frankfurter",
    }
    with patch("compare_aggregated_sources.get_mop_to_usd_rate", return_value=mocked_rate) as mocked:
        result = compare_aggregated(df_snowflake, df_clustered)
        mocked.assert_called_once_with(2026, 1)

    row = result[result["HS Chapter"] == "10"].iloc[0]

    assert row["status_value"] == "MATCH", (
        f"Attendu MATCH (800 MOP * 0.125 = 100 USD = clustered), obtenu "
        f"status={row['status_value']!r} reason={row['reason_value']!r}"
    )
    assert row["reason_value"] == "", f"reason_value devrait etre vide, obtenu {row['reason_value']!r}"
    assert row["total_value_snowflake_usd"] == 100.0, (
        f"Attendu 100.0 USD, obtenu {row['total_value_snowflake_usd']}"
    )
    print("OK  test_conversion_mop_passe_de_non_comparable_a_match")


if __name__ == "__main__":
    tests = [
        test_devise_manquante_valeur_nulle_exclue_silencieusement,
        test_devise_manquante_valeur_reelle_contamine_le_groupe,
        test_groupes_independants,
        test_conversion_mop_passe_de_non_comparable_a_match,
        test_quantity_zero_des_deux_cotes_est_un_match,
        test_quantity_zero_dun_seul_cote_reste_non_comparable,
        test_get_mop_to_usd_rate_coherente,
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
