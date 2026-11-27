"""Sauvegarde des resultats de comparaison dans la base SQL Server locale (SQL_SERVER / DATABASE),
separee de Snowflake : les resultats restent consultables dans SSMS sans relancer les scripts ni se
reconnecter a Snowflake. Connexion en authentification Windows, aucun mot de passe.

Chaque execution ajoute ses lignes (jamais d'ecrasement), avec la date d'execution dans `run_at` :
    SELECT * FROM dbo.dataset_vs_clustered WHERE run_at = (SELECT MAX(run_at) FROM dbo.dataset_vs_clustered);

Les ecarts sont stockes en pourcentage positif : 10.5 pour un ecart de 10,5 %, que Trading
Economics soit au-dessus ou en dessous de la source comparee.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pyodbc

SQL_SERVER = r".\SQLEXPRESS"
DATABASE = "Dnext vs TE"
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"

# Colonnes d'ecart (fractions, ex: 0.105) converties en pourcentage positif (10.5) dans la base :
# celles-ci, plus toute colonne commencant par "pct_diff" (pct_diff_value, pct_diff_quantity...).
PERCENT_COLUMNS = ("pct_diff", "match_pct", "median_signed_gap", "median_abs_gap", "max_abs_gap")


def _is_percent_column(column: str) -> bool:
    return column in PERCENT_COLUMNS or column.startswith("pct_diff")


def connect(database: str = DATABASE) -> pyodbc.Connection:
    # TrustServerCertificate : instance locale avec certificat auto-signe (chiffrement par defaut du driver 18).
    return pyodbc.connect(
        f"DRIVER={{{ODBC_DRIVER}}};SERVER={SQL_SERVER};DATABASE={database};"
        "Trusted_Connection=yes;TrustServerCertificate=yes"
    )


def to_db_frame(df: pd.DataFrame, run_at: dt.datetime) -> pd.DataFrame:
    """Copie prete pour la base : run_at en premiere colonne, ecarts en % positif, dates en date."""
    out = df.copy()
    for column in out.columns:
        if _is_percent_column(str(column)):
            out[column] = (pd.to_numeric(out[column], errors="coerce").abs() * 100).round(2)
    for column in out.columns:
        if out[column].map(lambda v: isinstance(v, pd.Timestamp)).any():
            out[column] = out[column].map(lambda v: v.date() if isinstance(v, pd.Timestamp) else v)
    out.insert(0, "run_at", run_at)
    return out


def _sql_type(series: pd.Series) -> str:
    """Type SQL Server d'une colonne, d'apres ses valeurs non vides."""
    values = series.dropna()
    if values.empty:
        return "NVARCHAR(MAX)"
    if values.map(lambda v: isinstance(v, dt.datetime)).all():
        return "DATETIME2(0)"
    if values.map(lambda v: isinstance(v, dt.date)).all():
        return "DATE"
    if pd.api.types.is_bool_dtype(values):
        return "BIT"
    if pd.api.types.is_integer_dtype(values):
        return "BIGINT"
    if pd.api.types.is_numeric_dtype(values):
        return "FLOAT"
    return "NVARCHAR(MAX)"


def _quote(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def _python_value(value):
    """Valeur acceptee par pyodbc : NaN/NaT -> NULL, types numpy -> types Python."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    return value.item() if hasattr(value, "item") else value


def save_to_db(df: pd.DataFrame, table: str, run_at: dt.datetime, conn: pyodbc.Connection | None = None) -> int:
    """Ajoute `df` a la table dbo.`table` (creee au besoin). Une colonne nouvelle par rapport a la
    table existante y est ajoutee, pour que l'ajout ne casse pas quand un script gagne une colonne.
    `conn` : connexion a utiliser (tests) ; par defaut, la base DATABASE. Renvoie le nombre de lignes."""
    if df.empty:
        return 0
    out = to_db_frame(df, run_at)
    own_conn = conn is None
    conn = conn or connect()
    try:
        cursor = conn.cursor()
        existing = {row.column_name for row in cursor.columns(table=table, schema="dbo")}
        if not existing:
            columns_sql = ", ".join(f"{_quote(c)} {_sql_type(out[c])} NULL" for c in out.columns)
            cursor.execute(f"CREATE TABLE dbo.{_quote(table)} ({columns_sql})")
        else:
            for column in out.columns:
                if column not in existing:
                    cursor.execute(f"ALTER TABLE dbo.{_quote(table)} ADD {_quote(column)} {_sql_type(out[column])} NULL")

        # Colonne texte aux valeurs mixtes (ex: nombres et libelles) : tout en texte, sinon pyodbc
        # refuse un nombre envoye dans une colonne NVARCHAR.
        for column in out.columns:
            if _sql_type(out[column]) == "NVARCHAR(MAX)":
                out[column] = out[column].map(lambda v: v if v is None or isinstance(v, str) or pd.isna(v) else str(v))

        placeholders = ", ".join("?" for _ in out.columns)
        insert = f"INSERT INTO dbo.{_quote(table)} ({', '.join(_quote(c) for c in out.columns)}) VALUES ({placeholders})"
        rows = [tuple(_python_value(v) for v in row) for row in out.itertuples(index=False, name=None)]
        cursor.fast_executemany = True
        cursor.executemany(insert, rows)
        conn.commit()
    finally:
        if own_conn:
            conn.close()
    return len(out)


def now() -> dt.datetime:
    """Horodatage d'une execution (heure locale, a la seconde), commun a toutes ses tables."""
    return dt.datetime.now().replace(microsecond=0)


def save_run(frames: dict[str, pd.DataFrame]) -> None:
    """Enregistre les resultats d'une execution ({table: DataFrame}) avec un meme run_at, et
    affiche ce qui a ete ecrit. Appele par chaque script de comparaison, avec ses propres tables."""
    run_at = now()
    conn = connect()
    try:
        written = {table: save_to_db(df, table, run_at, conn) for table, df in frames.items()}
    finally:
        conn.close()
    details = ", ".join(f"{n} ligne(s) dans dbo.{table}" for table, n in written.items())
    print(f"Base SQL Server {SQL_SERVER} / {DATABASE} (run_at={run_at}) : {details}")
