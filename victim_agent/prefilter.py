"""Préfiltre externe optionnel — modèle tiers HuggingFace
`NeuralTrust/prompt-guard-oss-small` (licence MIT, non gated, ~140M
paramètres, architecture ModernBERT/mmBERT-small).

⚠️ Solution externe open source, PAS un composant Veilleur : le règlement du
hackathon autorise les solutions externes à condition de le préciser
clairement. Ce module n'est utilisé que par le gardien hybride `hybrid`
(voir `HYBRID_GUARDIANS` dans `veilleur_gate.py`) — il ne touche jamais aux
gardiens LLM seuls (`baseline`/`contextual`), pour que la
comparaison "LLM seul" vs. "LLM + préfiltre externe" reste lisible dans
`runner.py --guardian all`.

Alternatives évaluées et écartées (voir les notes de recherche internes (non publiées)) :
- `meta-llama/Llama-Prompt-Guard-2-22M` : gated, nécessite d'accepter une
  licence Meta avant tout téléchargement — mauvais choix pour un dépôt
  public destiné à être cloné.
- `codesagar/prompt-guard-classification-v9` (Apache 2.0, non gated) :
  7 milliards de paramètres (base Mistral-7B) — disproportionné pour un
  projet qui ne s'appuie sur aucun GPU ailleurs.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

PREFILTER_MODEL_ID = "NeuralTrust/prompt-guard-oss-small"
PREFILTER_LICENSE = "MIT"
PREFILTER_THRESHOLD = 0.5  # score jailbreak à partir duquel on remonte une alerte


@dataclass
class PrefilterResult:
    label: str  # "jailbreak" | "benign"
    score: float  # probabilité de la classe jailbreak, dans [0, 1]


@lru_cache(maxsize=1)
def _load_model():
    """Chargement paresseux et mis en cache (un seul chargement par
    process) : le modèle est petit (~140M paramètres) mais son chargement
    depuis le disque/cache HuggingFace prend quelques secondes, à ne pas
    répéter à chaque appel.

    ⚠️ Sur certaines machines Windows, le magasin de certificats `certifi`
    embarqué dans Python ne connaît pas une CA locale (antivirus/proxy
    d'entreprise) que Windows connaît pourtant — le téléchargement échoue
    alors avec `CERTIFICATE_VERIFY_FAILED` même si `curl`/le navigateur
    fonctionnent. Fix scopé au strict nécessaire avec `truststore`
    (`inject_into_ssl`/`extract_from_ssl`) : on fait utiliser à Python le
    magasin Windows UNIQUEMENT pendant ce téléchargement, puis on revient à
    l'état normal. Ne PAS utiliser `pip-system-certs` à la place : ce paquet
    patche `ssl.SSLContext` de façon globale et permanente pour tout le
    process, ce qui casse `jinko_client.py` (le client MCP de Jinko, basé sur
    `httpcore2`/`anyio`, entre en récursion infinie face au patch global)."""
    import truststore
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    truststore.inject_into_ssl()
    try:
        tokenizer = AutoTokenizer.from_pretrained(PREFILTER_MODEL_ID)
        model = AutoModelForSequenceClassification.from_pretrained(PREFILTER_MODEL_ID)
    finally:
        truststore.extract_from_ssl()

    model.eval()
    return tokenizer, model


def classify_prompt_injection(text: str) -> PrefilterResult:
    """Classifie `text` avec le préfiltre externe. Appel synchrone et local
    (aucun réseau après le premier téléchargement du modèle, qui est mis en
    cache par HuggingFace) — donc rapide et sans risque de dépendance à un
    service tiers en direct pendant une démo."""
    import torch

    tokenizer, model = _load_model()
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    with torch.no_grad():
        logits = model(**inputs).logits[0]
    probabilities = torch.softmax(logits, dim=-1)
    jailbreak_score = probabilities[1].item()
    label = "jailbreak" if jailbreak_score >= PREFILTER_THRESHOLD else "benign"
    return PrefilterResult(label=label, score=jailbreak_score)
