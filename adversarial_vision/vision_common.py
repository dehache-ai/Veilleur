"""Code partagé entre `generate_attack.py` (fabrication des attaques,
tourne dans n'importe quel environnement ayant torch) et `detector.py` (détection en
temps réel, tourne dans l'environnement du projet) : chargement du classifieur
ImageNet pré-entraîné et prétraitement des images.

Les deux scripts utilisent obligatoirement le même prétraitement — sinon le
détecteur ne verrait pas exactement l'image que l'attaquant a fabriquée.
"""

from __future__ import annotations

from pathlib import Path

import torch
import torch.nn as nn
import torchvision.transforms.functional as TF
from PIL import Image, ImageOps
from torchvision.models import ResNet18_Weights, resnet18


class NormalizedModel(nn.Module):
    """Enveloppe le modèle avec la normalisation ImageNet, pour que toute
    perturbation ou tout epsilon reste défini en espace pixel [0,1] brut
    (plus interprétable et plus facile à borner que dans l'espace normalisé)."""

    def __init__(self, model: nn.Module, mean: list[float], std: list[float]) -> None:
        super().__init__()
        self.model = model
        self.register_buffer("mean", torch.tensor(mean).view(1, 3, 1, 1))
        self.register_buffer("std", torch.tensor(std).view(1, 3, 1, 1))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.model((x - self.mean) / self.std)


def load_model() -> tuple[NormalizedModel, list[str]]:
    weights = ResNet18_Weights.IMAGENET1K_V1
    base_model = resnet18(weights=weights)
    base_model.eval()
    categories = weights.meta["categories"]
    model = NormalizedModel(base_model, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
    model.eval()
    return model, categories


def load_image_tensor(path: Path) -> torch.Tensor:
    """Charge une image et la prépare pour le classifieur : recadrage carré
    minimal (côté court redimensionné directement à 224, puis crop centré
    uniquement sur le côté long) — un simple resize écrasé en carré déforme
    l'image et peut suffire à perturber la classification sur une photo très
    haute ou très large ; à l'inverse, le pipeline standard ImageNet
    (resize à 256 puis crop à 224) rogne inutilement les deux côtés en plus
    du côté long. Cette version minimale ne perd aucun pixel sur le côté
    déjà court (donc jamais de recadrage en haut/bas d'une photo panoramique
    par exemple). exif_transpose corrige aussi la rotation des photos de
    téléphone/appareil stockées avec une orientation EXIF."""
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    w, h = img.size
    short_side = min(w, h)
    new_w, new_h = round(w * 224 / short_side), round(h * 224 / short_side)
    img = img.resize((new_w, new_h))
    left, top = (new_w - 224) // 2, (new_h - 224) // 2
    img = img.crop((left, top, left + 224, top + 224))
    tensor = TF.to_tensor(img).unsqueeze(0)  # [1, 3, 224, 224], valeurs dans [0, 1]
    return tensor


def top_predictions(model: NormalizedModel, x: torch.Tensor, categories: list[str], k: int = 3) -> list[tuple[str, float]]:
    with torch.no_grad():
        probs = torch.softmax(model(x), dim=1)[0]
    top_probs, top_idx = probs.topk(k)
    return [(categories[i], float(p)) for p, i in zip(top_probs, top_idx)]
