# Veilleur Vision — principes retenus

Ce document résume, pour l'expliquer à l'oral, la partie du projet qui
détecte des **images manipulées** (en complément du gardien texte anti-
injection de prompt). Détails techniques : voir les docstrings de
`generate_attack.py` (non publié) et `detector.py`.

## Le problème : une image peut mentir à une IA sans que l'œil s'en aperçoive

Un classificateur d'images (par ex. un réseau entraîné sur ImageNet) prend
sa décision à partir des valeurs de pixels. On peut modifier une image de
façon **invisible à l'œil humain** — chaque pixel change d'au plus 1 ou 2
niveaux sur une échelle de 0 à 255 — et pourtant faire complètement basculer
la prédiction du modèle : une photo de coq reconnue comme une baleine, une
statue reconnue comme une voiture de sport. C'est ce qu'on appelle une
**perturbation adverse** (*adversarial example*), un phénomène connu et
documenté en sécurité de l'IA depuis 2014 (Goodfellow et al.).

C'est le pendant, côté image, de l'injection de prompt côté texte : dans
les deux cas, une manipulation du contenu d'entrée, invisible ou anodine en
apparence, détourne le comportement d'un système IA.

## Comment on fabrique l'attaque (`generate_attack.py` (non publié))

- On part d'une vraie photo, correctement classifiée par le modèle.
- On calcule, pas à pas, dans quelle direction modifier chaque pixel pour
  que le modèle penche de plus en plus vers une classe cible choisie
  (ex. "sports car"), tout en gardant chaque changement de pixel minuscule
  et borné (*Iterative Target Class Method* / PGD ciblé).
- Résultat : une image visuellement identique à l'originale, mais classée
  avec une confiance très élevée dans une catégorie n'ayant aucun rapport
  avec la réalité.
- Fait important : le nombre de pixels touchés n'est pas le facteur clé.
  Sur un de nos exemples, **52 % des valeurs de pixels ont changé**, mais
  chacune d'à peine 1 à 2 niveaux sur 256 — c'est l'amplitude par pixel qui
  reste minuscule, pas le nombre de pixels concernés.
- Une même amplitude de perturbation est plus visible sur une zone lisse
  (ciel bleu uni) que sur une zone texturée (herbe, feuillage, rochers) —
  contrainte dont on a dû tenir compte pour garder l'attaque réellement
  invisible.

## Comment Veilleur la détecte (`detector.py`)

**Idée centrale : une perturbation adverse est invisible mais fragile.**
Elle est calculée très précisément pour un modèle et une image donnés ; la
moindre transformation légère de l'image (un flou très léger) suffit à la
perturber, alors qu'une vraie photo garde un contenu cohérent à toutes les
échelles et résiste bien à un léger flou.

Le détecteur compare donc :
1. La classification de l'image telle quelle.
2. La classification de la même image après un flou gaussien très léger.

**Signal retenu : est-ce que la catégorie change ?** Une simple chute de
confiance s'est révélée peu fiable à elle seule (une vraie photo peut aussi
perdre en confiance sous l'effet du flou). En revanche, sur nos tests, une
vraie photo **garde la même catégorie** malgré le flou, alors qu'une image
piégée **change carrément de catégorie** — souvent vers quelque chose de
plus cohérent avec la scène réelle, comme si le flou "dissolvait" juste
assez l'attaque pour laisser réapparaître le vrai contenu.

## Ce que ça démontre (et ses limites, à assumer)

- Validé sur 4 images réelles (2 authentiques, 2 piégées, dont une avec une
  amplitude minimale de 1/255) : 4/4 verdicts corrects.
- Ce n'est **pas** un détecteur d'exemples adverses à l'état de l'art — la
  détection d'attaques adverses reste un problème de recherche ouvert, et
  des attaques plus sophistiquées (optimisées spécifiquement pour résister
  à ce type de défense) pourraient passer au travers. Le seuil de flou a
  été calibré empiriquement sur nos propres exemples, pas sur un grand jeu
  de données validé statistiquement. La littérature académique documente
  une trentaine de techniques de défense contre les perturbations adverses
  (recadrage, compression, filtrage fréquentiel, techniques d'entraînement
  robuste...) ; Veilleur en met volontairement en œuvre une seule, choisie
  pour sa simplicité d'explication plutôt que pour sa robustesse maximale.
- L'objectif ici est de démontrer le **principe** : Veilleur peut appliquer
  la même philosophie de détection (chercher ce qui est fragile/invisible,
  pas se fier à ce qui a l'air normal) aussi bien à du texte qu'à des
  images — deux modalités, une même intuition de sécurité.
