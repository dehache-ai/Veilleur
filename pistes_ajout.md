# Pistes d'ajout — à reprendre

Suite des premières pistes d'amélioration (notes internes, non publiées), issue de la session du 2026-09-25. Cette fois
les pistes partent de ce que les prototypes des piliers 3 et 4 ont réellement
montré, y compris leurs échecs. Classées par priorité.

## Ce qui est déjà fait (pour ne pas le refaire)

- Piste C de `partie_3.md` : gardien de périmètre d'un agent web
  (`network_guardian/scope_guard.py`), prototype avec LLM.
- Piste B de `partie_3.md` : supervision d'un canal entre agents
  (`collusion_guardian/channel_guard.py`), couches 0 à 5 dont un audit de
  trajectoire, plus un contrôle déterministe des destinations de sortie.
- Pilier 3 : juge LLM sur les connexions hors liste blanche et sur le rythme de
  connexions (`network_guardian/monitor.py`, `guardians/network.mthds`).
- Suppression de la variante `strict`.
- Pilier 1 : prétraitement des encodages (`victim_agent/preprocess.py`), `hybrid` par
  défaut, flux `ask_user` vocal (`victim_agent/confirmation.py`, testé avec des
  réponses simulées ; essai réel au micro jugé satisfaisant par le développeur,
  sans mesure).
- Canal entre agents : formats d'identifiants déclarés (priorité 6) et contrôle des
  sorties avec juge LLM de destination (priorité 7).
- Correctif de certificats TLS (`victim_agent/env.py`), option `--model` de
  `switch_backend.py`.

## Priorité 1 — Mesurer avant d'ajouter (fait le 2026-09-25, voir `partie_1.md`)

Fait : benchmark complet de 165 prompts avec `gpt-4o-mini`, jeu voyage de 40
textes écrit par nous, scénarios maison, sur les quatre variantes. `strict` a
été supprimée (39 bénins sur 40 bloqués). `baseline` et `hybrid` sont proches
(écart de 2 prompts sur 125 attaques).

Reste à faire : un jeu de test d'évasion **écrit par un tiers ou mis de côté avant d'écrire
les détecteurs** (le 100 % obtenu après prétraitement vient de données qui ont
servi à les écrire), un jeu de test **écrit par un tiers**, et une mesure avec un
autre modèle que `gpt-4o-mini`.

## Priorité 2 — Un agent de décision avec outils

Règle du hackathon (précisée) : les règles déterministes sont permises, mais tout
ne doit pas l'être : il faut des LLM qui décident. Le pilier 1 (gardiens LLM) et
maintenant les piliers 3 et 4 (juges LLM sur les cas ambigus) le respectent. Le
pilier 2 (image) reste entièrement déterministe.

**Arbitre de divergence entre `baseline` et `hybrid` : non justifié pour
l'instant.** Sur les 165 prompts, ils divergent dans 2 cas seulement (1,2 %), et
`hybrid` avait raison les deux fois (le préfiltre bloque, le LLM seul laisse
passer). La règle « si `hybrid` bloque, on bloque » donne déjà la bonne réponse.
À reconsidérer si un autre jeu de test montre des divergences plus nombreuses.

Pistes restantes pour un vrai agent (boucle avec outils) :
- Un agent qui reçoit les verdicts des piliers et choisit la réponse (bloquer,
  demander confirmation, consulter le journal, couper un processus).
- Le flux `ask_user` n'est pas implémenté (déjà noté dans les limites du
  `README`) : question orale, écoute, décision.
- Un LLM sur le pilier 2 (image) : il reçoit le résultat du test de flou et le
  contexte, et décide.
- Précautions : le contenu signalé reste une donnée non fiable (règle « donnée,
  pas commande », canari) ; les actions destructrices demandent une
  confirmation ; en cas d'échec du LLM, comportement prudent, jamais `allow`.

## Priorité 2 bis — Pilier 2 : deuxième avis par un modèle de vision-langage

Mesuré (voir `partie_2.md`) : l'attaque adaptative défait le détecteur de flou sur 57
photos sur 57. Un deuxième avis (un modèle de vision décrit l'image, un LLM juge la
compatibilité avec l'étiquette de `resnet18`, demandé seulement si `resnet18` est
confiant à au moins 60 %) donne, sur 29 photos de vérification jamais vues pendant le
réglage : 1 faux positif sur 29 photos, et 29 sur 29 attaques simples et 29 sur 29
attaques adaptatives détectées.

**Intégré** dans `detector.py` (blocage si incompatible). Reste à faire :
- attaque adaptative de la plage (photo locale) manquée par le juge : voir `partie_2.md` ;
- passer par Pipelex (comme les autres gardiens) au lieu d'un appel direct à l'API ;
- tester des attaques dont l'étiquette cible est **plausible** (proche de la vraie), que
  ce contrôle ne détecte pas par construction, et des attaques qui visent aussi le
  modèle de vision ;
- signaler l'envoi des images à un service tiers.

## Piste : demander à l'utilisateur en cas de doute (pilier 2)

Décision actuelle (2026-09-25) : quand le deuxième avis trouve l'étiquette du classifieur
incompatible avec l'image, l'image est **bloquée**. Une autre option, écartée pour
l'instant mais gardée en piste : **demander à l'utilisateur** (flux `ask_user` vocal de
`victim_agent/confirmation.py`) dans les cas de doute, par exemple quand l'alerte vient
seulement du deuxième avis et non du test du flou, ou quand la confiance de `resnet18`
est proche du seuil de 60 %. Avantage : un faux positif ne coûte qu'une question. Limite :
l'utilisateur ne voit pas une perturbation invisible et peut seulement dire si l'image
lui paraît cohérente.

## Fait le 2026-09-26 : faux positif du juge de message du canal

Le juge de message refusait à tort des messages ordinaires (23 % des essais légitimes en vérification). Corrigé en lui montrant le
résultat des contrôles déterministes sur le contenu, séparé du rôle des agents (0 sur 57 essais après correction ; voir `partie_3.md`,
« Correction du juge de message »). Reste à faire : un jeu de messages écrit par un tiers, plus de formulations ambiguës (5 sur 6 encore
bloquées), et un test avec un autre modèle.

## Piste : agents qui se parlent en stéganographie (limite ouverte du canal entre agents)

Traité seulement pour les acrostiches **simples** (initiales des lignes, mots, phrases : fait le 2026-09-25, voir
`partie_3.md`). Non traité : un message ordinaire qui porte un secret par un autre schéma convenu (deuxième lettre,
un mot sur trois...) passe, et un détecteur par schéma serait contourné par un autre schéma. Idées à
tester, sans certitude : le médiateur réécrit chaque message (perte de fidélité, coût d'un appel), formats
structurés obligatoires entre agents, accès minimal des agents aux données sensibles. À mesurer sur un jeu
de messages contenant plusieurs schémas, pas seulement l'acrostiche.

## Priorité 3 — Brancher les piliers 3 et 4 sur un vrai flux

**Fait le 2026-09-25 pour le gardien de périmètre** : il juge chaque appel d'outil de l'orchestrateur
avant exécution (voir `partie_3.md`), avec des outils dangereux factices ; le chemin avec le vrai
Jinko reste à exécuter. **Reste à brancher : le gardien de canal** entre agents.

Ancien énoncé :

Le gardien de périmètre et le gardien de canal ne tournaient que sur
des scénarios séparés (agents scriptés, site fictif, jeton fictif).

- Envelopper l'outil Jinko réel de l'orchestrateur avec `ScopeGuard` : requête
  jugée avant envoi, page jugée à la réception.
- Passer les échanges entre deux vrais agents par `ChannelGuard`.
- Objectif : une démonstration qui montre un usage réel, pas seulement un
  scénario écrit à la main.

## Priorité 4 — Un jeu de test étiqueté pour les nouveaux gardiens (premier jeu fait, voir `partie_3.md`)

Fait le 2026-09-25 : 80 cas écrits par nous, réglage et vérification séparés. Résultat : aucune
attaque manquée en vérification, mais le juge réseau bloque tous les cas légitimes dont le contexte
n'est pas directement lié à la mission (9 faux positifs sur 42). Le juge réseau a été corrigé et revalidé sur 24 nouveaux cas (voir `partie_3.md` : plus aucun
blocage à tort, 0 attaque manquée). Reste à faire : un jeu écrit par un tiers, des cas plus
nombreux et équilibrés, et les cas ambigus (encore bloqués 9 fois sur 12).

Ancien énoncé :

Les prompts de `scope.mthds` et de `collusion.mthds` ont été ajustés deux fois
en regardant le scénario de démonstration (voir `partie_3.md`). Un jeu de test
séparé, de 15 à 20 cas par gardien, dont des cas légitimes, dira si les
réglages généralisent ou si c'est du surapprentissage sur la démonstration.

## Priorité 5 — Blocage réel (pilier 3)

**Fait dans la démonstration locale (2026-09-25)** : un verdict `block` du moniteur arrête le process
de l'agent simulé (`enforcement="kill"`). Reste à faire : une règle de pare-feu par adresse (plus
fine, demande des droits administrateur) pour le vrai appel Jinko, où arrêter le process couperait le
travail légitime ; et un blocage avant la connexion plutôt qu'après la détection (proxy sortant). La détection d'un rythme anormal vers un domaine
autorisé est faite (juge LLM, voir `partie_3.md`).

## Priorité 6 — Réduire les faux positifs du canal entre agents (faite)

Un identifiant de session hexadécimal légitime était bloqué par le juge de
message. Fait : formats d'identifiants déclarés à l'avance
(`known_id_formats`). Deux approches ont échoué (signal informatif, règle de
prompt) ; ce qui marche : masquer l'identifiant dans le texte montré au juge.
Limite : un secret inconnu au format d'un identifiant déclaré passerait. Voir
`partie_3.md`.

## Priorité 7 — Contrôle du contenu sortant (fait, avec un juge LLM pour les destinations)

Fait : `record_action(..., destination=..., content=...)` cherche la donnée
sensible dans le contenu sortant (refus même vers une destination autorisée) et
fait juger par un LLM (`assess_egress`) toute destination hors de la liste de
l'agent. Voir `partie_3.md`.

Reste à faire : le juge de destination est très conservateur (il refuse un relais
de messagerie plausible), et le contenu sortant n'est pas soumis à un juge LLM
quand la destination est autorisée et que rien de sensible n'y est détecté.

## Priorité 8 — Revue de sécurité avant toute publication (obligatoire)

**État au 2026-09-25 :** revue faite (aucun vrai secret dans `sentinel/`), export public par
liste blanche construit et audité (`tools/export_public.py` vers `../sentinel_public/`, jeton canari
remplacé). Restent : relire l'export une dernière fois avant le premier
`git push`, initialiser Git dans `sentinel_public/` (pas dans `sentinel/`), choisir une licence,
et décider du moment du premier commit.


**Rien de sensible ne doit partir vers GitHub : ni secret, ni charge d'attaque.**
Cette revue est une étape à part entière, à faire **avant le premier `git push`**
(et à refaire avant chaque publication de nouveaux fichiers). Elle vérifie ce
que l'on croit ne pas publier, pas seulement ce que l'on veut publier.

### A. Secrets et données personnelles

- Chercher dans tout ce qui sera publié : clés (`sk-`, préfixes de clés API),
  valeurs de `GRADIUM_API_KEY`, `JINKO_API_KEY`, `OPENAI_API_KEY`, adresses
  email personnelles, chemins absolus contenant le nom d'utilisateur.
- Vérifier qu'aucun `.env` n'est publié (seul `.env.example`, sans valeur).
- Vérifier `results/` : `verdicts.jsonl` (contient le contenu complet de chaque
  scénario), `audio/`, `run_*.json`, `jinko_call_count.json`.
- Vérifier `.pipelex/traces/` et `.claude/` (traces d'exécution, fichiers de
  verrou) : à exclure.
- Vérifier les logs de benchmark (`benchmark/results/*/*.log`) : ils peuvent
  contenir des extraits de prompts et de réponses.
- Photos personnelles dans `adversarial_vision/input/` : métadonnées EXIF
  (position GPS) à supprimer ou fichiers à exclure.

### B. Charges d'attaque

- Jeux de test contenant des injections : `scenarios/`,
  `benchmark/travel_eval.jsonl`. À garder privés (ou remplacer par des versions
  édulcorées, comme `scenarios.example/`).
- Prompts affûtés des gardiens (`guardians/contextual.mthds` ; `strict.mthds` a
  été supprimé) : publié (décision du 2026-09-26 : la documentation et les mesures s'appuient sur ce gardien) ; seul `strict` a été supprimé.
- Outil de fabrication d'images piégées et versions abouties :
  `adversarial_vision/generate_attack.py` (non publié), `output/*_adversarial.png`,
  `output/*_demo_adversarial.png`.
- Charges d'exemple dans le code et la documentation : messages du scénario de
  collusion (acrostiche, décalage de César, injection `[SYSTÈME]`), textes
  d'exemple des `partie_*.md`, motifs de `victim_agent/heuristics.py`. Elles
  sont génériques et inertes, mais à relire une par une : aucune ne doit
  contenir d'adresse, de domaine ou de jeton réel.
- Dépôt tiers `raucle-bench-src/` : ne jamais le publier (déjà noté dans
  le suivi interne des fichiers publics et privés (non publié)).

### C. Méthode

1. Créer le `.gitignore` de `sentinel_public/` (généré par l'export) **avant** le premier `git add`.
2. `git status` avant chaque commit ; lire la liste fichier par fichier.
3. Rechercher les secrets avec une recherche de texte sur l'arbre des fichiers
   suivis, pas seulement sur la liste attendue.
4. Avant le push : `git log --all --full-history -- <chemin sensible>` doit être
   vide pour chaque chemin privé (scénarios, jeux de test d'attaque, scripts de fabrication d'attaques, `.env`, `results/`).
5. Cloner le dépôt dans un dossier séparé et relire ce qu'un lecteur extérieur
   verra réellement.
6. Une fois publié, un secret qui a fuité est considéré comme compromis :
   révoquer la clé, ne pas seulement supprimer le fichier.

### D. Autres points de publication

- Décider du moment du `git init` : le hackathon commence le 2026-09-25 et aucun
  commit n'était prévu avant.
- Mettre à jour `README.md` et les notes de développement (non publiées), qui ne décrivent ni les piliers 3 et
  4 étendus ni le correctif de certificats (le geste `unset SSL_CERT_FILE` n'est
  plus nécessaire).
- Relire les images piégées à conserver (avec ou sans tag) et supprimer le
  reste.
