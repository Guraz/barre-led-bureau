Tutoriel d'Installation Pas-à-Pas (Second Raspberry Pi)Pour installer et configurer proprement votre second système de contrôle LED sans passer par des scripts de mise à jour automatique en arrière-plan, suivez ces étapes directement depuis le terminal du Raspberry Pi.1. Préparation système et désactivation de l'audio (DMA LED)Ouvrez le Terminal de votre Raspberry Pi et exécutez ces commandes pour configurer les bus matériels (GPIO18 pour le DMA des WS2815 et SPI pour les DotStar) :   Bash# A. Supprime le blocage PEP 668 de Python sous Debian 13 Trixie
sudo rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED

# B. Désactive l'audio interne (libère le PWM0 pour les LED) et active le bus SPI
sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' /boot/firmware/config.txt
grep -q "dtparam=spi=on" /boot/firmware/config.txt || echo "dtparam=spi=on" | sudo tee -a /boot/firmware/config.txt

# C. Active le bouton d'arrêt physique sur GPIO3 (Pin 5)
grep -q "gpio-shutdown" /boot/firmware/config.txt || echo "dtoverlay=gpio-shutdown,gpio_pin=3,active_low=1,gpio_pull=up" | sudo tee -a /boot/firmware/config.txt

# D. Installe les dépendances graphiques PyQt6, les polices d'icônes et l'utilitaire d'écran wlopm
sudo apt update && sudo apt install -y git python3-pyqt6 python3-gpiozero python3-lgpio python3-rpi-lgpio python3-spidev python3-pip wlopm fonts-noto-color-emoji
2. Installation des librairies matérielles LEDInstallez les modules CircuitPython et NeoPixel pour le pilotage DMA et SPI :Bashsudo pip3 install --break-system-packages rpi_ws281x adafruit-circuitpython-neopixel adafruit-circuitpython-dotstar adafruit-blinka "Adafruit-Blinka-Raspberry-Pi-PWM>=1.0.2" sysv-ipc
pip3 install --upgrade "Adafruit-Blinka-Raspberry-Pi-PWM>=1.0.2"
3. Clonage du projet Git et création du script de mise à jour manuelle (update.sh)Placez-vous sur le Bureau et clonez votre dépôt :   Bashcd /home/user/Desktop
git clone https://github.com/Guraz/barre-led-bureau.git LED
cd LED
Créez un script de mise à jour manuelle nommé update.sh dans ce dossier :Bashcat << 'EOF' > /home/user/Desktop/LED/update.sh
#!/bin/bash
echo "🔄 Récupération des dernières modifications depuis Git..."
cd /home/user/Desktop/LED
git pull origin main
echo "✅ Mise à jour terminée avec succès !"
EOF
chmod +x /home/user/Desktop/LED/update.sh
Utilisation : Il vous suffira de double-cliquer sur update.sh (ou de le lancer dans le terminal) pour mettre à jour votre code localement à la demande.4. Création du lanceur sur le BureauCréez l'icône de lancement direct sur le Bureau pour l'interface graphique :Bashcat << 'EOF' > /home/user/Desktop/ControleurLED.desktop
[Desktop Entry]
Name=Controleur LED
Comment=Lancement de l'interface LED
Exec=sudo -E /usr/bin/python3 /home/user/Desktop/LED/led_system.py
Path=/home/user/Desktop/LED
Icon=preferences-desktop-display
Terminal=false
Type=Application
EOF
chmod +x /home/user/Desktop/ControleurLED.desktop
gio set /home/user/Desktop/ControleurLED.desktop metadata::trusted true 2>/dev/null || 