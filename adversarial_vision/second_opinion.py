"""Deuxième avis sur une image : un modèle de vision-langage la décrit, puis un LLM
juge si l'étiquette de `resnet18` est COMPATIBLE avec la description.

Pourquoi : le détecteur de flou (`detector.py`) est défait par une attaque
adaptative (57 photos sur 57 manquées, voir `partie_2.md`). Une perturbation calculée
contre `resnet18` trompe rarement un modèle différent : si `resnet18` voit une
« voiture de sport » là où le modèle de vision décrit « la Statue de la Liberté », il y
a un désaccord que le flou n'a pas vu.

Réglage FIGÉ par la calibration de `partie_2.md` (28 photos de réglage, 29 de
vérification, Wikimedia) :
- le deuxième avis n'est demandé que si `resnet18` est CONFIANT
  (`MIN_CONFIDENCE`) : une étiquette peu sûre est souvent simplement fausse sur une
  photo saine, ce n'est pas un indice d'attaque ;
- le modèle de vision décrit l'image SANS connaître l'étiquette (pas d'influence) ;
- le juge « origine » (le moins sévère) rend compatible / incompatible.

Limites (mesurées ou par construction) : ne détecte que les étiquettes trompeuses
IMPLAUSIBLES (une plage étiquetée « seashore » passe) ; un attaquant qui se contente
d'une confiance inférieure au seuil passe ; ne protège pas contre une attaque qui
vise aussi le modèle de vision ; l'image (224x224) est ENVOYÉE à l'API OpenAI.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from victim_agent.env import load_project_env  # noqa: E402

load_project_env()  # clé OpenAI + certificats TLS

MODEL = "gpt-4o-mini"
MIN_CONFIDENCE = 0.6  # figé sur le groupe de réglage, voir partie_2.md

DESCRIBE_PROMPT = (
    "Décris en une phrase courte, en français, ce que montre cette image (sujet principal et décor). "
    "Ne fais aucune hypothèse sur ce qu'un logiciel de classification pourrait y voir."
)

JUDGE_PROMPT = """Un petit classifieur d'images (ImageNet, `resnet18`) a donné l'étiquette « {label} » (confiance {conf:.0%}) pour une image.
Une description indépendante de la même image, faite par un modèle de vision, est : « {description} ».

L'étiquette est-elle COMPATIBLE avec la description ? Les étiquettes ImageNet sont grossières : une étiquette est compatible si elle désigne le sujet, une partie du sujet ou un élément plausiblement présent dans la scène décrite. Elle est INCOMPATIBLE si elle désigne un objet ou une scène qui n'est manifestement pas dans la description.

Réponds uniquement par un objet JSON : {{"compatible": true ou false, "explication": "une phrase"}}"""


@dataclass
class SecondOpinion:
    compatible: bool | None  # None : deuxième avis indisponible
    description: str
    explanation: str


def _png_data_url(pil_image) -> str:
    buffer = io.BytesIO()
    pil_image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def second_opinion(pil_image, label: str, confidence: float) -> SecondOpinion:
    """Fonction bloquante (appels réseau). `pil_image` est l'image telle que la voit le
    classifieur (224x224). En cas d'échec, renvoie `compatible=None` : l'appelant
    décide, il ne doit pas le traiter comme un accord."""
    try:
        from openai import OpenAI

        client = OpenAI()
        description = client.chat.completions.create(
            model=MODEL,
            temperature=0,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": DESCRIBE_PROMPT},
                {"type": "image_url", "image_url": {"url": _png_data_url(pil_image)}},
            ]}],
        ).choices[0].message.content.strip()
        verdict = json.loads(client.chat.completions.create(
            model=MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": JUDGE_PROMPT.format(label=label, conf=confidence, description=description)}],
        ).choices[0].message.content)
        return SecondOpinion(bool(verdict["compatible"]), description, verdict.get("explication", ""))
    except Exception as exc:
        return SecondOpinion(None, "", f"deuxième avis indisponible ({type(exc).__name__})")
