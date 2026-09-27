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
"""


def esc(x: str) -> str:
    return html.escape(x or "")


def card(verdict: dict, *, path: str = "", expected: str = "", replayed: bool = False) -> str:
    action = verdict["recommended_action"]
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
        f'<div class="vc {ACTION_CLASS[action]}"><div class="head">{ACTION_LABEL[action]}</div>'
        f'<div class="meta">{tags}</div><div class="why">{esc(verdict["explanation"])}</div>{compare}</div>'
    )


def banner() -> str:
    if core.live_mode():
        return '<div class="banner">Mode <b>direct</b> : les gardiens LLM sont appelés (votre clé, quelques centimes par essai).</div>'
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


def run_text(title: str, request: str, content: str, guardian: str):
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
        return card(r["verdict"], path=r["path"], expected=expected), preview_html(r["preview"])

    rec = core.recorded().get("text", {})
    key = next((k for k, e in TEXT_EXAMPLES.items() if e is ex), None)
    if same_as_example and key in rec:
        r = rec[key]
        note = "" if guardian == "baseline" else "<div class='note'>Le résultat enregistré correspond au gardien « baseline » (défaut).</div>"
        return card(r["verdict"], path=r["path"], expected=expected, replayed=True) + note, preview_html(preview)
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


def run_image(path: str | None, second: bool):
    if not path:
        return "<div class='banner'>Choisissez un exemple ou déposez une image.</div>", ""
    cap, expected = image_caption(path)
    rec = core.recorded().get("image", {}).get(Path(path).name)
    if rec and not core.live_mode():
        r, replayed = rec, True
    else:
        try:
            r, replayed = core.analyze_image_file(path, second and core.live_mode()), False
        except Exception as exc:
            return f"<div class='banner'>Analyse impossible ({esc(type(exc).__name__)} : {esc(str(exc)[:200])}).</div>", ""
    lines = [
        f"<b>Classifieur d'images (resnet18)</b> : « {esc(r['label'])} » à {r['conf']:.0%}.",
        f"<b>Après un léger flou</b> : « {esc(r['label_blur'])} » à {r['conf_blur']:.0%}.",
        "<b>Test du flou seul</b> : " + ACTION_LABEL[r["blur_verdict"]["recommended_action"]].lower() + ".",
    ]
    if r.get("description"):
        lines.append(f"<b>Deuxième avis (modèle de vision)</b> décrit : « {esc(r['description'])} ».")
    elif not r.get("second_opinion_used", True) and not replayed:
        lines.append("<b>Deuxième avis</b> : non utilisé (il enverrait l'image à l'API OpenAI).")
    detail = "<div class='note'>" + "<br>".join(lines) + "</div>"
    return card(r["verdict"], expected=expected, replayed=replayed), detail


# ── Pilier 3 ─────────────────────────────────────────────────────────────────────────────

def scenario_info(sc: dict) -> str:
    kind = f"**Comportement {sc['kind']}.** " if "kind" in sc else ""
    return f"{kind}{sc['summary']}\n\n**Ce que nous avons observé lors de nos essais :** {sc['expected']}"


def make_scenario_runner(key: str, sc: dict):
    def run():
        rec = core.recorded().get("scenario", {}).get(key)
        if not core.live_mode() and rec and key != "n3":
            yield "[Mode démonstration : résultat enregistré lors de nos essais. Sans clé API, le scénario n'est pas relancé]\n\n" + rec["output"], ""
            return
        header = ""
        for out, code in core.run_script(sc["args"]):
            summary = ""
            if code is not None:
                m = re.search(r"Issue de l'agent : (.+?) \(code", out)
                summary = f"<div class='vc {'no' if 'ARRÊTÉ' in (m.group(1) if m else '') else 'ok'}'><div class='head'>{esc(m.group(1) if m else 'Terminé')}</div></div>" if m else ""
            yield header + out, summary
    return run


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
                        guardian = gr.Radio(["baseline", "hybrid", "contextual"], value="baseline", label="Gardien",
                                            info="baseline = LLM seul (défaut) · hybrid = LLM + préfiltre HuggingFace externe · contextual = LLM en deux étapes")
                        go = gr.Button("Analyser", variant="primary")
                    with gr.Column(scale=5):
                        out = gr.HTML(label="Verdict")
                        seen = gr.HTML()
                sel.change(on_text_example, sel, [request, content, why])
                go.click(run_text, [sel, request, content, guardian], [out, seen])

            with gr.Tab("2 · Image", id="t_image"):
                gr.Markdown(
                    "### Cette image est-elle truquée ?\nUne perturbation invisible peut faire prendre une statue pour une voiture de sport. "
                    "Choisissez un exemple ou déposez une image (elle reste sur cette machine, sauf si vous cochez le deuxième avis)."
                )
                with gr.Row():
                    with gr.Column(scale=5):
                        img = gr.Image(type="filepath", label="Image", height=320, sources=["upload"])
                        ex_img = gr.Examples(
                            examples=[[str(ROOT / p)] for p, _, _ in IMAGE_EXAMPLES],
                            inputs=[img], example_labels=[c for _, c, _ in IMAGE_EXAMPLES], label="Exemples testés (un clic lance l'analyse)", examples_per_page=8,
                        )
                        second = gr.Checkbox(value=core.live_mode(), label="Deuxième avis (envoie l'image à l'API OpenAI)", interactive=core.live_mode())
                        goi = gr.Button("Analyser", variant="primary")
                    with gr.Column(scale=5):
                        outi = gr.HTML("<div class='note'>Le verdict s'affichera ici : cliquez sur un exemple ou déposez une image, puis « Analyser ».</div>")
                        detail = gr.HTML()
                goi.click(run_image, [img, second], [outi, detail])
                ex_img.load_input_event.then(run_image, [img, second], [outi, detail])

            with gr.Tab("3 · Comportement d'agent", id="t_agent"):
                gr.Markdown("### L'agent reste-t-il dans son périmètre ?\nChaque bouton lance le vrai scénario, dans un process séparé, avec la sortie en direct.")
                with gr.Tabs():
                    with gr.Tab("Agent sur le réseau"):
                        gr.Markdown("Un agent simulé (process distinct) ouvre des connexions **locales** ; le moniteur les observe de l'extérieur. Rien ne sort de votre machine. Comparez les comportements normaux et anormaux.")
                        for key, sc in NETWORK_SCENARIOS.items():
                            with gr.Accordion(f"{'⚠️' if sc['kind'] == 'anormal' else '✅'} {sc['title']}", open=(key == "n1")):
                                gr.Markdown(scenario_info(sc))
                                btn = gr.Button("Lancer ce scénario")
                                res = gr.HTML()
                                log = gr.Textbox(label="Sortie", lines=4, max_lines=24, autoscroll=True, placeholder="La sortie s'affiche ici, en direct, après « Lancer ce scénario ».")
                                btn.click(make_scenario_runner(key, sc), None, [log, res])
                    with gr.Tab("Outils, web, échanges entre agents"):
                        gr.Markdown("Scénarios où l'agent (scripté) sort de sa mission : outil non prévu, page réservée, message qui transporte un secret. **Ils appellent le LLM** : il faut une clé API.")
                        for key, sc in OTHER_SCENARIOS.items():
                            with gr.Accordion(sc["title"], open=False):
                                gr.Markdown(scenario_info(sc))
                                btn = gr.Button("Lancer ce scénario")
                                res = gr.HTML(visible=False)
                                log = gr.Textbox(label="Sortie", lines=4, max_lines=30, autoscroll=True, placeholder="La sortie s'affiche ici, en direct, après « Lancer ce scénario ».")
                                btn.click(make_scenario_runner(key, sc), None, [log, res])

            with gr.Tab("Journal"):
                gr.Markdown("### Un seul contrat, un seul journal\nTous les piliers produisent le même verdict et l'écrivent au même endroit.")
                table = gr.HTML(value=journal)
                gr.Button("Actualiser").click(journal, None, table)

        # boutons de la vue d'ensemble : on va à l'onglet, l'exemple est prêt et l'analyse est lancée
        trap = next(e for e in TEXT_EXAMPLES.values() if e["expected"] == "block")
        b_text.click(lambda: (gr.Tabs(selected="t_texte"), trap["title"], trap["request"], trap["content"], f"**Ce que ce cas teste.** {trap['why']}"),
                     None, [tabs, sel, request, content, why]).then(run_text, [sel, request, content, guardian], [out, seen])
        fake = str(ROOT / next(p for p, _, exp in IMAGE_EXAMPLES if exp == "block"))
        b_img.click(lambda: (gr.Tabs(selected="t_image"), fake), None, [tabs, img]).then(run_image, [img, second], [outi, detail])
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
