# Évaluation sur raucle-bench

[raucle-bench](https://github.com/epic28-ltd/raucle-bench) (MIT) est un
benchmark public de détection d'injection de prompt — un dataset partagé de
prompts d'attaque/bénins, avec un protocole de scoring commun (précision,
rappel, F1, taux de faux positifs, latence). Il ne fait pas partie de ce
projet : on l'utilise pour obtenir un score chiffré et reproductible de
Veilleur, plutôt que de s'appuyer uniquement sur nos 4 scénarios maison.

## Reproduire

```bash
git clone https://github.com/epic28-ltd/raucle-bench.git raucle-bench-src
# (dans l'environnement où requirements.txt est installé)
python benchmark/run_benchmark.py --sample-per-class 3 --guardian baseline
```

`raucle-bench-src/` n'est **jamais commité** dans ce dépôt (dépôt tiers,
licence propre) — à cloner soi-même avant de lancer l'évaluation.

## Pourquoi un échantillon, pas le jeu complet

Le jeu complet fait 165 prompts. `gpt-oss:20b` en local peut prendre
plusieurs minutes par appel (cf. les notes de développement (non publiées)) — le jeu complet prendrait
plusieurs heures. `--sample-per-class N` prélève N prompts par classe
d'attaque (7 classes) pour un score représentatif en un temps raisonnable.
`--sample-per-class 0` lance le jeu complet — plus réaliste avec le backend
OpenAI (`python switch_backend.py openai`), nettement plus rapide.

## `veilleur_adapter.py`

Traduit entre l'interface attendue par raucle-bench (`predict(prompt) ->
ALLOW/ALERT/BLOCK`) et notre `assess_risk()` existant — aucun nouveau code
de détection, juste un pont. Limite à noter : raucle-bench évalue des
prompts isolés, sans le contexte "agent voyage + Jinko" pour lequel nos
gardiens ont été écrits — un test volontairement hors distribution, pas une
reproduction de notre cas d'usage réel.
