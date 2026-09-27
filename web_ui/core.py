"""Logique de l'interface, sans Gradio : appels aux gardiens, mode direct ou rejeu, scénarios.

Deux modes :
- **direct** : les gardiens LLM sont réellement appelés (clé `OPENAI_API_KEY` dans `.env`,
  ou `VEILLEUR_UI_MODE=live` pour un modèle local Ollama) ;
- **rejeu** : sans clé, on affiche les résultats ENREGISTRÉS lors de nos essais
  (`recorded.json`, écrit par `record.py`), clairement signalés comme tels. Les couches
  déterministes (règles, prétraitement, test du flou) tournent, elles, toujours en direct.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "adversarial_vision"))

from victim_agent.env import load_project_env  # noqa: E402

load_project_env()

RECORDED_FILE = Path(__file__).resolve().parent / "recorded.json"
VERDICTS_LOG = ROOT / "results" / "verdicts.jsonl"

_run_lock = threading.Lock()  # un seul scénario à la fois : ils utilisent des ports locaux fixes


def live_mode() -> bool:
    mode = os.getenv("VEILLEUR_UI_MODE", "auto").lower()
    if mode == "live":
        return True
    if mode == "replay":
        return False
    return bool(os.getenv("OPENAI_API_KEY"))


def current_backend() -> str:
    """« openai », « ollama », ou « inconnu » si la configuration ne peut pas être lue
    (ex : `switch_backend.py` jamais lancé sur cette machine). Relit le même alias que
    `switch_backend.py --status`, sans jamais rien modifier — pour que le bandeau de
    l'UI dise le vrai backend actif plutôt que de supposer OpenAI (mode direct fonctionne
    aussi avec Ollama, gratuit, via `VEILLEUR_UI_MODE=live`)."""
    try:
        import tomlkit

        from switch_backend import ALIAS_NAME, BACKEND_TARGETS, DECK_FILE

        deck = tomlkit.parse(DECK_FILE.read_text(encoding="utf-8"))
        target = deck.get("llm", {}).get("aliases", {}).get(ALIAS_NAME)
        for name, cfg in BACKEND_TARGETS.items():
            if cfg["model_handle"] == target:
                return name
    except Exception:
        pass
    return "inconnu"


def recorded() -> dict:
    try:
        return json.loads(RECORDED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_recorded(data: dict) -> None:
    RECORDED_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ── Pilier 1 : texte ─────────────────────────────────────────────────────────────────────

PATH_LABELS = {
    "heuristic": "Règles déterministes (aucun modèle appelé)",
    "prefilter_hf": "Préfiltre externe HuggingFace (modèle tiers, identifié comme tel)",
}


def deterministic_preview(text: str) -> dict:
    """Ce que les couches SANS modèle voient : prétraitement et règles. Toujours disponible."""
    from victim_agent.heuristics import heuristic_scan
    from victim_agent.preprocess import preprocess

    prep = preprocess(text)
    hit = heuristic_scan(text)
    decoded = False
    if not hit.matched:
        for variant in prep.variants:
            hit = heuristic_scan(variant)
            if hit.matched:
                decoded = True
                break
    return {"notes": list(prep.notes), "rule_hit": bool(hit.matched), "rule_text": hit.explanation if hit.matched else "", "after_decoding": decoded}


def _last_log_entry() -> dict:
    try:
        with VERDICTS_LOG.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        return json.loads(lines[-1])
    except (OSError, ValueError, IndexError):
        return {}


def judge_text(request: str, content: str, guardian: str) -> dict:
    """Appel réel du gardien texte. Renvoie verdict, chemin de décision et indices du prétraitement."""
    from victim_agent.veilleur_gate import assess_risk

    preview = deterministic_preview(content)
    verdict = asyncio.run(assess_risk(source="interface web (test manuel)", original_user_intent=request, content_or_action=content, guardian=guardian))
    used = _last_log_entry().get("guardian", guardian)
    suffix = used.split("+", 1)[1] if "+" in used else ""
    return {"verdict": verdict.to_dict(), "path": PATH_LABELS.get(suffix, f"Modèle de langage (gardien « {used} »)"), "preview": preview, "replayed": False}


def narrate_if_blocking(verdict: dict) -> str:
    """Synthétise et joue la voix Gradium (comme la démo CLI) si le verdict bloque ou demande
    confirmation ; ne fait rien pour "allow". Renvoie "" en temps normal, ou un message
    D'ERREUR EN TEXTE BRUT (pas de HTML : à échapper par l'appelant, cf. docstring du module)
    si la synthèse a échoué (pas de GRADIUM_API_KEY, réseau...) — ne lève jamais, pour ne pas
    casser l'affichage du verdict. Lecture audio synchrone (`winsound`) : n'a de sens QUE si
    l'UI tourne localement sur la machine qu'on écoute (le son joue côté serveur Gradio, pas
    dans le navigateur)."""
    from victim_agent.narrator import narrate_verdict, play_audio
    from victim_agent.veilleur_gate import RiskVerdict

    v = RiskVerdict(**{k: verdict[k] for k in ("risk_level", "anomaly_type", "explanation", "recommended_action")})
    if v.recommended_action not in ("block", "ask_user"):
        return ""
    try:
        path = asyncio.run(narrate_verdict(v))
        if path:
            play_audio(path)
        return ""
    except Exception as exc:
        msg = str(exc)
        return f"Voix indisponible ({type(exc).__name__} : {msg[:150]}{'…' if len(msg) > 150 else ''})."


# ── Pilier 2 : image ─────────────────────────────────────────────────────────────────────

_vision = {}


def analyze_image_file(path: str, second_opinion: bool) -> dict:
    """Test du flou (toujours local) puis, si demandé, second avis (l'image part chez OpenAI)."""
    import torchvision.transforms.functional as TF
    from PIL import ImageFilter

    import detector
    from vision_common import load_image_tensor, load_model, top_predictions

    if "model" not in _vision:
        _vision["model"], _vision["categories"] = load_model()
    model, categories = _vision["model"], _vision["categories"]
    p = Path(path)
    x = load_image_tensor(p)
    label0, conf0 = top_predictions(model, x, categories, k=1)[0]
    blurred = TF.to_pil_image(x.squeeze(0)).filter(ImageFilter.GaussianBlur(radius=detector.BLUR_RADIUS))
    label1, conf1 = top_predictions(model, TF.to_tensor(blurred).unsqueeze(0), categories, k=1)[0]

    blur_verdict = detector.detect_manipulation(p, model, categories)
    verdict = blur_verdict
    description = ""
    # `apply_second_opinion` n'appelle le LLM que si le flou n'a pas déjà bloqué ET que la
    # confiance du classifieur est suffisante (mêmes conditions qu'en tête de cette fonction,
    # côté detector.py) — sinon il renvoie le verdict du flou tel quel, sans jamais appeler le
    # modèle. `llm_called` distingue ce cas pour que l'UI ne fasse pas croire à un second avis
    # qui n'a pas eu lieu.
    llm_called = False
    if second_opinion:
        llm_called = blur_verdict.recommended_action != "block" and conf0 >= detector.MIN_CONFIDENCE
        verdict = detector.apply_second_opinion(p, blur_verdict, model, categories)
        if "« " in verdict.explanation and "Deuxième avis" in verdict.explanation:
            description = verdict.explanation.split("« ", 1)[1].split(" »", 1)[0]
    from victim_agent.veilleur_gate import log_verdict

    log_verdict(source=f"image reçue : {p.name}", original_user_intent="(analyse d'image, interface web)", content_or_action=str(p), guardian=detector.GUARDIAN_NAME, verdict=verdict)
    return {
        "verdict": verdict.to_dict(),
        "blur_verdict": blur_verdict.to_dict(),
        "label": detector.french_label(label0), "conf": conf0,
        "label_blur": detector.french_label(label1), "conf_blur": conf1,
        "second_opinion_used": second_opinion, "llm_called": llm_called, "description": description, "replayed": False,
    }


# ── Pilier 3 : scénarios (sous-processus, sortie diffusée en direct) ───────────────────────

def clean_output(text: str) -> str:
    """Retire les tableaux d'affichage de Pipelex (cadres ┌│└) : le verdict est déjà résumé dans les lignes de Veilleur."""
    kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith(("┌", "│", "└", "├"))]
    out = "\n".join(kept)
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out


def run_script(args: list[str]) -> Iterator[tuple[str, int | None]]:
    """Lance un script du dépôt et renvoie (sortie cumulée, code de sortie ou None)."""
    if not _run_lock.acquire(blocking=False):
        yield "Un autre scénario est en cours. Attendez sa fin puis relancez.", None
        return
    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        env.pop("SSL_CERT_FILE", None)
        env.pop("SSL_CERT_DIR", None)
        with tempfile.TemporaryFile("w+", encoding="utf-8") as err:
            proc = subprocess.Popen([sys.executable, *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8", errors="replace", env=env)
            out = ""
            assert proc.stdout is not None
            for line in proc.stdout:
                out += line
                yield clean_output(out), None
            code = proc.wait()
            if code not in (0, 15) and code is not None:
                err.seek(0)
                tail = "\n".join(err.read().strip().splitlines()[-12:])
                out += f"\n[le script s'est terminé avec le code {code}]\n{tail}"
            yield clean_output(out), code
    finally:
        _run_lock.release()


def recent_verdicts(limit: int = 30) -> list[list[str]]:
    """Derniers verdicts du journal commun, tous piliers confondus."""
    rows: list[list[str]] = []
    try:
        lines = VERDICTS_LOG.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return rows
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        g = e.get("guardian", "")
        rows.append([e.get("timestamp", "")[11:19], pillar_of(g), g, e.get("recommended_action", ""), e.get("risk_level", ""), (e.get("explanation", "") or "")[:600]])
    return rows


def pillar_of(guardian: str) -> str:
    base = guardian.split("+", 1)[0]
    if base == "vision_detector":
        return "2 · Image"
    if base in {"baseline", "contextual", "hybrid", "user_confirmation"}:
        return "1 · Texte"
    return "3 · Agent"
