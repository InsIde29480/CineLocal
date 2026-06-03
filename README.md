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

### 2. Lancement

```bash
python server.py
```
L'interface est dispo sur → **http://<adresse-ip>:<adresse-port>**
---

## Chromecast / Cast sur la TV

### Condition obligatoire
**Utiliser Google Chrome** (le Cast SDK ne fonctionne que dans Chrome).

Deux configurations sont à réaliser sur chrome pour avoir accès au chromecast : `chrome://flags/`

- **Experimental Web Platform features** : `ON`.
- **Insecure origin treated as secure** : `http://<server-ip>:<server-port>`.

Format supportés par le chromecast (freebox revolution) :

- **Formats supportés** : `.mp4` `.mkv`.
- **Encodage vidéo** : `h.264`.
- **Encodage audio** : `aac 2 canaux`.
- **Encodage des sous-titres** : `fichier .str externe ou encodé directement`.

**Vérifiez bien la qualité maximale que votre chromecast peut supporter.**

Le fichier de conversion (convert.ps1) est un fichier powershell permettant d'utiliser ffmpeg pour convertir les films et séries du dossier . dans le format chromecast officiel.

Commande pour autoriser l'execution de programme powershell dans une session :

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```
puis lancer le programme pour convertir :

**Powershell**
```powershell
.\convert.ps1
```
**ATTENTION** : Le programme peut utiliser autant le `CPU` ou `NVIDIA` ou `AMD`, cette option est à configurer dans le programme en changant la variable `$Encoder` ligne 24.

Le programme check_format.ps1 (windows powershell) ou check_format.sh (linux) permet de vérifier le bon format vidéo de chromecast.

**Powershell**
```powershell
.\check_format.ps1
```
**Linux**
```bash
chmod +X check_foramt.sh
./check_format.sh
```
---

## Configuration avancée

Modifier les variables en tête de `server.py` :

| Variable      | Défaut          | Description                   |
|---------------|-----------------|-------------------------------|
| `MOVIES_DIR`  | `~/nvme_data`   | Dossier source des films      |
| `PORT`        | `8765`          | Port du serveur               |
| `HOST`        | `0.0.0.0`       | `0.0.0.0` = accessible réseau |

---

## Lancement automatique au démarrage (Linux)

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
