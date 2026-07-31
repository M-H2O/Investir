# Boussole

Prototype de pédagogie financière : comprendre les enveloppes, comparer des ETF
et des courtiers, simuler rétrospectivement une allocation. **Aucun produit
vendu, aucune commission, aucun conseil personnalisé.**

---

## Architecture

```
index.html          Le site entier (HTML + CSS + JS, sans dépendance externe)
data/
  tickers.csv       LA LISTE — éditable à la main, c'est le seul fichier à toucher
  cours.json        Cours quotidiens exportés (GÉNÉRÉ, ne pas éditer)
pipeline/
  schema.sql        Schéma SQLite + vue prices_weekly
  catalogue.py      Lecteur de data/tickers.csv (validation, valeurs par défaut)
  ingest.py         Récupération des cours -> boussole.db
  export_json.py    boussole.db -> data/cours.json
  portefeuille.py   Moteur de calcul de portefeuille (référence)
  tests/            Tests du moteur, avec valeurs de référence
  boussole.db       Base locale (non versionnée, reconstructible)
```

Le site est **statique** : il n'a pas de backend et ne peut pas lire SQLite.
`data/cours.json` est le pont entre le pipeline et l'interface. Le calcul de
simulation se fait dans le navigateur (un curseur de dates croisé à des poids
libres représente trop de combinaisons pour être précalculé).

`pipeline/portefeuille.py` (fonction `simulate`) et le moteur JS du simulateur
implémentent **le même calcul**. Le Python est la référence testée ; le JS est vérifié contre ses
valeurs (écart constaté : 0,0001 € sur 11 475 €, soit l'arrondi de l'export).

---

## Mise à jour des cours (manuel)

Trois commandes, dans cet ordre :

```bash
cd pipeline
python ingest.py            # 1. récupère les cours -> boussole.db
python export_json.py       # 2. régénère ../data/cours.json
python -m pytest tests/     # 3. vérifie que le moteur est toujours juste
```

Puis committer `data/cours.json` pour que le site publié en profite.

> **`export_json.py` n'est pas optionnel.** Sans lui, `ingest.py` met à jour la
> base mais le site continue d'afficher les cours du dernier export.

Première installation :

```bash
cd pipeline
pip install -r requirements-dev.txt
python ingest.py --init --full
python export_json.py
```

### Autres commandes utiles

| Commande | Effet |
|---|---|
| `python ingest.py --full` | Rejoue tout l'historique au lieu de l'incrémental |
| `python ingest.py --tickers CSPX IWDA` | Limite à quelques instruments |
| `python ingest.py --dry-run` | Montre notamment les lignes détectées comme NOUVEAU |
| `python ingest.py --dry-run` | Montre ce qui serait fait, n'écrit rien |
| `python ingest.py --fx` | Ajoute les paires de change (inutile tant que tout cote en euros) |

`ingest.py` renvoie le code de sortie `0` si tout est OK, `1` si au moins un
instrument est en échec — exploitable par une tâche planifiée.

---

## Faire tourner le site en local

Le simulateur charge `data/cours.json` par `fetch()`. Ouvrir `index.html` par
double-clic **ne fonctionne pas** : le navigateur bloque la lecture de fichiers
locaux (règle `file://`). Il faut servir le dossier en HTTP :

```bash
python -m http.server 8000
```

puis ouvrir <http://localhost:8000>. Le simulateur affiche d'ailleurs ce rappel
si le chargement échoue.

Les autres onglets (Comprendre, Comparer, Durable…) fonctionnent sans serveur,
leurs données étant inscrites directement dans la page.

---

## Gérer la liste d'instruments — `data/tickers.csv`

Toute la liste se pilote depuis ce seul fichier, **sans toucher au code**.
Éditez-le dans Excel, Google Sheets, ou directement sur GitHub.

**Une seule colonne est obligatoire : `yahoo_symbol`.** Le reste se complète
automatiquement depuis Yahoo à l'ingestion. Un fichier valide peut donc tenir
en deux lignes :

```csv
yahoo_symbol
SXR8.DE
```

| Colonne | Rôle |
|---|---|
| `yahoo_symbol` | **Obligatoire.** Le symbole Yahoo (`SXR8.DE`, `0GGH.L`, `AAPL`) |
| `ticker` | Nom court affiché ; par défaut le symbole sans son suffixe de place |
| `name` | Libellé long ; récupéré depuis Yahoo si vide |
| `isin` | Informatif (Yahoo ne le fournit pas de façon fiable) |
| `asset_type` | `etf` ou `stock` ; déduit si vide |
| `currency`, `exchange` | Déduits si vides |
| `active` | `non` retire la ligne du site sans la supprimer du fichier |

### Ajouter un instrument

1. Résoudre le **symbole Yahoo** — ce n'est presque jamais le ticker
   d'affichage (`CSPX` → `SXR8.DE`, `PE500` → `PSP5.PA`, `AGGH` → `0GGH.L`).
   La recherche par ISIN fonctionne bien :
   `https://query1.finance.yahoo.com/v1/finance/search?q=<ISIN>`
2. Ajouter une ligne au CSV avec ce symbole.
3. `python ingest.py` — la ligne est détectée comme nouvelle et **tout son
   historique est rapatrié**, sans avoir à penser à `--full`.
4. `python export_json.py`, puis committer `tickers.csv` et `cours.json`.

### Retirer un instrument

Supprimez sa ligne, ou passez `active` à `non`, puis relancez `export_json.py`.
Il disparaît du site à la régénération suivante. Son historique **reste en
base** : le réactiver plus tard ne coûte aucun re-téléchargement.

### Attention à la devise

Le simulateur additionne des montants **sans appliquer de taux de change**. À
ISIN égal, préférez toujours la ligne cotée **en euros**. Si vous ajoutez un
instrument dans une autre devise, `export_json.py` vous prévient en console et
le simulateur affiche un avertissement dès que l'utilisateur mélange les
devises — mais aucune conversion n'est faite.

> Attention aussi à `GBp` : c'est le **pence**, pas la livre. Un facteur 100.

Pour qu'un instrument apparaisse aussi dans le comparateur d'ETF (onglet
distinct), ajouter la ligne correspondante au tableau `ETFS` dans `index.html`.

---

## Conventions

Conformément à `CLAUDE.md` : **commentaires en français, identifiants en anglais**.
Les classes CSS et les libellés d'interface restent en français, comme le reste
du design system existant.

## Limites connues

- **Profondeur d'historique très inégale.** MEUD, WEBN, WPEA et EWLD n'ont que
  ~2 ans de cours chez Yahoo alors que les fonds sont plus anciens (fusions
  Lyxor/Amundi). Le simulateur borne le curseur en conséquence et nomme la ligne
  qui bride, plutôt que de combler le vide.
- **Le simulateur ne déduit ni frais de courtage, ni fiscalité, ni inflation.**
  Le gain affiché est donc plus flatteur que la réalité.
- **Achat unique, sans rééquilibrage.** Les versements programmés et le
  rééquilibrage périodique ne sont pas simulés — ce sont des modes de calcul
  distincts, pas des options du calcul actuel.
- **Yahoo Finance n'est pas une source contractuelle.** Elle convient aux tests ;
  un usage sérieux demanderait un fournisseur avec engagement de service.

---

## Avertissement

Boussole est un service d'information et de pédagogie. Il ne fournit aucun
conseil en investissement personnalisé, ne commercialise aucun produit et ne
perçoit aucune rémunération d'aucun établissement cité. Les simulations sont
**rétrospectives** : les performances passées ne préjugent pas des performances
futures. Investir comporte un risque de perte en capital.
