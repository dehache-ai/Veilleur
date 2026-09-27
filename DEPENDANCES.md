# Dépendances du projet

Toutes les commandes ci-dessous s'exécutent dans un environnement Python 3.11 ou plus (venv, conda ou
autre : `requirements.txt` donne les commandes pour les deux), sauf mention contraire. Dans ce dépôt, le
développement s'est fait dans un environnement conda nommé `rise-of-agents`. Voir `README.md` pour le contexte
général du projet.

> **Installation rapide : `pip install -r requirements.txt`** (versions testées, y compris les parties optionnelles ci-dessous).
> Ce document explique le rôle de chaque paquet.

## Cœur du projet (obligatoire)

Nécessaire pour faire tourner Veilleur : les gardiens Pipelex, l'agent
orchestrateur, le runner de scénarios, la bascule Ollama/OpenAI et la
narration vocale.

```bash
pip install pipelex gradium pyyaml rich python-dotenv tomlkit psutil mcp httpx2
```

| Paquet | Rôle dans le projet |
|---|---|
| `pipelex` | Moteur des pipes de classification de risque (`guardians/*.mthds`). Nécessite **Python ≥ 3.11**. |
| `gradium` | Narration vocale (TTS) des verdicts bloquants (`victim_agent/narrator.py`). |
| `pyyaml` | Lecture des scénarios de test (`scenarios/*.yaml`) par `runner.py`. |
| `rich` | Tableaux comparatifs affichés par `runner.py` (`--history`, résultats de run). |
| `python-dotenv` | Chargement des clés API depuis le `.env` (`GRADIUM_API_KEY`, `OPENAI_API_KEY`, `JINKO_API_KEY`). |
| `tomlkit` | Édition des fichiers de config Pipelex par `switch_backend.py`, en préservant leur mise en forme. |
| `psutil` | Pilier réseau (`network_guardian/monitor.py`) — lit les connexions sortantes réelles d'un process. |
| `mcp`, `httpx2` | Client MCP réel vers Jinko (`victim_agent/jinko_client.py`) — `mcp.client.streamable_http` pour le transport, `httpx2` pour l'authentification par en-tête `Authorization`. |

Il faut aussi :
- **Un modèle de langage pour les gardiens.** En pratique, **OpenAI** (paquet `openai`, clé `OPENAI_API_KEY` dans
  `.env`, puis `python switch_backend.py openai`) : c'est rapide, et c'est avec `gpt-4o-mini` (et `gpt-4.1-mini` pour le juge de canal)
  que les mesures ont été faites. Le même paquet sert au second avis image. L'alternative, [Ollama](https://ollama.com) avec `gpt-oss:20b`
  (`ollama pull gpt-oss:20b`), fonctionne mais est très lente (plusieurs minutes par appel possibles).
- Pipelex initialisé une fois (`pipelex init`) — config dans `~/.pipelex/`.

## Optionnel : détection d'images truquées (`adversarial_vision/`)

Cette partie est **indépendante du reste du projet** — elle démontre que
Veilleur peut aussi détecter une manipulation invisible dans une image
(perturbation adverse), pas seulement une instruction cachée dans du texte.
Elle n'est nécessaire que si tu veux fabriquer de nouvelles images pièges
ou faire tourner le détecteur d'images ; le reste de Veilleur (gardiens
texte, runner, orchestrateur, voix) fonctionne parfaitement sans elle.

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```

| Paquet | Rôle |
|---|---|
| `torch` / `torchvision` | Charge un classifieur d'images pré-entraîné (`resnet18`) — utilisé à la fois pour fabriquer les images pièges (`adversarial_vision/generate_attack.py` (non publié)) et pour les détecter (`adversarial_vision/detector.py`). |

**Poids sur le disque** : environ 1 Go pour torch (CPU uniquement, pas de
CUDA — ce projet n'a jamais besoin de GPU). Le flag `--index-url
.../whl/cpu` évite de télécharger les variantes CUDA, beaucoup plus
lourdes et inutiles ici.

**Note** : `adversarial_vision/generate_attack.py` (non publié) (fabrication des images
pièges) peut aussi tourner dans n'importe quel environnement ayant torch —
dans ce projet on l'a testé à la fois dans l'environnement du projet et dans un
autre environnement qui avait déjà torch installé. Seul
`adversarial_vision/detector.py` doit tourner dans le même environnement
que le reste de Veilleur, puisqu'il est censé être
appelé par l'agent en conditions réelles.

## Optionnel : gardien hybride avec préfiltre externe (`victim_agent/prefilter.py`)

⚠️ **Solution externe open source, pas un composant Veilleur.** Le gardien
`hybrid` (`python runner.py --guardian hybrid`) ajoute, avant l'appel au LLM
Veilleur, un préfiltre tiers HuggingFace :
[`NeuralTrust/prompt-guard-oss-small`](https://huggingface.co/NeuralTrust/prompt-guard-oss-small)
(licence **MIT**, non gated, ~140M paramètres, architecture ModernBERT). Le
règlement du hackathon autorise les solutions externes à condition de le
préciser clairement — c'est pourquoi ce gardien reste une **sous-partie
séparée** des gardiens LLM seuls (`baseline`/`contextual`), jamais
fusionnée avec eux. Voir `victim_agent/prefilter.py` pour les alternatives
écartées (modèle Meta gated, modèle 7B disproportionné) et leur justification.

```bash
pip install transformers truststore
```

Le modèle (~550 Mo) se télécharge automatiquement au premier appel et est
mis en cache par HuggingFace (`~/.cache/huggingface/`) — aucun appel réseau
ensuite. Réutilise `torch`, déjà nécessaire pour `adversarial_vision/`.

**Windows uniquement — problème de certificat SSL possible** : si le
téléchargement échoue avec `CERTIFICATE_VERIFY_FAILED` même après avoir
suivi la procédure `unset SSL_CERT_FILE SSL_CERT_DIR` du gotcha habituel
(cf. les notes de développement (non publiées)), c'est un problème distinct — le magasin de certificats
`certifi` embarqué dans Python ne connaît pas une CA locale (antivirus,
proxy d'entreprise) que Windows connaît pourtant (`curl`/navigateur
fonctionnent). `victim_agent/prefilter.py` corrige déjà ça tout seul, de
façon scopée, via `truststore.inject_into_ssl()`/`extract_from_ssl()` autour
du seul téléchargement du modèle.

⚠️ **Ne jamais installer `pip-system-certs`** comme fix "plus simple" à ce
problème : testé en session, ce paquet patche `ssl.SSLContext` de façon
globale et permanente pour tout le process Python, ce qui casse
`victim_agent/jinko_client.py` (le client MCP de Jinko, basé sur
`httpcore2`/`anyio`, entre en récursion infinie face à ce patch global).
`truststore` scopé (utilisé directement, sans `pip-system-certs`) est le fix
retenu — il ne touche `ssl.SSLContext` que pendant le téléchargement du
modèle, jamais pendant le reste de l'exécution.

## Versions testées (relevées le 2026-09-25)

```
pipelex          0.56.0
gradium          0.6.4
pyyaml           6.0.3
rich             14.3.4
python-dotenv    1.2.3
tomlkit          0.14.0
psutil           7.2.2
mcp              2.2.0
httpx2           2.13.0
openai           2.54.0
gradio           6.28.0
torch            2.14.0+cpu   (optionnel, cf. ci-dessus)
torchvision      0.29.0+cpu   (optionnel, cf. ci-dessus)
transformers     5.17.0       (optionnel, gardien hybride uniquement)
truststore       (optionnel, gardien hybride uniquement, Windows — jamais pip-system-certs, cf. ci-dessus)
```

## Optionnel : interface web (`web_ui/`)

```bash
pip install gradio
```

Interface Gradio de démonstration (version 6.x utilisée). Les paramètres `theme` et `css` sont passés à `launch()`, comme l'exige Gradio 6.

## Optionnel : micro pour la confirmation vocale (`ask_user`)

`victim_agent/confirmation.py` (classe `MicrophoneAnswer`) enregistre la réponse de
l'utilisateur avec `sounddevice` (`pip install sounddevice`, qui embarque PortAudio
sous Windows) et `numpy`. Testé le 2026-09-25 (version
0.5.6), avec le micro intégré d'un ordinateur portable Windows. Sans micro ou sans cette bibliothèque, utiliser
`--confirm typed` (clavier) ou `--confirm simulated` (réponses synthétiques).
