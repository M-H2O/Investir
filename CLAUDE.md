# Boussole — contexte projet

## Produit
SaaS B2C indépendant d'accompagnement financier pour particuliers français.
Zéro produit vendu, zéro commission, zéro conseil personnalisé — voir garde-fou CIF plus bas.
Solo founder, développement async (expatriation Asie prévue), stack low-ops.

## GARDE-FOU RÉGLEMENTAIRE (non négociable)
Toute feature doit rester "l'utilisateur choisit, outillé par nous" — jamais
"l'outil décide/recommande pour l'utilisateur à partir de ses données personnelles".
Si une tâche demandée s'approche d'une recommandation personnalisée (ex: "quelle
allocation choisir vu mon profil"), le signaler explicitement avant de coder au
lieu d'implémenter directement.

## Stack
- Backend : FastAPI (Python 3.12), PostgreSQL
- Frontend : HTML/CSS/JS vanilla, pas de framework JS
- Déploiement : Railway
- Design system existant à réutiliser tel quel : grille Séyès, police Young Serif
  (titres) + Public Sans (texte) + IBM Plex Mono (données), palette dans
  index.html actuel — NE PAS redesigner, seulement étendre.

## Conventions de code
- Commentaires en français, code (variables/fonctions) en anglais
- Tests systématiques sur toute logique de calcul (simulateur, futur fiscal)
- Un commit = une tâche cohérente, message clair, pas de commit auto sans relecture

## Simulateur de portefeuille (chantier en cours)
Source de données : yfinance en V1, à isoler derrière une interface pour pouvoir
changer de fournisseur sans tout refaire (Twelve Data / EOD HD en option si
yfinance devient limitant).
Jamais de recommandation d'allocation : l'utilisateur choisit ses pondérations,
l'outil affiche l'historique. Disclaimer performance passée visible en permanence.
