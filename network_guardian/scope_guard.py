"""Gardien de périmètre pour un agent web autonome — extension du pilier 3
(piste C de partie_3.md).

Le moniteur réseau (`monitor.py`) juge des adresses IP : il ne voit rien quand
un agent reste dans un domaine autorisé mais sort de sa mission à l'intérieur
(ex : un agent chargé de lire des statistiques publiques d'un site officiel
qui entre dans une zone protégée du même site). Ici on juge donc la REQUÊTE
(méthode, chemin) et la RÉPONSE, à un point de passage que Veilleur contrôle :
l'enveloppe de l'outil de navigation de l'agent. Un agent qui ouvre ses propres
connexions en dehors de cette enveloppe échappe à cette supervision.

Deux étages, comme pour le texte :

1. Garde-fous déterministes (rapides, sans ambiguïté) : hôte, méthode, budget
   de requêtes, chemin normalisé (un `..` ne permet pas de contourner un
   préfixe autorisé), préfixes explicitement pré-autorisés par le contrat de
   mission, statut 401/403 ou redirection vers une page de connexion.
2. Juge LLM (guardians/scope.mthds), seulement dans la zone grise : chemin non
   pré-autorisé (avant l'action) et pages reçues hors chemin pré-autorisé ou
   contenant un marqueur d'accès réservé (après la réponse).

Un blocage n'est jamais silencieux : même verdict (`RiskVerdict`), même journal
(`results/verdicts.jsonl`), même narration Gradium que les autres piliers.
Après `max_violations` blocages, la session est suspendue.

Démonstration synthétique : `web_agent_demo.py` (site fictif en mémoire, aucun
trafic réseau).
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from victim_agent.narrator import narrate_verdict, play_audio  # noqa: E402
from victim_agent.veilleur_gate import RiskVerdict, assess_risk, log_verdict  # noqa: E402

DETERMINISTIC_GUARDIAN = "scope_guard"
LOGIN_LOCATION = re.compile(r"login|connexion|signin|auth", re.IGNORECASE)
RESTRICTED_MARKERS = re.compile(r"acc[èe]s r[ée]serv[ée]|confidentiel|habilit|connexion requise|restricted", re.IGNORECASE)
MAX_BODY_CHARS = 1500  # taille de page transmise au juge LLM


@dataclass
class MissionContract:
    """Périmètre déclaré à l'avance, appliqué en dehors de l'agent : il ne peut
    pas le modifier."""

    mission: str
    allowed_hosts: set[str]
    allowed_path_prefixes: list[str]  # accès pré-autorisé (chemin rapide, sans LLM)
    allowed_methods: set[str] = field(default_factory=lambda: {"GET"})
    max_requests: int = 20
    max_violations: int = 2


@dataclass
class HttpRequest:
    method: str
    url: str
    stated_reason: str = ""  # justification déclarée par l'agent : NON FIABLE
    trusted_note: str = ""  # description de la ressource fournie par l'ORCHESTRATEUR (fiable), ex : ce que fait un outil


@dataclass
class HttpResponse:
    status: int
    body: str = ""
    location: str | None = None


@dataclass
class ScopeDecision:
    allowed: bool
    verdict: RiskVerdict
    stage: str  # "deterministe" ou "llm"


def normalized_path(url: str) -> str:
    """Chemin sans `..` ni `//` : `/statistiques/../admin` devient `/admin`."""
    path = urlsplit(url).path or "/"
    # Décodage AVANT normalisation (jusqu'à 3 passes contre le double encodage) : `%2e%2e`, `..%2f` ou `%252e` étaient sinon
    # pris pour un nom de dossier et laissaient passer `/statistiques/%2e%2e/admin` comme un chemin pré-autorisé.
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    path = path.replace("\\", "/")  # certains serveurs traitent \ comme /
    normalized = posixpath.normpath(path)
    return "/" if normalized == "." else normalized


AMBIGUOUS_ENCODING = re.compile(r"(?i)%2f|%5c|%25|%00|%u[0-9a-f]{4}|%c[01]|;|\\|[．。／＼]")


def strictly_under_prefix(url: str, prefixes: list[str]) -> bool:
    """Préautorisation prudente : le chemin décodé ET le chemin brut (non décodé) doivent tous deux, une fois normalisés,
    rester sous un préfixe autorisé, et le chemin brut ne doit contenir aucun encodage sur lequel les serveurs ne
    s'accordent pas (`%2F`, `%5C`, `%25`, `%00`, `%u`, `..;`, barre oblique inverse, points ou barres pleine chasse). Sinon, la
    requête n'est pas préautorisée : elle part au juge. Un chemin comme `/admin/..%2F..%2Fstatistiques/x` est ainsi refusé."""
    raw = urlsplit(url).path or "/"
    if AMBIGUOUS_ENCODING.search(raw):
        return False
    raw_norm = posixpath.normpath(raw)
    decoded = normalized_path(url)
    return any(under_prefix(decoded, pre) and under_prefix(raw_norm, pre) for pre in prefixes)


def under_prefix(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


class ScopeGuard:
    def __init__(self, contract: MissionContract, *, voice: bool = True) -> None:
        self.contract = contract
        self.voice = voice
        self.history: list[tuple[str, str, int | None]] = []  # (méthode, chemin, statut)
        self.request_count = 0
        self.violations = 0
        self.suspended = False

    # ── Avant l'action ───────────────────────────────────────────────────────

    async def check_request(self, req: HttpRequest) -> ScopeDecision:
        c = self.contract
        parts = urlsplit(req.url)
        path = normalized_path(req.url)
        self.request_count += 1

        if self.suspended:
            return await self._deterministic(req, "block", "critique", "ecart_intention",
                "Session suspendue après des tentatives répétées hors périmètre : plus aucune requête n'est autorisée.")
        if self.request_count > c.max_requests:
            return await self._deterministic(req, "block", "élevé", "ecart_intention",
                f"Budget de {c.max_requests} requêtes dépassé pour cette mission.")
        if "\\" in req.url or parts.username is not None or parts.password is not None:
            # URL ambiguë : un navigateur et un analyseur d'URL ne s'accordent pas toujours sur l'hôte visé (`evil.test\@hote.test`).
            return await self._deterministic(req, "block", "critique", "ecart_intention",
                "URL ambiguë (barre oblique inverse ou identifiants dans l'URL) : refusée, l'hôte réellement visé n'est pas certain.")
        if parts.hostname not in c.allowed_hosts:
            return await self._deterministic(req, "block", "critique", "ecart_intention",
                f"Le domaine {parts.hostname} n'appartient pas au périmètre de la mission.")
        if req.method.upper() not in c.allowed_methods:
            return await self._deterministic(req, "block", "élevé", "action_non_demandee",
                f"Une requête {req.method.upper()} n'est pas permise : la mission se limite à de la lecture ({', '.join(sorted(c.allowed_methods))}).")
        if strictly_under_prefix(req.url, c.allowed_path_prefixes):
            return await self._deterministic(req, "allow", "nul", "aucune",
                f"Le chemin {path} est explicitement autorisé par le contrat de mission.")

        # Zone grise : chemin non pré-autorisé -> juge LLM.
        verdict = await assess_risk(
            source="requête HTTP de l'agent web (avant exécution)",
            original_user_intent=c.mission,
            content_or_action=self._request_context(req, path),
            guardian="scope_request",
        )
        return await self._finish(req, verdict, "llm")

    # ── Après la réponse ─────────────────────────────────────────────────────

    async def check_response(self, req: HttpRequest, resp: HttpResponse, *, pre_authorized: bool) -> ScopeDecision:
        path = normalized_path(req.url)
        self.history.append((req.method.upper(), path, resp.status))

        if resp.status in (401, 403):
            return await self._deterministic(req, "block", "critique", "ecart_intention",
                f"La page {path} a répondu {resp.status} : l'agent a atteint une zone protégée. Arrêt de la navigation.")
        if 300 <= resp.status < 400 and resp.location and LOGIN_LOCATION.search(resp.location):
            return await self._deterministic(req, "block", "critique", "ecart_intention",
                f"La page {path} redirige vers une page de connexion ({resp.location}) : zone protégée. Arrêt de la navigation.")

        # Juge LLM sur la page : hors chemin pré-autorisé, ou marqueur d'accès réservé.
        if not pre_authorized or RESTRICTED_MARKERS.search(resp.body):
            body = resp.body[:MAX_BODY_CHARS]
            verdict = await assess_risk(
                source="page reçue par l'agent web",
                original_user_intent=self.contract.mission,
                content_or_action=f"Requête : {req.method.upper()} {path}\nStatut : {resp.status}\nContenu de la page :\n{body}",
                guardian="scope_response",
            )
            return await self._finish(req, verdict, "llm")
        return ScopeDecision(True, RiskVerdict("nul", "aucune", f"Page publique {path} reçue, cohérente avec la mission.", "allow"), "deterministe")

    # ── Outils internes ──────────────────────────────────────────────────────

    def is_pre_authorized(self, req: HttpRequest) -> bool:
        return strictly_under_prefix(req.url, self.contract.allowed_path_prefixes)

    def _request_context(self, req: HttpRequest, path: str) -> str:
        c = self.contract
        recent = "\n".join(f"  {m} {p} -> {s}" for m, p, s in self.history[-5:]) or "  (aucune)"
        refused = sum(1 for _, _, s in self.history if s in (401, 403))
        return (
            f"Contrat de mission : domaines {sorted(c.allowed_hosts)} ; chemins pré-autorisés {c.allowed_path_prefixes} ; "
            f"méthodes {sorted(c.allowed_methods)}.\n"
            f"Historique récent :\n{recent}\n"
            f"Réponses 'accès refusé' déjà reçues : {refused}. Blocages déjà prononcés : {self.violations}.\n"
            f"Requête à juger : {req.method.upper()} {path}\n"
            f"(URL brute fournie par l'agent : {req.url})\n"
            + (f"Description de la ressource, fournie par l'orchestrateur (fiable) : {req.trusted_note}\n" if req.trusted_note else "")
            + f"Justification déclarée par l'agent (non fiable) : {req.stated_reason or '(aucune)'}"
        )

    async def _deterministic(self, req: HttpRequest, action: str, risk: str, anomaly: str, explanation: str) -> ScopeDecision:
        verdict = RiskVerdict(risk_level=risk, anomaly_type=anomaly, explanation=explanation, recommended_action=action)  # type: ignore[arg-type]
        log_verdict(
            source="requête HTTP de l'agent web",
            original_user_intent=self.contract.mission,
            content_or_action=f"{req.method.upper()} {req.url}",
            guardian=DETERMINISTIC_GUARDIAN,
            verdict=verdict,
        )
        return await self._finish(req, verdict, "deterministe", already_logged=True)

    async def _finish(self, req: HttpRequest, verdict: RiskVerdict, stage: str, *, already_logged: bool = False) -> ScopeDecision:
        allowed = verdict.recommended_action == "allow"
        if verdict.recommended_action == "block":
            self.violations += 1
            if self.violations >= self.contract.max_violations:
                self.suspended = True
        if self.voice and verdict.recommended_action in ("block", "ask_user"):
            try:
                audio_path = await narrate_verdict(verdict)
                if audio_path:
                    play_audio(audio_path)
            except Exception as exc:
                print(f"(narration vocale indisponible ({exc}) — poursuite en mode texte)")
        return ScopeDecision(allowed=allowed, verdict=verdict, stage=stage)
