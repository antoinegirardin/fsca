# Extraction structurée des avis de sécurité Swissmedic (FSCA)

Chaque semaine, le script récupère les publications nouvelles ou mises à jour sur
[fsca.swissmedic.ch](https://fsca.swissmedic.ch/), télécharge la notice (FR en priorité,
sinon EN, DE, IT), en extrait le texte localement puis demande à Claude une fiche
structurée en français : équipements et identifiants concernés, synthèse, actions,
urgence et risque.

## Installation

```bash
mamba env create -f environment.yml
mamba activate fsca
```

(Sans mamba : `python -m venv .venv`, l'activer, puis `pip install -r requirements.txt`.)

Clé API : variable d'environnement `ANTHROPIC_API_KEY`
(Windows : `setx ANTHROPIC_API_KEY "sk-ant-..."` puis rouvrir le terminal).

## Utilisation

```bash
python run_weekly.py                         # depuis le dernier passage réussi (7 jours la 1re fois)
python run_weekly.py --dry-run --limit 5     # test sans Claude : téléchargement, texte, regex
python run_weekly.py --ref Vk_20261104_023   # une notice précise (--force pour la ré-extraire)
python run_weekly.py --from 2026-10-01 --to 2026-10-31
python run_weekly.py --export-all            # réexporte toute la base
```

Code de sortie : 0 = OK, 1 = au moins une notice en échec (elle sera retentée au passage
suivant), 2 = clé API refusée, 3 = publication git échouée.

### Planification (optionnelle, Planificateur de tâches Windows)

Le script peut aussi être lancé à la main : il reprend toujours au dernier passage réussi.

```bat
schtasks /Create /TN "FSCA Swissmedic" /SC WEEKLY /D MON /ST 07:00 ^
  /TR "<chemin python de l'env> <dossier du projet>\run_weekly.py"
```

Chemin du Python de l'environnement : `mamba activate fsca` puis `where python` (Windows)
ou `which python` (WSL). Sous WSL, une tâche cron fait l'affaire tant que WSL tourne.

Le passage hebdomadaire reprend automatiquement à la date du dernier passage réussi
(avec 2 jours de recouvrement). Une notice n'est ré-extraite que si le document retenu
change : un simple « ajout de langue » ne coûte rien, sauf s'il apporte la version FR.

## Sorties (`data/`)

| Fichier | Contenu | Publié sur GitHub |
|---|---|---|
| `exports/complet/identifiants.csv` | **Fichier de filtrage**, toute la base : une ligne par identifiant capable de désigner un produit dans un inventaire (voir ci-dessous), avec le dispositif et la notice recopiés sur chaque ligne (séparateur `;`, ouvrable dans Excel). | oui |
| `exports/complet/notices.jsonl` | Un enregistrement complet par notice. | oui |
| `exports/complet/fiches/*.md` | Une fiche lisible par notice, adaptée à l'indexation par un LLM local. | oui |
| `exports/AAAA-MM-JJ/` | Mêmes fichiers, limités aux notices traitées lors du passage. | non |
| `fsca.sqlite` | Base cumulée : `publications`, `identifiants` (restrictions comprises), `cles_recherche` (une ligne par variante, indexée). | non |
| `pdfs/`, `logs/` | Notices originales et journaux. | non |

L'export complet est régénéré à chaque passage ; les nouveautés se repèrent par `date_statut`.

## Publication sur GitHub (git, sans gh)

Une seule fois : créer un dépôt **vide** sur github.com (sans README ni .gitignore), puis :

```bash
git init -b main
git add .gitignore README.md environment.yml requirements.txt run_weekly.py fsca/*.py data/exports/complet
git commit -m "Version initiale"
git remote add origin git@github.com:<utilisateur>/<depot>.git   # ou https://github.com/<utilisateur>/<depot>.git
git push -u origin main
```

Ensuite, à chaque passage :

```bash
python run_weekly.py --publish
```

Le script commit et pousse `data/exports/complet` (code de sortie 3 si git échoue ; les
exports restent alors disponibles localement et seront poussés au passage suivant).

## Rapprochement avec un inventaire

Hiérarchie : **notice** (`swissmedic_ref`) → **dispositif** (`dispositif_id`, `nom_commercial`)
→ **identifiant** (`role`, `type_id`, `valeur`, `fiabilite`) → **variantes** (`cles_recherche`,
séparées par ` | ` dans la même cellule).

Chaque ligne du CSV est un identifiant qui peut, seul, désigner un produit :

- `role = produit` : nom commercial, nom alternatif, modèle, référence, UDI-DI/GTIN, Basic UDI-DI.
  Une correspondance avec l'inventaire suffit à dire que la notice vise ce produit.
- `role = associé` : produit tiers qui définit la configuration à risque, sans être lui-même
  défectueux (ex. implant utilisé avec un logiciel). Il signale la notice, sans rien à retirer.
  Il apparaît une seule fois par notice, et `dispositif_id` liste les dispositifs auxquels il est lié.

Les **restrictions** ne sont pas des lignes, car elles ne désignent rien seules. Elles sont
recopiées en colonnes sur chaque ligne du dispositif, pour confirmer une correspondance :
`restriction_lots`, `restriction_numeros_serie`, `restriction_versions`, `restriction_plages`
(début → fin), `restriction_autres` (critères textuels : dates de fabrication, motifs UDI-PI…).
Colonnes vides = tout le dispositif est concerné (voir aussi `portee`). `liste_complete = False`
signale une liste de restrictions incomplète dans la notice, à consulter.

Démarche : chercher chaque clé de `cles_recherche` dans l'inventaire (en lui appliquant
`fsca.keys.search_keys`), puis, pour chaque correspondance, vérifier les restrictions et
`non_concernes`, et pondérer selon `fiabilite` :
`haute` (UDI/GTIN, Basic UDI-DI, références ≥ 5 caractères avec chiffres), `moyenne`
(références de 4 caractères, noms de plusieurs mots), `faible` (nom d'un seul mot,
référence très courte) : à confirmer avec le fabricant et le `type_equipement`.

Dans SQLite, la table `identifiants` contient aussi les restrictions (`role = restriction`),
et `cles_recherche` une ligne par variante, indexée, pour une jointure exacte.

## Champs extraits et référentiels

Le schéma complet est dans [fsca/schema.py](fsca/schema.py).

- **Identifiants** par dispositif : UDI-DI/GTIN, Basic UDI-DI, références catalogue,
  modèles, versions logicielles, lots, numéros de série, plages/motifs, indicateur de
  liste incomplète, et ce qui est explicitement non concerné.
- **Description** : type d'équipement générique en français, nature (DM, IVD, logiciel…),
  catégorie EMDN de niveau 1, classe réglementaire si indiquée.
- **Actions** : types d'action et actions utilisateur alignés sur le modèle de FSN de la
  Commission européenne, avec délais.
- **Évaluation (estimation du modèle, pas la position du fabricant)** :
  - urgence : immédiate / élevée / modérée / faible ;
  - gravité selon l'échelle à 5 niveaux usuelle d'ISO 14971 ;
  - probabilité (seulement si la notice donne des éléments) ;
  - classe de rappel estimée selon les définitions FDA I/II/III (21 CFR 7.3) ;
  - justification.

## Contrôles qualité

- Les GTIN présents dans le texte (codes GS1 `(01)…` ou précédés de GTIN/EAN/UDI, avec
  contrôle de la clé) sont détectés par regex. S'ils manquent dans l'extraction, ils sont
  signalés dans le journal, dans `controles` et dans la fiche.
- Les PDF scannés (peu de texte extractible) sont envoyés tels quels à Claude, qui lit
  les pages ; ils sont signalés dans le journal.
- Sortie structurée validée par Pydantic. Si un classifieur de sécurité refuse une notice
  à tort, la requête est rejouée sur un modèle de repli (`fallbacks="default"`).

## Coût

Modèle par défaut : `claude-opus-5-5`, effort `high` (modifiable avec `--model` et
`--effort`). Les tokens consommés sont journalisés à chaque passage et stockés par notice ;
mesurer sur quelques semaines avant d'ajuster. Pour un rattrapage de l'historique
(plus de 11 000 publications), l'API Batch (-50 %) serait plus adaptée.
