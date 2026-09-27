"""Télécharge des photos libres de droits de Wikimedia Commons pour CALIBRER le
juge du deuxième avis LLM du pilier 2 (voir `partie_2.md`, `vlm_experiment.py`).

Usage strictement interne : ces photos servent à régler et vérifier un détecteur.
Elles sont écrites HORS du dépôt (`../calibration_images/`, au niveau du dossier
parent de `sentinel/`) pour ne jamais partir sur GitHub, et ne sont pas
redistribuées. Le fichier `manifest.json` conserve pour chacune le titre, l'auteur,
la licence et l'adresse d'origine.

Sélection reproductible et sans tri à la main : pour chaque thème de voyage, on prend
les premiers résultats de la recherche Commons dont la licence est libre (domaine
public, CC0, CC BY, CC BY-SA), le format JPEG et la taille suffisante. Le partage
réglage / vérification est fixé AU TÉLÉCHARGEMENT (graine 0), avant tout réglage du
juge : les photos de vérification ne doivent pas être regardées pendant le réglage.

Usage :
    python adversarial_vision/fetch_calibration_images.py
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from victim_agent.env import load_project_env  # noqa: E402

load_project_env()  # certificats TLS (bundle système) avant tout appel réseau

import httpx  # noqa: E402

OUT_DIR = HERE.parent.parent / "calibration_images"  # hors de sentinel/
API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {"User-Agent": "VeilleurCalibration/0.1 (projet de hackathon, usage de recherche interne)"}
PER_THEME = 3
THUMB_WIDTH = 800
ALLOWED = re.compile(r"^(public domain|cc0|cc[ -]by(-sa)?[ -]?\d|cc[ -]by(-sa)?$|pd)", re.IGNORECASE)

THEMES = [
    "Eiffel Tower", "Colosseum Rome", "sandy beach", "hotel room interior", "restaurant dish food",
    "street market", "alpine lake", "cathedral interior", "old stone bridge", "railway station",
    "museum gallery", "lighthouse coast", "medieval castle", "waterfall", "city skyline night",
    "cafe terrace", "swimming pool resort", "harbour fishing boats", "Buddhist temple", "desert dunes",
]


def search(client: httpx.Client, term: str) -> list[dict]:
    params = {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{term} filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": 25, "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": THUMB_WIDTH,
    }
    data = None
    for attempt in range(4):  # Commons limite le débit : nouvel essai avec attente si la réponse n'est pas du JSON
        resp = client.get(API, params=params, timeout=30)
        try:
            data = resp.json()
            break
        except ValueError:
            print(f"  réponse non JSON (HTTP {resp.status_code}), nouvel essai dans {5 * (attempt + 1)} s")
            time.sleep(5 * (attempt + 1))
    if data is None:
        return []
    pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 0))
    return pages


def acceptable(page: dict) -> dict | None:
    info = (page.get("imageinfo") or [{}])[0]
    meta = info.get("extmetadata", {})
    license_name = meta.get("LicenseShortName", {}).get("value", "")
    if info.get("mime") != "image/jpeg" or min(info.get("width", 0), info.get("height", 0)) < 500:
        return None
    if not ALLOWED.match(license_name.strip()):
        return None
    if meta.get("Restrictions", {}).get("value"):  # marque déposée, personnalité, etc.
        return None
    return {
        "title": page["title"],
        "author": re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "inconnu"))[:120],
        "license": license_name,
        "source_page": info.get("descriptionurl"),
        "thumb_url": info.get("thumburl") or info.get("url"),
    }


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "images").mkdir(exist_ok=True)
    rng = random.Random(0)
    manifest = []
    with httpx.Client(headers=HEADERS, follow_redirects=True) as client:
        for theme in THEMES:
            kept = 0
            for page in search(client, theme):
                item = acceptable(page)
                if not item:
                    continue
                name = f"{len(manifest) + 1:03d}_{re.sub(r'[^a-z0-9]+', '_', theme.lower()).strip('_')}.jpg"
                target = OUT_DIR / "images" / name
                if not target.exists():  # déjà téléchargé lors d'un essai précédent
                    try:
                        resp = client.get(item["thumb_url"], timeout=60)
                        resp.raise_for_status()
                    except Exception as exc:
                        print(f"  ignoré ({exc}) : {item['title']}")
                        continue
                    target.write_bytes(resp.content)
                    time.sleep(0.5)  # courtoisie envers Wikimedia
                manifest.append({"file": name, "theme": theme, **item})
                kept += 1
                if kept == PER_THEME:
                    break
            print(f"{theme:24} : {kept} photo(s)")
    # Partage réglage / vérification, fixé maintenant.
    order = list(range(len(manifest)))
    rng.shuffle(order)
    half = len(order) // 2
    for rank, idx in enumerate(order):
        manifest[idx]["split"] = "reglage" if rank < half else "verification"
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "LISEZ-MOI.txt").write_text(
        "Photos de Wikimedia Commons pour calibrer le juge du pilier 2 (usage interne).\n"
        "NE PAS PUBLIER ce dossier : les licences (CC BY, CC BY-SA...) imposent une attribution\n"
        "et ces images n'ont pas vocation à être redistribuées ici. Titre, auteur, licence et\n"
        "adresse de chaque photo : manifest.json.\n",
        encoding="utf-8",
    )
    n_dev = sum(1 for m in manifest if m["split"] == "reglage")
    print(f"\n{len(manifest)} photos dans {OUT_DIR} ({n_dev} réglage, {len(manifest) - n_dev} vérification)")


if __name__ == "__main__":
    main()
