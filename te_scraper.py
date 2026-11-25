"""
Trading Economics - Combined scraper
=====================================

Merges two scrapers for TradingEconomics indicator pages (e.g.
"Balance of Trade", "Exports", "Imports"):

    https://tradingeconomics.com/tunisia/balance-of-trade
    https://tradingeconomics.com/germany/exports
    https://tradingeconomics.com/united-states/imports

1) TABLE scraper (requests + BeautifulSoup)
   Pulls the "Latest" summary table row(s) -- actual, previous, unit, date --
   for Balance of Trade / Exports / Imports. Fast, no browser needed.

2) CHART scraper (Selenium + Highcharts)
   Pulls the FULL historical time series straight out of the live
   Highcharts JS object rendered on the page. Needs a real headless
   Chrome browser.

The two scrapers are independent -- you can run either one, or both.

Saving to Excel is kept SEPARATE for each: the table data goes to its own
workbook, and the chart history goes to its own workbook (each has its own
save_*_to_excel function and its own output filename).

Requirements
------------
    pip install requests beautifulsoup4 openpyxl selenium webdriver-manager

You also need Google Chrome installed locally for the chart scraper.
webdriver-manager downloads the matching chromedriver automatically.

Usage
-----
    # Table only
    python te_scraper.py tunisia --mode table

    # Chart only (note: page_path form, e.g. "tunisia/balance-of-trade")
    python te_scraper.py tunisia/balance-of-trade --mode chart

    # Both (default) -- writes two separate .xlsx files
    python te_scraper.py tunisia --mode both

Notes / caveats
----------------
- The table is static server-rendered HTML, so requests + BeautifulSoup
  works fine for it.
- The chart is drawn client-side with Highcharts, so its full history is
  only reliably readable via a real (or headless) browser executing JS
  against the live chart object -- there's no clean way to parse it out
  of the raw HTML.
- TradingEconomics may rate-limit or block scripted requests without a
  realistic User-Agent, and their Terms of Service restrict automated
  scraping for redistribution -- use this for personal/analysis purposes
  and check their ToS / consider their official API for production use.
"""

import argparse
import datetime as dt
import os
import re
import time

import requests
from bs4 import BeautifulSoup

from exchange_rate import ExchangeRateError, get_monthly_usd_rate, get_mop_to_usd_rate

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# Map of the indicators we want to pull off the summary table, keyed by the
# text that appears in the <a> tag / URL slug for that row.
INDICATORS = {
    "balance-of-trade": "Balance of Trade",
    "exports": "Exports",
    "imports": "Imports",
}


# ---------------------------------------------------------------------------
# 1) TABLE SCRAPER (requests + BeautifulSoup)
# ---------------------------------------------------------------------------


def get_balance_of_trade(country_slug: str) -> dict:
    """
    country_slug: the part of the URL after tradingeconomics.com/, e.g.
                  'tunisia', 'germany', 'united-states'
    Returns a dict like:
    {
      "Balance of Trade": {"actual": -2886.70, "previous": -2296.20,
                            "unit": "TND Million", "date": "May 2026",
                            "month": "May", "year": 2026},
      "Exports": {...},
      "Imports": {...},
    }
    """
    url = f"https://tradingeconomics.com/{country_slug}/balance-of-trade"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results = {}

    rows = soup.select("tr.datatable-row, tr.datatable-row-alternating")
    for row in rows:
        link = row.select_one("td a")
        if not link:
            continue

        href = link.get("href", "")
        slug_match = re.search(r"/([^/]+)$", href.rstrip("/"))
        row_slug = slug_match.group(1) if slug_match else ""

        if row_slug not in INDICATORS:
            continue

        cells = row.find_all("td")
        # Expected layout: [name, actual, previous, unit, date]
        if len(cells) < 5:
            continue

        actual = cells[1].get_text(strip=True)
        previous = cells[2].get_text(strip=True)
        unit = cells[3].get_text(strip=True)
        date_str = cells[4].get_text(strip=True)

        month, year = None, None
        date_match = re.match(r"([A-Za-z]+)\s+(\d{4})", date_str)
        if date_match:
            month, year = date_match.group(1), int(date_match.group(2))

        def to_num(x):
            try:
                return float(x.replace(",", ""))
            except ValueError:
                return x

        results[INDICATORS[row_slug]] = {
            "actual": to_num(actual),
            "previous": to_num(previous),
            "unit": unit,
            "date": date_str,
            "month": month,
            "year": year,
        }

    return results


def save_table_to_excel(data: dict, country_slug: str, path: str = None) -> str:
    """
    Writes the scraped TABLE indicators to their own .xlsx file.
    One row per indicator: Country, Indicator, Actual, Previous, Unit, Month, Year, Date.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    if path is None:
        path = f"{country_slug}_balance_of_trade_table.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "Balance of Trade"

    headers = ["Country", "Indicator", "Actual", "Previous", "Unit", "Month", "Year", "Date"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    for indicator, values in data.items():
        ws.append([
            country_slug.replace("-", " ").title(),
            indicator,
            values["actual"],
            values["previous"],
            values["unit"],
            values["month"],
            values["year"],
            values["date"],
        ])

    for col, width in zip("ABCDEFGH", [16, 18, 12, 12, 14, 10, 8, 12]):
        ws.column_dimensions[col].width = width

    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# 2) CHART SCRAPER (Selenium + Highcharts)
# ---------------------------------------------------------------------------


def scrape_highcharts_series(page_path: str, headless: bool = True, wait_seconds: float = 4.0):
    """
    page_path: the part of the URL after tradingeconomics.com/, e.g.
               'tunisia/balance-of-trade'
    Returns a list of (date, value) tuples, sorted chronologically.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager

    url = f"https://tradingeconomics.com/{page_path}"

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    try:
        driver.get(url)
        time.sleep(wait_seconds)  # let the chart finish rendering/loading its data

        # Click the "10Y" range button (the 1Y/3Y/5Y/10Y/MAX selector shown
        # above the chart). 10Y is the longest range available without a
        # TradingEconomics subscription -- "MAX" is a subscriber-only
        # feature and won't return more data for anonymous requests.
        try:
            ten_y_button = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((
                    By.XPATH,
                    "//*[translate(normalize-space(string(.)), "
                    "'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')='10Y' "
                    "and not(.//*[translate(normalize-space(string(.)), "
                    "'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')='10Y'])]"
                ))
            )
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", ten_y_button
            )
            driver.execute_script("arguments[0].click();", ten_y_button)
            time.sleep(wait_seconds)  # let the chart re-render with the 10Y range
        except Exception:
            # If the 10Y button can't be found/clicked, fall back to whatever
            # range was already loaded rather than failing the whole scrape.
            pass

        raw = driver.execute_script(
            """
            if (typeof Highcharts === 'undefined' || !Highcharts.charts.length) {
                return null;
            }
            const chart = Highcharts.charts.find(c => c && c.series && c.series.length);
            if (!chart) return null;
            return chart.series[0].data.map(p => [p.x, p.y]);
            """
        )
    finally:
        driver.quit()

    if not raw:
        raise RuntimeError(
            "Could not read Highcharts series data -- the chart may not have "
            "finished loading (try increasing wait_seconds) or the page "
            "structure has changed."
        )

    series = []
    for x_ms, y in raw:
        date = dt.datetime.fromtimestamp(x_ms / 1000, dt.timezone.utc).date()
        series.append((date, y))

    series.sort(key=lambda p: p[0])
    return series


def save_chart_to_excel(series, page_path: str, path: str = None) -> str:
    """
    Writes the scraped CHART history to its own .xlsx file, separate from
    the table workbook.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment

    if path is None:
        safe_name = page_path.replace("/", "_")
        path = f"{safe_name}_chart_history.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "History"

    ws.append(["Date", "Month", "Year", "Value"])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    for date, value in series:
        ws.append([date.isoformat(), date.strftime("%b"), date.year, value])

    for col, width in zip("ABCD", [12, 8, 8, 14]):
        ws.column_dimensions[col].width = width

    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# 3) UNIT PARSING (currency + scale) -- pure logic, no network
# ---------------------------------------------------------------------------

# Multiplicateurs d'echelle Trading Economics -> valeur reelle dans la devise d'origine.
SCALE_MULTIPLIERS = {
    "THOUSAND": 1e3,
    "MILLION": 1e6,
    "BILLION": 1e9,
}


def _parse_unit(unit: str) -> tuple:
    """Decompose une unite Trading Economics (ex: "AUD Million") en (devise, multiplicateur).

    Attend le format "<CODE_DEVISE> <ECHELLE>" (echelle insensible a la casse, doit etre dans
    SCALE_MULTIPLIERS). Leve ValueError si le format ne correspond pas -- pas de fusion
    silencieuse d'un format d'unite inattendu.
    """
    parts = unit.strip().split()
    if len(parts) != 2:
        raise ValueError(f"Format d'unite Trading Economics inattendu : {unit!r}")

    currency, scale = parts
    scale_key = scale.upper()
    if scale_key not in SCALE_MULTIPLIERS:
        raise ValueError(f"Echelle inconnue dans l'unite {unit!r} : {scale!r}")

    return currency.upper(), SCALE_MULTIPLIERS[scale_key]


# ---------------------------------------------------------------------------
# 4) CURRENCY CONVERSION -- reutilise exchange_rate.py, pas de logique dupliquee
# ---------------------------------------------------------------------------


def _convert_to_usd(value: float, currency: str, year: int, month: int) -> float:
    """Convertit `value` (dans `currency`, deja mise a l'echelle reelle) en USD.

    Reutilise exchange_rate.py -- memes fonctions que compare_aggregated_sources.py
    (_convert_snowflake_row_to_usd), aucune logique de change dupliquee ici. MOP (pataca de
    Macao) traite via le peg fixe MOP->HKD (get_mop_to_usd_rate), comme cote Snowflake.

    Toute autre devise passe par get_monthly_usd_rate : parite fixe USD, sinon Frankfurter, sinon
    currency-api -- meme chaine de sources que compare_aggregated_sources.py.

    Leve ExchangeRateError si aucune source n'a de taux (API injoignable...) -- laissee a
    l'appelant, qui decide comment gerer un mois en echec.
    """
    currency = currency.upper().strip()
    if currency == "USD":
        return value

    if currency == "MOP":
        rate_info = get_mop_to_usd_rate(year, month)
    else:
        rate_info = get_monthly_usd_rate(currency, year, month)

    return value * rate_info["rate"]


# ---------------------------------------------------------------------------
# 5) & 6) PER-COUNTRY SERIES + MULTI-COUNTRY ORCHESTRATION
# ---------------------------------------------------------------------------

# Delai (secondes) entre deux pays traites dans build_comparison_dataframe, pour rester
# respectueux du site quand plusieurs pays sont scrapes a la suite.
SCRAPE_DELAY_SECONDS = 5


def scrape_country_trade_series(country_slug: str, wait_seconds: float = 4.0):
    """Combine table (unite Export/Import) et graphique (historique complet) pour un pays.

    Renvoie un pandas DataFrame avec les colonnes :
    Country, Month, Direction, value_original, currency, value_usd.

    value_original = valeur brute du graphique * multiplicateur d'echelle (donc en devise
    d'origine, unites reelles -- pas encore convertie USD). value_usd = value_original convertie
    via _convert_to_usd ; NaN pour un point dont la conversion a echoue (voir ci-dessous), avec
    un avertissement affiche plutot qu'une exception qui interromprait tout le pays.

    Hypothese : l'unite (devise + echelle) d'un indicateur ne change pas dans le temps pour un
    pays donne -- on l'applique donc a toute la serie historique du graphique. A revoir si un
    jour un pays presente un changement d'unite en cours d'historique.
    """
    import pandas as pd

    table_data = get_balance_of_trade(country_slug)

    rows = []
    for direction, indicator_name in (("Export", "Exports"), ("Import", "Imports")):
        indicator = table_data.get(indicator_name)
        if indicator is None or not indicator.get("unit"):
            print(f"[AVERTISSEMENT] {country_slug} : unite introuvable pour {indicator_name}, pays ignore pour cette direction.")
            continue

        try:
            currency, scale = _parse_unit(indicator["unit"])
        except ValueError as exc:
            print(f"[AVERTISSEMENT] {country_slug}/{indicator_name} : {exc} -- direction ignoree.")
            continue

        page_path = f"{country_slug}/{indicator_name.lower()}"
        series = scrape_highcharts_series(page_path, wait_seconds=wait_seconds)

        for date, raw_value in series:
            month = date.replace(day=1)
            value_original = raw_value * scale
            try:
                value_usd = _convert_to_usd(value_original, currency, month.year, month.month)
            except ExchangeRateError as exc:
                print(f"[AVERTISSEMENT] {country_slug} {direction} {month} : conversion USD echouee ({exc}), value_usd=NaN.")
                value_usd = float("nan")

            rows.append({
                "Country": country_slug,
                "Month": month,
                "Direction": direction,
                "value_original": value_original,
                "currency": currency,
                "value_usd": value_usd,
            })

    return pd.DataFrame(rows, columns=["Country", "Month", "Direction", "value_original", "currency", "value_usd"])


def build_comparison_dataframe(country_slugs, wait_seconds: float = 4.0):
    """Boucle sur `country_slugs`, appelle scrape_country_trade_series pour chacun (avec un
    delai SCRAPE_DELAY_SECONDS entre pays), concatene le tout en un seul DataFrame.

    Un pays en echec (table ou graphique indisponible) est logue et ignore, sans interrompre le
    traitement des pays suivants -- meme logique defensive que la boucle datasets dans
    compare_aggregated_sources.py.
    """
    import pandas as pd

    all_results = []
    for i, country_slug in enumerate(country_slugs):
        print(f"\n########## Trading Economics : {country_slug} ##########")
        try:
            country_df = scrape_country_trade_series(country_slug, wait_seconds=wait_seconds)
            all_results.append(country_df)
        except Exception as exc:
            print(f"[ERREUR] {country_slug} ignore : {exc}")

        if i < len(country_slugs) - 1:
            time.sleep(SCRAPE_DELAY_SECONDS)

    if not all_results:
        return pd.DataFrame(columns=["Country", "Month", "Direction", "value_original", "currency", "value_usd"])

    return pd.concat(all_results, ignore_index=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Scrape TradingEconomics table data and/or Highcharts history, "
            "saving each to its own separate .xlsx file."
        )
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="united-states",
        help=(
            "For --mode table: country slug, e.g. tunisia, germany. "
            "For --mode chart or both: page path, e.g. tunisia/balance-of-trade "
            "(if only a country slug is given, '/balance-of-trade' is appended)."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["table", "chart", "both"],
        default="both",
        help="Which scraper(s) to run (default: both).",
    )
    parser.add_argument("--table-output", default=None, help="Output .xlsx path for the table data.")
    parser.add_argument("--chart-output", default=None, help="Output .xlsx path for the chart history.")
    parser.add_argument("--wait", type=float, default=4.0, help="Seconds to wait for chart to load (chart mode only).")
    args, _unknown = parser.parse_known_args()

    # Normalize target into a country_slug (for table) and page_path (for chart)
    if "/" in args.target:
        page_path = args.target
        country_slug = args.target.split("/")[0]
    else:
        country_slug = args.target
        page_path = f"{args.target}/balance-of-trade"

    if args.mode in ("table", "both"):
        table_data = get_balance_of_trade(country_slug)
        table_path = save_table_to_excel(table_data, country_slug, args.table_output)
        print(f"Table saved: {os.path.abspath(table_path)}")

    if args.mode in ("chart", "both"):
        chart_series = scrape_highcharts_series(page_path, wait_seconds=args.wait)
        chart_path = save_chart_to_excel(chart_series, page_path, args.chart_output)
        print(f"Chart history saved ({len(chart_series)} points): {os.path.abspath(chart_path)}")
