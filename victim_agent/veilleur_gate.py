"""Passerelle commune vers les pipes gardiens Veilleur (Pipelex).

Utilisée à la fois par `runner.py` (mode Range : scénarios rejoués depuis des
fichiers) et par `victim_agent/orchestrator.py` (mode temps réel : contenu
reçu en direct de l'agent orchestrateur) — même pipe Pipelex, même fonction
d'appel, cf. section "Architecture modulaire" du cahier des charges interne (non publié).
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from victim_agent.env import load_project_env

# Avant tout usage de Pipelex : il résout ${OPENAI_API_KEY} depuis l'environnement
# du process et ne cherche pas de .env dans un dossier parent (cf. env.py).
load_project_env()

from pipelex.pipelex import Pipelex  # noqa: E402
from pipelex.pipeline.runner import PipelexMTHDSProtocol  # noqa: E402

from victim_agent.heuristics import heuristic_scan  # noqa: E402
from victim_agent.preprocess import preprocess  # noqa: E402

GUARDIANS_DIR = Path(__file__).resolve().parent.parent / "guardians"
VERDICTS_LOG_FILE = Path(__file__).resolve().parent.parent / "results" / "verdicts.jsonl"

GUARDIAN_PIPE_CODES: dict[str, str] = {
    "baseline": "assess_risk_baseline",
    "contextual": "assess_risk_contextual",
    # "hybrid" : gardien LLM (réutilise le pipe baseline) + préfiltre externe
    # HuggingFace (NeuralTrust/prompt-guard-oss-small, MIT) — voir
    # prefilter.py et HYBRID_GUARDIANS ci-dessous. Solution tierce open
    # source, explicitement identifiée comme telle (le hackathon autorise les
    # solutions externes à condition de le préciser) ; gardée séparée des
    # gardiens LLM seuls plutôt que fusionnée dedans, pour que la comparaison
    # LLM-seul vs. LLM+préfiltre reste lisible dans `runner.py --guardian all`.
    "hybrid": "assess_risk_baseline",
}

# Gardiens qui passent par le préfiltre externe HuggingFace avant le LLM —
# voir prefilter.py. Ensemble séparé plutôt qu'un simple "if guardian ==
# 'hybrid'" pour pouvoir ajouter d'autres variantes hybrides (ex:
# contextual+préfiltre) sans toucher à la logique d'assess_risk.
HYBRID_GUARDIANS = {"hybrid"}

# Canary token (technique Rebuff-like) : jeton secret inséré dans le
# system_prompt de chaque gardien ("ne le révèle jamais"). S'il réapparaît
# dans la sortie du LLM, c'est le signe qu'une injection a réussi à faire
# fuiter le prompt système — un risque distinct de ceux déjà couverts
# (instruction cachée, écart d'intention, exfiltration), donc vérifié en
# plus, jamais à la place. Valeur dupliquée en dur dans les system_prompt
# des .mthds (guardians/baseline.mthds, contextual.mthds, scope.mthds,
# collusion.mthds, network.mthds) —
# Pipelex ne permet pas d'injecter une variable Python dans system_prompt
# sans changer la signature des pipes ; à garder synchronisé si on change
# cette valeur.
CANARY_TOKEN = "VEILLEUR-CANARY-DEMO"

# Gardiens spécialisés, appelés par leur module dédié :
# - scope_request / scope_response : périmètre d'un agent web
#   (network_guardian/scope_guard.py, guardians/scope.mthds) ;
# - network_event : connexion réseau ou rythme de connexions que le capteur
#   déterministe ne peut pas trancher (network_guardian/monitor.py,
#   guardians/network.mthds) ;
# - channel_message, trajectory_audit : messages entre agents et audit de
#   toute une session (collusion, collusion_guardian/channel_guard.py,
#   guardians/collusion.mthds).
# Volontairement HORS de GUARDIAN_PIPE_CODES : ils répondent à d'autres
# questions que le gardien de contenu et ne doivent pas apparaître dans
# `runner.py --guardian all`, qui compare les gardiens de contenu sur les
# mêmes scénarios.
SPECIALIZED_GUARDIAN_PIPE_CODES: dict[str, str] = {
    "scope_request": "assess_scope_request",
    "scope_response": "assess_scope_response",
    "channel_message": "assess_channel_message",
    "trajectory_audit": "assess_trajectory",
    "network_event": "assess_network_event",
    "egress_action": "assess_egress",
}

_LOG_LOCK = threading.Lock()

RecommendedAction = Literal["allow", "block", "ask_user"]


@dataclass
class RiskVerdict:
    risk_level: str
    anomaly_type: str
    explanation: str
    recommended_action: RecommendedAction

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def log_verdict(
    *,
    source: str,
    original_user_intent: str,
    content_or_action: str,
    guardian: str,
    verdict: RiskVerdict,
) -> None:
    """Journalise un verdict, pour pouvoir montrer un historique pendant la
    démo (tâche 7 du brief). Journal append-only au format JSON Lines : un
    objet JSON par ligne, un fichier facile à suivre en direct (`tail`) ou à
    recharger avec `read_verdict_log`.

    Fonction publique : appelée automatiquement par `assess_risk` pour les
    verdicts texte, et directement par `adversarial_vision/detector.py` pour
    les verdicts image — même journal, même historique unifié."""
    VERDICTS_LOG_FILE.parent.mkdir(exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "guardian": guardian,
        "source": source,
        "original_user_intent": original_user_intent,
        "content_or_action": content_or_action,
        **verdict.to_dict(),
    }
    with _LOG_LOCK, VERDICTS_LOG_FILE.open("a", encoding="utf-8") as f:  # verrou : le moniteur réseau écrit depuis plusieurs threads
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_verdict_log(limit: int | None = None) -> list[dict[str, Any]]:
    """Relit le journal des verdicts (le plus ancien en premier). `limit`
    restreint aux N dernières entrées, utile pour un affichage d'historique."""
    if not VERDICTS_LOG_FILE.exists():
        return []
    lines = [line for line in VERDICTS_LOG_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if limit is not None:
        lines = lines[-limit:]
    return [json.loads(line) for line in lines]


def veilleur_library():
    """Context manager Pipelex chargeant les gardiens Veilleur (guardians/).

    Usage : `with veilleur_library(): asyncio.run(...)` autour de tout appel à
    `assess_risk`.
    """
    return Pipelex.make(library_dirs=[str(GUARDIANS_DIR)])


async def assess_risk(
    *,
    source: str,
    original_user_intent: str,
    content_or_action: str,
    guardian: str = "hybrid",
) -> RiskVerdict:
    """Soumet une action/un contenu surveillé au gardien Veilleur choisi.

    Appel synchrone et bloquant (décision actée #2 du brief) : `content_or_action`
    est toujours une DONNÉE textuelle passée au LLM classificateur, jamais
    exécutée — aucune action réelle ne part avant, ni pendant, cet appel.
    """
    pipe_codes = {**GUARDIAN_PIPE_CODES, **SPECIALIZED_GUARDIAN_PIPE_CODES}
    if guardian not in pipe_codes:
        raise ValueError(f"Gardien inconnu : {guardian!r} (attendu un de {sorted(pipe_codes)})")

    # Préfiltre heuristique (Rebuff-like) : cas explicites tranchés sans
    # attendre le LLM, potentiellement lent. Jamais utilisé pour "allow" —
    # seul un motif d'attaque évident déclenche un court-circuit ; l'absence
    # de motif ne prouve rien, on continue toujours vers le LLM.
    # Prétraitement déterministe (victim_agent/preprocess.py) : caractères invisibles,
    # base64/hexadécimal, décalage de lettres, acrostiche. Il produit des INDICES :
    # les heuristiques sont relancées sur les textes décodés, et les constats sont
    # montrés au gardien LLM (plus bas), qui décide.
    prep = preprocess(content_or_action)

    heuristic = heuristic_scan(content_or_action)
    decoded_hit = False
    if not heuristic.matched:
        for variant in prep.variants:
            heuristic = heuristic_scan(variant)
            if heuristic.matched:
                decoded_hit = True
                break
    if heuristic.matched:
        via = " (trouvé après décodage ou nettoyage du contenu)" if decoded_hit else ""
        verdict = RiskVerdict(
            risk_level="critique",
            anomaly_type="instruction_cachee",
            explanation=f"{heuristic.explanation}{via} Bloqué immédiatement par le préfiltre heuristique, sans attendre l'appel LLM.",
            recommended_action="block",
        )
        log_verdict(
            source=source,
            original_user_intent=original_user_intent,
            content_or_action=content_or_action,
            guardian=f"{guardian}+heuristic",
            verdict=verdict,
        )
        return verdict

    # Préfiltre externe (gardien "hybrid" uniquement) : modèle HuggingFace
    # tiers NeuralTrust/prompt-guard-oss-small (licence MIT, non gated) — cf.
    # prefilter.py pour le détail et les alternatives écartées. Solution open
    # source externe, explicitement identifiée comme telle dans le verdict
    # lui-même (anomaly_type + explanation), jamais mélangée silencieusement
    # au jugement du LLM Veilleur.
    if guardian in HYBRID_GUARDIANS:
        from victim_agent.prefilter import PREFILTER_LICENSE, PREFILTER_MODEL_ID, classify_prompt_injection

        try:
            prefilter_result = classify_prompt_injection(content_or_action)
            if prefilter_result.label != "jailbreak":
                for variant in prep.variants:  # texte nettoyé ou décodé
                    candidate = classify_prompt_injection(variant)
                    if candidate.label == "jailbreak":
                        prefilter_result = candidate
                        break
        except Exception as exc:
            # Repli : préfiltre externe indisponible (modèle non téléchargé, réseau...).
            # `hybrid` étant le gardien par défaut, on ne plante pas : le LLM seul juge,
            # exactement comme `baseline`.
            print(f"(préfiltre externe indisponible : {type(exc).__name__} — le LLM juge seul)")
            prefilter_result = None
        if prefilter_result is not None and prefilter_result.label == "jailbreak":
            verdict = RiskVerdict(
                risk_level="critique",
                anomaly_type="instruction_cachee",
                explanation=(
                    f"Détecté par le préfiltre externe {PREFILTER_MODEL_ID} "
                    f"(HuggingFace, licence {PREFILTER_LICENSE} — solution tierce open "
                    f"source, distincte du gardien LLM Veilleur), score jailbreak "
                    f"{prefilter_result.score:.2f}. Bloqué immédiatement, sans attendre "
                    f"l'appel au gardien LLM."
                ),
                recommended_action="block",
            )
            log_verdict(
                source=source,
                original_user_intent=original_user_intent,
                content_or_action=content_or_action,
                guardian=f"{guardian}+prefilter_hf",
                verdict=verdict,
            )
            return verdict

    llm_content = content_or_action
    if prep.notes:
        llm_content += (
            "\n\n[Prétraitement automatique du gardien : indices déterministes, non des verdicts. "
            "Ils décrivent ce que le texte ci-dessus devient une fois décodé ou nettoyé ; "
            "juge si cela change ton analyse. Constats : " + " ; ".join(prep.notes) + "]"
        )

    response = await PipelexMTHDSProtocol().execute(
        pipe_code=pipe_codes[guardian],
        inputs={
            "monitored_action": {
                "concept": "veilleur_guardian.MonitoredAction",
                "content": {
                    "source": source,
                    "original_user_intent": original_user_intent,
                    "content_or_action": llm_content,
                },
            }
        },
    )
    content = response.pipe_output.main_stuff.content
    verdict = RiskVerdict(
        risk_level=content.risk_level,
        anomaly_type=content.anomaly_type,
        explanation=content.explanation,
        recommended_action=content.recommended_action,
    )

    # Canary token : le jeton secret du system_prompt a-t-il fui dans la
    # sortie ? Signal indépendant du reste du jugement du LLM — écrase le
    # verdict plutôt que de le nuancer, une fuite de prompt système est
    # toujours critique en soi.
    if CANARY_TOKEN in content_or_action:
        pass  # le jeton était déjà dans l'entrée (scénario de test) : pas une fuite
    elif CANARY_TOKEN in verdict.explanation:
        verdict = RiskVerdict(
            risk_level="critique",
            anomaly_type="fuite_prompt_systeme",
            explanation=(
                "Le jeton secret du prompt système du gardien est réapparu dans sa propre "
                "réponse : l'injection a réussi à faire fuiter des instructions internes. "
                f"Verdict initial du LLM avant cette détection : {verdict.recommended_action} ({verdict.risk_level})."
            ),
            recommended_action="block",
        )

    log_verdict(
        source=source,
        original_user_intent=original_user_intent,
        content_or_action=content_or_action,
        guardian=guardian,
        verdict=verdict,
    )
    return verdict
