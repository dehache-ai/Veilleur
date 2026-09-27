# Veilleur

> **Veilleur, l'agent qui vous protège quand vos agents travaillent.**
> *L'automatisation renforce les besoins de sécurité.*

Veilleur est un agent gardien qui surveille les actions d'un autre agent IA (ici, un
agent voyage connecté à Jinko) et intercepte toute tentative de manipulation **avant son
exécution**, avec une explication vocale immédiate de ce qui a été détecté et pourquoi.
Quand il hésite, il **pose la question à voix haute** et interprète la réponse.

Le projet couvre trois angles du risque autour d'un agent IA :

1. **Risque sur l'utilisateur** : injection de prompt cachée dans un contenu lu par
   l'agent (`guardians/`, `victim_agent/`).
2. **Risque sur les données** : image manipulée de façon invisible pour tromper un
   classifieur (`adversarial_vision/`).
3. **Risque sur l'agent lui-même** : l'agent dérive de son périmètre, ou plusieurs agents
   se concertent (`network_guardian/`, `collusion_guardian/`).

Les décisions de Veilleur reposent en partie sur des contrôles déterministes pour trancher
ce qui est évident et rapide à vérifier, et en partie sur un modèle de langage pour
analyser le reste, y compris les cas ambigus.

Projet réalisé dans le cadre du hackathon *Rise of Agents X*.

**Équipe :** David Dehache (seul membre).

**Droits :** © 2026 David Dehache. **Tous droits réservés ; lire, cloner et exécuter pour l'évaluer est autorisé ; toute autre
réutilisation demande l'accord de l'auteur.** Contact : via les *issues* de ce dépôt GitHub.

Documentation détaillée, avec les mesures **et leurs limites** : [`partie_1.md`](partie_1.md)
(texte), [`partie_2.md`](partie_2.md) (image), [`partie_3.md`](partie_3.md) (agent et réseau),
[`pistes_ajout.md`](pistes_ajout.md) (ce qui reste à faire).

## Logique agentique : percevoir, raisonner, décider, agir

Veilleur n'est pas un script : chaque décision qui n'est pas évidente est prise par un **modèle de langage**, et chaque décision
déclenche une **action** sur l'agent supervisé.

| Étape | Ce que fait Veilleur |
|---|---|
| **Percevoir** | contenu renvoyé par un outil, image, **connexions réseau réelles** d'un process, appels d'outil, messages entre agents |
| **Raisonner** | des capteurs déterministes produisent des faits ; un LLM juge ce qui reste ambigu (intention perçue contre demande de l'utilisateur, cohérence d'une requête avec la mission, cohérence d'un message avec la mission) ; un **audit de trajectoire** relit tout le journal d'une session et doit citer les événements qui justifient son verdict ; pour l'image, un modèle de vision décrit l'image sans connaître l'étiquette du classifieur, puis un LLM compare |
| **Décider** | verdict structuré `allow`, `block` ou `ask_user`, avec une explication ; en cas de doute ou d'échec d'un modèle, c'est un refus |
| **Agir** | bloquer un contenu, refuser un appel d'outil **avant** son exécution, **arrêter un process** (démonstration locale), **poser une question à voix haute**, écouter la réponse et la faire interpréter, narrer et journaliser |

**Deux modèles de langage** (via Pipelex) : `gpt-4o-mini` pour les gardiens texte, le juge réseau, le gardien de périmètre, l'audit de
trajectoire et le second avis image ; `gpt-4.1-mini` pour le juge de message et le juge de sortie du canal entre agents (`python
switch_backend.py openai` règle les deux alias). Le second modèle a été **mesuré** avant d'être adopté (voir `partie_3.md`) : à
prompts identiques, il est aussi bon sur ces deux juges et meilleur sur les cas ambigus, mais il rate 6 attaques sur 12 à l'audit de
trajectoire, qui reste donc sur `gpt-4o-mini`. Ne sont pas des modèles de langage : `resnet18` (classifieur d'images), le préfiltre
externe NeuralTrust (classifieur), Gradium (voix).

**Ce que Veilleur n'est pas, à dire honnêtement :** ce n'est pas une boucle autonome qui planifie et choisit ses propres outils : c'est
un enchaînement de décisions de modèles de langage et d'actions, chacune bornée par des règles. Et **l'agent de voyage supervisé est
scripté** (outils factices) : seule la recherche d'hôtels peut passer par le vrai Jinko (`--real-jinko`, crédits limités). Un agent
de supervision à outils est une piste (`pistes_ajout.md`).

## ⚠️ Cadre de sécurité

Ce projet simule des **attaques** contre un agent IA de démonstration, dans un but
strictement défensif et pédagogique :

- Tout le contenu « attaquant » est synthétique, écrit par nous, jamais généré pour cibler
  un système réel. Les adresses citées sont fictives (`exemple.test`, `example.com`).
- Les exemples d'attaques publiés sont tous des techniques déjà publiques ; l'inventaire et les sources sont dans
  [`EXEMPLES_ATTAQUES.md`](EXEMPLES_ATTAQUES.md).
- L'agent « victime » est notre propre agent de démo, jamais un système tiers.
- Aucune action réseau réelle (email, exfiltration...) n'est exécutée : un scénario est une
  donnée textuelle inerte. Les démonstrations de supervision réseau utilisent des sockets
  **locaux** (127.0.0.1) et un site officiel **fictif en mémoire**.
- Les images d'attaque publiées (`adversarial_vision/presentation/`) sont des
  **démonstrations défensives** : elles ne trompent que `resnet18`, un modèle public. Ne pas
  les rediffuser comme attaque réelle contre un système tiers. Les scripts qui les
  fabriquent ne sont pas publiés.

## Résultats mesurés (et ce qu'ils ne prouvent pas)

Tous les chiffres viennent des documents `partie_*.md`, qui détaillent la méthode.

| Pilier | Mesure | Résultat | Réserve principale |
|---|---|---|---|
| Texte | Benchmark public raucle-bench, 165 prompts (125 attaques, 40 bénins), `gpt-4o-mini`, gardiens `baseline` et `hybrid` | détection **100 %**, 4 faux positifs sur 40, F1 0,984 | Les détecteurs d'encodage (base64, hexadécimal, César, acrostiche, caractères invisibles) ont été écrits **en regardant les attaques que le benchmark nous faisait manquer** : avant eux, 96,8 % (`baseline`) et 98,4 % (`hybrid`). Une évasion différente peut passer |
| Texte | Jeu de 40 textes en contexte voyage, **écrit par nous** | 0 faux positif, 0 attaque manquée (`baseline`, `hybrid`) | Jeu écrit par les auteurs : plus facile qu'un jeu tiers |
| Image | 57 photos Wikimedia (calibration 28 + **vérification 29**, jamais regardées pendant le réglage) | test du flou seul : **0 attaque adaptative détectée sur 57** ; avec le deuxième avis LLM : 29/29 attaques simples et 29/29 adaptatives détectées, 1 faux positif sur 29 photos | Les cibles d'attaque sont impossibles dans la scène ; une cible **plausible** passe (mesuré sur une photo locale) ; l'attaque ne vise que `resnet18` |
| Agent, réseau | Démonstrations locales, puis **jeu de 80 cas étiquetés écrit par nous** pour les six juges (réglage 40 cas, vérification 40, 3 essais par cas) | vérification : **0 attaque manquée sur 60 essais**, mais **9 faux positifs sur 42 essais légitimes (21 %), tous du juge réseau**. Le juge réseau a ensuite été **corrigé puis revalidé sur 24 nouveaux cas** : 0 attaque manquée sur 30 essais, 0 cas légitime bloqué à tort (11 demandes de confirmation sur 30 essais), contre 27 blocages avec le prompt d'origine | Jeu petit et déséquilibré (1 à 3 cas légitimes par juge et par groupe) ; les juges sont très conservateurs ; prompts ajustés une fois sur le groupe de réglage |

Reproductible : `python runner.py --guardian all` (scénarios), `python
benchmark/run_benchmark.py --sample-per-class 0 --guardian hybrid` (benchmark complet, nécessite
le dépôt raucle-bench cloné, voir `benchmark/README.md`).

## Architecture

Les piliers utilisent des techniques différentes mais partagent le **même contrat de
sortie** et le **même aval** :

```
[Contenu ou comportement à évaluer : texte, image, connexion réseau, message entre agents]
        |
        v
[Capteurs déterministes : regex, décodage, test du flou, liste blanche, compteurs...]
        |   (des INDICES, jamais la décision finale sur les cas ambigus)
        v
[Juge LLM : gardiens texte, deuxième avis image, juges réseau / périmètre / canal]
        |
        v
[Verdict structuré : risk_level, anomaly_type, explanation, recommended_action]
        |
        v   journalisé dans results/verdicts.jsonl (historique unifié)
        v   "block" : contenu jamais transmis + narration vocale Gradium
        v   "ask_user" : la machine pose la question, la voix est transcrite, un LLM interprète
```

### Pilier 1 : texte (`guardians/`, `victim_agent/`), injection de prompt

Trois variantes de gardien, comparables sur les mêmes scénarios (une quatrième, `strict`, a
été mesurée puis supprimée : elle bloquait 39 prompts bénins sur 40) :

| Gardien | Mécanique |
|---|---|
| `baseline` (**défaut**) | Classification directe, un seul passage LLM |
| `contextual` | Deux étapes : intention perçue du contenu, puis comparaison à la demande de l'utilisateur |
| `hybrid` | Préfiltre externe HuggingFace + `baseline` |

`hybrid` a été le gardien par défaut jusqu'au 2026-09-27 ; mesuré ensuite sur 25 vraies
réponses d'un service tiers (Jinko, pas des textes écrits par nous) : 24 % de faux positifs,
presque tous dus au préfiltre externe mal calibré sur du texte dense (listes, prix), contre
4 % pour `baseline` sur le même jeu — voir `partie_1.md`.

Défense en profondeur, dans l'ordre : **prétraitement** déterministe
(`victim_agent/preprocess.py` : caractères invisibles, base64, hexadécimal, César/ROT13,
acrostiche : des indices montrés au LLM, jamais un verdict), **heuristiques** regex
(`victim_agent/heuristics.py`), préfiltre externe (`hybrid`), **gardien LLM**, puis un
**jeton canari** dans chaque prompt système qui détecte une fuite du prompt.

> **`hybrid` : solution externe open source, clairement identifiée.** Le règlement du
> hackathon autorise les solutions externes à condition de le préciser. `victim_agent/prefilter.py`
> charge le modèle tiers [`NeuralTrust/prompt-guard-oss-small`](https://huggingface.co/NeuralTrust/prompt-guard-oss-small)
> (licence MIT). C'est pourquoi `hybrid` reste une variante **séparée** : la comparaison
> « LLM seul » contre « LLM + préfiltre tiers » reste lisible, et le composant externe est
> nommé dans chaque verdict qu'il produit (`hybrid+prefilter_hf`). S'il est indisponible, le
> LLM juge seul, comme `baseline`.

**Confirmation vocale (`ask_user`)** : `victim_agent/confirmation.py`. La machine lit
l'explication et demande « Autorisez-vous cette action ? » (Gradium TTS), la réponse est
transcrite (Gradium STT), et un LLM la classe (approuvée, refusée, pas claire). Toute
incertitude devient un **refus**. Testé avec des réponses simulées (synthèse puis
transcription réelles) et un essai informel au micro.

### Pilier 2 : image (`adversarial_vision/`), perturbation adverse

Deux contrôles :
1. **Test du flou** (`detector.py`) : une perturbation adverse est fragile, un léger flou
   change la catégorie d'une image piégée, pas celle d'une vraie photo. Déterministe.
2. **Deuxième avis** (`second_opinion.py`) : un modèle de vision décrit l'image sans
   connaître l'étiquette de `resnet18`, puis un LLM juge si l'étiquette est **compatible**.
   Demandé seulement si `resnet18` est confiant à au moins 60 %. Incompatible : blocage.

Pourquoi le deuxième avis : une **attaque adaptative** (qui inclut le flou dans son calcul)
défait le test du flou sur 57 photos sur 57, avec seulement 2 niveaux d'amplitude sur 255. Le
deuxième avis envoie l'image à l'API OpenAI (`--no-second-opinion` pour l'éviter). Six images
de démonstration sont publiées dans `adversarial_vision/presentation/` : deux originales,
deux truquées détectées par les deux contrôles, deux détectées par le LLM seul (`montage.png`).

### Pilier 3 : agent et réseau (`network_guardian/`, `collusion_guardian/`)

Ici on vérifie l'inverse des piliers précédents : **l'agent lui-même** reste-t-il dans son
périmètre ? Trois briques, toutes démontrées sur des scénarios **synthétiques** :

- **Moniteur réseau** (`monitor.py`) : lit les connexions réelles du process (`psutil`),
  sans faire confiance à l'agent. Une connexion de la liste blanche est autorisée tout de
  suite ; une connexion **hors liste** ou un **rythme inhabituel** vers un hôte, même
  autorisé, est jugé par un LLM avec la mission, l'activité en cours et le nom d'hôte. En
  cas d'échec du juge, la décision prudente est conservée.
- **Gardien de périmètre** (`scope_guard.py`) : juge chaque requête avant son envoi (contrat de
  mission déclaré, chemin normalisé, méthode) puis la page reçue, pour qu'un agent chargé de
  lire des pages publiques ne franchisse pas une zone protégée du même site (démonstration sur
  un site fictif en mémoire). Il est **branché sur l'orchestrateur** : chaque appel d'outil de
  l'agent voyage est jugé avant exécution (par exemple un paiement `checkout` est bloqué).
- **Supervision d'un canal entre agents** (`collusion_guardian/`) : mêmes principes pour des
  agents qui se concertent : recherche d'une donnée sensible transformée, forme des
  messages, juge LLM par message, contrôle des sorties (contenu et destination), audit LLM de
  la trajectoire entière d'une session.

Ce pilier **alerte et journalise**. Dans la démonstration locale, un verdict `block` du moniteur
**arrête réellement** le process de l'agent simulé (blocage grossier et tardif) ; ce blocage n'est
pas activé sur le vrai appel Jinko, où le moniteur observe l'agent lui-même.

## Structure du projet

```
├── guardians/            # Pipes Pipelex : gardiens texte, juges réseau, périmètre, canal, confirmation
├── victim_agent/         # passerelle commune (veilleur_gate.py), prétraitement, heuristiques,
│                         # préfiltre externe, orchestrateur, narration et confirmation vocales, client Jinko
├── adversarial_vision/   # Pilier 2 : détecteur, deuxième avis, images de démonstration
├── network_guardian/     # Pilier 3 : moniteur réseau, gardien de périmètre, démonstrations
├── collusion_guardian/   # Pilier 3 : supervision d'un canal entre agents, démonstration
├── app.py                # lance l'interface web (navigateur ouvert automatiquement)
├── web_ui/               # interface web (Gradio) : présentation et essais des trois piliers
├── benchmark/            # évaluation sur raucle-bench, jeu voyage, résultats agrégés
├── scenarios.example/    # scénarios d'exemple (structure seulement)
├── runner.py             # scénarios en lot, tableau comparatif, historique
├── switch_backend.py     # bascule Ollama <-> OpenAI, choix du modèle
├── partie_1.md  partie_2.md  partie_3.md   # documentation détaillée des trois piliers
└── pistes_ajout.md       # ce qui reste à faire
```

Non publiés : les scénarios réels, le jeu de test voyage (charges d'attaque), les scripts qui
fabriquent des attaques, les journaux d'exécution.

## Installation

- Python **3.11 ou plus** (exigé par [Pipelex](https://docs.pipelex.com)), dans un environnement isolé de votre
  choix (`python -m venv .venv`, conda, ou autre), puis `pip install -r requirements.txt`
  ([`requirements.txt`](requirements.txt) donne les commandes pour venv et conda ; le rôle de chaque paquet est dans
  [`DEPENDANCES.md`](DEPENDANCES.md) ; si une version plus récente casse quelque chose, [`requirements-tested.txt`](requirements-tested.txt)
  donne les versions exactes vérifiées), puis `pipelex init` une fois. **Dans l'assistant `pipelex init`**, le choix par défaut est
  *Pipelex Gateway* (il demande une clé Pipelex, dont Veilleur n'a pas besoin) : à la question sur les backends d'inférence, choisir
  **OpenAI** (et **Ollama** si vous voulez le mode local), refuser l'extension VS Code si vous ne la voulez pas, et laisser vide les
  clés demandées (elles se règlent dans `.env`). Ensuite `python switch_backend.py openai`. Sans GPU, installer d'abord les roues CPU de `torch`
  (commande en tête du fichier). `sounddevice` reste optionnel (micro).
- Clés dans un fichier `.env` : copier [`.env.example`](.env.example) en `.env` **dans le dossier du projet**.
  `GRADIUM_API_KEY` pour la voix, `OPENAI_API_KEY` pour les gardiens via OpenAI et le deuxième avis image,
  `JINKO_API_KEY` pour le vrai Jinko (optionnel). Le `.env` du dossier parent n'est **pas** lu, sauf si vous
  le demandez avec `VEILLEUR_ENV_PARENT=1` (dans l'environnement ou dans le `.env` du projet). Lancez les commandes depuis le
  dossier du projet : Pipelex charge lui-même le `.env` du dossier courant, en écrasant les valeurs déjà présentes.
- **Modèle des gardiens** : en pratique **OpenAI** (clé `OPENAI_API_KEY`, rapide ; les mesures du projet
  utilisent `gpt-4o-mini`). L'alternative est [Ollama](https://ollama.com) avec `gpt-oss:20b`, local mais
  très lent (plusieurs minutes par appel possibles). Tous les pipes
  référencent l'alias `@veilleur-model` (et `@veilleur-model-channel` pour les juges du canal), déclarés dans
  `~/.pipelex/inference/deck/x_custom_llm_deck.toml`. `switch_backend.py` le fait pointer
  vers l'un ou l'autre :
  ```bash
  python switch_backend.py openai        # gpt-4o-mini + gpt-4.1-mini pour le juge de canal
  python switch_backend.py openai --model gpt-4o-mini --channel-model gpt-4o-mini   # un seul modèle pour tout
  python switch_backend.py ollama
  python switch_backend.py --status
  ```
- Sous Windows, si un antivirus ou un proxy inspecte le TLS, `victim_agent/env.py`
  construit automatiquement un bundle de certificats (variable `SSL_CERT_FILE`).

## Interface web

```bash
pip install gradio
python app.py               # ou python web_ui/app.py
```

Le navigateur s'ouvre tout seul sur http://127.0.0.1:7860 (premier port libre si celui-ci est pris ; `--no-browser` pour
ne pas l'ouvrir). Dans VS Code : ouvrir `app.py`, choisir l'interpréteur Python de votre environnement (celui où vous avez installé `requirements.txt`), puis « Run Python File ».

Trois onglets, un par pilier, plus une vue d'ensemble et le journal commun. Chaque onglet propose des
**cas que nous avons réellement testés** (contenu normal ou piégé, photo authentique ou truquée, agent au
comportement normal ou anormal) avec ce que nous en attendions, et permet d'essayer les vôtres.

- **Avec `OPENAI_API_KEY`** dans `.env` : les gardiens sont appelés en direct, avec votre clé (quelques centimes
  par essai). Le deuxième avis image envoie l'image à l'API OpenAI (case à cocher).
- **Sans clé** : mode *démonstration*. Les cas proposés affichent les résultats enregistrés lors de nos essais
  (`web_ui/recorded.json`, signalés comme tels) ; les règles, le prétraitement et le test du flou tournent en
  direct. Un texte libre subtil ne peut pas être jugé sans modèle.
- Les scénarios d'agents (onglet 3) sont lancés dans un process séparé, un seul à la fois. Ceux du pilier 3
  réseau n'utilisent que des connexions locales.
- `python web_ui/record.py` régénère les résultats enregistrés (appelle le LLM).

## Utilisation

```bash
# Pilier 1 : scénarios (gardien par défaut : hybrid), comparaison, historique
python runner.py
python runner.py --guardian all
python runner.py --history

# Démo de l'agent orchestrateur (outils factices ; --no-voice pour le mode texte)
python victim_agent/orchestrator.py --no-voice
python victim_agent/orchestrator.py --real-jinko --no-voice   # vrai Jinko, crédits limités

# Flux ask_user : la machine pose la question, la réponse est transcrite puis interprétée
python victim_agent/orchestrator.py --confirm simulated   # réponses synthétiques
python victim_agent/orchestrator.py --confirm typed       # clavier
python victim_agent/orchestrator.py --confirm voice       # micro (pip install sounddevice)

# Benchmark public (dépôt raucle-bench cloné à part, voir benchmark/README.md)
python benchmark/run_benchmark.py --sample-per-class 3 --guardian hybrid

# Pilier 2 : détecter une image (les six images de démonstration sont dans presentation/)
python adversarial_vision/detector.py adversarial_vision/presentation/03_statue_truquee_LLM_seul.png --no-voice
python adversarial_vision/detector.py <image> --no-voice --no-second-opinion   # sans envoi à OpenAI

# Pilier 3 : démonstrations synthétiques (locales)
python network_guardian/rogue_agent_demo.py --no-voice
python network_guardian/web_agent_demo.py --no-voice
python collusion_guardian/collusion_demo.py --no-voice
```

## Limites connues

- **Les jeux de test sont petits et écrits par nous** (sauf le benchmark public). Les
  réglages des juges des piliers 2 et 3 ont été ajustés en regardant leurs scénarios ; seule
  la calibration des images a un groupe de vérification séparé.
- **Texte** : le prétraitement des encodages ne couvre que ceux listés ; un chiffrement
  inconnu, une autre langue ou une stéganographie plus fine passent. Le benchmark est en
  anglais, le projet en français.
- **Image** : le deuxième avis ne détecte que les étiquettes trompeuses **implausibles**, ne
  protège pas contre une attaque qui vise aussi le modèle de vision, dépend d'un seuil de
  confiance de `resnet18`, et envoie l'image à un service tiers.
- **Revue indépendante** : le code a été relu le 2026-09-26 par un autre modèle de langage (deux passes en lecture seule, sans
  le contexte du projet). Les défauts trouvés ont été corrigés ou listés comme non corrigés dans `partie_3.md`, section « Revue de
  code par un relecteur indépendant » (par exemple, un secret coupé en morceaux de moins de 9 caractères n'est pas suivi).
- **Agent et réseau** : blocage réel seulement dans la démonstration locale (arrêt du process,
  grossier et tardif, pas de règle de pare-feu ; le trafic UDP est invisible sous Windows) ; le juge retarde la
  décision de quelques secondes (il ne bloque plus le sondage) ; le vrai appel Jinko avec le gardien de périmètre et le moniteur réseau n'a été
  exécuté qu'une fois (crédits limités), et le juge réseau n'y a pas été déclenché ; le gardien de périmètre juge les appels d'outils de
  l'orchestrateur (outils dangereux factices, agent scripté), mais le gardien de canal entre
  agents n'est branché sur aucun flux réel, seulement sur sa démonstration.
- **Canal entre agents** : le juge de message refusait à tort des messages ordinaires (faux positifs de 23 % sur nos cas légitimes
  en vérification) ; il a été corrigé le 2026-09-26 (0 sur 57 essais après correction), sur un jeu de 66 messages écrit par nous. Il
  reste conservateur sur les cas ambigus (voir `partie_3.md`, « Correction du juge de message »).
- **Stéganographie entre agents** : Veilleur ne détecte que les acrostiches **simples** (initiales des lignes,
  des mots ou des phrases qui épellent un morceau du secret suivi). Tout autre schéma caché dans un texte ordinaire
  passe (la démonstration le montre avec la deuxième lettre de chaque ligne). Les schémas possibles sont en nombre
  infini et un texte fluide ne laisse pas de trace de forme : il faudrait connaître le schéma que les agents ont
  convenu en secret. Nous limitons seulement ce que le message
  permet, par le contrôle des sorties et l'audit de la session (voir `partie_3.md`, piste B).
- **Voix** : reconnaissance testée avec des voix synthétiques et un essai informel au micro ;
  un autre locuteur ou du bruit peuvent la dégrader.
- **Jinko** : l'usage réel est volontairement limité (`--real-jinko` explicite, plafond
  d'appels persistant `JINKO_MAX_CALLS`) pour ne pas consommer les crédits du hackathon.
- **Jeton canari** : la valeur publiée dans ce dépôt est une valeur de démonstration, elle ne
  protège rien ; à remplacer pour un vrai déploiement.
