"""Enregistre les résultats réels des cas proposés par l'interface, pour le mode rejeu (sans clé API).

    python web_ui/record.py            # tout enregistrer (appelle le LLM ; scénarios : quelques minutes)
    python web_ui/record.py text image # seulement certaines parties

Écrit `web_ui/recorded.json`. À relancer quand un gardien ou un exemple change.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
from examples import IMAGE_EXAMPLES, NETWORK_SCENARIOS, OTHER_SCENARIOS, TEXT_EXAMPLES  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    only = next((a.split("=", 1)[1].split(",") for a in args if a.startswith("scenario=")), None)
    parts = {a.split("=", 1)[0] for a in args} or {"text", "image", "scenario"}
    data = core.recorded()
    from victim_agent.veilleur_gate import veilleur_library

    with veilleur_library():
        if "text" in parts:
            data["text"] = {}
            for key, ex in TEXT_EXAMPLES.items():
                r = core.judge_text(ex["request"], ex["content"], "hybrid")
                data["text"][key] = {"verdict": r["verdict"], "path": r["path"]}
                print("texte", key, r["verdict"]["recommended_action"], "(attendu", ex["expected"] + ")", flush=True)
        if "image" in parts:
            data["image"] = {}
            for path, cap, expected in IMAGE_EXAMPLES:
                r = core.analyze_image_file(str(core.ROOT / path), True)
                data["image"][Path(path).name] = r
                print("image", Path(path).name, r["verdict"]["recommended_action"], "(attendu", expected + ")", flush=True)
    if "scenario" in parts:
        data.setdefault("scenario", {})
        for key, sc in {**NETWORK_SCENARIOS, **OTHER_SCENARIOS}.items():
            if only and key not in only:
                continue
            out, code = "", None
            for out, code in core.run_script(sc["args"]):
                pass
            data["scenario"][key] = {"output": core.clean_output(out), "exit": code}
            print("scénario", key, "code", code, flush=True)
    core.save_recorded(data)
    print("écrit :", core.RECORDED_FILE)


if __name__ == "__main__":
    main()
