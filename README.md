# CineLocal — Interface de films locale

Serveur personnel de films et séries en local, façon Netflix, avec posters
automatiques, détection des séries, sélection des pistes audio / sous-titres,
et trois modes de lecture : navigateur, Chromecast, et sortie HDMI directe.

---

## Fonctionnalités

- **Interface web** type Netflix (catalogue, recherche, fiche série/épisodes)
- **Affiches et synopsis** récupérés automatiquement via TMDB
- **Détection automatique** des films, séries, saisons et épisodes
- **Trois modes de lecture** au choix :
  -  **PC** — lecture dans le navigateur (lecteur HTML5)
  -  **Chromecast** — diffusion sur la TV via un Chromecast
- **Sélection de la piste audio** et des **sous-titres** avant lecture
- **Extraction automatique** des sous-titres internes (SRT/ASS) en VTT, et
  détection des sous-titres externes (`.srt` / `.vtt`) à côté du fichier
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
├── server.py            # Serveur Flask (API + streaming + transcodage)
├── static/
│   ├── index.html       # Structure de la page
│   ├── style.css        # Styles
│   └── app.js           # Logique front-end
├── convert.ps1          # Conversion des films (Windows / PowerShell)
├── check_format.ps1     # Vérification du format (Windows)
└── check_format.sh      # Vérification du format (Linux)
```

Les fichiers `style.css` et `app.js` sont servis automatiquement depuis `static/`.

---

## Convention de nommage des fichiers

Les sous-titres externes sont détectés s'ils portent le **même nom** que la vidéo :
`Film.srt`, `Film.fr.srt`, `Film.en.srt`, etc.

---

## Configuration

Variables en tête de `server.py` :

| Variable        | Défaut          | Description                          |
|-----------------|-----------------|--------------------------------------|
| `MOVIES_DIR`    | `~/nvme_data`   | Dossier source des films             |
| `PORT`          | `8765`          | Port du serveur                      |
| `HOST`          | `0.0.0.0`       | `0.0.0.0` = accessible sur le réseau |
| `TMDB_API_KEY`  | *(à remplir)*   | Clé API TMDB (posters / synopsis)    |
| `SUBS_LANG_OK`  | `fr`, `en`, …   | Langues de sous-titres à extraire    |

> Le filtre `SUBS_LANG_OK` évite d'extraire des dizaines de pistes de sous-titres
> inutiles : seules les langues listées (français / anglais par défaut) sont traitées.

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

`convert.ps1` (Windows / PowerShell) utilise ffmpeg pour préparer les films au
format compatible Chromecast : **H.264 + AAC-LC stéréo**, downscale des **4K en
1080p**, et **tonemapping HDR → SDR**.

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
- La sortie est toujours un **nouveau fichier** `<nom>_conv.mkv`.
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
.\check_format.ps1      # Windows
```

```bash
chmod +x check_format.sh
./check_format.sh        # Linux
```

---

## Compatibilité matérielle (important)

Le décodage dépend de **qui** lit le fichier :

- **Mode PC** : c'est l'ordinateur client qui décode → tout format passe.
- **Mode Chromecast** : le Chromecast décode → H.264 1080p uniquement (le serveur
  transcode le reste, ce qui est lourd pour une petite machine).

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
Vérifiez que le `.srt` correspond au **framerate** du film (souvent 23.976 fps pour
les Blu-ray). Outil recommandé : Subtitle Edit (resync, changement de framerate, OCR).

**Image saccadée / muette sur le Chromecast**
Le fichier est probablement HEVC, 4K, 10-bit ou en HE-AAC/5.1 → convertissez-le en
H.264 1080p AAC stéréo avec `convert.ps1`.

**Re-extraire les sous-titres après changement de filtre de langue**
Videz le cache : `rm -rf .tracks_cache/` puis redémarrez le serveur.

---

## Licence

Projet personnel. Utilisez et adaptez librement.
