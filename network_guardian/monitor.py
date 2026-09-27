"""Surveillance réseau d'un agent supervisé — 3ᵉ pilier de Veilleur.

Les deux autres piliers (`guardians/` pour le texte, `adversarial_vision/`
pour l'image) vérifient si un CONTENU reçu par un agent sain est piégé. Ici,
on vérifie autre chose : est-ce que l'AGENT LUI-MÊME reste dans son
périmètre autorisé ? On observe ses connexions réseau sortantes réelles
plutôt que de lui faire confiance pour se signaler lui-même — ce qui marche
même si l'agent est compromis ou dérive et ne coopère plus avec Veilleur.

Deux étages :

1. Capteur déterministe : appartenance à une liste blanche de domaines
   autorisés (résolue en IP au démarrage), plus les ports locaux autorisés
   (Ollama). Une connexion dans la liste blanche est autorisée tout de suite,
   sans LLM : c'est le cas courant, et il doit rester rapide.
2. Juge LLM (si un `NetworkContext` est fourni) : une connexion HORS liste
   blanche n'est plus bloquée d'office. Une adresse absente de la liste peut
   être un service légitime (adresse de CDN qui a changé, service local) ou une
   exfiltration ; le juge tranche avec le contexte (mission de l'utilisateur,
   activité en cours de l'agent, nom d'hôte par DNS inverse). Idem pour un
   rythme de connexions inhabituel vers un hôte, même autorisé (piste A de
   partie_3.md). En cas d'échec du juge, la connexion est bloquée par prudence.

Sans `NetworkContext`, on retrouve l'ancien comportement purement déterministe.

Même verdict (`RiskVerdict`), même journal (`results/verdicts.jsonl`,
guardian="network_monitor") et même narration Gradium que les deux autres
piliers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from victim_agent.narrator import narrate_verdict, play_audio  # noqa: E402
from victim_agent.veilleur_gate import RiskVerdict, assess_risk, log_verdict  # noqa: E402

GUARDIAN_NAME = "network_monitor"

# Domaines légitimes de l'écosystème Veilleur — à élargir si de nouveaux
# services externes sont ajoutés au projet.
ALLOWED_DOMAINS = [
    "api.gradium.ai",
    "api.openai.com",
    "jinko.so",
    "mcp.builders.gojinko.com",
    "mcp.builders.sandbox.gojinko.com",
    "huggingface.co",  # téléchargement du modèle du préfiltre externe (victim_agent/prefilter.py)
]


def resolve_allowed_ips(domains: list[str] = ALLOWED_DOMAINS) -> set[str]:
    """Résout les domaines autorisés en IP une fois au démarrage. Un domaine
    injoignable (ex: Jinko down) est simplement ignoré, pas bloquant pour le
    reste de la surveillance."""
    ips: set[str] = set()
    for domain in domains:
        try:
            for info in socket.getaddrinfo(domain, None):
                ips.add(info[4][0])
        except socket.gaierror:
            pass
    return ips


def is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


# Ports locaux légitimes — volontairement une liste explicite de (ip, port),
# pas "toute adresse locale/privée" : un agent qui contacte un port local
# inattendu (ex: un service qu'on ne lui connaît pas) est tout aussi
# suspect qu'une IP externe inconnue. Seul Ollama (11434) est autorisé ici.
ALLOWED_LOCAL_PORTS = {11434}

# Détection de fréquence : nouvelles connexions vers un même hôte dans une fenêtre.
FREQUENCY_WINDOW_S = 5.0
FREQUENCY_THRESHOLD = 10
MAX_PARALLEL_JUDGES = 3  # jugements et narrations en parallèle du sondage
JUDGE_JOIN_TIMEOUT_S = 20.0  # attente maximale des jugements en cours à la fin de la surveillance


@dataclass
class NetworkContext:
    """Ce que le juge LLM doit savoir pour trancher : la mission confiée à
    l'agent et ce qu'il est en train de faire. `activity` peut être une
    fonction, pour refléter l'activité au moment de la connexion."""

    mission: str
    activity: "Callable[[], str] | str" = "aucune activité déclarée"

    def current_activity(self) -> str:
        return self.activity() if callable(self.activity) else self.activity


def _resolve_confirmed_host(ip: str) -> str:
    """DNS inverse CONFIRMÉ par la résolution directe : le nom rendu par le PTR est choisi par le propriétaire de
    l'adresse, donc n'importe quel nom peut y être écrit (même « mcp.builders.gojinko.com »). On ne le retient que si
    la résolution directe de ce nom redonne bien cette adresse (« forward-confirmed reverse DNS »)."""
    try:
        name = socket.gethostbyaddr(ip)[0]
        _, _, addresses = socket.gethostbyname_ex(name)
    except OSError:
        return "inconnu"
    return name if ip in addresses else "inconnu"


def reverse_dns(ip: str, timeout: float = 1.5) -> str:
    """Nom d'hôte de l'adresse (DNS inverse confirmé par la résolution directe), ou « inconnu ». C'est un indice donné
    au juge, pas une preuve. Le délai est réellement borné : l'exécuteur n'est pas attendu à la sortie (un résolveur
    lent ne bloque plus l'appelant au-delà de `timeout`)."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(_resolve_confirmed_host, ip).result(timeout=timeout)
    except (FuturesTimeout, OSError):
        return "inconnu"
    finally:
        pool.shutdown(wait=False)  # ne pas attendre un résolveur lent (le thread s'éteint seul)


# Fournisseurs cloud / réseaux de diffusion, reconnus par SUFFIXE de domaine (une recherche « n'importe où dans le nom »
# se laissait tromper par un nom comme « cdn.amazonaws.com.attaquant.test »).
CLOUD_SUFFIXES = (
    "amazonaws.com", "cloudfront.net", "cloudflare.com", "cloudflare.net", "akamai.net", "akamaiedge.net",
    "akamaitechnologies.com", "fastly.net", "googleusercontent.com", "azure.com", "azureedge.net", "cloudapp.azure.com",
)


def is_cloud_host(host: str) -> bool:
    h = host.lower().rstrip(".")
    return any(h == s or h.endswith("." + s) for s in CLOUD_SUFFIXES)


async def judge_network_event(
    kind: str,
    remote_ip: str,
    remote_port: int,
    ctx: NetworkContext,
    details: str,
    fallback: RiskVerdict,
    local_ports: set[int] | None = None,
) -> RiskVerdict:
    """Fait juger par le LLM un événement réseau que le capteur déterministe ne
    peut pas trancher. En cas d'échec de l'appel (réseau, quota...), renvoie
    `fallback` (le verdict prudent du capteur), jamais un « allow »."""
    try:
        return await _judge_network_event(kind, remote_ip, remote_port, ctx, details, local_ports)
    except Exception as exc:  # TOUT échec (activité, DNS, journal, LLM...) : décision prudente du capteur, jamais un « allow »
        return RiskVerdict(
            risk_level=fallback.risk_level,
            anomaly_type=fallback.anomaly_type,
            explanation=f"Juge LLM indisponible ({type(exc).__name__}) : décision prudente du capteur. {fallback.explanation}",
            recommended_action=fallback.recommended_action,
        )


async def _judge_network_event(
    kind: str,
    remote_ip: str,
    remote_port: int,
    ctx: NetworkContext,
    details: str,
    local_ports: set[int] | None,
) -> RiskVerdict:
    host = reverse_dns(remote_ip)
    activity = ctx.current_activity()
    # Vérification DÉTERMINISTE, montrée comme un fait : l'activité déclarée mentionne-t-elle exactement
    # cette destination ? Sans elle, le juge comparait mal des adresses et des ports, et se fiait à la
    # liste blanche (destination hors liste = suspect) même quand l'orchestrateur l'expliquait.
    # Le nom d'hôte n'est retenu que s'il est confirmé par la résolution directe et cité comme un mot entier.
    ip_token = r"(?<![\d.])" + re.escape(remote_ip) + r"(?![\d.])"  # l'adresse entière, pas un morceau d'une autre
    port_token = r"(?<!\d)" + str(remote_port) + r"(?!\d)"
    mentions = (
        re.search(ip_token + ":" + str(remote_port) + r"(?!\d)", activity) is not None
        or (host != "inconnu" and re.search(r"(?<![\w.-])" + re.escape(host) + r"(?![\w-])", activity, re.IGNORECASE) is not None)
        or (re.search(ip_token, activity) is not None and re.search(port_token, activity) is not None)
    )
    is_rate = kind.startswith("rythme")
    cloud = is_cloud_host(host)
    checks = ""
    if is_rate:
        # Un événement de rythme ne concerne que le NOMBRE de connexions : la vérification de la destination
        # est traitée par l'événement « connexion hors liste blanche », et l'afficher ici faisait bloquer à tort.
        checks = "Cet événement ne concerne que le nombre de connexions, pas l'identité de la destination.\n"
    else:
        checks = (
            f"Vérification automatique : l'activité déclarée mentionne-t-elle exactement cette destination ({remote_ip}:{remote_port}) ? {'OUI' if mentions else 'non'}\n"
            f"Vérification automatique : le nom d'hôte appartient-il à un fournisseur cloud ou à un réseau de diffusion (donc un service légitime peut y être hébergé) ? {'OUI' if cloud else 'non'}\n"
        )
    content = (
        f"Type d'événement : {kind} (signalé au juge parce que le capteur ne peut pas le trancher : ce n'est pas, en soi, un indice d'anomalie)\n"
        f"Destination : {remote_ip}:{remote_port} (nom d'hôte par DNS inverse : {host})\n"
        f"Activité en cours de l'agent, déclarée par l'orchestrateur : {activity}\n"
        f"{checks}"
        f"Faits : {details}"
    )
    return await assess_risk(
        source="supervision réseau du process de l'agent",
        original_user_intent=ctx.mission,
        content_or_action=content,
        guardian="network_event",
    )


def classify_connection(
    remote_ip: str,
    remote_port: int,
    allowed_ips: set[str],
    *,
    self_paired_loopback: bool = False,
    allowed_local_ports: set[int] | None = None,
) -> RiskVerdict:
    """Verdict déterministe : l'IP est-elle dans le périmètre autorisé
    (port local explicitement whitelisté, ou IP résolue depuis un domaine de
    la liste blanche) ? Une adresse locale sur un port non prévu (ex: un
    décoy de test) est traitée exactement comme une IP externe inconnue —
    voir ALLOWED_LOCAL_PORTS ci-dessus.

    `self_paired_loopback=True` couvre un cas distinct, découvert en câblant
    la surveillance sur le vrai appel Jinko : `httpx2`/`anyio` ouvrent, pour
    CHAQUE requête HTTPS asynchrone, une paire de sockets 127.0.0.1
    symétrique (A→B et B→A, les deux extrémités appartenant au process
    surveillé lui-même) — de la plomberie interne asyncio sur Windows, sans
    aucun rapport avec un vrai service local. Voir `monitor_process` pour
    comment cette symétrie est détectée : ça n'affaiblit PAS la détection du
    décoy de `rogue_agent_demo.py`, puisque là le décoy est un process
    SÉPARÉ — seule la moitié sortante de la connexion apparaît côté agent
    surveillé, jamais la paire miroir complète."""
    if self_paired_loopback:
        return RiskVerdict(
            risk_level="nul",
            anomaly_type="aucune",
            explanation=(
                f"Connexion boucle locale symétrique vers {remote_ip}:{remote_port} — les deux "
                "extrémités appartiennent au process surveillé lui-même (plomberie interne "
                "asyncio/anyio pour un appel réseau asynchrone), pas un service local tiers."
            ),
            recommended_action="allow",
        )
    local_ports = ALLOWED_LOCAL_PORTS if allowed_local_ports is None else allowed_local_ports
    if (is_loopback(remote_ip) and remote_port in local_ports) or remote_ip in allowed_ips:
        return RiskVerdict(
            risk_level="nul",
            anomaly_type="aucune",
            explanation=f"Connexion vers {remote_ip}:{remote_port}, dans le périmètre autorisé de l'agent.",
            recommended_action="allow",
        )
    return RiskVerdict(
        risk_level="critique",
        anomaly_type="connexion_non_autorisee",
        explanation=(
            f"L'agent a ouvert une connexion vers {remote_ip} sur le port {remote_port}, "
            "une destination hors de son périmètre autorisé. Une IA qui contacte un serveur "
            "non prévu peut signaler un détournement de son fonctionnement normal : "
            "exfiltration de données, ou agent compromis exécutant des instructions qui ne "
            "viennent pas de nous."
        ),
        recommended_action="block",
    )


def _record_connection(pid: int, remote_ip: str, remote_port: int, verdict: RiskVerdict, *, already_logged: bool = False, source: str = "") -> None:
    """Journalise (si ce n'est pas déjà fait) et affiche le verdict. Rapide : aucun réseau.

    `source` dit qui a tranché (« règle déterministe » ou « juge LLM ») — affiché à l'écran pour
    qu'on ne confonde jamais les deux, y compris quand le juge LLM échoue et qu'on retombe sur
    la décision prudente du capteur (repli explicitement signalé comme tel)."""
    if not already_logged:  # un verdict du juge LLM est déjà journalisé par assess_risk
        log_verdict(
            source=f"connexion sortante de l'agent surveillé (pid {pid})",
            original_user_intent="(supervision réseau, pas de requête texte)",
            content_or_action=f"{remote_ip}:{remote_port}",
            guardian=GUARDIAN_NAME,
            verdict=verdict,
        )
    tag = f" [{source}]" if source else ""
    print(f"[{remote_ip}:{remote_port}]{tag} Verdict : {verdict.recommended_action} ({verdict.risk_level}) — {verdict.explanation}")


async def _narrate_connection(verdict: RiskVerdict) -> None:
    """Narration vocale (réseau Gradium, puis lecture synchrone : plusieurs secondes). Toujours APRÈS le blocage."""
    try:
        audio_path = await narrate_verdict(verdict)
        if audio_path:
            print(f"🔊 Narration Gradium : {audio_path}")
            play_audio(audio_path)
    except Exception as exc:
        print(f"(narration vocale indisponible ({exc}) — poursuite en mode texte)")


def monitor_process(
    pid: int,
    *,
    duration: float = 30.0,
    interval: float = 0.5,
    voice: bool = True,
    stop_event: threading.Event | None = None,
    context: NetworkContext | None = None,
    extra_allowed_local_ports: set[int] | None = None,
    enforcement: str | None = None,
) -> list[RiskVerdict]:
    """Surveille les connexions sortantes du process `pid` pendant `duration`
    secondes (sondage toutes les `interval` secondes), ou jusqu'à ce que
    `stop_event` soit levé si fourni — utile pour arrêter la surveillance dès
    qu'on sait que l'appel surveillé est terminé, sans attendre toute la
    durée par défaut (voir `victim_agent/orchestrator.py::real_search_hotels_jinko`,
    qui lance cette fonction dans un thread pendant l'appel réel à Jinko).
    Journalise et narre tout ce qui apparaît, dans le périmètre autorisé ou
    non. Renvoie la liste des verdicts rendus, dans l'ordre d'apparition.

    `enforcement="kill"` : BLOCAGE RÉEL. Quand le verdict final d'un événement est `block`, le
    process surveillé est ARRÊTÉ (toutes ses connexions), puis la surveillance s'arrête. À réserver à
    un process distinct de l'agent légitime : dans `orchestrator.py::real_search_hotels_jinko`, le
    moniteur observe le process de l'agent lui-même, l'arrêter couperait aussi le travail légitime, donc
    l'option n'y est pas activée. Le blocage intervient APRÈS la détection (et l'éventuel appel au
    juge LLM, environ 2 s) : la connexion a déjà été établie. Un verdict `ask_user` n'arrête rien.

    Le juge LLM et la narration tournent dans des threads de travail (`MAX_PARALLEL_JUDGES`) : le sondage
    continue pendant qu'ils s'exécutent. La liste renvoyée est dans l'ordre de fin des jugements."""
    allowed_ips = resolve_allowed_ips()
    local_ports = ALLOWED_LOCAL_PORTS | (extra_allowed_local_ports or set())
    seen: set[tuple[str, int]] = set()
    seen_connections: set[tuple[int, str, int]] = set()  # (port local, ip, port distant) déjà comptés
    recent: dict[str, deque[float]] = defaultdict(deque)  # nouvelles connexions par hôte, fenêtre glissante
    flagged_at: dict[str, float] = {}
    verdicts: list[RiskVerdict] = []
    deadline = time.time() + duration

    lock = threading.Lock()  # protège `verdicts` et `tracked`
    kill_lock = threading.Lock()
    killed = threading.Event()
    tracked: dict[int, psutil.Process] = {}  # descendants déjà vus (un orphelin, sorti de l'arbre, reste surveillé)
    seen_parents: dict[int, float] = {}  # pid -> date de création des process de l'arbre déjà vus, MÊME MORTS (adoption des orphelins)
    futures: list = []
    executor = ThreadPoolExecutor(max_workers=MAX_PARALLEL_JUDGES)

    def submit(fn, *args) -> None:
        futures.append(executor.submit(fn, *args))

    try:
        proc = psutil.Process(pid)  # cet objet mémorise la date de création : `kill()` refuse de tuer un pid réutilisé
        seen_parents[pid] = proc.create_time()
    except psutil.NoSuchProcess:
        print(f"Process {pid} introuvable.")
        executor.shutdown(wait=False)
        return verdicts
    # Le moniteur et ses ancêtres ne sont JAMAIS suivis ni arrêtés (si le process surveillé est l'un d'eux, on refuse).
    try:
        protected = {os.getpid(), *(a.pid for a in psutil.Process().parents())}
    except psutil.Error:
        protected = {os.getpid()}

    def enforce_if_needed(verdict: RiskVerdict, target: str) -> bool:
        """Applique le blocage réel. Renvoie True si le process a été arrêté (ou n'existe plus)."""
        if enforcement != "kill" or verdict.recommended_action != "block":
            return False
        if pid in protected:
            print("(blocage réel refusé : le process visé est le moniteur ou l'un de ses ancêtres)")
            return False
        with kill_lock:  # deux jugements simultanés ne tuent pas deux fois
            if killed.is_set():
                return True
            # On utilise l'objet `proc` du départ, PAS un nouveau `psutil.Process(pid)` : un pid réutilisé par un
            # process sans rapport ferait tuer celui-là. Les descendants viennent de la liste cumulée (orphelins inclus).
            with lock:
                victims = {c.pid: c for c in list(tracked.values()) if c.pid not in protected}
            try:
                for c in proc.children(recursive=True):
                    if c.pid not in protected:
                        victims[c.pid] = c
            except psutil.NoSuchProcess:
                pass
            problems = []
            everyone = [proc, *victims.values()]
            for victim in everyone:
                try:
                    victim.kill()
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied:  # un enfant protégé n'empêche pas d'arrêter les autres
                    problems.append(victim.pid)
                except Exception as exc:
                    problems.append(victim.pid)
                    print(f"(arrêt du process {victim.pid} : {type(exc).__name__})")
            # `is_running()` juste après `kill()` renvoie encore True sous Windows (le lanceur garde un handle) : on ATTEND la fin.
            gone, alive = psutil.wait_procs(everyone, timeout=3)
            problems += [v.pid for v in alive if v.pid not in problems]
            if proc in alive:
                print(f"(blocage réel impossible : le process {pid} n'a pas pu être arrêté ; problème pour {problems})")
                return False
            killed.set()
        suffix = f" (impossible d'arrêter aussi les process {problems})" if problems else ""
        print(f"⛔ BLOCAGE RÉEL : le process {pid} a été arrêté ({target}).{suffix}")
        log_verdict(
            source=f"application de la décision sur le process {pid}",
            original_user_intent="(supervision réseau, pas de requête texte)",
            content_or_action=f"arrêt du process {pid} après un verdict block sur {target}",
            guardian=GUARDIAN_NAME + "+enforcement",
            verdict=RiskVerdict("critique", verdict.anomaly_type, f"Process {pid} arrêté : {verdict.explanation}", "block"),
        )
        return True

    # Le juge LLM (environ 2 s) et la narration vocale ne doivent PAS bloquer le sondage : sinon une
    # connexion brève ouverte et fermée pendant ce temps est manquée. Chaque événement qui demande un
    # juge, une narration ou un blocage part dans un thread de travail ; la boucle continue de sonder.
    def finish(verdict: RiskVerdict, ip: str, port: int, target: str, already_logged: bool, source: str = "") -> None:
        """Ordre : journal et affichage (rapides), BLOCAGE, puis narration (lente). Aucune étape ne peut empêcher la suivante."""
        with lock:
            verdicts.append(verdict)
        try:
            _record_connection(pid, ip, port, verdict, already_logged=already_logged, source=source)
        except Exception as exc:
            print(f"(journalisation impossible : {type(exc).__name__} : {exc})")
        try:
            enforce_if_needed(verdict, target)
        except Exception as exc:
            print(f"(application du blocage impossible : {type(exc).__name__} : {exc})")
        if voice and verdict.recommended_action in ("block", "ask_user"):
            try:
                asyncio.run(_narrate_connection(verdict))
            except Exception as exc:
                print(f"(narration impossible : {type(exc).__name__})")

    def judge_then_finish(kind: str, ip: str, port: int, details: str, fallback: RiskVerdict, target: str) -> None:
        """Toute exception est capturée : elle donnerait sinon ni verdict, ni journal, ni blocage, et la destination
        ne serait plus jamais rejugée (`seen`). Le repli est le verdict prudent du capteur."""
        try:
            verdict = asyncio.run(judge_network_event(kind, ip, port, context, details, fallback, local_ports))
            source = "juge LLM"
        except BaseException as exc:
            verdict = RiskVerdict(fallback.risk_level, fallback.anomaly_type, f"Juge LLM indisponible ({type(exc).__name__}) : décision prudente du capteur. {fallback.explanation}", fallback.recommended_action)
            source = "juge LLM indisponible — repli sur le capteur déterministe"
        finish(verdict, ip, port, target, already_logged=not verdict.explanation.startswith("Juge LLM indisponible"), source=source)

    while time.time() < deadline and not killed.is_set() and (stop_event is None or not stop_event.is_set()):
        parent_alive = True
        try:
            conns = proc.net_connections(kind="inet")
        except psutil.NoSuchProcess:
            # Le process désigné est mort : on continue tant qu'un descendant suivi vit (orphelin).
            conns, parent_alive = [], False
        except psutil.AccessDenied:
            print("Accès refusé par l'OS pour observer ce process (relancer avec des droits administrateur).")
            break
        # Les processus ENFANTS comptent aussi : sous Windows, un `python.exe` de venv est un lanceur dont le vrai
        # interpréteur est un enfant, et un agent peut lancer des sous-process. On garde la liste CUMULÉE des
        # descendants vus : un intermédiaire qui lance un travailleur puis se termine laisse un orphelin, sorti de
        # l'arbre, qui doit rester surveillé (et arrêté avec les autres).
        try:
            fresh = proc.children(recursive=True) if parent_alive else []
        except psutil.NoSuchProcess:
            fresh = []
        with lock:
            for child in fresh:
                if child.pid not in protected:
                    tracked[child.pid] = child
            snapshot = list(tracked.values())
        for child in snapshot:  # les process suivis lancent à leur tour des sous-process
            try:
                seen_parents.setdefault(child.pid, child.create_time())
                for grandchild in child.children(recursive=True):
                    if grandchild.pid not in protected:
                        with lock:
                            tracked[grandchild.pid] = grandchild
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # Adoption des orphelins : un process dont le parent est un process de l'arbre (vivant OU mort, créé avant lui) en fait
        # partie. Sous Windows, le ppid d'un orphelin reste celui du parent disparu.
        try:
            for other in psutil.process_iter(["ppid", "create_time"]):
                parent_created = seen_parents.get(other.info["ppid"])
                if (
                    parent_created is not None
                    and other.pid not in seen_parents
                    and other.pid not in protected
                    and (other.info["create_time"] or 0) >= parent_created
                ):
                    seen_parents[other.pid] = other.info["create_time"]
                    with lock:
                        tracked[other.pid] = other
        except psutil.Error:
            pass
        with lock:
            snapshot = list(tracked.values())
        for child in snapshot:
            try:
                conns = conns + child.net_connections(kind="inet")
            except psutil.AccessDenied:
                continue
            except psutil.NoSuchProcess:
                with lock:
                    tracked.pop(child.pid, None)
        if not parent_alive and not tracked:
            break  # plus aucun process à surveiller

        # TCP établi seulement. LIMITE : l'UDP n'est pas surveillé. Sous Windows, `psutil` n'expose AUCUNE adresse distante
        # pour l'UDP (vérifié : `raddr` vide), et un `sendto` sur une socket non connectée n'en expose sur aucun système ;
        # sous Linux/macOS, prendre en compte l'UDP « connecté » ferait bloquer le résolveur DNS du système (`connect()`
        # UDP vers 127.0.0.53:53) : non retenu.
        established = [c for c in conns if c.status == psutil.CONN_ESTABLISHED and c.raddr and c.laddr]
        # Paires loopback présentes dans CE sondage, pour repérer une
        # connexion 127.0.0.1 symétrique (A→B et B→A simultanément, les deux
        # possédées par ce même pid) — cf. docstring de classify_connection.
        loopback_pairs = {
            (c.laddr.ip, c.laddr.port, c.raddr.ip, c.raddr.port)
            for c in established
            if is_loopback(c.laddr.ip) and is_loopback(c.raddr.ip)
        }

        now = time.time()
        for c in established:
            self_paired_loopback = (c.raddr.ip, c.raddr.port, c.laddr.ip, c.laddr.port) in loopback_pairs

            # Fréquence : chaque NOUVELLE connexion (même vers une destination déjà vue) compte.
            conn_id = (c.laddr.port, c.raddr.ip, c.raddr.port)
            if conn_id not in seen_connections and not self_paired_loopback:
                seen_connections.add(conn_id)
                window = recent[c.raddr.ip]
                window.append(now)
                while window and now - window[0] > FREQUENCY_WINDOW_S:
                    window.popleft()
                if len(window) >= FREQUENCY_THRESHOLD and now - flagged_at.get(c.raddr.ip, 0.0) > FREQUENCY_WINDOW_S:
                    flagged_at[c.raddr.ip] = now
                    details = (
                        f"{len(window)} nouvelles connexions vers {c.raddr.ip} en {FREQUENCY_WINDOW_S:.0f} s "
                        f"(seuil de signalement : {FREQUENCY_THRESHOLD})."
                    )
                    fallback = RiskVerdict(
                        risk_level="moyen",
                        anomaly_type="exfiltration_suspectee",
                        explanation=f"Rythme de connexions inhabituel vers {c.raddr.ip} : {details}",
                        recommended_action="ask_user",
                    )
                    target = f"{c.raddr.ip}:{c.raddr.port} (rythme)"
                    if context:
                        submit(judge_then_finish, "rythme de connexions inhabituel", c.raddr.ip, c.raddr.port, details, fallback, target)
                    else:
                        submit(finish, fallback, c.raddr.ip, c.raddr.port, target, False, "règle déterministe (pas de contexte fourni pour juger le rythme)")

            # Destination : jugée une seule fois par (adresse, port).
            key = (c.raddr.ip, c.raddr.port)
            if key in seen:
                continue
            seen.add(key)
            verdict = classify_connection(
                c.raddr.ip, c.raddr.port, allowed_ips, self_paired_loopback=self_paired_loopback, allowed_local_ports=local_ports
            )
            target = f"{c.raddr.ip}:{c.raddr.port}"
            if verdict.recommended_action == "allow":
                finish(verdict, c.raddr.ip, c.raddr.port, target, False, "règle déterministe (liste blanche)")  # cas courant : immédiat, sans LLM ni narration
            elif context:
                # Hors liste blanche : le capteur ne tranche pas, le juge LLM oui (sans bloquer le sondage).
                submit(
                    judge_then_finish, "connexion hors liste blanche", c.raddr.ip, c.raddr.port,
                    "destination absente de la liste blanche du capteur déterministe.", verdict, target,
                )
            else:
                submit(finish, verdict, c.raddr.ip, c.raddr.port, target, False, "règle déterministe (pas de contexte fourni pour juger)")

        time.sleep(interval)

    # Laisse finir les jugements en cours, mais pas indéfiniment : un appel au modèle figé ne doit pas bloquer l'appelant.
    _done, pending = futures_wait(futures, timeout=JUDGE_JOIN_TIMEOUT_S)
    if pending:
        print(f"({len(pending)} jugement(s) encore en cours après {JUDGE_JOIN_TIMEOUT_S:.0f} s : abandonnés)")
    executor.shutdown(wait=False, cancel_futures=True)
    return verdicts
