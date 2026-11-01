# 💡 Système d'Éclairage Intelligent & Centre de Contrôle Domotique ('Barre LED')

Ce dépôt contient le code source de l'interface PyQt6 pour le pilotage de rubans LED adressables (WS2815 12V et Adafruit DotStar 5V SPI) sur Raspberry Pi 4B, couplé à l'API locale de la Homey Pro.

---

## 🛠️ 1. Installation initiale sur le Raspberry Pi (De zéro)

Ouvrez le **Terminal** de votre Raspberry Pi et exécutez les commandes suivantes pour cloner le projet et installer les dépendances matérielles :

```bash
# 1. Installer Git et les outils de base
sudo apt update && sudo apt install -y git python3-pyqt6 python3-gpiozero python3-lgpio python3-rpi-lgpio python3-spidev python3-pip wlopm fonts-noto-color-emoji

# 2. Cloner le dépôt sur le Bureau du Raspberry Pi
cd /home/user/Desktop
git clone [https://github.com/VOTRE_PSEUDO/Barre-LED.git](https://github.com/VOTRE_PSEUDO/Barre-LED.git) LED
cd LED

# 3. Installer les librairies Adafruit pour le DMA et les rubans LED
sudo pip3 install --break-system-packages rpi_ws281x adafruit-circuitpython-neopixel adafruit-circuitpython-dotstar adafruit-blinka "Adafruit-Blinka-Raspberry-Pi-PWM>=1.0.2" sysv-ipc
pip3 install --upgrade "Adafruit-Blinka-Raspberry-Pi-PWM>=1.0.2"
