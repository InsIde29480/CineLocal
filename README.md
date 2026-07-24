# CineLocal — Interface de films locale

Serveur personnel de films et séries en local, façon Netflix, avec posters
automatiques, détection des séries, sélection des pistes audio / sous-titres,
et Deux modes de lecture : navigateur, Chromecast.

---

## Fonctionnalités

- **Interface web** type Netflix (catalogue, recherche, fiche série/épisodes)
- **Affiches et synopsis** récupérés automatiquement via TMDB
- **Détection automatique** des films, séries, saisons et épisodes
- **Deux modes de lecture** au choix :
  -  **PC** — lecture dans le navigateur (lecteur HTML5)
  -  **Chromecast** — diffusion sur la TV via un Chromecast
- **Sélection de la piste audio** et des **sous-titres** avant lecture
- **Choix de la version** (4K / 1080p / HD) à la lecture — pour les **films**
  comme pour les **épisodes de séries** (fichiers regroupés par qualité)
- **Extraction automatique** des sous-titres internes (SRT/ASS) en VTT, et
  détection des sous-titres externes (`.srt` / `.vtt`) à côté du fichier
- **Extraction en masse** de tous les sous-titres avec **fenêtre de progression**
  (fichier en cours, total, échecs détaillés, films sans sous-titre)
- **Téléchargement automatique** des sous-titres **français** manquants via
  **OpenSubtitles** (appariement par empreinte du fichier ou identifiant TMDB)
- **Sous-titres partagés** entre les versions d'un même film/épisode (HD ↔ 4K)
- **Resynchronisation des sous-titres** décalés directement depuis l'interface
  (décalage ± en secondes, films et épisodes)
- **Analyse automatique périodique** : détecte les nouveaux films, extrait les
  sous-titres et télécharge le français manquant, à intervalle réglable
- **Onglet Paramètres** : toute la configuration (clés API, dossiers, langues,
  analyse auto) se règle **depuis le site**, sans éditer les fichiers
- **Plusieurs dossiers de films** (multi-disques) scannés ensemble
- **Durée des films**, **badge 4K** et **tri alphabétique** du catalogue
- **Mise en cache** des posters et des pistes pour un affichage rapide

---

## Modes de lecture en détail

| Mode | Qui décode la vidéo | Idéal pour |
|------|---------------------|------------|
|  **PC** (navigateur) | L'ordinateur client | Tout format, le PC fait le travail |
|  **Chromecast** | Le Chromecast (le serveur transcode si besoin) | H.264 1080p |

Le mode se choisit dans la barre de navigation et est mémorisé.

---

## Prérequis

```bash
# Python 3.10+
python3 --version

# ffmpeg + ffprobe (extraction pistes, transcodage Cast)
sudo apt install ffmpeg            # Debian / Ubuntu / Raspberry Pi OS

# MPV (uniquement pour le mode "TV directe" sur sortie HDMI)
sudo apt install mpv
```

Dépendances Python :

```bash
pip install flask waitress requests
```

---

## Installation

### 1. Récupérer le projet

```bash
git clone https://github.com/InsIde29480/cinelocal.git
cd cinelocal
```

### 2. Configurer

Édite les variables en tête de `server.py` (voir [Configuration](#configuration)).
En particulier, renseigne **ta propre clé TMDB** (gratuite sur
<https://www.themoviedb.org/settings/api>) dans la variable `TMDB_API_KEY`.

### 3. Lancer

```bash
python server.py
```

Interface disponible sur → **http://localhost:8765**
Depuis un autre appareil du réseau → **http://&lt;ip-du-serveur&gt;:8765**

---

## Structure du projet

```
cinelocal/
├── server.py                # Serveur Flask (API + streaming + transcodage + sous-titres)
├── static/
│   ├── index.html           # Structure de la page
│   ├── style.css            # Styles
│   └── app.js               # Logique front-end
├── convert_gui.ps1          # IHM de conversion (Windows) — choix GPU/CPU, qualité, résolution
├── convert.ps1              # Conversion en ligne de commande + downscale 1080p (Windows)
├── convert_only_format.ps1  # Conversion codec seul, sans downscale (Windows)
├── check.ps1                # Vérification du format (Windows)
└── check.sh                 # Vérification du format (Linux)
```

Les fichiers `style.css` et `app.js` sont servis automatiquement depuis `static/`.

> Fichiers générés au premier lancement (à côté du dossier de films ou dans le
> répertoire configuré) : `.tracks_cache/` (sous-titres VTT + métadonnées),
> `.tmdb_cache.json` (posters/synopsis), `.cinelocal_settings.json` (paramètres
> réglés depuis l'interface).

---

## Convention de nommage des fichiers

Les sous-titres externes sont détectés s'ils portent le **même nom** que la vidéo :
`Film.srt`, `Film.fr.srt`, `Film.en.srt`, etc.

---

## Configuration

La plupart des réglages se font **directement depuis l'onglet ⚙ Paramètres du
site** (voir [Paramètres](#paramètres-interface-web)) et sont enregistrés dans
`.cinelocal_settings.json` — plus besoin d'éditer `server.py`. Les valeurs en
tête de `server.py` servent alors de **valeurs par défaut**.

| Réglage (Paramètres / `server.py`) | Défaut          | Description                          |
|------------------------------------|-----------------|--------------------------------------|
| Dossiers des films                 | *(à définir)*   | Un ou **plusieurs** dossiers, même structure `Films/…` |
| Dossier des sous-titres (cache)    | `.tracks_cache` | Où sont stockés les VTT + métadonnées |
| Clé API TMDB                       | *(à remplir)*   | Posters / synopsis                   |
| OpenSubtitles (clé, identifiant, mot de passe, langues) | *(vide)* | Téléchargement des sous-titres manquants |
| Analyse automatique (activée + intervalle) | `oui`, `60 min` | Détection/extraction/téléchargement périodiques |
| `PORT`                             | `8765`          | Port du serveur (`server.py`)        |
| `HOST`                             | `0.0.0.0`       | `0.0.0.0` = accessible sur le réseau |
| `SUBS_ACCEPT_ALL_LANGS`            | `True`          | Extrait toutes les langues de sous-titres (`server.py`) |

> Par défaut, **toutes** les langues de sous-titres texte sont extraites (on
> choisit ensuite dans l'interface). Mettre `SUBS_ACCEPT_ALL_LANGS = False` dans
> `server.py` pour se limiter à la liste `SUBS_LANG_OK` (fr / en).
>
> Les identifiants sont stockés localement dans `.cinelocal_settings.json` ; le
> mot de passe OpenSubtitles n'est jamais renvoyé au navigateur.

---

## Paramètres (interface web)

Le bouton **⚙ Paramètres** (barre de navigation) ouvre une fenêtre pour tout
configurer sans toucher aux fichiers :

- **📁 Chemins** — un ou plusieurs **dossiers de films** (un par ligne, plusieurs
  disques possibles) et le **dossier de cache** des sous-titres. Un changement
  relance automatiquement l'analyse de la bibliothèque.
- **🎬 TMDB** — clé API pour les affiches et synopsis.
- **💬 OpenSubtitles** — clé API, identifiant, mot de passe et ordre des langues
  (`fr, en`) pour le téléchargement des sous-titres.
- **🔁 Analyse automatique** — activer/désactiver et régler l'intervalle (min. 5 min).
- **🛠️ Outils** — accès à la **resynchronisation des sous-titres** (voir ci-dessous).

Tout est enregistré côté serveur et rechargé au démarrage.

---

## Sous-titres (extraction, téléchargement, resynchronisation)

Le bouton **💬 Sous-titres** ouvre la fenêtre de gestion :

- **Extraction en masse** : parcourt tous les films/épisodes et extrait leurs
  sous-titres en VTT (toutes les pistes d'un fichier sont extraites **en une
  seule passe**, indispensable pour les gros remux 4K). Une **barre de
  progression** montre le fichier en cours, le total, et deux listes
  dépliables : **films sans sous-titre** (avec la raison : image/PGS, aucune
  piste…) et **échecs** (avec le détail).
- **Reprise ciblée** : *Réessayer les échecs & sans S-T* (ne retraite que ce qui
  manque), ou *Tout ré-extraire*.
- **Téléchargement du français manquant** : *Télécharger le français manquant*
  interroge OpenSubtitles pour tout film/épisode sans piste FR (même s'il a déjà
  de l'anglais), enregistre un `.srt` à côté de la vidéo et le convertit.
- **Analyse automatique** (réglable dans les Paramètres) : refait ce cycle
  périodiquement pour les nouveaux fichiers.

> Le cache utilise un **identifiant MD5 stable** par fichier : il **survit à un
> redémarrage** du service (`systemctl restart`) — seuls les fichiers nouveaux ou
> modifiés sont ré-extraits. L'extraction n'est jamais interrompue par un plafond
> de temps total, seulement en cas d'**inactivité prolongée** (fichier bloqué).

### Resynchroniser un sous-titre décalé

**⚙ Paramètres → 🛠️ Outils → Resynchroniser des sous-titres décalés** : recherche
un film/épisode, choisis la langue, puis **avance ou retarde** les timecodes de X
secondes (décimales acceptées, ex. `1.05`). Le décalage s'applique au sous-titre
servi (et au `.srt` source pour les externes). Ré-applique pour affiner.

---

## Chromecast / Cast sur la TV

### Condition obligatoire

**Utiliser Google Chrome** (le Cast SDK ne fonctionne que dans Chrome).

Deux réglages à faire dans Chrome via `chrome://flags/` :

- **Experimental Web Platform features** : `ON`
- **Insecure origins treated as secure** : ajouter `http://<ip-serveur>:<port>`

### Formats supportés par le Chromecast (ex. Freebox Revolution)

- **Conteneurs** : `.mp4`, `.mkv`
- **Vidéo** : `H.264`
- **Audio** : `AAC 2 canaux` (stéréo)
- **Sous-titres** : fichier `.srt` externe (même nom que la vidéo) ou piste interne

> Le Chromecast classique ne lit **pas** le HEVC, le 4K ni le 10-bit, et gère mal
> le HE-AAC et le 5.1. Vérifiez la résolution maximale supportée par votre modèle.
> Pour ces fichiers, utilisez le script de conversion ci-dessous.

### Choix audio / sous-titres

Au moment de caster, un menu permet de choisir la **piste audio** et les
**sous-titres**. La piste audio est appliquée côté serveur ; les sous-titres sont
envoyés nativement au Chromecast.

---

## Conversion des films

Trois outils Windows (ffmpeg requis dans le PATH) pour préparer les films au
format compatible Chromecast : **H.264 + AAC-LC stéréo**, downscale **4K → 1080p**.

### Interface graphique — `convert_gui.ps1` (recommandé)

Une **fenêtre** (aucune installation, WinForms intégré à Windows) pour tout piloter
sans éditer de script :

- choix du **dossier**, scan et liste des fichiers (codec, résolution, statut) ;
- **encodeur** : NVIDIA / Intel / AMD (GPU) ou CPU (libx264) ;
- **qualité** (CRF/CQ) et **résolution max** (1080p / 720p / aucune limite) ;
- **audio** : AAC stéréo (Chromecast) ou copie si déjà compatible ;
- sélection multiple, **barre de progression + ETA**, bouton **Annuler**.

```powershell
powershell -ExecutionPolicy Bypass -File .\convert_gui.ps1
```

> ⚠️ « Aucune limite » sur une source 4K produit un **H.264 4K** très lourd et
> souvent injouable sur le Raspberry Pi. Pour le Pi/Chromecast, garde **1080p**.

### En ligne de commande — `convert.ps1` / `convert_only_format.ps1`

`convert.ps1` prépare au format Chromecast et **downscale les 4K en 1080p**.
`convert_only_format.ps1` ne change **que le codec** (sans downscale), utile pour
garder la résolution d'origine.

Autoriser l'exécution de scripts pour la session :

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Lancer la conversion :

```powershell
.\convert.ps1
```

Le script scanne le dossier courant, liste les fichiers à convertir (mauvais codec,
audio non compatible, ou résolution > 1080p), et permet d'en sélectionner.

### Sécurité

- Le script **ne supprime jamais** vos fichiers vidéo.
- La sortie est toujours un **nouveau fichier** `<nom>_out.mkv`.
- Une conversion n'est validée que si ffmpeg réussit **et** que le fichier produit
  a une taille cohérente ; sinon le résultat partiel est jeté et l'original reste intact.
- Les anciens fichiers se suppriment **manuellement**, après vérification.

### Réglages (en tête du script)

| Variable      | Rôle |
|---------------|------|
| `$Encoder`    | `cpu`, `nvidia`, `intel` ou `amd` (accélération matérielle) |
| `$Crf`        | Qualité (≈18 = très haute, 20 = équilibré, 23 = compressé) |
| `$MaxWidth` / `$MaxHeight` | Seuils de downscale (défaut 1920×1080) |
| `$TonemapHDR` | Conversion HDR → SDR (nécessite un ffmpeg avec zimg/zscale) |

> Pour le tonemapping HDR, utilisez un build **« full »** de ffmpeg
> (<https://www.gyan.dev/ffmpeg/builds/>). Si la conversion d'un film HDR échoue
> avec une erreur `zscale`, passez `$TonemapHDR` à `$false`.

### Vérification du format

```powershell
.\check.ps1      # Windows
```

```bash
chmod +x check.sh
./check.sh        # Linux
```

---

## Compatibilité matérielle (important)

Le décodage dépend de **qui** lit le fichier :

- **Mode PC** : c'est l'ordinateur client qui décode → tout format passe.
- **Mode Chromecast** : le Chromecast décode → H.264 1080p uniquement (le serveur
  transcode le reste, ce qui est lourd pour une petite machine).
- **Mode TV directe** : la machine serveur décode →
  - **HEVC 4K** : OK sur une machine au décodeur matériel HEVC (ex. Raspberry Pi 5)
  - **H.264 4K** : souvent injouable sur Raspberry Pi (pas de décodeur H.264 matériel,
    le CPU ne suit pas) → convertir en 1080p avec `convert.ps1`

**En résumé** : gardez vos **HEVC** tels quels pour la TV directe, et convertissez les
**H.264 4K** (ou tout ce qui doit passer par le Chromecast) en 1080p H.264.

---

## Lancement automatique au démarrage (Linux / systemd)

Créer `/etc/systemd/system/cinelocal.service` :

```ini
[Unit]
Description=CineLocal - Serveur de films local
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=nas
Group=nas
WorkingDirectory=/home/nas/serveur_films
ExecStart=/usr/bin/python3 /home/nas/serveur_films/server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cinelocal

# Nécessaire pour le mode "TV directe" (MPV sur la sortie HDMI)
Environment="DISPLAY=:0"
Environment="XAUTHORITY=/home/nas/.Xauthority"

# Sécurité
NoNewPrivileges=true
PrivateTmp=true
MemoryMax=80%

[Install]
WantedBy=multi-user.target
```

Activer et gérer le service :

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cinelocal     # démarre + active au boot
sudo systemctl restart cinelocal          # redémarrer
sudo journalctl -u cinelocal -f           # suivre les logs
```

> Les lignes `DISPLAY` / `XAUTHORITY` sont indispensables pour que MPV puisse
> afficher la vidéo sur l'écran branché en HDMI. Sans elles, le mode TV directe
> ne fonctionne pas.

---

## Dépannage

**Erreur 504 / timeout sur le réseau local avec un VPN actif**
Un VPN peut router le trafic local vers l'extérieur. Activez le *split tunneling*
et excluez les plages locales : `192.168.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`.
(Ou coupez simplement le VPN pour un accès local.)

**Le Cast ne démarre pas**
Vérifiez les deux *flags* Chrome (ci-dessus), que vous êtes bien sous Chrome, et
que l'ordinateur et le Chromecast sont sur le **même réseau WiFi**.

**Modifications de l'interface non prises en compte**
Forcez le rechargement sans cache : `Ctrl + Shift + R`.

**Sous-titres décalés**
Utilisez l'outil intégré : **⚙ Paramètres → 🛠️ Outils → Resynchroniser des
sous-titres décalés** (avance/retarde de X secondes). Pour un décalage qui
s'aggrave au fil du film, c'est un problème de **framerate** (souvent 23.976 fps
pour les Blu-ray) : Subtitle Edit permet le changement de framerate et l'OCR.

**Image saccadée / muette sur le Chromecast**
Le fichier est probablement HEVC, 4K, 10-bit ou en HE-AAC/5.1 → convertissez-le en
H.264 1080p AAC stéréo avec `convert.ps1`.

**Re-extraire les sous-titres (changement de langue, encodage, etc.)**
Depuis l'interface : **💬 Sous-titres → Tout ré-extraire** (ou *Réessayer les
échecs & sans S-T*). Sinon, videz le cache `rm -rf .tracks_cache/` puis
redémarrez le serveur.

**Mojibake dans les sous-titres (accents `Ã©`, `�`)**
Les `.srt` non-UTF-8 (Windows-1252 / Latin-1) sont désormais détectés et
convertis automatiquement. Relancez **💬 Sous-titres → Tout ré-extraire** pour
régénérer les anciens sous-titres avec le bon encodage.

---

## Licence

Projet personnel. Utilisez et adaptez librement.
