"""Évalue Veilleur sur un échantillon de raucle-bench (github.com/epic28-ltd/
raucle-bench, licence MIT — un benchmark public de détection d'injection de
prompt, pas notre travail).

Échantillon plutôt que le jeu complet (165 prompts) : `gpt-oss:20b` en local
peut prendre plusieurs minutes par appel (cf. les notes de développement (non publiées)), donc le jeu complet
prendrait plusieurs heures. `--sample-per-class` prélève N prompts par
classe d'attaque pour un score représentatif et reproductible en un temps
raisonnable ; le jeu complet reste utilisable tel quel avec `--sample-per-class 0`
si on bascule sur OpenAI (bien plus rapide) ou qu'on a le temps.

Usage :
    (dans l'environnement où requirements.txt est installé)
    python benchmark/run_benchmark.py --sample-per-class 3 --guardian baseline
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAUCLE_SRC = HERE.parent / "raucle-bench-src" / "src"
sys.path.insert(0, str(RAUCLE_SRC))
sys.path.insert(0, str(HERE))

from raucle_bench.dataset import load_dataset  # noqa: E402
from raucle_bench.report import build_report, render_markdown, write_json  # noqa: E402
from raucle_bench.runner import run_adapter  # noqa: E402
from raucle_bench.scoring import score  # noqa: E402

from veilleur_adapter import VeilleurAdapter  # noqa: E402

sys.path.insert(0, str(HERE.parent))
from victim_agent.veilleur_gate import veilleur_library  # noqa: E402


def sample_prompts(prompts: list, per_class: int, seed: int = 42, benign: int | None = None) -> list:
    """`benign` : nombre de prompts bénins à prélever (défaut : `per_class`). Les
    prompts bénins servent à mesurer les faux positifs, donc on peut vouloir
    en prendre plus que les autres classes."""
    if per_class <= 0:
        return prompts
    rng = random.Random(seed)
    by_class: dict[str, list] = {}
    for p in prompts:
        by_class.setdefault(p.attack_class, []).append(p)
    sampled = []
    for cls, items in by_class.items():
        n = benign if (cls == "benign" and benign is not None) else per_class
        sampled.extend(rng.sample(items, min(n, len(items))))
    return sampled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--datasets-dir", default=str(HERE.parent / "raucle-bench-src" / "datasets"))
    parser.add_argument("--sample-per-class", type=int, default=3, help="0 = jeu complet (165 prompts, long)")
    parser.add_argument("--sample-benign", type=int, default=None, help="Prompts bénins à prélever (défaut : --sample-per-class).")
    parser.add_argument("--guardian", default="baseline", choices=["baseline", "contextual", "hybrid"])
    parser.add_argument("--results-dir", default=str(HERE / "results"))
    args = parser.parse_args()

    prompts = load_dataset(args.datasets_dir)
    sampled = sample_prompts(prompts, args.sample_per_class, benign=args.sample_benign)
    print(f"Échantillon : {len(sampled)}/{len(prompts)} prompts ({args.sample_per_class}/classe, gardien={args.guardian})")

    adapter = VeilleurAdapter(guardian=args.guardian)
    with veilleur_library():
        result = run_adapter(adapter, sampled)

    s = score(result)
    report = build_report([s], sampled, run_id=f"veilleur-{args.guardian}-sample")

    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / f"{adapter.name}-sample.json"
    write_json(report, out_path)
    (results_dir / f"{adapter.name}-sample.md").write_text(render_markdown(report), encoding="utf-8")

    print(render_markdown(report))
    print(f"\nRésultats enregistrés dans {out_path}")


if __name__ == "__main__":
    main()
