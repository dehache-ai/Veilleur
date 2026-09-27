"""Supervision d'un canal entre agents (collusion) — piste B de partie_3.md,
prototype.

Problème : plusieurs agents peuvent se coordonner pour faire ce qu'aucun ne
ferait seul (l'agent A lit une donnée sensible, l'agent B peut émettre vers
l'extérieur, A transmet à B). Chaque agent respecte sa propre règle : le
moniteur réseau ne voit rien. On ne prétend PAS comprendre tout ce qui
circule : les couches déterministes détectent des propriétés (forme, présence
d'une donnée connue sous une forme transformée, cumul de capacités), et le LLM
n'est appelé que sur les messages signalés, pour juger si ce qui reste lisible
ressemble à du langage de tâche.

Condition préalable : tous les messages passent par `ChannelGuard.inspect()`,
un point de passage que Veilleur contrôle (médiateur). Un canal qui le
contourne échappe à la supervision.

Couches, de la plus rapide à la plus fine :
0. marqueurs d'injection connus (`victim_agent/heuristics.py`), déterministe ;
1. suivi de la circulation d'une donnée sensible, y compris transformée
   (base64, hexadécimal, inversée, ROT13, lettres espacées ou séparées par des
   caractères invisibles, acrostiche SIMPLE : initiales des lignes, des mots ou des phrases),
   déterministe. Les autres schémas de stéganographie ne sont PAS détectés (voir partie_3.md) ;
2. forme du message (caractères invisibles, blobs base64/hexadécimal, charabia,
   entropie), déterministe : un signal, pas une preuve ;
3. cumul des capacités : flux d'un agent qui lit du sensible vers un agent qui
   peut émettre vers l'extérieur, déterministe : examen prioritaire ;
4. juge LLM (guardians/collusion.mthds) sur les messages signalés par 2 ou 3 ;
   (en parallèle, hors message : contrôle des SORTIES de chaque agent,
   `record_action(..., destination=..., content=...)` : la donnée sensible est
   cherchée dans le contenu sortant, quelle que soit la destination
   (déterministe) ; une destination hors de la liste autorisée de l'agent est
   jugée par un LLM, qui refuse par défaut) ;
5. audit de trajectoire (LLM) : relit le journal de toute la session (messages
   et actions) et juge si l'ensemble est cohérent avec la mission. Un verdict
   qui n'est pas « autorisé » doit citer des événements du journal, sinon il
   est rétrogradé en demande de confirmation.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import math
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from victim_agent.heuristics import heuristic_scan  # noqa: E402
from victim_agent.narrator import narrate_verdict, play_audio  # noqa: E402
from victim_agent.preprocess import HIDING_FORMAT_CHARS, canonical_text, is_format_char, tag_text  # noqa: E402
from victim_agent.veilleur_gate import RiskVerdict, assess_risk, log_verdict  # noqa: E402

DETERMINISTIC_GUARDIAN = "channel_guard"
INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff"
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/_-]{24,}={0,2}")
HEX_BLOB = re.compile(r"(?:[0-9a-fA-F]{2}){16,}")
ODD_SYMBOLS = re.compile(r"[^\w\s.,;:!?'’\-()€%/\"«»\n]", re.UNICODE)
# Décodage : on décode dès 12 caractères (un secret coupé en morceaux donne de courts blobs), la CHASSE au secret se
# fait ensuite sur tout le texte décodé. Le signal de FORME (juge LLM) garde les seuils longs ci-dessus.
BASE64_DECODE = re.compile(r"[A-Za-z0-9+/_-]{12,}={0,2}")
HEX_DECODE = re.compile(r"(?:[0-9a-fA-F]{2}){6,}")
MIN_FRAGMENT = 12  # longueur minimale d'un fragment de secret cherché dans un message en clair
MIN_FRAGMENT_DECODED = 9  # ... et dans un texte issu d'un décodage (base64, hexadécimal, caractères « tag ») : déjà suspect
MIN_FRAGMENT_MIXED = 9  # ... en clair, si le fragment mêle chiffres et lettres (allure d'identifiant, pas de mot courant)
MIN_ACROSTIC = 5  # longueur minimale d'un morceau de secret cherché dans les initiales (acrostiche SIMPLE)
# Mots qui désignent une donnée sensible : un message qui les mentionne (par exemple pour la
# DEMANDER) est envoyé au juge, même dans un sens de circulation sans risque particulier.
SENSITIVE_WORDS = re.compile(
    r"(?i)(num[ée]ro de (carte|compte)|carte (bancaire|de cr[ée]dit)|mot de passe|password|\biban\b|\bcvv\b|code (secret|pin)|passeport|\bjeton\b|\btoken\b|\bsecret\b)"
)
# Motifs de données personnelles courantes dans un contenu SORTANT : suite de 13 à 19 chiffres (carte
# bancaire, avec ou sans espaces ou tirets) ou IBAN. Un indice envoyé au juge, pas un verdict.
PII_PATTERN = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


@dataclass
class AgentProfile:
    name: str
    role: str
    capabilities: set[str] = field(default_factory=set)  # "reads_sensitive", "egress"
    # Destinations externes autorisées pour cet agent (noms d'hôte). Vide : aucune
    # sortie autorisée. Contrôle déterministe appliqué par `record_action`.
    allowed_destinations: set[str] = field(default_factory=set)


@dataclass
class ChannelMessage:
    sender: str
    receiver: str
    text: str


@dataclass
class ChannelDecision:
    delivered: bool
    verdict: RiskVerdict
    stage: str  # "deterministe" ou "llm"
    signals: list[str]


@dataclass
class JournalEvent:
    """Une ligne du journal de session lu par l'audit de trajectoire."""

    event_id: str  # "E1", "E2"...
    kind: str  # "message" ou "action"
    text: str
    # État posé par le CODE du gardien (jamais lu dans le texte, que les agents peuvent écrire) :
    # "delivered", "refused", "refused_sensitive", "pending", "egress_refused", "egress_pending". Vide : événement écrit à la
    # main (jeux de test), le résumé retombe alors sur la recherche dans le texte.
    status: str = ""


TAG_LOW, TAG_HIGH = 0xE0000, 0xE007F  # caractères « tag » Unicode : texte ASCII invisible
_is_format_char = is_format_char
_tag_text = tag_text


def _canonical(text: str) -> str:
    """Forme canonique pour la chasse au secret : normalisation partagée (victim_agent/preprocess.py), en minuscules."""
    return canonical_text(text, lower=True)


def _normalize(text: str) -> str:
    """Forme canonique, sans espaces ni ponctuation."""
    return re.sub(r"[\W_]+", "", _canonical(text))


def _initials_forms(text: str) -> dict[str, str]:
    """Initiales du texte selon trois découpages SIMPLES : lignes, mots, phrases (minuscules, sans accents).
    Volontairement limité : les autres schémas (deuxième lettre, un mot sur trois, longueurs...) ne sont pas
    couverts, ils sont en nombre infini."""
    def first(chunk: str) -> str:
        for ch in unicodedata.normalize("NFKD", chunk):
            if ch.isalnum():
                return ch.lower()
        return ""

    lines = [c for c in text.split("\n") if c.strip()]
    words = re.findall(r"[^\W_]+", text)
    sentences = [c for c in re.split(r"(?<=[.!?])\s+|\n+", text) if c.strip()]
    return {
        "premières lettres des lignes": "".join(first(c) for c in lines),
        "premières lettres des mots": "".join(first(c) for c in words),
        "premières lettres des phrases": "".join(first(c) for c in sentences),
    }


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    total = len(text)
    return -sum(c / total * math.log2(c / total) for c in counts.values())


def _decoded_candidates(text: str) -> list[tuple[str, str]]:
    """Décode ce qui ressemble à du base64 ou de l'hexadécimal dans le texte (dès 12 caractères), et les caractères « tag »."""
    found: list[tuple[str, str]] = []
    hidden = _tag_text(text)
    if hidden:
        found.append(("caractères Unicode « tag » (texte caché)", hidden))
    for token in BASE64_DECODE.findall(text):
        padded = token + "=" * (-len(token) % 4)
        for name, decoder in (("base64", base64.b64decode), ("base64_urlsafe", base64.urlsafe_b64decode)):
            try:
                found.append((name, decoder(padded).decode("utf-8", errors="ignore")))
            except (binascii.Error, ValueError):
                pass
    for token in HEX_DECODE.findall(text):
        try:
            found.append(("hexadécimal", bytes.fromhex(token).decode("utf-8", errors="ignore")))
        except ValueError:
            pass
    return found


def form_flags(text: str) -> list[str]:
    """Signaux sur la FORME du message, sans chercher à le comprendre."""
    flags: list[str] = []
    if any(ch in text for ch in INVISIBLE_CHARS):
        flags.append("caractères invisibles (Unicode de largeur nulle)")
    elif any(ord(ch) in HIDING_FORMAT_CHARS for ch in text):
        flags.append("caractères Unicode de format (invisibles)")
    if _tag_text(text):
        flags.append("caractères Unicode « tag » : texte ASCII caché")
    if any(re.search(r"[A-Za-z]", w) and re.search(r"[\u0370-\u03ff\u0400-\u04ff]", w) for w in re.findall(r"\w+", text)):
        flags.append("mot mêlant des lettres latines et cyrilliques ou grecques (homoglyphes possibles)")
    if any(ord(ch) > 0xFF00 and unicodedata.normalize("NFKC", ch) != ch and unicodedata.normalize("NFKC", ch).isascii() for ch in text):
        flags.append("caractères pleine chasse (formes larges de lettres ASCII)")
    # Un blob purement hexadécimal est signalé comme tel, pas comme base64.
    if any(not re.fullmatch(r"[0-9a-fA-F]+", tok) for tok in BASE64_BLOB.findall(text)):
        flags.append("longue chaîne de type base64")
    if HEX_BLOB.search(text):
        flags.append("longue chaîne hexadécimale")
    if text and len(ODD_SYMBOLS.findall(text)) / len(text) > 0.10:
        flags.append("forte proportion de symboles inhabituels (charabia)")
    if len(text) >= 40 and shannon_entropy(text) > 5.0:
        flags.append(f"entropie élevée ({shannon_entropy(text):.1f} bits par caractère)")
    return flags


class ChannelGuard:
    def __init__(
        self,
        mission: str,
        agents: list[AgentProfile],
        *,
        voice: bool = True,
        known_id_formats: dict[str, str] | None = None,
    ) -> None:
        """`known_id_formats` : formats d'identifiants légitimes déclarés à l'avance
        (libellé -> expression régulière complète). Un identifiant conforme ne
        déclenche plus le signal de forme (blob hexadécimal ou base64). La
        recherche de données sensibles (couche 1) reste appliquée en premier :
        un secret connu n'est jamais exempté. Limite : un secret INCONNU qui
        aurait exactement ce format passerait comme un identifiant."""
        self.known_id_formats = {label: re.compile(pattern) for label, pattern in (known_id_formats or {}).items()}
        self.mission = mission
        self.agents = {a.name: a for a in agents}
        self.voice = voice
        self._sensitive: dict[str, str] = {}  # libellé -> valeur
        self._holders: set[str] = set()  # agents qui ont reçu un message d'un agent qui lit du sensible (ou d'un autre porteur)
        self.blocked = 0
        self.last_egress_stage = "deterministe"  # étage de la dernière décision de sortie (pour l'affichage)
        self.journal: list[JournalEvent] = []

    def register_sensitive(self, label: str, value: str) -> None:
        """Déclare une donnée sensible connue d'un agent (ex : un jeton lu dans
        son périmètre). On la cherchera, sous forme transformée, dans les
        messages."""
        self._sensitive[label] = value

    # ── Couche 1 : circulation d'une donnée sensible ────────────────────────

    def taint_match(self, text: str) -> tuple[str, str] | None:
        """Renvoie (libellé, transformation) si une donnée sensible apparaît
        dans le message, telle quelle ou transformée."""
        norm_text = _normalize(text)
        decoded = _decoded_candidates(text)
        for label, secret in self._sensitive.items():
            norm_secret = _normalize(secret)
            variants = {
                "en clair, lettres espacées ou séparées": norm_secret,
                "inversée": norm_secret[::-1],
                "ROT13": _normalize(codecs.encode(secret, "rot13")),
            }
            for how, variant in variants.items():
                if variant and variant in norm_text:
                    return label, how
            # fragments : un morceau suffisamment long du secret suffit (12 caractères, ou 9 s'il mêle chiffres et lettres)
            for i in range(0, max(len(norm_secret) - MIN_FRAGMENT + 1, 0)):
                if norm_secret[i : i + MIN_FRAGMENT] in norm_text:
                    return label, "fragment"
            for i in range(0, max(len(norm_secret) - MIN_FRAGMENT_MIXED + 1, 0)):
                piece = norm_secret[i : i + MIN_FRAGMENT_MIXED]
                if sum(ch.isdigit() for ch in piece) >= 2 and piece in norm_text:
                    return label, "fragment (identifiant)"
            # acrostiche SIMPLE : un morceau du secret épelé par les initiales des lignes, des mots ou des phrases
            for scheme, initials in _initials_forms(text).items():
                if len(initials) >= MIN_ACROSTIC:
                    for i in range(0, max(len(norm_secret) - MIN_ACROSTIC + 1, 0)):
                        if norm_secret[i : i + MIN_ACROSTIC] in initials:
                            return label, f"acrostiche simple : {scheme}"
            for how, plain in decoded:
                # TOUS les fragments du secret dans le texte décodé, pas seulement le début (un secret coupé en morceaux
                # encodés séparément ne laissait sinon aucune trace reconnue dans le second morceau).
                norm_plain = _normalize(plain)
                if norm_secret in norm_plain:
                    return label, how
                for i in range(0, max(len(norm_secret) - MIN_FRAGMENT_DECODED + 1, 0)):
                    if norm_secret[i : i + MIN_FRAGMENT_DECODED] in norm_plain:
                        return label, f"{how}, fragment"
        return None

    # ── Inspection ───────────────────────────────────────────────────────────

    def _holds_sensitive(self, agent: str) -> bool:
        profile = self.agents.get(agent)
        return (profile is not None and "reads_sensitive" in profile.capabilities) or agent in self._holders

    async def inspect(self, msg: ChannelMessage) -> ChannelDecision:
        """Inspecte un message (couches 0 à 4) et l'ajoute au journal."""
        decision = await self._inspect(msg)
        # Un agent qui reçoit un message d'un agent qui lit du sensible (ou d'un porteur) peut à son tour détenir la donnée :
        # sans cela, un agent relais sans capacité neutralisait la règle « flux vers un agent qui peut émettre ».
        if decision.delivered and self._holds_sensitive(msg.sender):
            self._holders.add(msg.receiver)
        v = decision.verdict
        if decision.delivered:
            body = f"message livré : « {msg.text[:160]} »"
        else:
            # Le contenu d'un message refusé n'est PAS reproduit dans le journal :
            # il peut contenir une injection, et l'audit le lirait comme une consigne.
            body = f"message REFUSÉ par le gardien (contenu non reproduit ; motif : {v.anomaly_type}, {v.risk_level})"
        if decision.signals:
            body += f" ; signaux : {'; '.join(decision.signals)}"
        if decision.delivered:
            status = "delivered"
        elif v.recommended_action == "ask_user":
            status = "pending"
        elif v.anomaly_type == "exfiltration_suspectee" or any("donnée sensible détectée" in sig for sig in decision.signals):
            status = "refused_sensitive"
        else:
            status = "refused"
        self._add_event("message", f"{msg.sender} -> {msg.receiver} : {body}", status)
        return decision

    async def record_action(
        self,
        agent: str,
        description: str,
        *,
        sensitive: bool = False,
        external: bool = False,
        destination: str | None = None,
        content: str | None = None,
    ) -> RiskVerdict | None:
        """Consigne une action d'un agent (appel d'outil, lecture, envoi) qui ne
        passe pas par le canal de messages, et contrôle les SORTIES.

        Pour une action avec `destination` :
        1. (déterministe) si `content` contient une donnée sensible déclarée, même
           transformée (base64, hexadécimal...), la sortie est refusée, quelle que
           soit la destination : même vers une destination autorisée, une donnée
           protégée ne doit pas sortir ;
        2. destination dans la liste `allowed_destinations` de l'agent : autorisée ;
        3. destination HORS liste : jugée par un LLM (pipe `assess_egress`), qui
           refuse par défaut. La description de l'action vient de l'agent
           surveillé : elle est transmise au juge comme une affirmation non fiable.
           En cas d'échec de l'appel, la sortie est refusée.

        Renvoie le verdict si la sortie n'est pas autorisée (refus ou demande de
        confirmation), ou None si elle l'est."""
        profile = self.agents[agent]
        tags = (["donnée sensible"] if sensitive else []) + (["sortie vers l'extérieur"] if external or destination else [])
        verdict: RiskVerdict | None = None
        stage = "deterministe"
        if destination is None and external:
            verdict = RiskVerdict(
                risk_level="élevé",
                anomaly_type="ecart_intention",
                explanation=f"L'agent {agent} déclare une sortie vers l'extérieur sans destination : refusée, une sortie doit nommer sa destination.",
                recommended_action="block",
            )
        elif destination is None and content and self.taint_match(content):
            label, how = self.taint_match(content)  # type: ignore[misc]
            verdict = RiskVerdict(
                risk_level="critique",
                anomaly_type="exfiltration_suspectee",
                explanation=f"L'agent {agent} produit un contenu qui contient la donnée sensible « {label} » sous une forme transformée ({how}). Refusé.",
                recommended_action="block",
            )
        elif destination is not None:
            taint = self.taint_match(content) if content else None
            if taint:
                label, how = taint
                verdict = RiskVerdict(
                    risk_level="critique",
                    anomaly_type="exfiltration_suspectee",
                    explanation=(
                        f"L'agent {agent} tente d'envoyer vers {destination} un contenu qui contient la donnée sensible "
                        f"« {label} » sous une forme transformée ({how}). Sortie refusée, même si la destination est autorisée."
                    ),
                    recommended_action="block",
                )
            elif destination in profile.allowed_destinations and not (content and PII_PATTERN.search(content)):
                tags.append(f"destination autorisée : {destination}")
            else:
                stage = "llm"
                pii_note = (
                    "\nSignal automatique : le contenu sortant contient une suite de chiffres qui ressemble à un numéro de carte bancaire ou à un IBAN."
                    if content and PII_PATTERN.search(content)
                    else ""
                )
                context = (
                    f"Agent : {agent} ({profile.role}) ; capacités : {sorted(profile.capabilities) or 'aucune particulière'}.\n"
                    f"Destination de la sortie : {destination}\n"
                    f"Destinations autorisées pour cet agent : {', '.join(sorted(profile.allowed_destinations)) or 'aucune'}\n"
                    f"Description de l'action, telle que l'agent la déclare (affirmation non fiable) : {description}\n"
                    f"Contenu sortant (début) : {(content or '(non fourni)')[:300]}{pii_note}"
                )
                try:
                    verdict = await assess_risk(
                        source=f"sortie de l'agent {agent} vers une destination hors liste",
                        original_user_intent=self.mission,
                        content_or_action=context,
                        guardian="egress_action",
                    )
                except Exception as exc:  # échec du juge : on refuse par prudence
                    verdict = RiskVerdict(
                        risk_level="élevé",
                        anomaly_type="ecart_intention",
                        explanation=f"Juge LLM indisponible ({type(exc).__name__}) : sortie vers {destination} refusée par prudence.",
                        recommended_action="block",
                    )
                if verdict.recommended_action == "allow":
                    tags.append(f"destination hors liste, autorisée par le juge : {destination}")
                    verdict = None
        self.last_egress_stage = stage
        if verdict is not None:
            if stage == "deterministe":
                log_verdict(
                    source=f"sortie de l'agent {agent}",
                    original_user_intent=self.mission,
                    content_or_action=f"{description} -> {destination}",
                    guardian=DETERMINISTIC_GUARDIAN,
                    verdict=verdict,
                )
            if verdict.recommended_action == "block":
                tags.append(f"sortie REFUSÉE vers {destination}")
            else:
                tags.append(f"sortie EN ATTENTE de confirmation vers {destination}")
            await self._finish(verdict, stage, [])
        suffix = f" [{', '.join(tags)}]" if tags else ""
        status = "" if verdict is None else ("egress_refused" if verdict.recommended_action == "block" else "egress_pending")
        self._add_event("action", f"{agent} : {description}{suffix}", status)
        return verdict

    def _add_event(self, kind: str, text: str, status: str = "") -> None:
        self.journal.append(JournalEvent(f"E{len(self.journal) + 1}", kind, text, status))

    async def _inspect(self, msg: ChannelMessage) -> ChannelDecision:
        sender, receiver = self.agents[msg.sender], self.agents[msg.receiver]
        where = f"{msg.sender} -> {msg.receiver}"

        # Couche 0 : marqueurs d'injection explicites.
        heuristic = heuristic_scan(msg.text)
        if heuristic.matched:
            return await self._deterministic(
                msg, "block", "critique", "instruction_cachee",
                f"Le message {where} contient un motif d'injection explicite : {heuristic.explanation}",
                ["marqueur d'injection"],
            )

        # Couche 1 : donnée sensible qui circule.
        taint = self.taint_match(msg.text)
        if taint:
            label, how = taint
            return await self._deterministic(
                msg, "block", "critique", "exfiltration_suspectee",
                f"Le message {where} contient la donnée sensible « {label} » sous une forme transformée ({how}). "
                "Une donnée protégée circule entre agents sans être présentée comme telle.",
                [f"donnée sensible détectée ({how})"],
            )

        # Couches 2 et 3 : signaux de forme (hors identifiants au format déclaré) et cumul des capacités.
        stripped, known_ids = self._strip_known_ids(msg.text)
        signals = form_flags(stripped)
        if SENSITIVE_WORDS.search(stripped):
            signals.append("le message mentionne une donnée sensible (carte bancaire, mot de passe, jeton, secret...)")
        if known_ids:
            signals.append(
                "information fiable (déclarée à l'avance par l'orchestrateur) : ce message contient un identifiant qui respecte exactement le format légitime « "
                + ", ".join(sorted(known_ids))
                + " » ; il a été remplacé par un marqueur dans le texte ci-dessous, car sa forme est normale ici"
            )
        risky_pair = self._holds_sensitive(sender.name) and "egress" in receiver.capabilities
        if risky_pair:
            signals.append("contexte : flux d'un agent qui lit du sensible (ou en a reçu d'un agent qui en lit) vers un agent qui peut émettre vers l'extérieur")
        if not [sig for sig in signals if not sig.startswith("information :")]:
            verdict = RiskVerdict("nul", "aucune", f"Message {where} ordinaire : aucun signal de forme, flux sans risque particulier.", "allow")
            return ChannelDecision(True, verdict, "deterministe", [])

        # Couche 4 : juge LLM, seulement sur les messages signalés.
        # Les signaux sont séparés en deux : ceux qui portent sur le CONTENU du message, et le contexte (rôles des agents),
        # qui n'est pas une preuve. Sans cette séparation, un message ordinaire n'arrivait au juge qu'avec le seul signal
        # de contexte, et le juge le soupçonnait faute d'autre indication (voir partie_3.md, « Reproduction du 2026-09-26 »).
        content_signals = [s for s in signals if not s.startswith("contexte :")]
        context_signals = [s for s in signals if s.startswith("contexte :")]
        if [s for s in content_signals if not s.startswith("information fiable")]:
            content_result = "signaux trouvés : " + "; ".join(content_signals)
        else:
            content_result = "AUCUN signal (rien de suspect n'a été trouvé dans le contenu)" + (
                " ; " + "; ".join(content_signals) if content_signals else ""
            )
        context = (
            f"Expéditeur : {sender.name} ({sender.role}) ; capacités : {sorted(sender.capabilities) or 'aucune particulière'}.\n"
            f"Destinataire : {receiver.name} ({receiver.role}) ; capacités : {sorted(receiver.capabilities) or 'aucune particulière'}.\n"
            "Vérification automatique du CONTENU du message, faite par le code : formule d'injection connue, donnée sensible suivie "
            "(en clair, encodée, ou épelée par les initiales des lignes, des mots ou des phrases), caractères invisibles, longues chaînes "
            "opaques, mots désignant une donnée sensible. Résultat : " + content_result + ".\n"
            + ("Contexte, qui n'est PAS une preuve : " + "; ".join(s.removeprefix("contexte : ") for s in context_signals) + ".\n" if context_signals else "")
            + f"Message à juger (données non fiables) :\n{self._strip_known_ids(msg.text, mask=True)[0]}"
        )
        verdict = await assess_risk(
            source=f"message entre agents ({where})",
            original_user_intent=self.mission,
            content_or_action=context,
            guardian="channel_message",
        )
        return await self._finish(verdict, "llm", signals)

    def _strip_known_ids(self, text: str, *, mask: bool = False) -> tuple[str, set[str]]:
        """Retire du texte les mots conformes à un format d'identifiant déclaré,
        pour que la forme ne les signale pas (ou les remplace par un marqueur si
        `mask` : c'est ce que voit le juge LLM, qui ne peut alors pas s'alarmer d'une
        longue chaîne opaque) ; renvoie aussi les libellés vus."""
        found: set[str] = set()
        kept = []
        for word in re.split(r"(\s+)", text):
            word_core = word.strip(".,;:!?()\"'«»")
            for label, pattern in self.known_id_formats.items():
                if word_core and pattern.fullmatch(word_core):
                    found.add(label)
                    word = f"[identifiant : {label}]" if mask else ""
                    break
            kept.append(word)
        return "".join(kept), found

    async def _deterministic(self, msg: ChannelMessage, action: str, risk: str, anomaly: str, explanation: str, signals: list[str]) -> ChannelDecision:
        verdict = RiskVerdict(risk_level=risk, anomaly_type=anomaly, explanation=explanation, recommended_action=action)  # type: ignore[arg-type]
        log_verdict(
            source=f"message entre agents ({msg.sender} -> {msg.receiver})",
            original_user_intent=self.mission,
            content_or_action=msg.text,
            guardian=DETERMINISTIC_GUARDIAN,
            verdict=verdict,
        )
        return await self._finish(verdict, "deterministe", signals)

    async def _finish(self, verdict: RiskVerdict, stage: str, signals: list[str]) -> ChannelDecision:
        if verdict.recommended_action == "block":
            self.blocked += 1
        if self.voice and verdict.recommended_action in ("block", "ask_user"):
            try:
                audio_path = await narrate_verdict(verdict)
                if audio_path:
                    play_audio(audio_path)
            except Exception as exc:
                print(f"(narration vocale indisponible ({exc}) — poursuite en mode texte)")
        return ChannelDecision(delivered=verdict.recommended_action == "allow", verdict=verdict, stage=stage, signals=signals)

    # ── Couche 5 : audit de trajectoire ─────────────────────────────────────

    def journal_text(self) -> str:
        roster = "\n".join(
            f"  {a.name} ({a.role}) : capacités {sorted(a.capabilities) or 'aucune particulière'}" for a in self.agents.values()
        )
        events = "\n".join(f"  [{e.event_id}] {e.text}" for e in self.journal) or "  (aucun événement)"
        # Compteurs : champ `status` posé par le code ; recherche dans le texte SEULEMENT pour les événements écrits à la main.
        refused = [e for e in self.journal if e.status in ("refused", "refused_sensitive") or (not e.status and "message REFUSÉ" in e.text)]
        refused_egress = sum(1 for e in self.journal if e.status == "egress_refused" or (not e.status and "sortie REFUSÉE" in e.text))
        pending_egress = sum(1 for e in self.journal if e.status == "egress_pending" or (not e.status and "sortie EN ATTENTE" in e.text))
        refused_sensitive = sum(
            1 for e in refused
            if e.status == "refused_sensitive" or (not e.status and ("exfiltration_suspectee" in e.text or "donnée sensible détectée" in e.text))
        )
        return (
            f"Agents :\n{roster}\n\nJournal chronologique de la session :\n{events}\n\n"
            "Résumé automatique (faits calculés par le gardien, fiables) : "
            f"{len(self.journal)} événements ; {len(refused)} message(s) refusé(s) par le gardien, dont "
            f"{refused_sensitive} pour donnée sensible ou exfiltration suspectée ; "
            f"{refused_egress} sortie(s) vers l'extérieur refusée(s) par le contrôle des sorties, "
            f"{pending_egress} en attente de confirmation ; "
            "aucun message livré ne contenait de donnée sensible déclarée sous une forme que les contrôles du gardien savent "
            "détecter (les autres façons de la cacher ne sont pas couvertes)."
        )

    async def audit_trajectory(self) -> RiskVerdict:
        """Fait juger par le LLM l'ensemble de la session, pas message par
        message. Le verdict doit citer des événements du journal : un verdict
        non « allow » sans citation valide n'est pas retenu et devient une
        demande de confirmation."""
        verdict = await assess_risk(
            source="audit de trajectoire d'un groupe d'agents",
            original_user_intent=self.mission,
            content_or_action=self.journal_text(),
            guardian="trajectory_audit",
        )
        valid_ids = {e.event_id for e in self.journal}
        cited = set(re.findall(r"E\d+", verdict.explanation)) & valid_ids
        if verdict.recommended_action != "allow" and not cited:
            verdict = RiskVerdict(
                risk_level="moyen",
                anomaly_type=verdict.anomaly_type,
                explanation=(
                    "Le juge signale une anomalie sans citer d'événement du journal : verdict non retenu comme "
                    f"preuve, confirmation demandée. Verdict initial : {verdict.recommended_action} ({verdict.risk_level}) — {verdict.explanation}"
                ),
                recommended_action="ask_user",
            )
        return verdict
