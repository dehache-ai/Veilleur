"""Préfiltre heuristique rapide (regex), inspiré de la composante
"heuristics" de projets comme Rebuff — première brique de défense en
profondeur ajoutée au gardien texte, en complément du LLM (jamais en
remplacement : les heuristiques n'attrapent que les cas les plus explicites,
un cas ambigu retombe toujours sur le LLM).

But concret pour Veilleur : attraper les marqueurs d'injection les plus
évidents sans attendre un appel LLM (rappel : `gpt-oss:20b` en local peut
prendre plusieurs minutes par appel, cf. les notes de développement (non publiées)) — une réaction quasi
instantanée sur les cas déjà connus/évidents, le LLM restant nécessaire pour
tout ce qui demande du jugement sémantique (écart d'intention, subtilité).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("marqueur_systeme_explicite", re.compile(r"\[(SYSTEME|SYSTÈME|SYSTEM)\]", re.IGNORECASE)),
    (
        "instruction_ignorer_fr",
        re.compile(r"ignor[ez]?\s+(toutes?\s+)?les?\s+instructions?\s+pr[ée]c[ée]dentes?", re.IGNORECASE),
    ),
    (
        "instruction_ignorer_en",
        re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions?", re.IGNORECASE),
    ),
    ("instruction_disregard_en", re.compile(r"disregard\s+(the\s+)?(above|previous)", re.IGNORECASE)),
    (
        "instruction_nouveau_role",
        re.compile(r"(tu es|you are)\s+maintenant\s+|new\s+instructions?\s*:", re.IGNORECASE),
    ),
]


@dataclass
class HeuristicMatch:
    matched: bool
    pattern_name: str | None
    explanation: str | None


def heuristic_scan(text: str) -> HeuristicMatch:
    """Cherche un marqueur d'injection explicite dans `text`. Ne renvoie un
    match que sur des motifs sans ambiguïté raisonnable (balises système,
    formulations classiques de contournement) — volontairement conservateur
    pour ne jamais générer de faux positif sur du texte normal."""
    for name, pattern in PATTERNS:
        match = pattern.search(text)
        if match:
            return HeuristicMatch(
                matched=True,
                pattern_name=name,
                explanation=f"Motif d'injection explicite détecté ({name}) : « {match.group(0)} »",
            )
    return HeuristicMatch(matched=False, pattern_name=None, explanation=None)
