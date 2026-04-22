# 🎬 CineLocal — Interface de films locale style Netflix

Interface Netflix personnelle pour tes films stockés sur SSD, avec support Chromecast.

---

## ⚡ Installation rapide

### 1. Prérequis

```bash
# Python 3.10+
python3 --version

# ffmpeg (pour les miniatures)
sudo apt install ffmpeg       # Ubuntu/Debian
sudo pacman -S ffmpeg         # Arch
brew install ffmpeg           # macOS
```

### 2. Installation des dépendances Python

```bash
cd ~/movie-browser
pip install -r requirements.txt
# ou avec pip3 :
pip3 install -r requirements.txt
```

### 3. Lancement

```bash
python server.py
```

L'interface est dispo sur → **http://localhost:8765**

---

## 📁 Structure attendue des films

Le serveur scanne **~/nvme_data** récursivement.
Les sous-dossiers deviennent des **catégories** dans l'interface :

```
~/nvme_data/
├── Action/
│   ├── Mad.Max.Fury.Road.2015.1080p.mkv
│   └── John.Wick.2014.mkv
├── Sci-Fi/
│   └── Dune.2021.2160p.mkv
└── Inception.2010.BluRay.mp4
```

**Formats supportés** : `.mp4` `.mkv` `.avi` `.mov` `.m4v` `.wmv` `.flv` `.webm` `.ts` `.m2ts`

---

## 📺 Chromecast / Cast sur la TV

### Condition obligatoire
**Utiliser Google Chrome** (le Cast SDK ne fonctionne que dans Chrome).

### Étapes
1. Ouvre Chrome → **http://localhost:8765**
2. Clique sur l'icône Cast 📡 dans la nav ou sur un film
3. Chrome affiche le dialogue de Cast → sélectionne ta TV
4. Le film se lance directement sur la TV !

### Accès depuis d'autres appareils sur le réseau
Pour accéder depuis une autre machine (ex: laptop dans le salon) :

```bash
# Trouve ton IP locale
ip route get 1 | awk '{print $7}'   # Linux
ipconfig getifaddr en0               # macOS
```

Puis ouvre Chrome sur l'autre machine : **http://192.168.x.x:8765**

> ⚠️ Le Cast nécessite que le serveur soit accessible par la TV.
> Utilise l'IP locale (pas localhost) quand tu castes.

---

## 🔧 Configuration avancée

Modifier les variables en tête de `server.py` :

| Variable       | Défaut          | Description                    |
|---------------|-----------------|-------------------------------|
| `MOVIES_DIR`  | `~/nvme_data`   | Dossier source des films       |
| `PORT`        | `8765`          | Port du serveur                |
| `HOST`        | `0.0.0.0`       | `0.0.0.0` = accessible réseau |

---

## 🚀 Lancement automatique au démarrage (Linux)

```bash
# Créer un service systemd
cat > ~/.config/systemd/user/cinelocal.service << EOF
[Unit]
Description=CineLocal Movie Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/$USER/movie-browser/server.py
WorkingDirectory=/home/$USER/movie-browser
Restart=on-failure

[Install]
WantedBy=default.target
EOF

systemctl --user enable cinelocal
systemctl --user start cinelocal
```

---

## 🎨 Fonctionnalités

- ✅ Scan automatique de tous les formats vidéo
- ✅ Miniatures générées automatiquement via ffmpeg (capture au 1/4 du film)
- ✅ Catégories basées sur les dossiers
- ✅ Recherche en temps réel
- ✅ Lecteur vidéo intégré avec scrubbing
- ✅ Streaming avec support Range requests
- ✅ Bouton Cast (Chromecast) dans la nav et sur chaque film
- ✅ Hero dynamique au survol d'un film
- ✅ Actualisation à chaud sans redémarrer
