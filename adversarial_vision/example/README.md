# Exemple public — structure seulement

Cette paire d'images est l'équivalent, pour le pilier image, de
`scenarios.example/` côté texte : elle montre que `detector.py` fonctionne
réellement, sans fournir l'outil de fabrication (`generate_attack.py` (non publié), privé)
ni les versions les plus abouties utilisées pour la vraie démo (privées).

- `exemple_clean.png` — photo authentique, classée "pedestal" (22%)
- `exemple_adversarial.png` — même photo, perturbation adverse ciblée vers
  "sports car" (100% de confiance) — volontairement moins optimisée que la
  version utilisée en démo (amplitude plus large, artefact visible sur le
  ciel bleu à l'œil attentif) ; sert uniquement à vérifier que le détecteur
  fonctionne, pas à démontrer l'invisibilité maximale de l'attaque.

## Tester

```bash
python detector.py example/exemple_clean.png --no-voice        # -> allow
python detector.py example/exemple_adversarial.png --no-voice  # -> block
```

## Usage

Ces images sont une **démonstration défensive** : elles servent à tester le
détecteur. Ne pas les diffuser comme attaque réelle contre un système tiers.
