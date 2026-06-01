# CineLocal — Interface de films locale

Interface personnelle pour films / séries local, avec support Chromecast.

---

## Installation rapide

### 1. Prérequis

```bash
# Python 3.10+
python3 --version

# ffmpeg
sudo apt install ffmpeg       # Ubuntu/Debian
```

### 3. Lancement

```bash
python server.py
```
L'interface est dispo sur → **http://localhost:8765**
---

## Structure attendue des films

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
---

## Chromecast / Cast sur la TV

### Condition obligatoire
**Utiliser Google Chrome** (le Cast SDK ne fonctionne que dans Chrome).
**Formats supportés** : `.mp4` `.mkv`
**Encodage vidéo** : `h.264`
**Encodage audio** : `aac 2 canaux`
**Encodage des sous-titres** : `fichier .str externe ou encodé directement`
**Vérifiez bien la qualité maximale que votre chromecast peut supporter.**

Le fichier de conversion (convert.ps1) est un fichier powershell permettant d'utiliser ffmpeg pour convertir les films et séries du dossier . dans le format chromecast officiel.

Commande pour autoriser l'execution de programme powershell dans une session :
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

---

## 🔧 Configuration avancée

Modifier les variables en tête de `server.py` :

| Variable      | Défaut          | Description                   |
|---------------|-----------------|-------------------------------|
| `MOVIES_DIR`  | `~/nvme_data`   | Dossier source des films      |
| `PORT`        | `8765`          | Port du serveur               |
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
