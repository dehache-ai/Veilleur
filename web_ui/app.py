"""Interface web de Veilleur (Gradio) : présente les trois piliers et permet de les essayer.

    python web_ui/app.py                 # ouvre le navigateur sur http://127.0.0.1:7860
    python app.py                        # idem, depuis la racine (bouton « Run » de VS Code)
    python web_ui/app.py --port 7870 --no-browser

Avec une clé `OPENAI_API_KEY` dans `.env`, les gardiens LLM sont appelés en direct. Sans clé,
l'interface affiche les résultats enregistrés lors de nos essais (signalés comme tels : « mode démonstration ») ; les
couches déterministes (règles, prétraitement, test du flou) restent, elles, exécutées en direct.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import html
import re
import sys
from pathlib import Path

import gradio as gr

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
from examples import IMAGE_EXAMPLES, MISSION, NETWORK_SCENARIOS, OTHER_SCENARIOS, TEXT_EXAMPLES  # noqa: E402

ROOT = core.ROOT
FREE = "Saisie libre"
ACTION_LABEL = {"allow": "AUTORISÉ", "block": "BLOQUÉ", "ask_user": "CONFIRMATION DEMANDÉE"}
ACTION_CLASS = {"allow": "ok", "block": "no", "ask_user": "ask"}

CSS = """
.vc {border-left: 6px solid var(--border-color-primary); border-radius: 8px; padding: 14px 18px; margin: 6px 0;
     background: var(--background-fill-secondary);}
.vc.ok {border-left-color: #2e9e5b; background: rgba(46,158,91,.10);}
.vc.no {border-left-color: #d64545; background: rgba(214,69,69,.10);}
.vc.ask {border-left-color: #d9962b; background: rgba(217,150,43,.12);}
.vc .head {font-weight: 700; font-size: 1.15em; letter-spacing: .02em;}
.vc .meta {opacity: .75; font-size: .9em; margin: 2px 0 8px 0;}
.vc .why {margin-top: 6px; line-height: 1.5;}
.vc .tag {display: inline-block; padding: 1px 9px; border-radius: 10px; font-size: .82em; margin-right: 6px;
          background: var(--background-fill-primary); border: 1px solid var(--border-color-primary);}
.pill-grid {display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin: 10px 0;}
.pill {border: 1px solid var(--border-color-primary); border-radius: 10px; padding: 14px 16px; background: var(--background-fill-secondary);}
.pill h4 {margin: 0 0 6px 0;}
.pill .q {font-style: italic; opacity: .85; margin-bottom: 8px;}
.note {font-size: .9em; opacity: .8;}
.jt-wrap {max-height: 520px; overflow: auto; border: 1px solid var(--border-color-primary); border-radius: 8px;}
.jt {border-collapse: collapse; width: 100%; font-size: .9em;}
.jt th {position: sticky; top: 0; background: var(--background-fill-secondary); text-align: left; padding: 8px 10px;}
.jt td {vertical-align: top; padding: 8px 10px; border-top: 1px solid var(--border-color-primary); line-height: 1.45;}
.jt td:nth-child(2) {white-space: nowrap;}
.jt td.act {font-weight: 700; white-space: nowrap;}
.jt td.act.ok {color: #2e9e5b;} .jt td.act.no {color: #d64545;} .jt td.act.ask {color: #b9791a;}
.banner {padding: 8px 14px; border-radius: 8px; border: 1px solid var(--border-color-primary); margin: 4px 0 10px 0;}
.voice-box {border: 1px solid #7c5cbf; border-radius: 8px; padding: 6px 12px; margin: 4px 0 10px 0;
            background: rgba(124,92,191,.10);}
/* Gradio stylise sa case via border/background-color (appearance:none), pas accent-color :
   on doit reprendre les mêmes propriétés pour que le contraste change vraiment. */
.voice-box input[type="checkbox"] {
    width: 22px !important; height: 22px !important; min-width: 22px !important;
    border: 2px solid #7c5cbf !important; border-radius: 4px !important;
    background-color: #fff !important;
}
.voice-box input[type="checkbox"]:checked {
    background-color: #7c5cbf !important; border-color: #7c5cbf !important;
}
.caption-box {border: 1px dashed var(--border-color-primary); border-radius: 8px; padding: 10px 14px; margin: 8px 0;}
"""


def esc(x: str) -> str:
    return html.escape(x or "")


def card(verdict: dict, *, path: str = "", expected: str = "", replayed: bool = False, avis: str = "") -> str:
    action = verdict["recommended_action"]
    head = f"{avis} : {ACTION_LABEL[action]}" if avis else ACTION_LABEL[action]
    tags = f'<span class="tag">niveau de risque : {esc(verdict["risk_level"])}</span><span class="tag">type : {esc(verdict["anomaly_type"])}</span>'
    if replayed:
        tags += '<span class="tag">résultat enregistré lors de nos essais (mode démonstration)</span>'
    if path:
        tags += f'<span class="tag">décidé par : {esc(path)}</span>'
    compare = ""
    if expected:
        same = expected == action
        compare = f'<div class="meta">Attendu : <b>{ACTION_LABEL[expected]}</b> — {"conforme" if same else "<b>différent de ce que nous attendions</b>"}.</div>'
    return (
        f'<div class="vc {ACTION_CLASS[action]}"><div class="head">{esc(head)}</div>'
        f'<div class="meta">{tags}</div><div class="why">{esc(verdict["explanation"])}</div>{compare}</div>'
    )


def banner() -> str:
    if core.live_mode():
        backend = core.current_backend()
        label = {"openai": "LLM externe (OpenAI)", "ollama": "LLM local (Ollama)"}.get(backend, "LLM externe ou local (config inconnue)")
        return f'<div class="banner">Mode : <b>{label}</b></div>'
    return (
        '<div class="banner">Mode <b>démonstration</b> : aucune clé API détectée, donc les cas proposés affichent le résultat <b>enregistré</b> lors de nos essais ; '
        "les couches déterministes tournent en direct. Pour tout essayer en direct, ajoutez <code>OPENAI_API_KEY</code> dans <code>.env</code> "
        "(voir <code>.env.example</code>) puis relancez.</div>"
    )


# ── Pilier 1 ─────────────────────────────────────────────────────────────────────────────

def text_example_choices() -> list[str]:
    return [FREE] + [e["title"] for e in TEXT_EXAMPLES.values()]


def find_text_example(title: str) -> dict | None:
    return next((e for e in TEXT_EXAMPLES.values() if e["title"] == title), None)


def on_text_example(title: str):
    ex = find_text_example(title)
    if ex is None:
        return gr.update(), gr.update(), "Écrivez votre propre contenu : une réponse d'outil que l'agent est sur le point de lire."
    return ex["request"], ex["content"], f"**Ce que ce cas teste.** {ex['why']}"


def preview_html(preview: dict) -> str:
    rows = []
    rows.append("Règle déterministe déclenchée : <b>oui</b>" + (" (après décodage)" if preview["after_decoding"] else "") if preview["rule_hit"] else "Règle déterministe déclenchée : non")
    if preview["notes"]:
        rows.append("Prétraitement : " + " ; ".join(esc(n) for n in preview["notes"]))
    else:
        rows.append("Prétraitement : rien d'inhabituel (pas de caractères invisibles ni de texte encodé repéré)")
    return "<div class='note'>" + "<br>".join(rows) + "</div>"


def voice_html(verdict: dict, voice: bool) -> str:
    """Joue la voix (si cochée et si le verdict bloque/demande confirmation) et renvoie un
    petit encart d'erreur si la synthèse a échoué — vide dans le cas normal (silencieux ou
    voix jouée sans souci)."""
    if not voice:
        return ""
    msg = core.narrate_if_blocking(verdict)
    return f"<div class='note'>{esc(msg)}</div>" if msg else ""


def run_text(title: str, request: str, content: str, guardian: str, voice: bool):
    if not (content or "").strip():
        return "<div class='banner'>Entrez un contenu à analyser.</div>", ""
    ex = find_text_example(title)
    same_as_example = ex is not None and ex["content"] == content and ex["request"] == request
    expected = ex["expected"] if same_as_example else ""
    preview = core.deterministic_preview(content)

    if core.live_mode():
        try:
            r = core.judge_text(request or MISSION, content, guardian)
        except Exception as exc:  # jamais un écran blanc : on l'explique
            return f"<div class='banner'>Le gardien n'a pas pu répondre ({esc(type(exc).__name__)} : {esc(str(exc)[:200])}). Par prudence, ce contenu ne doit pas être considéré comme sûr.</div>", preview_html(preview)
        return card(r["verdict"], path=r["path"], expected=expected) + voice_html(r["verdict"], voice), preview_html(r["preview"])

    rec = core.recorded().get("text", {})
    key = next((k for k, e in TEXT_EXAMPLES.items() if e is ex), None)
    if same_as_example and key in rec:
        r = rec[key]
        note = "" if guardian == "baseline" else "<div class='note'>Le résultat enregistré correspond au gardien « baseline » (défaut).</div>"
        return card(r["verdict"], path=r["path"], expected=expected, replayed=True) + note + voice_html(r["verdict"], voice), preview_html(preview)
    if preview["rule_hit"]:
        v = {"risk_level": "critique", "anomaly_type": "instruction_cachee", "explanation": preview["rule_text"] + " Bloqué par les règles, sans modèle.", "recommended_action": "block"}
        return card(v, path=core.PATH_LABELS["heuristic"]), preview_html(preview)
    return ("<div class='banner'>Aucune règle déterministe n'a réagi, et le mode démonstration ne peut pas juger un texte libre : "
            "le modèle de langage est nécessaire pour les cas subtils. Ajoutez une clé API pour l'essayer en direct.</div>"), preview_html(preview)


# ── Pilier 2 ─────────────────────────────────────────────────────────────────────────────

def image_caption(path: str) -> tuple[str, str]:
    name = Path(path).name
    for p, cap, exp in IMAGE_EXAMPLES:
        if Path(p).name == name:
            return cap, exp
    return "", ""


PLACEHOLDER_VERDICT = "<div class='note'>Cliquez sur « Analyser » pour voir le verdict de Veilleur.</div>"


def on_image_selected(path: str | None):
    """Se déclenche à la sélection (exemple ou dépôt) : dit ce qu'est l'image AVANT toute
    analyse, sans jamais lancer le gardien — le verdict n'apparaît qu'au clic sur « Analyser »."""
    if not path:
        return "", "<div class='banner'>Choisissez un exemple ou déposez une image.</div>", ""
    cap, expected = image_caption(path)
    if not cap:
        caption_html = "<div class='caption-box'>Image personnalisée : nous ne savons pas si elle est truquée ou authentique avant analyse.</div>"
    else:
        truth = "authentique" if expected == "allow" else "truquée"
        caption_html = f"<div class='caption-box'><b>{esc(cap)}</b> — cette image est en réalité <b>{truth}</b>.</div>"
    return caption_html, PLACEHOLDER_VERDICT, ""


def run_image(path: str | None, second: bool, voice: bool):
    if not path:
        return "<div class='banner'>Choisissez un exemple ou déposez une image.</div>", ""
    _, expected = image_caption(path)
    rec = core.recorded().get("image", {}).get(Path(path).name)
    if rec and not core.live_mode():
        r, replayed = rec, True
    else:
        try:
            r, replayed = core.analyze_image_file(path, second and core.live_mode()), False
        except Exception as exc:
            return f"<div class='banner'>Analyse impossible ({esc(type(exc).__name__)} : {esc(str(exc)[:200])}).</div>", ""

    # Double résultat : le test du flou est déterministe (aucun LLM) ; le hackathon demande un
    # jugement LLM, apporté par le second avis — les deux sont montrés séparément plutôt que
    # fondus dans un seul verdict, pour que la part de chacun soit visible.
    blur_card = card(r["blur_verdict"], path="Test du flou (signal, sans LLM)", expected=expected, replayed=replayed, avis="Premier avis (déterministe)")
    if not r.get("second_opinion_used"):
        second_card = ("<div class='note'>Deuxième avis (LLM) non exécuté sur cet essai — cochez la case pour l'inclure "
                        "(l'image part alors vers l'API OpenAI). Sans lui, seul le test déterministe a jugé.</div>")
    elif not r.get("llm_called", True):
        second_card = ("<div class='note'>Photo rejetée par les règles déterministes (test du flou) : pas d'appel au LLM, "
                        "inutile puisque l'image est déjà bloquée sans lui.</div>")
    else:
        second_card = card(r["verdict"], path="Modèle de vision + jugement LLM", expected=expected, replayed=replayed, avis="Second avis (LLM)")

    lines = [
        f"<b>Classifieur d'images (resnet18)</b> : « {esc(r['label'])} » à {r['conf']:.0%}.",
        f"<b>Après un léger flou</b> : « {esc(r['label_blur'])} » à {r['conf_blur']:.0%}.",
    ]
    if r.get("description"):
        lines.append(f"<b>Deuxième avis (modèle de vision)</b> décrit : « {esc(r['description'])} ».")
    detail = "<div class='note'>" + "<br>".join(lines) + "</div>"

    # La voix se déclenche si L'UN OU L'AUTRE des deux résultats signale une image truquée
    # (le second avis ne peut qu'aggraver le verdict du flou, jamais l'adoucir — voir
    # apply_second_opinion — donc en pratique le verdict final suffit, mais on vérifie les
    # deux explicitement pour rester correct si cette règle changeait).
    worst = r["verdict"] if r["verdict"]["recommended_action"] != "allow" else r["blur_verdict"]
    voice_note = voice_html(worst, voice) if worst["recommended_action"] != "allow" else ""
    return blur_card + second_card + voice_note, detail


# ── Pilier 3 ─────────────────────────────────────────────────────────────────────────────

def scenario_info(sc: dict) -> str:
    kind = f"**Comportement {sc['kind']}.** " if "kind" in sc else ""
    llm = f"\n\n**Appel à un LLM ?** {sc['llm']}" if sc.get("llm") else ""
    return f"{kind}{sc['summary']}\n\n**Ce que nous avons observé lors de nos essais :** {sc['expected']}{llm}"


VERDICT_RANK = {"allow": 0, "ask_user": 1, "block": 2}


def scenario_outcome_card(out: str) -> str:
    """Résume la fin du scénario : le code de sortie du process dit seulement s'il a été TUÉ
    (blocage réel, réservé au scénario 1) — un verdict `ask_user` ou même `block` peut avoir été
    journalisé sans arrêter le process (`ask_user` n'arrête jamais rien tout seul ; `block` non
    plus sans `enforcement=\"kill\"`). "terminé normalement (rien n'a été arrêté)" à lui seul
    laissait croire que rien de notable ne s'était passé — on ajoute donc le pire verdict vu
    dans la sortie."""
    m = re.search(r"Issue de l'agent : (.+?) \(code", out)
    outcome = m.group(1) if m else "Terminé"
    stopped = "ARRÊTÉ" in outcome
    # Les scripts n'utilisent pas tous le même préfixe ("Verdict :", "Décision [stage] :",
    # "Avant l'action [stage] :", "Gardien de périmètre... :"...) : on ne matche que la fin
    # commune, universelle à tous ("... : action (...)"), pour couvrir les 4 scripts de ce pilier.
    verdicts = re.findall(r":\s*(allow|ask_user|block)\s*\(", out)
    worst = max(verdicts, key=lambda v: VERDICT_RANK.get(v, 0)) if verdicts else "allow"
    if stopped:
        cls, note = "no", ""
    elif worst == "block":
        cls, note = "no", "Un verdict « block » a été journalisé sans que le process ne soit arrêté (l'arrêt réel n'est activé que pour ce type de scénario précis) — voir le détail ci-dessus."
    elif worst == "ask_user":
        cls, note = "ask", ("Veilleur a demandé une confirmation (« ask_user ») en cours de route. « rien n'a été arrêté » veut seulement dire que "
                             "le process a continué — ask_user ne stoppe jamais rien tout seul, contrairement à un vrai blocage.")
    else:
        cls, note = "ok", ""
    why = f"<div class='why'>{esc(note)}</div>" if note else ""
    return f"<div class='vc {cls}'><div class='head'>{esc(outcome)}</div>{why}</div>"


def make_scenario_runner(key: str, sc: dict, voice: bool = False, confirm_mode: str | None = None):
    def run():
        rec = core.recorded().get("scenario", {}).get(key)
        if not core.live_mode() and rec and key != "n3":
            yield "[Mode démonstration : résultat enregistré lors de nos essais. Sans clé API, le scénario n'est pas relancé]\n\n" + rec["output"], ""
            return
        args = [a for a in sc["args"] if not (voice and a == "--no-voice")]
        if confirm_mode:  # seuls les scénarios réseau (rogue_agent_demo.py) reconnaissent --confirm
            args = [*args, "--confirm", confirm_mode]
        header = ""
        for out, code in core.run_script(args):
            summary = scenario_outcome_card(out) if code is not None else ""
            yield header + out, summary
    return run


def run_scenario_by_key(scenarios: dict, key: str, voice: bool, confirm_mode: str | None = None):
    """Comme `make_scenario_runner`, mais `key` (et `voice`) sont lus au moment du clic (depuis
    une liste déroulante et une case à cocher) plutôt que figés à la construction de l'UI — pour
    un seul bouton « Lancer ce scénario » partagé par plusieurs scénarios."""
    yield from make_scenario_runner(key, scenarios[key], voice, confirm_mode)()


# ── Journal ──────────────────────────────────────────────────────────────────────────────

def journal() -> str:
    """Tableau HTML (l'explication complète, à la ligne) plutôt que gr.Dataframe, qui coupe les cellules longues."""
    rows = core.recent_verdicts()
    if not rows:
        return "<div class='note'>Aucun verdict pour l'instant : essayez un des onglets, puis revenez ici.</div>"
    head = "".join(f"<th>{h}</th>" for h in ("Heure (UTC)", "Pilier", "Gardien", "Action", "Risque", "Explication"))
    body = []
    for hour, pillar, guardian, action, risk, why in rows:
        cls = {"allow": "ok", "block": "no", "ask_user": "ask"}.get(action, "")
        body.append(f"<tr><td>{esc(hour)}</td><td>{esc(pillar)}</td><td>{esc(guardian)}</td>"
                    f"<td class='act {cls}'>{esc(action)}</td><td>{esc(risk)}</td><td>{esc(why)}</td></tr>")
    return f"<div class='jt-wrap'><table class='jt'><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


# ── Interface ────────────────────────────────────────────────────────────────────────────

OVERVIEW_TOP = """
# Veilleur — un gardien pour agents IA

Un agent IA (ici un agent de voyage) lit des pages, des outils, des images, et agit. Chacune de ces entrées peut le
manipuler, et l'agent lui-même peut dévier. **Veilleur se place entre l'agent et le monde** et rend, pour chaque
événement, un même verdict : *autoriser*, *bloquer* ou *demander confirmation*, avec une explication.

<div class="pill-grid">
<div class="pill"><h4>1 · Texte</h4><div class="q">Ce contenu contient-il une consigne cachée ?</div>
Règles déterministes + prétraitement (texte encodé, caractères invisibles) + préfiltre externe + <b>LLM</b> pour les cas subtils.</div>
<div class="pill"><h4>2 · Image</h4><div class="q">Cette image est-elle truquée pour tromper un classifieur ?</div>
Test de stabilité au flou (traitement du signal, sans LLM) + <b>second avis</b> d'un modèle de vision, car le flou seul est contournable.</div>
<div class="pill"><h4>3 · Agent</h4><div class="q">L'agent est-il resté dans son périmètre ?</div>
Observation des connexions réelles du process, liste blanche déterministe, <b>juge LLM</b> pour l'ambigu, blocage réel possible.</div>
</div>

"""

OVERVIEW_BOTTOM = """
**Principe commun** : les capteurs déterministes tranchent le cas évident, vite et de façon prévisible ; le LLM ne juge
que ce qui est ambigu ; en cas d'échec, le résultat est un refus, jamais un feu vert. Tous les piliers écrivent dans le même
journal (onglet *Journal*).

### Comment tester par vous-même
- **Texte** : choisissez un exemple (contenu normal, consigne cachée, texte encodé…) ou écrivez le vôtre, et essayez de tromper le gardien.
- **Image** : choisissez une photo authentique ou truquée, ou déposez la vôtre.
- **Agent** : lancez un comportement normal puis un comportement anormal, et comparez avec ce que nous avons observé.

### Ce que ce projet ne prétend pas
Les jeux de test sont écrits par nous et petits ; un LLM varie d'une exécution à l'autre ; seuls les acrostiches simples sont détectés (un autre schéma caché passe) ;
le test du flou seul est défait par une attaque adaptative. Les chiffres et les limites sont détaillés dans `partie_1.md`, `partie_2.md` et `partie_3.md`.
Les scénarios sont synthétiques : aucune action réelle, adresses en `example.com` / `.test`, données fictives.
"""


def build() -> gr.Blocks:
    with gr.Blocks(title="Veilleur") as demo:
        gr.HTML(banner())
        with gr.Tabs() as tabs:
            with gr.Tab("Vue d'ensemble", id="t_vue"):
                gr.Markdown(OVERVIEW_TOP)
                gr.Markdown("### Essayer en un clic")
                with gr.Row():
                    b_text = gr.Button("1 · Un texte piégé", variant="primary")
                    b_img = gr.Button("2 · Une image truquée", variant="primary")
                    b_agent = gr.Button("3 · Un agent qui dévie", variant="primary")
                gr.Markdown(OVERVIEW_BOTTOM)

            with gr.Tab("1 · Texte", id="t_texte"):
                gr.Markdown("### Un outil a renvoyé ce contenu à l'agent : est-il piégé ?\nChoisissez un cas testé ou écrivez le vôtre.")
                with gr.Row():
                    with gr.Column(scale=5):
                        sel = gr.Dropdown(text_example_choices(), value=text_example_choices()[1], label="Cas")
                        why = gr.Markdown(f"**Ce que ce cas teste.** {list(TEXT_EXAMPLES.values())[0]['why']}")
                        request = gr.Textbox(value=list(TEXT_EXAMPLES.values())[0]["request"], label="Demande de l'utilisateur à l'agent", lines=1)
                        content = gr.Textbox(value=list(TEXT_EXAMPLES.values())[0]["content"], label="Contenu reçu par l'agent (à juger)", lines=7)
                        guardian = gr.Radio(
                            ["baseline", "hybrid", "contextual"], value="baseline", label="Gardien",
                            info="baseline = LLM seul (défaut, le mieux mesuré) · hybrid = + préfiltre HuggingFace externe "
                                 "(mesuré moins bon : 24 % de faux positifs sur données réelles, gardé pour montrer le "
                                 "composant tiers) · contextual = LLM en deux étapes (mesuré moins bon aussi : 70 % de "
                                 "faux positifs sur le benchmark public, latence doublée)",
                        )
                        go = gr.Button("Analyser", variant="primary")
                    with gr.Column(scale=5):
                        with gr.Group(elem_classes=["voice-box"]):
                            voice = gr.Checkbox(value=True, label="🔊 Voix sur détection (Gradium)",
                                                 info="Joue à voix haute le verdict si Veilleur bloque ou demande confirmation — comme la démo en ligne de commande. Ne marche que si vous lancez cette UI en local (le son joue sur cette machine).")
                        out = gr.HTML(label="Verdict")
                        seen = gr.HTML()
                sel.change(on_text_example, sel, [request, content, why])
                go.click(lambda: gr.Button(interactive=False), None, go) \
                    .then(run_text, [sel, request, content, guardian, voice], [out, seen]) \
                    .then(lambda: gr.Button(interactive=True), None, go)

            with gr.Tab("2 · Image", id="t_image"):
                gr.Markdown(
                    "### Cette image est-elle truquée ?\nUne perturbation invisible peut faire prendre une statue pour une voiture de sport. "
                    "Choisissez un des 6 exemples testés (statue et plage, chacune en authentique / attaque simple / attaque adaptative) "
                    "ou déposez votre propre image (elle reste sur cette machine, sauf si vous cochez le "
                    "deuxième avis). La sélection dit ce qu'est l'image ; Veilleur ne se prononce qu'après « Analyser »."
                )
                with gr.Row():
                    with gr.Column(scale=5):
                        img = gr.Image(type="filepath", label="Image", height=320, sources=["upload"])
                        ex_img = gr.Examples(
                            examples=[[str(ROOT / p)] for p, _, _ in IMAGE_EXAMPLES],
                            inputs=[img], example_labels=[c for _, c, _ in IMAGE_EXAMPLES], label="Les 6 exemples testés (cliquez pour choisir)", examples_per_page=6,
                        )
                        cap_html = gr.HTML()
                        second = gr.Checkbox(value=core.live_mode(), label="Deuxième avis — LLM de vision (envoie l'image à l'API OpenAI)", interactive=core.live_mode())
                        goi = gr.Button("Analyser", variant="primary")
                    with gr.Column(scale=5):
                        with gr.Group(elem_classes=["voice-box"]):
                            voicei = gr.Checkbox(value=True, label="🔊 Voix sur détection (Gradium)",
                                                  info="Joue à voix haute dès que l'un des deux résultats signale une image truquée. Local uniquement.")
                        outi = gr.HTML("<div class='note'>Le verdict s'affichera ici : cliquez sur un exemple ou déposez une image, puis « Analyser ».</div>")
                        detail = gr.HTML()
                goi.click(lambda: gr.Button(interactive=False), None, goi) \
                    .then(run_image, [img, second, voicei], [outi, detail]) \
                    .then(lambda: gr.Button(interactive=True), None, goi)
                ex_img.load_input_event.then(on_image_selected, img, [cap_html, outi, detail])
                img.upload(on_image_selected, img, [cap_html, outi, detail])

            with gr.Tab("3 · Comportement d'agent", id="t_agent"):
                gr.Markdown("### L'agent reste-t-il dans son périmètre ?\nChaque bouton lance le vrai scénario, dans un process séparé, avec la sortie en direct.")
                with gr.Tabs():
                    with gr.Tab("Agent sur le réseau"):
                        gr.Markdown("Un agent simulé (process distinct) ouvre des connexions **locales** ; le moniteur les observe de l'extérieur. Rien ne sort de votre machine. Choisissez un scénario, regardez s'il appelle un LLM avant de le lancer, puis comparez.")
                        n_choices = [(f"{'⚠️ anormal' if sc['kind'] == 'anormal' else '✅ normal'} — {sc['title']}", key) for key, sc in NETWORK_SCENARIOS.items()]
                        n_sel = gr.Dropdown(n_choices, value=n_choices[0][1], label="Scénario")
                        n_info = gr.Markdown(scenario_info(NETWORK_SCENARIOS[n_choices[0][1]]))
                        with gr.Group(elem_classes=["voice-box"]):
                            n_voice = gr.Checkbox(value=True, label="🔊 Voix sur détection (Gradium)",
                                                   info="Ce sous-process narre lui-même à voix haute quand le juge bloque ou demande confirmation. Local uniquement.")
                        n_confirm = gr.Radio(
                            [("🎙️ Voix réelle — répondez à voix haute au micro", "voice"), ("Réponse simulée (sans micro, toujours « non »)", "simulated")],
                            value="voice", label="Confirmation (scénario 2, s'il rend ask_user)",
                            info="« Voix réelle » attend votre réponse au micro (quelques secondes) puis un LLM l'interprète. « Simulée » répond toujours non, sans vous écouter — utile sans micro.",
                        )
                        n_btn = gr.Button("Lancer ce scénario", variant="primary")
                        n_res = gr.HTML()
                        n_log = gr.Textbox(label="Sortie", lines=6, max_lines=24, autoscroll=True, placeholder="La sortie s'affiche ici, en direct, après « Lancer ce scénario ».")
                        n_sel.change(lambda k: scenario_info(NETWORK_SCENARIOS[k]), n_sel, n_info)
                        n_btn.click(lambda: gr.Button(interactive=False), None, n_btn) \
                            .then(functools.partial(run_scenario_by_key, NETWORK_SCENARIOS), [n_sel, n_voice, n_confirm], [n_log, n_res]) \
                            .then(lambda: gr.Button(interactive=True), None, n_btn)
                    with gr.Tab("Outils, web, échanges entre agents"):
                        gr.Markdown(
                            "Scénarios où l'agent (scripté — la séquence d'actions est écrite à l'avance dans la démo, "
                            "pas décidée par un agent autonome) sort de sa mission : outil non prévu, page réservée, "
                            "message qui transporte un secret. Chaque scénario mélange des décisions **sans LLM** (liste "
                            "blanche, contrat de mission) et des décisions **avec LLM** (le juge tranche l'ambigu) — le "
                            "détail est indiqué dans chacun, dépliez pour le voir. Une clé API est nécessaire pour la partie LLM."
                        )
                        with gr.Group(elem_classes=["voice-box"]):
                            o_voice = gr.Checkbox(value=True, label="🔊 Voix sur détection (Gradium)",
                                                   info="Chaque sous-process narre lui-même à voix haute quand un gardien bloque ou demande confirmation. Local uniquement.")
                        for key, sc in OTHER_SCENARIOS.items():
                            with gr.Accordion(sc["title"], open=False):
                                gr.Markdown(scenario_info(sc))
                                btn = gr.Button("Lancer ce scénario")
                                res = gr.HTML(visible=False)
                                log = gr.Textbox(label="Sortie", lines=4, max_lines=30, autoscroll=True, placeholder="La sortie s'affiche ici, en direct, après « Lancer ce scénario ».")
                                btn.click(lambda: gr.Button(interactive=False), None, btn) \
                                    .then(functools.partial(run_scenario_by_key, OTHER_SCENARIOS, key), o_voice, [log, res]) \
                                    .then(lambda: gr.Button(interactive=True), None, btn)

            with gr.Tab("Journal"):
                gr.Markdown("### Un seul contrat, un seul journal\nTous les piliers produisent le même verdict et l'écrivent au même endroit.")
                table = gr.HTML(value=journal)
                gr.Button("Actualiser").click(journal, None, table)

        # boutons de la vue d'ensemble : on va à l'onglet, l'exemple est prêt et l'analyse est lancée.
        # Le bouton "un clic" ET le bouton "Analyser" de l'onglet visé sont désactivés pendant le
        # traitement (modèle + éventuellement LLM + voix, plusieurs secondes) : sans ça, un second
        # clic pendant que le premier tourne encore se met en file et relance toute l'analyse une
        # deuxième fois (observé en test : deux verdicts et deux narrations, ~36 s d'écart).
        trap = next(e for e in TEXT_EXAMPLES.values() if e["expected"] == "block")
        b_text.click(lambda: (gr.Tabs(selected="t_texte"), trap["title"], trap["request"], trap["content"], f"**Ce que ce cas teste.** {trap['why']}", gr.Button(interactive=False)),
                     None, [tabs, sel, request, content, why, go]) \
            .then(run_text, [sel, request, content, guardian, voice], [out, seen]) \
            .then(lambda: gr.Button(interactive=True), None, go)
        fake = str(ROOT / next(p for p, _, exp in IMAGE_EXAMPLES if exp == "block"))
        # "Essayer en un clic" ne fait que sélectionner l'exemple truqué et afficher sa légende —
        # exactement comme un clic manuel sur un exemple — SANS lancer l'analyse : le verdict ne
        # doit apparaître qu'au clic sur « Analyser », ici comme ailleurs dans ce pilier.
        b_img.click(lambda: (gr.Tabs(selected="t_image"), fake), None, [tabs, img]) \
            .then(on_image_selected, img, [cap_html, outi, detail])
        b_agent.click(lambda: gr.Tabs(selected="t_agent"), None, tabs)
    return demo


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=None, help="Port (défaut : 7860, ou le premier libre au-dessus).")
    parser.add_argument("--no-browser", action="store_true", help="Ne pas ouvrir le navigateur automatiquement.")
    parser.add_argument("--share", action="store_true", help="Lien public temporaire Gradio (à éviter avec votre clé API).")
    args = parser.parse_args()

    ctx = contextlib.nullcontext()
    if core.live_mode():
        from victim_agent.veilleur_gate import veilleur_library
        ctx = veilleur_library()
    with ctx:
        build().queue().launch(server_name="127.0.0.1", server_port=args.port, share=args.share, theme=gr.themes.Soft(), css=CSS, inbrowser=not args.no_browser)


if __name__ == "__main__":
    main()
