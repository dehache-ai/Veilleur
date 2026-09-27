"""Bascule le backend LLM de Veilleur entre Ollama (dev local) et OpenAI (démo).

Met à jour de façon cohérente, en une seule commande, les 3 endroits dont
dépend la résolution de l'alias stable `@veilleur-model` référencé par tous
les pipes Veilleur (cf. décision actée #3 du cahier des charges interne (non publié)) :

1. ~/.pipelex/inference/deck/x_custom_llm_deck.toml
   -> la cible réelle de l'alias `veilleur-model`.
2. ~/.pipelex/inference/backends.toml
   -> active/désactive le backend OpenAI (Ollama reste toujours activé : ça
      ne coûte rien de le laisser tel quel et ça sert de filet de secours).
3. ~/.pipelex/inference/routing_profiles.toml
   -> un profil de routage DÉDIÉ à Veilleur ("veilleur"), qui ne route QUE le
      handle utilisé par notre alias vers le bon backend, sans jamais toucher
      au profil global par défaut de la machine (`all_ollama`) — pour ne
      perturber aucun autre pipe/projet Pipelex qui tournerait sur cette même
      machine avec ses propres handles.

Usage :
    python switch_backend.py ollama    # dev local, gratuit, plus lent
    python switch_backend.py openai --model gpt-4.1-mini   # autre modèle du même backend
    python switch_backend.py openai    # démo finale, rapide, nécessite OPENAI_API_KEY dans .env
    python switch_backend.py --status  # affiche la configuration actuelle sans rien modifier
"""

from __future__ import annotations

import argparse
from pathlib import Path

import tomlkit

PIPELEX_DIR = Path.home() / ".pipelex"
DECK_FILE = PIPELEX_DIR / "inference" / "deck" / "x_custom_llm_deck.toml"
BACKENDS_FILE = PIPELEX_DIR / "inference" / "backends.toml"
ROUTING_FILE = PIPELEX_DIR / "inference" / "routing_profiles.toml"

ALIAS_NAME = "veilleur-model"
CHANNEL_ALIAS_NAME = "veilleur-model-channel"  # second modèle : juges de message et de sortie du canal entre agents
ROUTING_PROFILE_NAME = "veilleur"

BACKEND_TARGETS = {
    "ollama": {"model_handle": "gpt-oss-20b", "channel_model_handle": "gpt-oss-20b", "backend": "ollama"},
    "openai": {"model_handle": "gpt-4o-mini", "channel_model_handle": "gpt-4.1-mini", "backend": "openai"},
}


def _load(path: Path) -> tomlkit.TOMLDocument:
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _save(path: Path, doc: tomlkit.TOMLDocument) -> None:
    backup = path.with_name(path.name + ".bak")
    if not backup.exists():
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def _update_alias(target_handle: str, channel_handle: str) -> None:
    doc = _load(DECK_FILE)
    llm = doc.setdefault("llm", tomlkit.table())
    aliases = llm.setdefault("aliases", tomlkit.table())
    aliases[ALIAS_NAME] = target_handle
    aliases[CHANNEL_ALIAS_NAME] = channel_handle
    _save(DECK_FILE, doc)


def _update_backend_enabled(backend_name: str) -> None:
    doc = _load(BACKENDS_FILE)
    if "ollama" in doc:
        doc["ollama"]["enabled"] = True
    if "openai" in doc:
        doc["openai"]["enabled"] = backend_name == "openai"
    _save(BACKENDS_FILE, doc)


def _update_routing(target_handle: str, channel_handle: str, backend_name: str) -> None:
    doc = _load(ROUTING_FILE)
    profiles = doc.setdefault("profiles", tomlkit.table())
    profile = profiles.setdefault(ROUTING_PROFILE_NAME, tomlkit.table())
    profile["description"] = (
        "Profil dédié à Veilleur (switch_backend.py) : ne route que le "
        "handle utilisé par @veilleur-model, laisse tout le reste sur le "
        "comportement par défaut de la machine (ollama)."
    )
    profile["default"] = "ollama"
    routes = tomlkit.table()
    if backend_name != "ollama":
        routes[target_handle] = backend_name
        routes[channel_handle] = backend_name
    profile["routes"] = routes
    doc["active"] = ROUTING_PROFILE_NAME
    _save(ROUTING_FILE, doc)


def switch_to(backend_name: str, model: str | None = None, channel_model: str | None = None) -> None:
    if backend_name not in BACKEND_TARGETS:
        raise SystemExit(f"Backend inconnu : {backend_name!r} (attendu 'ollama' ou 'openai')")
    target = dict(BACKEND_TARGETS[backend_name])
    if model:
        # Le handle doit exister dans ~/.pipelex/inference/backends/<backend>.toml
        # (ex: gpt-4o-mini, gpt-4.1-mini, gpt-5-mini pour openai).
        target["model_handle"] = model
        # Sans --channel-model, le second modèle suit le modèle choisi (un seul modèle pour tout).
        target["channel_model_handle"] = channel_model or model
    elif channel_model:
        target["channel_model_handle"] = channel_model
    _update_alias(target["model_handle"], target["channel_model_handle"])
    _update_backend_enabled(target["backend"])
    _update_routing(target["model_handle"], target["channel_model_handle"], target["backend"])
    print(f"OK : @{ALIAS_NAME} -> {target['model_handle']} ; @{CHANNEL_ALIAS_NAME} -> {target['channel_model_handle']} (backend {target['backend']}, profil de routage 'veilleur' actif).")
    if backend_name == "openai":
        print("Rappel : OPENAI_API_KEY doit être renseigné dans le .env du projet avant de lancer un pipe.")


def show_status() -> None:
    deck = _load(DECK_FILE)
    routing = _load(ROUTING_FILE)
    backends = _load(BACKENDS_FILE)
    alias_target = deck.get("llm", {}).get("aliases", {}).get(ALIAS_NAME, "(non défini)")
    active_profile = routing.get("active", "(non défini)")
    ollama_enabled = backends.get("ollama", {}).get("enabled", "?")
    openai_enabled = backends.get("openai", {}).get("enabled", "?")
    channel_target = deck.get("llm", {}).get("aliases", {}).get(CHANNEL_ALIAS_NAME, "(non défini : relancer switch_backend.py)")
    print(f"@{ALIAS_NAME:20s} -> {alias_target}")
    print(f"@{CHANNEL_ALIAS_NAME:20s} -> {channel_target}")
    print(f"Profil de routage actif : {active_profile}")
    print(f"Backend ollama enabled  : {ollama_enabled}")
    print(f"Backend openai enabled  : {openai_enabled}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backend", nargs="?", choices=sorted(BACKEND_TARGETS), help="Backend cible.")
    parser.add_argument(
        "--model",
        help="Handle du modèle à utiliser sur ce backend (défaut : celui de BACKEND_TARGETS, ex: gpt-4o-mini pour openai).",
    )
    parser.add_argument(
        "--channel-model",
        help="Handle du second modèle (juges de message et de sortie du canal entre agents). Défaut : gpt-4.1-mini pour openai ; "
        "avec --model seul, le même modèle que --model.",
    )
    parser.add_argument("--status", action="store_true", help="Affiche la configuration actuelle sans rien modifier.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.backend or args.status:
        show_status()
        return
    switch_to(args.backend, args.model, args.channel_model)


if __name__ == "__main__":
    main()
