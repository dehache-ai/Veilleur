"""Client MCP réel vers Jinko (environnement production —
`https://mcp.builders.gojinko.com/`), utilisé par
`orchestrator.py::real_search_hotels_jinko`.

⚠️ Crédits limités : la clé disponible est une clé de production (pas de
compte sandbox actif), et les crédits additionnels du hackathon n'ont pas
encore été reçus. Un compteur d'appels persistant (`JINKO_CALL_COUNTER_FILE`)
bloque tout appel au-delà de `JINKO_MAX_CALLS` — volontairement conservateur,
à ajuster consciemment (jamais par accident) une fois la situation des
crédits connue.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from victim_agent.env import load_project_env

# Clé JINKO_API_KEY : `.env` du projet (cf. env.py).
load_project_env()

JINKO_PROD_URL = "https://mcp.builders.gojinko.com/"
JINKO_CALL_COUNTER_FILE = Path(__file__).resolve().parent.parent / "results" / "jinko_call_count.json"
JINKO_MAX_CALLS = 10  # plafond conservateur — cf. note de sécurité en tête de fichier


class JinkoBudgetExceeded(RuntimeError):
    pass


def get_call_count() -> int:
    if not JINKO_CALL_COUNTER_FILE.exists():
        return 0
    return json.loads(JINKO_CALL_COUNTER_FILE.read_text(encoding="utf-8")).get("count", 0)


def _increment_call_count() -> int:
    """Vérifie le plafond puis incrémente. Lève `JinkoBudgetExceeded` sans
    rien incrémenter si le plafond est déjà atteint — l'appel réseau qui
    suivrait n'a jamais lieu dans ce cas."""
    count = get_call_count()
    if count >= JINKO_MAX_CALLS:
        raise JinkoBudgetExceeded(
            f"Plafond de {JINKO_MAX_CALLS} appels réels à Jinko atteint ({count} déjà utilisés). "
            f"Crédits limités, crédits hackathon pas encore reçus (cf. jinko_client.py). "
            f"Voir {JINKO_CALL_COUNTER_FILE} — augmenter JINKO_MAX_CALLS consciemment si besoin."
        )
    count += 1
    JINKO_CALL_COUNTER_FILE.parent.mkdir(exist_ok=True)
    JINKO_CALL_COUNTER_FILE.write_text(json.dumps({"count": count}), encoding="utf-8")
    return count


async def search_hotels(destination: str, check_in: str, check_out: str, adults: int) -> str:
    """Recherche réelle d'hôtels via Jinko (production, vrais crédits).

    Lève `JinkoBudgetExceeded` si le plafond d'appels est atteint (aucun
    appel réseau n'a alors lieu), ou toute exception réseau/API — à
    l'appelant de décider s'il retombe sur `fake_search_hotels`.
    """
    api_key = os.environ["JINKO_API_KEY"]
    count = _increment_call_count()
    print(f"⚠️  Appel réel à Jinko (crédits limités) — {count}/{JINKO_MAX_CALLS} utilisés.")

    http_client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0)
    async with streamable_http_client(JINKO_PROD_URL, http_client=http_client) as (read, write, *_rest):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "hotel_search",
                {
                    "destination": {"query": destination},
                    "checkin": check_in,
                    "checkout": check_out,
                    "adults": adults,
                    "currency": "EUR",
                },
            )
            texts = [c.text for c in result.content if getattr(c, "text", None)]
            return "\n".join(texts)
