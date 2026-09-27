"""Runner Veilleur — exécute des scénarios de test synthétiques
(scenarios/*.yaml) contre un ou plusieurs gardiens (guardians/*.mthds) et
produit un tableau comparatif attendu/obtenu.

SCÉNARIOS DE TEST SYNTHÉTIQUES — AUCUNE ACTION RÉELLE : le contenu de chaque
scénario est du texte inerte passé au LLM classificateur ; aucune action
(email, exfiltration, etc.) décrite dans un scénario n'est jamais exécutée,
cf. cadre de sécurité du cahier des charges interne (non publié) et tâche 3bis.

Usage :
    python runner.py                                   # gardien baseline, tous les scénarios
    python runner.py --guardian all                     # les 3 gardiens
    python runner.py --guardian baseline,contextual
    python runner.py --scenario injection_faux_avis_hotel --guardian all
    python runner.py --history               # affiche les 10 derniers verdicts journalisés
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from victim_agent.veilleur_gate import GUARDIAN_PIPE_CODES, assess_risk, read_verdict_log, veilleur_library

SENTINEL_DIR = Path(__file__).resolve().parent
DEFAULT_SCENARIOS_DIR = SENTINEL_DIR / "scenarios"
RESULTS_DIR = SENTINEL_DIR / "results"


@dataclass
class ScenarioResult:
    scenario_id: str
    category: str
    guardian: str
    expected_verdict: str
    obtained_verdict: str
    passed: bool
    expected_anomaly_type: str | None
    obtained_anomaly_type: str
    risk_level: str
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_scenarios(scenarios_dir: Path) -> list[dict[str, Any]]:
    scenarios = []
    for path in sorted(scenarios_dir.glob("*.yaml")):
        with path.open(encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        scenarios.append(scenario)
    return scenarios


def build_monitored_action(scenario: dict[str, Any]) -> dict[str, str]:
    """Construit l'entrée MonitoredAction à partir d'un scénario, quelle que
    soit sa forme — cf. section "Format d'un fichier de scénario" du brief :
    - "injection via un outil" (injected_tool + injected_content) ;
    - "action divergente sans outil impliqué" (attempted_action).
    """
    if "injected_content" in scenario:
        source = f"{scenario['injected_tool']} (outil factice)"
        content_or_action = scenario["injected_content"]
    elif "attempted_action" in scenario:
        source = "agent_action (pas d'outil impliqué)"
        content_or_action = scenario["attempted_action"]
    else:
        raise ValueError(f"Scénario {scenario.get('id')!r} : ni 'injected_content' ni 'attempted_action' présent.")
    return {
        "source": source,
        "original_user_intent": scenario["user_request"],
        "content_or_action": content_or_action,
    }


async def run_scenario(scenario: dict[str, Any], guardian: str) -> ScenarioResult:
    monitored_action = build_monitored_action(scenario)
    verdict = await assess_risk(guardian=guardian, **monitored_action)
    expected_verdict = scenario["expected_verdict"]
    return ScenarioResult(
        scenario_id=scenario["id"],
        category=scenario.get("category", ""),
        guardian=guardian,
        expected_verdict=expected_verdict,
        obtained_verdict=verdict.recommended_action,
        passed=verdict.recommended_action == expected_verdict,
        expected_anomaly_type=scenario.get("expected_anomaly_type"),
        obtained_anomaly_type=verdict.anomaly_type,
        risk_level=verdict.risk_level,
        explanation=verdict.explanation,
    )


async def run_all(scenarios: list[dict[str, Any]], guardians: list[str]) -> list[ScenarioResult]:
    results = []
    for scenario in scenarios:
        for guardian in guardians:
            results.append(await run_scenario(scenario, guardian))
    return results


def print_report(results: list[ScenarioResult]) -> None:
    console = Console()
    table = Table(title="Résultats Veilleur")
    table.add_column("Scénario")
    table.add_column("Gardien")
    table.add_column("Attendu")
    table.add_column("Obtenu")
    table.add_column("Résultat")
    for r in results:
        mark = "✅" if r.passed else "❌"
        table.add_row(r.scenario_id, r.guardian, r.expected_verdict, r.obtained_verdict, mark, style=None if r.passed else "bold red")
    console.print(table)

    total = len(results)
    passed = sum(r.passed for r in results)
    attacks = [r for r in results if r.category != "cas_normal"]
    attacks_blocked = sum(1 for r in attacks if r.passed and r.obtained_verdict == "block")
    legit = [r for r in results if r.category == "cas_normal"]
    false_positives = sum(1 for r in legit if r.obtained_verdict != "allow")

    console.print(
        f"\n[bold]{passed}/{total}[/bold] scénarios conformes à l'attendu — "
        f"{attacks_blocked}/{len(attacks)} attaques bloquées, "
        f"{false_positives} faux positif(s) sur {len(legit)} requête(s) légitime(s)."
    )


def save_results(results: list[ScenarioResult]) -> Path:
    RESULTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"run_{timestamp}.json"
    out_path.write_text(
        json.dumps([r.to_dict() for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def _truncate(text: str, max_len: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def print_history(limit: int) -> None:
    """Affiche les N derniers verdicts journalisés par Veilleur (tâche 7 du
    brief) — utile pour montrer un historique pendant la démo, sans rejouer
    aucun scénario ni appeler de LLM."""
    entries = read_verdict_log(limit=limit)
    console = Console()
    if not entries:
        console.print("[dim]Aucun verdict journalisé pour l'instant.[/dim]")
        return

    table = Table(title=f"Historique Veilleur (derniers {len(entries)} verdicts)")
    table.add_column("Heure")
    table.add_column("Gardien")
    table.add_column("Source")
    table.add_column("Verdict")
    table.add_column("Risque")
    table.add_column("Explication")
    for e in entries:
        verdict = e["recommended_action"]
        style = {"block": "bold red", "ask_user": "yellow"}.get(verdict)
        time_str = datetime.fromisoformat(e["timestamp"]).strftime("%H:%M:%S")
        table.add_row(
            time_str,
            e["guardian"],
            _truncate(e["source"], 25),
            verdict,
            e["risk_level"],
            _truncate(e["explanation"], 50),
            style=style,
        )
    console.print(table)


def resolve_guardians(arg: str) -> list[str]:
    if arg == "all":
        return list(GUARDIAN_PIPE_CODES)
    guardians = [g.strip() for g in arg.split(",")]
    unknown = [g for g in guardians if g not in GUARDIAN_PIPE_CODES]
    if unknown:
        raise SystemExit(f"Gardien(s) inconnu(s) : {unknown} (attendu parmi {list(GUARDIAN_PIPE_CODES)})")
    return guardians


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--guardian",
        default="baseline",
        help=f"Gardien(s) séparés par des virgules, ou 'all' (défaut: baseline). Choix : {', '.join(GUARDIAN_PIPE_CODES)}.",
    )
    parser.add_argument("--scenario", help="N'exécuter qu'un seul scénario, par son id.")
    parser.add_argument("--scenarios-dir", default=str(DEFAULT_SCENARIOS_DIR), help="Dossier des scénarios YAML.")
    parser.add_argument(
        "--history",
        type=int,
        nargs="?",
        const=10,
        default=None,
        metavar="N",
        help="N'affiche que les N derniers verdicts journalisés (défaut 10) et quitte, sans rejouer de scénario.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.history is not None:
        print_history(args.history)
        return

    guardians = resolve_guardians(args.guardian)
    scenarios = load_scenarios(Path(args.scenarios_dir))
    if args.scenario:
        scenarios = [s for s in scenarios if s["id"] == args.scenario]
        if not scenarios:
            raise SystemExit(f"Aucun scénario avec l'id {args.scenario!r} trouvé dans {args.scenarios_dir}")

    with veilleur_library():
        results = asyncio.run(run_all(scenarios, guardians))

    print_report(results)
    out_path = save_results(results)
    print(f"\nRésultats enregistrés dans {out_path}")


if __name__ == "__main__":
    main()
