# Dnext — comparaison de sources Snowflake

## Installation

```
pip install -r requirements.txt --break-system-packages
```

## Configuration

Ouvre `cred.yaml` (déjà présent à la racine) et remplace les valeurs entre `<...>` par tes identifiants Snowflake :

```yaml
snowflake:
  account: <ACCOUNT>
  user: <USER>
  password: <PASSWORD>
  warehouse: <WAREHOUSE>
  database: <DATABASE>
  schema: <SCHEMA>
```

`cred.yaml` contient un mot de passe en clair : ne jamais le committer ni le partager.

## Choisir le(s) dataset(s)

Le(s) dataset(s) à comparer correspondent aux valeurs de la colonne `DataSource` côté Clustered. Deux façons de les choisir, la ligne de commande étant prioritaire sur le fichier de config :

- **Fichier `datasets_config.yaml`** (déjà présent à la racine) : édite la clé `datasets` avec un dataset, une liste de datasets, ou `all` pour tous les traiter.
- **Argument en ligne de commande**, ignore le fichier de config s'il est fourni :
  ```
  python compare_aggregated_sources.py dnexr-all-che-tf-customs-bilateral-monthly
  python compare_aggregated_sources.py dataset1 dataset2
  python compare_aggregated_sources.py all
  ```

## Lancer la comparaison agrégée

```
python compare_aggregated_sources.py
```

Un dataset en échec (table Snowflake introuvable, etc.) est loggé et ignoré, sans bloquer les autres. Résultat combiné écrit dans `aggregated_comparison_result.csv` (colonne `Dataset` en premier), résumé affiché dans le terminal pour chaque dataset.

## Comparer un dataset source à Trading Economics

```
python compare_dataset_vs_tradingeconomics.py dnexr-all-che-tf-customs-bilateral-monthly
python compare_dataset_vs_tradingeconomics.py dataset1 dataset2
python compare_dataset_vs_tradingeconomics.py all            # tous les datasets de Clustered (hors Eurostat / UN Comtrade)
python compare_dataset_vs_tradingeconomics.py                # datasets de datasets_config.yaml
python compare_dataset_vs_tradingeconomics.py --refresh      # re-scrape Trading Economics
```

Contrairement à `compare_all_clustered_vs_tradingeconomics.py`, la valeur est lue dans la table brute du dataset (× `Value Unit multiplier`). On peut ainsi voir si un écart avec Trading Economics vient déjà de la source ou de Clustered. Marche aussi pour un dataset absent de Clustered (pays lu dans le nom, ex. `vnm`, ou `--country`).

**Devise de comparaison**, décidée mois par mois (colonne `compared_in`) :

- toutes les valeurs du mois sont dans la devise de Trading Economics (ex. TND pour la Tunisie, EUR pour la Finlande) → comparaison directe dans cette devise, sans taux de change ;
- sinon (autre devise, ou plusieurs devises dans le mois) → tout est converti en USD (même conversion que `compare_aggregated_sources.py`) et comparé à la série TE en USD. Un mois dont une devise n'a pas de taux est `NON_COMPARABLE` (`source_conversion_echouee`).

Les devises du dataset sont listées par mois (`dataset_currencies`) et dans le résumé (`dataset_currencies`, avec le nombre de mois/sens par devise). La colonne `notes` du résumé signale ce qui est à vérifier : plusieurs devises, devise manquante (`N/A`), aucune valeur dans la devise de Trading Economics, conversion impossible, mois avec un écart aberrant (`flag`, ratio TE/dataset > 3× ou < 1/3 : erreur d'échelle ou de devise).

**Choisir la période** (par défaut tout l'historique), à la main :

```
python compare_dataset_vs_tradingeconomics.py <dataset> --month 2025-03              # un mois
python compare_dataset_vs_tradingeconomics.py <dataset> --month 2025-03 2025-04      # plusieurs mois
python compare_dataset_vs_tradingeconomics.py <dataset> --year 2025                  # année complète
python compare_dataset_vs_tradingeconomics.py ds1 ds2 ds3 --from 2025-01 --to 2025-06 # intervalle de mois
python compare_dataset_vs_tradingeconomics.py ds1 ds2 --from 2024 --to 2025           # janv. 2024 -> déc. 2025
python compare_dataset_vs_tradingeconomics.py ds1 ds2 --year 2024 2025 --direction Export
```

`--to` absent : jusqu'au mois en cours. Les périodes se cumulent (`--month`, `--year` et `--from/--to` ensemble).

ou par fichier Excel/CSV (exemple : `dataset_selection_example.xlsx`), une ligne par demande :

| Dataset | Year | Month | From | To | Trade Direction |
|---|---|---|---|---|---|
| dnexr-ins-all-tun-tf-customs-bilateral-monthly | 2025 | 3 | | | |
| dnexr-dnexr-all-fin-tf-customs-bilateral-monthly | 2025 | | | | Export |
| dnexr-ssb-all-nor-tf-customs-bilateral-monthly | | | 2026-01 | 2026-06 | |
| dnexr-exg-all-vnm-tf-customs-extrafields-monthly | | | | | |

```
python compare_dataset_vs_tradingeconomics.py --file ma_liste.xlsx
```

- `Dataset` (ou `Dataset code`) : seule colonne obligatoire.
- `Year` + `Month` : un mois (`Month` = `3`, `03`, `March`, `mars`...). `Year` seul : l'année complète. `Month` peut aussi être écrit `AAAA-MM` sans colonne `Year`.
- `From` / `To` : intervalle de mois (`AAAA-MM`, ou `AAAA`), à la place de `Year`/`Month` ; `To` vide = jusqu'au mois en cours.
- Aucune période sur la ligne : tout l'historique.
- `Trade Direction` : `Export`, `Import` ou vide (les deux).
- Seule la période demandée est lue dans Snowflake.

- Le cache `te_cache/` est partagé avec `compare_all_clustered_vs_tradingeconomics.py`.
- Sorties : `dataset_vs_tradingeconomics_result.xlsx` (feuilles `Resume` et `Detail`), plus les mêmes en CSV (`..._result.csv`, `..._summary.csv`). `--output autre_nom` change le début du nom des fichiers.

## Vérifier des mois précis contre Trading Economics

Pour contrôler quelques mois (par exemple les `MISMATCH` d'une comparaison précédente, ou un mois corrigé) sans relancer toute la comparaison Clustered vs Trading Economics. La liste est un fichier Excel (ou CSV), une ligne par vérification :

| Dataset | Month | Trade direction |
|---|---|---|
| dnexr-all-eth-tf-customs-bilateral-monthly | 2026-05 | Export |

```
python check_months_vs_tradingeconomics.py                            # lit month_checks.xlsx (INPUT_FILE)
python check_months_vs_tradingeconomics.py ma_liste.xlsx
python check_months_vs_tradingeconomics.py ma_liste.xlsx --refresh    # re-scrape Trading Economics
```

Sans chemin en argument, le script lit `INPUT_FILE` (en haut du script, `month_checks.xlsx` par défaut).

- Dataset : valeur de `DataSource` côté Clustered. Month : `AAAA-MM` (texte ou date Excel). Trade direction : `Export` ou `Import` ; les autres flux (`RE_IMP`, `REEX`, `DEXP`...) sortent en `NON_COMPARABLE` (`flux_non_publie_te`), Trading Economics ne les publiant pas.
- Les autres colonnes du fichier (ex. `Type`, `Severity`) sont recopiées dans le résultat, dans le même ordre de lignes.
- Le pays vient du dataset (son `Reporter` dans Clustered) ; un dataset multi-pays est `NON_COMPARABLE` (`pays_indetermine`).
- Clustered est interrogé en direct ; Trading Economics vient du cache `te_cache/`.
- `NON_COMPARABLE` : mois absent d'une des sources (`absent_clustered`, `absent_te`), ou pays sans série Export/Import sur Trading Economics (`te_vide`, ex. Éthiopie). La colonne `ratio_te_clustered` (TE / Clustered) fait ressortir une erreur d'échelle : un ratio proche de 1000 indique une valeur chargée en milliers.
- Sortie : `month_checks_result.xlsx` (filtres, en-tête figé, montants formatés).

## Pages Trading Economics lues

Par défaut, `te_scraper.py` lit les pages `<pays>/exports` et `<pays>/imports`. Pour les pays de `GOODS_ONLY_COUNTRIES` (aujourd'hui les États-Unis), ces pages incluent les services : on lit à la place `goods-exports` et `goods-imports`, seul périmètre comparable aux données douanières (ex. sept. 2026 : 321,5 Md USD d'exportations contre 209,1 Md USD de biens). Après un changement de cette liste, supprimer le fichier du pays dans `te_cache/` ou relancer avec `--refresh`.
