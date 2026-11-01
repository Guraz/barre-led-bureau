# 💡 Système d'Éclairage Intelligent & Centre de Contrôle Domotique ('Barre LED')

Ce dépôt contient le code source de l'interface PyQt6 pour le pilotage de rubans LED adressables (WS2815 12V et Adafruit DotStar 5V SPI) sur **Raspberry Pi 4B**, couplé à l'API locale de la Homey Pro.

---

## 🚀 Installation initiale sur le Raspberry Pi (De zéro)

Ouvrez le **Terminal** de votre Raspberry Pi et exécutez le bloc de commandes suivant pour cloner le projet, configurer les droits administrateur et installer toutes les dépendances matérielles en une seule fois :

```bash
# 1. Configurer sudo sans mot de passe pour l'utilisateur courant (évite les blocages graphiques)
echo "$USER ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010_user-nopasswd
sudo chmod 440 /etc/sudoers.d/010_user-nopasswd

# 2. Lever le verrou PEP 668 de Debian Trixie et installer les paquets système
sudo rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED
sudo apt update && sudo apt install -y git python3-pyqt6 python3-gpiozero python3-lgpio python3-rpi-lgpio python3-spidev python3-pip wlopm fonts-noto-color-emoji

# 3. Cloner le dépôt sur le Bureau du Raspberry Pi
cd /home/user/Desktop
git clone [https://github.com/Guraz/barre-led-bureau.git](https://github.com/Guraz/barre-led-bureau.git) LED
cd LED

# 4. Installer les librairies Adafruit pour le DMA et les rubans LED
sudo pip3 install --break-system-packages rpi_ws281x adafruit-circuitpython-neopixel adafruit-circuitpython-dotstar adafruit-blinka "Adafruit-Blinka-Raspberry-Pi-PWM>=1.0.2" sysv-ipc
pip3 install --upgrade "Adafruit-Blinka-Raspberry-Pi-PWM>=1.0.2"