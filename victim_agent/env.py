"""Chargement centralisé du `.env` (clés API : GRADIUM_API_KEY, JINKO_API_KEY,
OPENAI_API_KEY).

Le `.env` du DOSSIER DU PROJET est chargé (voir `.env.example` pour la liste des
variables). Le `.env` du dossier PARENT n'est chargé que sur demande explicite
(variable `VEILLEUR_ENV_PARENT=1`, dans l'environnement ou dans le `.env` du
projet) : sans cela, un dépôt cloné dans un dossier qui contient un `.env` sans
rapport avec le projet en lirait les variables sans que personne l'ait demandé.
Le premier fichier qui définit une variable gagne (aucun n'écrase l'autre). Réserve : Pipelex charge lui-même le `.env` du
dossier COURANT en écrasant les valeurs présentes : lancer les commandes depuis le dossier du projet.

À appeler AVANT toute utilisation de Pipelex : il résout `${OPENAI_API_KEY}`
depuis l'environnement du process, et ne lit lui-même qu'un `.env` situé dans
le dossier courant ou dans `~/.pipelex/` — jamais dans un dossier parent.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import tempfile
from pathlib import Path

from dotenv import load_dotenv

SENTINEL_ROOT = Path(__file__).resolve().parent.parent

_loaded = False


def _ensure_windows_ca_bundle() -> None:
    """Windows uniquement : bundle de certificats TLS = `certifi` + magasin de
    confiance de Windows + éventuel `SSL_CERT_FILE` déjà défini.

    Sur certaines machines (antivirus / proxy qui inspecte le TLS), Windows
    fait confiance à une autorité locale que ni `certifi` ni le `cacert.pem`
    de conda ne connaissent : l'appel HTTPS échoue alors en
    CERTIFICATE_VERIFY_FAILED (vu avec api.openai.com, et avec Jinko quand
    `SSL_CERT_FILE` pointe sur le cacert.pem de conda). Le bundle est un
    sur-ensemble de ce qui existait déjà, donc sans effet néfaste sur une
    machine saine.

    Passe par la variable d'environnement `SSL_CERT_FILE` (lue par httpx) et
    NON par un patch global de `ssl` (type `pip-system-certs`, qui casse
    `jinko_client.py`). Best-effort : toute erreur est ignorée, on ne veut
    jamais empêcher le programme de démarrer pour ça.
    """
    if not hasattr(ssl, "enum_certificates"):
        return  # Linux/macOS : le magasin système est déjà géré par OpenSSL
    try:
        import certifi

        existing = os.environ.get("SSL_CERT_FILE")
        # Nom de fichier lié au SSL_CERT_FILE d'origine : un changement de
        # cette variable régénère un bundle différent (pas de cache périmé).
        tag = hashlib.sha1((existing or "").encode()).hexdigest()[:8]
        bundle = Path(tempfile.gettempdir()) / f"veilleur_ca_bundle_{tag}.pem"
        if not bundle.is_file():
            pems = [Path(certifi.where()).read_text(encoding="utf-8")]
            if existing and Path(existing).is_file() and Path(existing) != bundle:
                pems.append(Path(existing).read_text(encoding="utf-8"))
            for store in ("ROOT", "CA"):
                for der, encoding, _trust in ssl.enum_certificates(store):
                    if encoding == "x509_asn":
                        pems.append(ssl.DER_cert_to_PEM_cert(der))
            bundle.write_text("\n".join(pems), encoding="utf-8")
        os.environ["SSL_CERT_FILE"] = str(bundle)
    except Exception:
        pass


def load_project_env() -> None:
    """Idempotent : charge le `.env` du projet, puis celui du dossier parent SEULEMENT si
    `VEILLEUR_ENV_PARENT` est à 1/true/oui, et prépare les certificats TLS
    (cf. `_ensure_windows_ca_bundle`)."""
    global _loaded
    if _loaded:
        return
    project_env = SENTINEL_ROOT / ".env"
    if project_env.is_file():
        load_dotenv(project_env, override=False)
    if os.environ.get("VEILLEUR_ENV_PARENT", "").strip().lower() in {"1", "true", "yes", "oui"}:
        parent_env = SENTINEL_ROOT.parent / ".env"
        if parent_env.is_file():
            load_dotenv(parent_env, override=False)
    _ensure_windows_ca_bundle()
    _loaded = True
