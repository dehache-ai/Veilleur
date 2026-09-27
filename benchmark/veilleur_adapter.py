"""Adaptateur Veilleur pour raucle-bench (github.com/epic28-ltd/raucle-bench,
licence MIT) — traduit entre l'interface `Adapter` attendue par raucle-bench
(`predict(prompt) -> Prediction`) et notre `assess_risk()` existant, sans
rien changer côté détection.

Nécessite le dépôt raucle-bench cloné séparément (pas commité dans ce
projet, cf. benchmark/README.md) :
    git clone https://github.com/epic28-ltd/raucle-bench.git raucle-bench-src
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from victim_agent.veilleur_gate import assess_risk  # noqa: E402

RAUCLE_ACTION_MAP = {
    "allow": "ALLOW",
    "ask_user": "ALERT",
    "block": "BLOCK",
}


class VeilleurAdapter:
    """raucle-bench n'a pas de notion de "requête utilisateur d'origine" ou
    de "source" — chaque prompt du dataset est évalué seul, sans le contexte
    agent-voyage/Jinko pour lequel les gardiens Veilleur ont été conçus. On
    utilise donc des valeurs neutres pour source/original_user_intent et on
    ne teste que le contenu du prompt lui-même — un test hors distribution
    pour nos gardiens, pas une simulation de leur cas d'usage réel."""

    name = "veilleur-baseline"
    version = "0.1.0"

    def __init__(self, guardian: str = "baseline") -> None:
        self.guardian = guardian
        self.name = f"veilleur-{guardian}"

    def setup(self) -> None:
        pass  # veilleur_library() est géré par le script appelant (une seule fois pour tout le run)

    def teardown(self) -> None:
        pass

    def predict(self, prompt: str):
        from raucle_bench.adapter import Prediction  # import tardif : dépôt raucle-bench cloné à part

        try:
            verdict = asyncio.run(
                assess_risk(
                    source="raucle-bench (prompt de benchmark, hors contexte agent-voyage)",
                    original_user_intent="Réponds normalement à la demande de l'utilisateur.",
                    content_or_action=prompt,
                    guardian=self.guardian,
                )
            )
            return Prediction(
                action=RAUCLE_ACTION_MAP[verdict.recommended_action],
                confidence=1.0 if verdict.risk_level in ("élevé", "critique") else 0.5,
                detail=verdict.explanation,
            )
        except Exception as exc:
            return Prediction(action="ALLOW", error=str(exc))
