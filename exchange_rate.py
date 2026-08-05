"""Calcul du taux de change mensuel moyen (devise -> USD) via l'API Frankfurter."""

from __future__ import annotations

import calendar
from datetime import date
from functools import lru_cache

import pandas as pd
import requests

FRANKFURTER_BASE_URL = "https://api.frankfurter.app"


class ExchangeRateError(Exception):
    """Erreur levée lorsque le taux de change mensuel ne peut pas être calculé."""


@lru_cache(maxsize=None)
def get_monthly_average_rate(
    currency: str, year: int, month: int, timeout: int = 10
) -> dict:
    """Calcule le taux de change mensuel moyen d'une devise vers USD.

    Récupère les taux journaliers disponibles auprès de l'API Frankfurter
    pour tous les jours ouvrables (lundi-vendredi) du mois donné, puis
    calcule la moyenne arithmétique de ces taux.

    Args:
        currency: code ISO de la devise source (ex: "EUR", "TRY", "JPY").
        year: année (ex: 2010).
        month: mois entre 1 et 12 (ex: 1 pour janvier).
        timeout: délai max en secondes pour la requête HTTP.

    Returns:
        Un dict avec les clés:
            - "rate": taux de change moyen (float)
            - "days_used": nombre de jours ouvrables utilisés dans le calcul (int)
            - "dates": liste des dates (str "YYYY-MM-DD") effectivement utilisées

    Raises:
        ExchangeRateError: si la devise est invalide, si l'API est
            injoignable, ou si aucun taux n'a pu être récupéré pour le mois.

    Note:
        Le résultat est mis en cache (par currency/year/month/timeout) via
        `lru_cache` : les appels répétés pour la même devise et le même mois
        ne déclenchent pas de nouvelle requête HTTP. Les appels qui lèvent
        une exception ne sont pas mis en cache et seront retentés.
    """
    currency = currency.upper().strip()

    business_days = pd.bdate_range(
        start=date(year, month, 1),
        end=date(year, month, calendar.monthrange(year, month)[1]),
    )
    if len(business_days) == 0:
        raise ExchangeRateError(f"Aucun jour ouvrable trouvé pour {year}-{month:02d}.")

    business_day_strs = {d.strftime("%Y-%m-%d") for d in business_days}
    start_str = business_days[0].strftime("%Y-%m-%d")
    end_str = business_days[-1].strftime("%Y-%m-%d")

    url = f"{FRANKFURTER_BASE_URL}/{start_str}..{end_str}"
    params = {"from": currency, "to": "USD"}

    try:
        response = requests.get(url, params=params, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise ExchangeRateError(
            f"Impossible de contacter l'API Frankfurter : {exc}"
        ) from exc

    if response.status_code == 404:
        raise ExchangeRateError(
            f"Devise invalide ou inconnue pour Frankfurter : '{currency}'."
        )
    if not response.ok:
        raise ExchangeRateError(
            f"L'API Frankfurter a renvoyé une erreur HTTP {response.status_code} : "
            f"{response.text}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ExchangeRateError(
            "Réponse de l'API Frankfurter illisible (JSON invalide)."
        ) from exc

    rates_by_date = payload.get("rates", {})
    if not rates_by_date:
        raise ExchangeRateError(
            f"Aucun taux disponible pour {currency}->USD sur {year}-{month:02d}."
        )

    daily_rates = {}
    for day_str, day_rates in rates_by_date.items():
        if day_str not in business_day_strs:
            continue
        usd_rate = day_rates.get("USD")
        if usd_rate is not None:
            daily_rates[day_str] = usd_rate

    if not daily_rates:
        raise ExchangeRateError(
            f"Aucun taux exploitable pour {currency}->USD sur {year}-{month:02d} "
            "(jours fériés ou données manquantes pour tout le mois)."
        )

    rates_series = pd.Series(daily_rates)
    average_rate = float(rates_series.mean())
    used_dates = sorted(daily_rates.keys())

    return {
        "rate": average_rate,
        "days_used": len(used_dates),
        "dates": used_dates,
    }


if __name__ == "__main__":
    result = get_monthly_average_rate("EUR", 2010, 1)
    print(f"Taux moyen EUR->USD (2010-01) : {result['rate']:.5f}")
    print(f"Jours utilisés : {result['days_used']}")
    print(f"Dates : {result['dates']}")
