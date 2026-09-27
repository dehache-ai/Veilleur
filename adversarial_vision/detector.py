"""Détecteur d'images manipulées (perturbation adverse) pour Veilleur —
tourne dans l'environnement du projet (torch n'est nécessaire que pour ce
module, cf. requirements.txt : c'est la seule partie du projet qui en a besoin).

Principe : contrairement à l'injection de prompt (texte), une perturbation
adverse est invisible à l'œil mais fragile — un léger flou suffit à la
perturber. Sur une vraie photo, la classification reste globalement stable
sous un flou léger (même si la confiance peut un peu baisser) ; sur une
image piégée par une perturbation adverse ciblée, le TOP-1 change carrément
de catégorie, souvent vers quelque chose de plus proche de la réalité de la
scène. C'est ce changement de label — pas seulement une baisse de confiance,
trop bruitée pour être fiable seule — qui sert de signal principal ici.

⚠️ Limite honnête (cf. les notes de recherche internes (non publiées)) : ceci est une démonstration du
principe de "défense par transformation d'entrée", pas un détecteur
d'exemples adverses garanti à l'état de l'art — la détection d'exemples
adverses reste un problème de recherche ouvert. Le seuil de flou (radius de
0.9) a été calibré empiriquement sur les 4 images de adversarial_vision/,
pas sur un grand jeu de données validé statistiquement.

Deuxième avis (voir `second_opinion.py`, `partie_2.md`) : le flou seul est défait par une
attaque adaptative ; un modèle de vision-langage décrit l'image et un LLM juge la
compatibilité avec l'étiquette du classifieur. Incompatible : blocage.

Intégration au flux Veilleur : même verdict (`RiskVerdict`), même journal
(`results/verdicts.jsonl`, guardian="vision_detector") et même narration
Gradium que le gardien texte — `analyze_image()` fait les trois d'un coup,
exactement comme `handle_user_request` côté texte dans `orchestrator.py`.

Usage :
    python adversarial_vision/detector.py <chemin_image>
    python adversarial_vision/detector.py <chemin_image> --no-voice
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import torchvision.transforms.functional as TF
from PIL import ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vision_common import load_image_tensor, load_model, top_predictions  # noqa: E402
from second_opinion import MIN_CONFIDENCE, second_opinion  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from victim_agent.narrator import narrate_verdict, play_audio  # noqa: E402
from victim_agent.veilleur_gate import RiskVerdict, log_verdict  # noqa: E402

BLUR_RADIUS = 0.9  # calibré empiriquement, cf. note ci-dessus
LARGE_DROP_THRESHOLD = 0.50  # 50 points de confiance perdus = signal fort
GUARDIAN_NAME = "vision_detector"

# Traduction des labels ImageNet les plus courants dans ce projet — évite de
# mélanger anglais et français dans le texte narré par Gradium (un nom de
# classe anglais au milieu d'une phrase française sonne mal, et sans pause
# claire avant, peut même se souder au mot précédent, ex. "flou" + "sports"
# entendu comme "flou sport"). Repli sur le label anglais si absent du dict.
FRENCH_LABELS = {
    "cock": "un coq",
    "killer whale": "une orque",
    "grey whale": "une baleine grise",
    "pedestal": "un piédestal",
    "breakwater": "une digue",
    "fountain": "une fontaine",
    "sports car": "une voiture de sport",
    "promontory": "un promontoire",
    "cliff": "une falaise",
    "valley": "une vallée",
    "castle": "un château",
    "palace": "un palais",
    "church": "une église",
    "monastery": "un monastère",
    "alp": "une montagne",
    "car wheel": "une roue de voiture",
    "racer": "une voiture de course",
    "obelisk": "un obélisque",
    "beacon": "un phare",
}


def french_label(label: str) -> str:
    return FRENCH_LABELS.get(label, label)


def detect_manipulation(image_path: Path, model=None, categories=None) -> RiskVerdict:
    """Soumet une image à Veilleur : compare sa classification brute à sa
    classification après un léger flou. Un changement de catégorie de premier
    rang est le signal caractéristique d'une perturbation adverse — une vraie
    photo garde la même catégorie même si sa confiance varie un peu."""
    if model is None or categories is None:
        model, categories = load_model()

    x = load_image_tensor(image_path)
    label0, conf0 = top_predictions(model, x, categories, k=1)[0]

    img = TF.to_pil_image(x.squeeze(0))
    blurred = img.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS))
    x_blurred = TF.to_tensor(blurred).unsqueeze(0)
    label1, conf1 = top_predictions(model, x_blurred, categories, k=1)[0]

    drop = conf0 - conf1
    label_changed = label0 != label1
    fr0, fr1 = french_label(label0), french_label(label1)

    if label_changed and drop >= LARGE_DROP_THRESHOLD:
        return RiskVerdict(
            risk_level="élevé",
            anomaly_type="perturbation_adverse_suspectee",
            explanation=(
                "La classification change radicalement sous un léger flou. "
                f"L'image était reconnue comme {fr0}, avec {conf0:.0%} de confiance. "
                f"Après le flou, elle devient {fr1}, avec seulement {conf1:.0%} de confiance. "
                "Une vraie photo ne change pas de catégorie pour une "
                "transformation aussi légère : c'est la signature d'une "
                "perturbation adverse invisible ajoutée pour tromper un "
                "classifieur, pas d'un contenu authentique."
            ),
            recommended_action="block",
        )
    if label_changed:
        return RiskVerdict(
            risk_level="moyen",
            anomaly_type="instabilite_suspectee",
            explanation=(
                "La catégorie change sous un léger flou. "
                f"Elle passe de {fr0} à {fr1}. "
                "Mais la perte de confiance reste modérée : signe possible d'une "
                "manipulation, sans certitude suffisante pour bloquer d'emblée."
            ),
            recommended_action="ask_user",
        )
    return RiskVerdict(
        risk_level="nul",
        anomaly_type="aucune",
        explanation=(
            "La classification reste stable sous un léger flou. "
            f"L'image reste reconnue comme {fr0}, la confiance passant de "
            f"{conf0:.0%} à {conf1:.0%}. "
            "C'est cohérent avec une image authentique non manipulée."
        ),
        recommended_action="allow",
    )


def apply_second_opinion(image_path: Path, verdict: RiskVerdict, model=None, categories=None) -> RiskVerdict:
    """Deuxième avis par un modèle de vision-langage (`second_opinion.py`).

    Le détecteur de flou est défait par une attaque adaptative. Si le flou n'a rien
    bloqué et que `resnet18` est CONFIANT (au moins `MIN_CONFIDENCE`), un modèle de
    vision décrit l'image et un LLM juge si l'étiquette de `resnet18` est compatible
    avec la description. Incompatible : l'image est BLOQUÉE (option retenue ; demander
    à l'utilisateur en cas de doute reste une piste, voir `pistes_ajout.md`).

    Deuxième avis indisponible (réseau, quota) : on garde le verdict du flou en le
    signalant, ce qui laisse passer une attaque adaptative — limite documentée.
    Attention : l'image (224x224) est ENVOYÉE à l'API OpenAI."""
    if verdict.recommended_action == "block":
        return verdict  # déjà bloquée : inutile d'envoyer l'image
    if model is None or categories is None:
        model, categories = load_model()
    x = load_image_tensor(image_path)
    label, conf = top_predictions(model, x, categories, k=1)[0]
    if conf < MIN_CONFIDENCE:
        return verdict  # étiquette peu sûre : pas un indice d'attaque
    opinion = second_opinion(TF.to_pil_image(x.squeeze(0)), label, conf)
    if opinion.compatible is False:
        return RiskVerdict(
            risk_level="élevé",
            anomaly_type="perturbation_adverse_suspectee",
            explanation=(
                f"Deuxième avis : le classifieur d'images voit {french_label(label)} avec {conf:.0%} de confiance, "
                f"mais un autre modèle de vision décrit l'image ainsi : « {opinion.description} ». "
                "Les deux ne sont pas compatibles : c'est le signe d'une perturbation adverse qui trompe le classifieur "
                "sans tromper un autre modèle. Image bloquée."
            ),
            recommended_action="block",
        )
    if opinion.compatible is None:
        return RiskVerdict(
            risk_level=verdict.risk_level,
            anomaly_type=verdict.anomaly_type,
            explanation=f"{verdict.explanation} (Le deuxième avis par un modèle de vision est indisponible : seul le test du flou a été appliqué.)",
            recommended_action=verdict.recommended_action,
        )
    return verdict


async def analyze_image(image_path: Path, *, voice: bool = True, use_second_opinion: bool = True) -> RiskVerdict:
    """Boucle complète du gardien image : détecte, journalise dans le même
    historique que le texte (`results/verdicts.jsonl`), puis narre à voix
    haute si le verdict est bloquant/ambigu — même flux que
    `handle_user_request` côté texte dans `orchestrator.py`.

    `use_second_opinion=False` désactive l'envoi de l'image à un service tiers."""
    verdict = detect_manipulation(image_path)
    if use_second_opinion:
        verdict = await asyncio.to_thread(apply_second_opinion, image_path, verdict)

    log_verdict(
        source=f"image reçue : {image_path.name}",
        original_user_intent="(analyse d'image, pas de requête texte)",
        content_or_action=str(image_path),
        guardian=GUARDIAN_NAME,
        verdict=verdict,
    )

    if voice:
        try:
            audio_path = await narrate_verdict(verdict)
            if audio_path:
                print(f"🔊 Narration Gradium : {audio_path}")
                play_audio(audio_path)
        except Exception as exc:
            # Mode de secours texte (tâche 6) : la voix est un bonus, jamais
            # un point de blocage.
            print(f"(narration vocale indisponible ({exc}) — poursuite en mode texte)")

    return verdict


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", type=Path, help="Chemin de l'image à analyser.")
    parser.add_argument("--no-voice", action="store_true", help="Désactive la narration Gradium (mode de secours texte).")
    parser.add_argument(
        "--no-second-opinion",
        action="store_true",
        help="N'utilise que le test du flou : n'envoie PAS l'image à l'API OpenAI (deuxième avis par un modèle de vision).",
    )
    args = parser.parse_args()

    verdict = asyncio.run(analyze_image(args.image, voice=not args.no_voice, use_second_opinion=not args.no_second_opinion))
    print(f"Verdict : {verdict.recommended_action} ({verdict.risk_level})")
    print(verdict.explanation)


if __name__ == "__main__":
    main()
