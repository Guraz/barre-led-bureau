import sys
import os

# ============================================================================
# 0. AUTO-ÉLÉVATION SUDO & RÉPARATION DE L'ENVIRONNEMENT GRAPHIQUE (Wayland/X11)
# ============================================================================
# Permet de lancer le script depuis l'icône Bureau, Thonny ou Geany tout en
# garantissant les droits root requis par le DMA NeoPixel (/dev/mem sur GPIO18).
if os.geteuid() != 0 and sys.platform.startswith("linux"):
    try:
        os.execvp("sudo", ["sudo", "-E", sys.executable] + sys.argv)
    except Exception:
        pass

if os.geteuid() == 0:
    uid = os.environ.get("SUDO_UID", "1000")
    os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    if os.path.exists(f"/run/user/{uid}/wayland-0"):
        os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("XAUTHORITY", "/home/user/.Xauthority")

import json
import math
import time
import socket
import signal
import subprocess
import colorsys
import http.client
import threading
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QSizePolicy, QFrame, QStackedWidget)
from PyQt6.QtCore import (Qt, pyqtSignal, QTimer, QRectF, QPointF, QThread, 
                          QTime, QVariantAnimation, QEasingCurve, QEvent)
from PyQt6.QtGui import (QPainter, QColor, QBrush, QPen, QLinearGradient, QPainterPath, QPixmap, QCursor)

# ============================================================================
# 1. CONFIGURATION MATÉRIELLE & CONSTANTES
# ============================================================================
TOTAL_LEDS      = 288        # Ruban WS2815 12V (2m x 144 LED/m) sur GPIO18
DOTSTAR_LEDS    = 144        # Ruban Adafruit DotStar 5V SPI sur GPIO10/GPIO11
ENABLE_DOTSTAR  = False      # Passer à True lorsque le ruban DotStar est raccordé
PIN_RADAR       = 23         # Capteur radar 24GHz HLK-LD2410B
PIN_FAN         = 16         # Pin physique 36 : MOSFET LR7843 (Ventilateur)
PIN_RELAY       = 20         # Pin physique 38 : Relais SEEIT TTL-RELAY (Active-Low)
UDP_SYNC_PORT   = 21324      # Port UDP Broadcast pour synchroniser les 2 bureaux
MEMORY_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "led_config.json")

LED_TIMEOUT_STEPS    = [15, 30, 60, 120, 300, 600]          
RELAY_DELAY_MINS     = [5, 10, 15, 30, 60]                  
SCREEN_TIMEOUT_STEPS = [0, 30, 60, 120, 300]                
PANEL_AUTO_CLOSE_S   = [0, 5, 8, 15, 30]                    
SCREEN_MODES         = ["ICÔNE VEILLE", "ASSOMBRIR", "HDMI OFF"]
TRANSITION_STYLES    = ["FONDU", "BALAYAGE G→D", "BALAYAGE D→G", "RIDEAU CENTRE"]
SYNC_MODES           = ["INDÉPENDANT", "SYNC MAÎTRE", "SYNC ESCLAVE"]

EFFECT_MODES  = ["FIXE", "RESPIRATION", "ONDE", "COMÈTE", "AURORE", "ARC-EN-CIEL", "FEU"]
SPEED_PRESETS = [("LENT", 0.5), ("NORMAL", 1.0), ("RAPIDE", 2.0)]

# ============================================================================
# 2. CONFIGURATION HOMEY PRO (API LOCALE KEEP-ALIVE)
# ============================================================================
HOMEY_IP = "192.168.1.69"
HOMEY_API_KEY = "1726f046-3116-47ca-8424-7c88dacb19ac:a08dd2a3-c89e-4a02-a5a7-020a697ca771:96dbdf27e019d6ad44c35e4aa8c39feb4411bdfe"

ID_DEVICE_AWAIR       = "8b74dcbf-8076-42c4-9fd9-3f0b997442ab"
ID_DEVICE_UNRAID      = "629363f6-6302-40b0-99c9-0e422b4fca33"
ID_DEVICE_NANOLEAF    = "c8cb59ad-ed3b-452a-9ddc-eb216ce2f642"
ID_DEVICE_HUE_BUREAU  = "1221cd54-ed1a-4522-94b6-1ecfc572a953"
ID_DEVICE_CLIM_BUREAU = "30051f6b-0c4f-45c3-9772-709cfa8e5a0d"
ID_DEVICE_SONOFF_APPT = "bd2ea971-f118-400c-88c3-96cab977106f"

HARDWARE_ACTIVE = False
LEDS_DMA_READY  = False
pixels          = None
dotstars        = None
radar_sensor    = None
fan             = None
cpu             = None

def set_relay_hw_pin(turn_on: bool):
    """
    Pilote physiquement le relais Active-Low (GPIO20) via pinctrl :
    - ON (Relais fermé)  : Sortie forcée à 0V (op dl = Drive Low) -> 5V complets sur la bobine.
    - OFF (Relais ouvert): Entrée Haute Impédance sans pull (ip pn) -> Coupe le courant résiduel
      de 1.7V (5V - 3.3V) dans l'optocoupleur pour que le ressort claque franchement à l'ouverture !
    """
    if not HARDWARE_ACTIVE:
        return
    try:
        if turn_on:
            subprocess.run(["pinctrl", "set", str(PIN_RELAY), "op", "dl"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["pinctrl", "set", str(PIN_RELAY), "ip", "pn"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

try:
    from gpiozero import PWMOutputDevice, DigitalInputDevice, CPUTemperature
    radar_sensor = DigitalInputDevice(PIN_RADAR, pull_up=False)
    fan = PWMOutputDevice(PIN_FAN, frequency=25, initial_value=0.0)
    cpu = CPUTemperature()
    HARDWARE_ACTIVE = True
    # S'assure que le relais est ouvert proprement avant l'initialisation complète
    set_relay_hw_pin(False)
    print("✅ GPIO ACTIVÉS (Radar + Fan PWM + Relais Haute-Impédance)")
except Exception as e:
    print(f"⚠️ Mode simulation PC (GPIO non détectés) : {e}")

if os.geteuid() == 0:
    try:
        import board
        import neopixel
        pixels = neopixel.NeoPixel(board.D18, TOTAL_LEDS, brightness=1.0, auto_write=False, pixel_order=neopixel.GRB)
        LEDS_DMA_READY = True
        if ENABLE_DOTSTAR:
            import adafruit_dotstar
            dotstars = adafruit_dotstar.DotStar(board.SCK, board.MOSI, DOTSTAR_LEDS, brightness=1.0, auto_write=False)
        print("✅ RUBAN LED DMA ACTIVÉ (GPIO18)")
    except Exception as e:
        print(f"⚠️ Ruban LED DMA non initialisé : {e}")

# ============================================================================
# 3. THREAD MATÉRIEL LED (Écriture DMA asynchrone sans blocage de l'IHM)
# ============================================================================
class LedHardwareWorker(QThread):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._latest_frame = None
        self._running = True

    def push_frame(self, buf):
        with self._lock:
            self._latest_frame = buf
        self._event.set()

    def run(self):
        global LEDS_DMA_READY
        while self._running:
            self._event.wait(0.1)
            self._event.clear()
            if not self._running:
                break
            frame = None
            with self._lock:
                if self._latest_frame is not None:
                    frame = self._latest_frame
                    self._latest_frame = None
            if frame is not None and LEDS_DMA_READY:
                try:
                    if pixels is not None:
                        pixels[:] = frame
                        pixels.show()
                    if dotstars is not None:
                        dotstars[:] = frame[:DOTSTAR_LEDS]
                        dotstars.show()
                except Exception:
                    LEDS_DMA_READY = False

# ============================================================================
# 4. THREAD SYNCHRONISATION INTER-BUREAUX (UDP Broadcast Port 21324)
# ============================================================================
class DeskSyncWorker(QThread):
    remoteZonesReceived = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("", UDP_SYNC_PORT))
            self.sock.settimeout(0.4)
        except Exception:
            pass

    def broadcast_zones(self, zones_dict_list, sender_id):
        payload = json.dumps({"src": sender_id, "zones": zones_dict_list}).encode("utf-8")
        try:
            self.sock.sendto(payload, ("255.255.255.255", UDP_SYNC_PORT))
        except Exception:
            pass

    def run(self):
        while self._running:
            try:
                data, _ = self.sock.recvfrom(8192)
                msg = json.loads(data.decode("utf-8"))
                if "zones" in msg and "src" in msg:
                    self.remoteZonesReceived.emit([msg["src"], msg["zones"]])
            except Exception:
                pass

# ============================================================================
# 5. THREAD HOMEY API (Connexion HTTP Keep-Alive persistante)
# ============================================================================
class HomeyAPIWorker(QThread):
    data_updated = pyqtSignal(dict)

    def run(self):
        devices_to_check = {
            "awair": ID_DEVICE_AWAIR,
            "unraid": ID_DEVICE_UNRAID,
            "nanoleaf": ID_DEVICE_NANOLEAF,
            "hue_bureau": ID_DEVICE_HUE_BUREAU,
            "clim_bureau": ID_DEVICE_CLIM_BUREAU,
            "sonoff_appt": ID_DEVICE_SONOFF_APPT
        }
        headers = {
            "Authorization": f"Bearer {HOMEY_API_KEY}",
            "Connection": "keep-alive"
        }
        conn = None
        
        while True:
            results = {}
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(HOMEY_IP, timeout=2.5)
                for name, dev_id in devices_to_check.items():
                    if not dev_id:
                        continue
                    conn.request("GET", f"/api/manager/devices/device/{dev_id}", headers=headers)
                    resp = conn.getresponse()
                    raw = resp.read()
                    if resp.status == 200:
                        data = json.loads(raw)
                        results[name] = data.get("capabilitiesObj", {})
            except Exception:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
            
            if results:
                self.data_updated.emit(results)
                
            time.sleep(20)

# ============================================================================
# 6. CALIBRATION OPTIQUE (LUT 256 PALIERS) & MODÈLE DE ZONE ANIMÉ
# ============================================================================
class ColorCalibration:
    gain_r = 1.00
    gain_g = 0.82
    gain_b = 0.58
    gamma  = 2.2
    _lut_hw = []
    _lut_ui = []

    @classmethod
    def rebuild_lut(cls):
        cls._lut_hw = []
        cls._lut_ui = []
        for i in range(256):
            tn = i / 255.0
            kelvin = 1800.0 + tn * (6500.0 - 1800.0)
            temp = kelvin / 100.0
            if temp <= 66.0:
                r = 255.0
                g = 99.4708025861 * math.log(max(1.0, temp)) - 161.1195681661
                b = 0.0 if temp <= 19.0 else 138.5177312231 * math.log(temp - 10.0) - 305.0447927307
            else:
                r = 329.698727446 * math.pow(temp - 60.0, -0.1332047592)
                g = 288.1221695283 * math.pow(temp - 60.0, -0.0755148492)
                b = 255.0

            rn = max(0.0, min(1.0, r / 255.0))
            gn = max(0.0, min(1.0, g / 255.0))
            bn = max(0.0, min(1.0, b / 255.0))

            rh = int(max(0, min(255, ((rn ** cls.gamma) * cls.gain_r) * 255.0)))
            gh = int(max(0, min(255, ((gn ** cls.gamma) * cls.gain_g) * 255.0)))
            bh = int(max(0, min(255, ((bn ** cls.gamma) * cls.gain_b) * 255.0)))
            cls._lut_hw.append((rh, gh, bh))

            ru = int(max(0, min(255, rn * 255.0)))
            gu = int(max(0, min(255, (gn ** 1.35) * 255.0)))
            bu = int(max(0, min(255, (bn ** 1.55) * 255.0)))
            cls._lut_ui.append((ru, gu, bu))

ColorCalibration.rebuild_lut()

def kelvin_to_rgb_calibrated(temp_norm: float, apply_hw_calib: bool = True):
    idx = max(0, min(255, int(round(temp_norm * 255.0))))
    return ColorCalibration._lut_hw[idx] if apply_hw_calib else ColorCalibration._lut_ui[idx]

class Zone:
    _id_counter = 1
    def __init__(self, start=0, end=48, data=None, animate_spawn=False):
        self.id = Zone._id_counter
        Zone._id_counter += 1
        self.name = f"Z{self.id}"
        self.selected = False
        self.is_closing = False
        self.spawn_glow = 1.0 if animate_spawn else 0.0
        
        if data: 
            self.start = float(data.get('start', 0))
            self.end = float(data.get('end', 48))
            self.mode = data.get('mode', 'RGB')
            self.hue = float(data.get('hue', 0.60))
            self.temp = float(data.get('temp', 0.35))
            self.brightness = float(data.get('brightness', 0.6))
            self.effect = data.get('effect', 'FIXE')
            self.speed = float(data.get('speed', 1.0))
        else: 
            self.start = float(start)
            self.end = float(end)
            self.mode = 'RGB'
            self.hue = 0.60
            self.temp = 0.35
            self.brightness = 0.6
            self.effect = 'FIXE'
            self.speed = 1.0

        if animate_spawn:
            mid = (self.start + self.end) * 0.5
            self.vis_start  = mid
            self.vis_end    = mid
            self.vis_bright = 0.0
        else:
            self.vis_start  = self.start
            self.vis_end    = self.end
            self.vis_bright = self.brightness

    def step_visual_lerp(self, factor: float = 0.28) -> bool:
        if self.is_closing:
            mid = (self.start + self.end) * 0.5
            self.vis_start  += (mid - self.vis_start) * 0.32
            self.vis_end    += (mid - self.vis_end) * 0.32
            self.vis_bright += (0.0 - self.vis_bright) * 0.32
            self.spawn_glow  = min(1.0, self.spawn_glow + 0.15)
            return True

        ds = self.start - self.vis_start
        de = self.end - self.vis_end
        db = self.brightness - self.vis_bright
        moving = (abs(ds) > 0.10 or abs(de) > 0.10 or abs(db) > 0.004 or self.spawn_glow > 0.02)
        if moving:
            self.vis_start  += ds * factor
            self.vis_end    += de * factor
            self.vis_bright += db * factor
            self.spawn_glow  = max(0.0, self.spawn_glow - 0.08)
        else:
            self.vis_start  = self.start
            self.vis_end    = self.end
            self.vis_bright = self.brightness
            self.spawn_glow = 0.0
        return moving

    def is_dead(self) -> bool:
        return self.is_closing and abs(self.vis_end - self.vis_start) < 1.8 and self.vis_bright < 0.04

    def to_dict(self):
        return {
            'start': round(self.start, 1),
            'end': round(self.end, 1),
            'mode': self.mode,
            'hue': round(self.hue, 4),
            'temp': round(self.temp, 4),
            'brightness': round(self.brightness, 3),
            'effect': self.effect,
            'speed': self.speed
        }

    def get_rgb(self, for_hardware: bool = True):
        if self.mode == 'RGB':
            r, g, b = colorsys.hsv_to_rgb(self.hue, 1.0, 1.0)
            return (int(r * 255), int(g * 255), int(b * 255))
        else:
            return kelvin_to_rgb_calibrated(self.temp, apply_hw_calib=for_hardware)

    def compute_pixels(self, elapsed: float, for_hardware: bool = True):
        s = max(0, int(round(self.vis_start)))
        e = min(TOTAL_LEDS, int(round(self.vis_end)))
        length = max(1, e - s)
        br, bg, bb = self.get_rgb(for_hardware=for_hardware)
        b_lvl = max(0.0, min(1.0, self.vis_bright))
        t = elapsed * self.speed

        if self.effect == "FIXE":
            col = (int(br * b_lvl), int(bg * b_lvl), int(bb * b_lvl))
            return [col] * length

        elif self.effect == "RESPIRATION":
            s_val = 0.5 * (1.0 - math.cos(t * 2.2))
            factor = (0.10 + 0.90 * (s_val ** 1.6)) * b_lvl
            col = (int(br * factor), int(bg * factor), int(bb * factor))
            return [col] * length

        elif self.effect == "ONDE":
            out = []
            freq = (2.0 * math.pi) / max(12.0, length * 0.5)
            for idx in range(length):
                w = 0.5 * (1.0 + math.sin(idx * freq - t * 3.5))
                f = (0.12 + 0.88 * (w ** 1.8)) * b_lvl
                out.append((int(br * f), int(bg * f), int(bb * f)))
            return out

        elif self.effect == "COMÈTE":
            out = []
            head = (t * (length * 0.45)) % length
            tail = max(6.0, length * 0.40)
            for idx in range(length):
                dist = (head - idx) % length
                if dist < 1.2:
                    f = b_lvl
                    out.append((min(255, int(br * f + 55 * f)),
                                min(255, int(bg * f + 55 * f)),
                                min(255, int(bb * f + 55 * f))))
                elif dist < tail:
                    f = ((1.0 - dist / tail) ** 2.2) * b_lvl
                    out.append((int(br * f), int(bg * f), int(bb * f)))
                else:
                    f = 0.05 * b_lvl
                    out.append((int(br * f), int(bg * f), int(bb * f)))
            return out

        elif self.effect == "AURORE":
            out = []
            base_h = self.hue if self.mode == 'RGB' else 0.45
            for idx in range(length):
                pos = idx / max(1.0, float(length))
                h_shift = 0.14 * math.sin(pos * 4.0 + t * 0.8) + 0.06 * math.cos(pos * 9.0 - t * 1.3)
                h = (base_h + h_shift) % 1.0
                v_wave = 0.55 + 0.45 * math.sin(pos * 5.0 - t * 1.1)
                r, g, b = colorsys.hsv_to_rgb(h, 0.88, v_wave * b_lvl)
                out.append((int(r * 255), int(g * 255), int(b * 255)))
            return out

        elif self.effect == "ARC-EN-CIEL":
            out = []
            for idx in range(length):
                h = (self.hue + (idx / max(18.0, float(length))) - t * 0.22) % 1.0
                r, g, b = colorsys.hsv_to_rgb(h, 0.95, b_lvl)
                out.append((int(r * 255), int(g * 255), int(b * 255)))
            return out

        elif self.effect == "FEU":
            out = []
            gg = ColorCalibration.gain_g
            gb = ColorCalibration.gain_b
            for idx in range(length):
                w1 = math.sin(idx * 0.35 - t * 4.2)
                w2 = math.sin(idx * 0.80 + t * 6.8) * 0.5
                w3 = math.cos(idx * 0.18 - t * 2.5) * 0.5
                heat = max(0.18, min(1.0, 0.62 + 0.22 * (w1 + w2 + w3))) * b_lvl
                r = int(255 * heat)
                g = int(105 * (heat ** 1.7) * gg)
                b = int(10 * (heat ** 2.8) * gb)
                out.append((r, g, b))
            return out

        return [(int(br * b_lvl), int(bg * b_lvl), int(bb * b_lvl))] * length

# ============================================================================
# 7. OVERLAY PARAMÈTRES INTERNE (Plein écran garanti sur 1480x320)
# ============================================================================
class SettingsOverlay(QWidget):
    def __init__(self, app_ref):
        super().__init__(app_ref)
        self.app_ref = app_ref
        self.setVisible(False)
        self.bg_alpha = 0
        
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.container = QFrame(self)
        self.container.setFixedSize(940, 244)
        self.container.setObjectName("MainContainer")
        self.container.setStyleSheet("""
            QFrame#MainContainer {
                background-color: #181b22; color: white; 
                border: 2px solid #4a5568; border-radius: 14px;
            }
            QFrame.SubPanel {
                background-color: #222630; border: 1px solid #323846; border-radius: 8px;
            }
            QLabel { border: none; color: #ecf0f1; }
        """)
        outer_layout.addWidget(self.container)
        
        main_vbox = QVBoxLayout(self.container)
        main_vbox.setContentsMargins(12, 8, 12, 8)
        main_vbox.setSpacing(6)

        # QPushButton utilise && pour afficher une esperluette simple
        tab_bar = QHBoxLayout()
        tab_bar.setSpacing(6)
        self.btn_tab0 = QPushButton("⚡ Veille, Relais && Transitions")
        self.btn_tab1 = QPushButton("🎨 Calibration Blancs && Ventilateur")
        self.btn_tab2 = QPushButton("🏠 Domotique, Sync && Système")
        
        for idx, btn in enumerate((self.btn_tab0, self.btn_tab1, self.btn_tab2)):
            btn.setFixedHeight(32)
            btn.clicked.connect(lambda checked, i=idx: self.switch_tab(i))
            tab_bar.addWidget(btn)

        btn_close_top = QPushButton("✖ Fermer")
        btn_close_top.setFixedSize(90, 32)
        btn_close_top.setStyleSheet("background-color: #27ae60; color: white; font-weight: bold; font-size: 13px; border-radius: 7px; border: none;")
        btn_close_top.clicked.connect(self.close_overlay)
        tab_bar.addWidget(btn_close_top)
        main_vbox.addLayout(tab_bar)

        self.stack = QStackedWidget()
        main_vbox.addWidget(self.stack)

        btn_base = "font-size: 12px; font-weight: bold; min-height: 30px; max-height: 30px; border: none; border-radius: 6px; color: white;"
        btn_step = "background-color: #353b48; font-size: 15px; font-weight: bold; border-radius: 6px; color: white; border: none;"

        # ONGLET 0 : VEILLE RADAR, RELAIS 15 MIN, TRANSITIONS & ÉCRAN
        page0 = QWidget()
        p0_lay = QHBoxLayout(page0)
        p0_lay.setContentsMargins(0, 0, 0, 0)
        p0_lay.setSpacing(8)

        c0_1 = QFrame(); c0_1.setProperty("class", "SubPanel")
        l0_1 = QVBoxLayout(c0_1); l0_1.setContentsMargins(8, 6, 8, 6); l0_1.setSpacing(5)
        t_r = QLabel("📡 Détection Radar & LED"); t_r.setStyleSheet("font-size: 13px; font-weight: bold; color: #3498db;"); t_r.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l0_1.addWidget(t_r)

        self.btn_radar_mode = QPushButton()
        self.btn_radar_mode.clicked.connect(self.toggle_radar_mode)
        l0_1.addWidget(self.btn_radar_mode)

        row_t_led = QHBoxLayout()
        b_tl_m = QPushButton("−"); b_tl_m.setFixedSize(34, 30); b_tl_m.setStyleSheet(btn_step); b_tl_m.clicked.connect(lambda: self.change_led_timeout(-1))
        self.lbl_led_timeout = QLabel(); self.lbl_led_timeout.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_led_timeout.setStyleSheet("font-size: 12px; font-weight: bold;")
        b_tl_p = QPushButton("+"); b_tl_p.setFixedSize(34, 30); b_tl_p.setStyleSheet(btn_step); b_tl_p.clicked.connect(lambda: self.change_led_timeout(1))
        row_t_led.addWidget(b_tl_m); row_t_led.addWidget(self.lbl_led_timeout); row_t_led.addWidget(b_tl_p)
        l0_1.addLayout(row_t_led)

        row_t_rel = QHBoxLayout()
        b_tr_m = QPushButton("−"); b_tr_m.setFixedSize(34, 30); b_tr_m.setStyleSheet(btn_step); b_tr_m.clicked.connect(lambda: self.change_relay_delay(-1))
        self.lbl_relay_delay = QLabel(); self.lbl_relay_delay.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_relay_delay.setStyleSheet("font-size: 12px; font-weight: bold; color: #f1c40f;")
        b_tr_p = QPushButton("+"); b_tr_p.setFixedSize(34, 30); b_tr_p.setStyleSheet(btn_step); b_tr_p.clicked.connect(lambda: self.change_relay_delay(1))
        row_t_rel.addWidget(b_tr_m); row_t_rel.addWidget(self.lbl_relay_delay); row_t_rel.addWidget(b_tr_p)
        l0_1.addLayout(row_t_rel)
        p0_lay.addWidget(c0_1, stretch=1)

        c0_2 = QFrame(); c0_2.setProperty("class", "SubPanel")
        l0_2 = QVBoxLayout(c0_2); l0_2.setContentsMargins(8, 6, 8, 6); l0_2.setSpacing(5)
        t_tr = QLabel("✨ Transition & Bandeau"); t_tr.setStyleSheet("font-size: 13px; font-weight: bold; color: #9b59b6;"); t_tr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l0_2.addWidget(t_tr)

        self.btn_trans_style = QPushButton()
        self.btn_trans_style.setStyleSheet("background-color: #8e44ad; " + btn_base)
        self.btn_trans_style.clicked.connect(self.cycle_transition_style)
        l0_2.addWidget(self.btn_trans_style)

        row_tdur = QHBoxLayout()
        b_td_m = QPushButton("−"); b_td_m.setFixedSize(34, 30); b_td_m.setStyleSheet(btn_step); b_td_m.clicked.connect(lambda: self.change_trans_duration(-0.4))
        self.lbl_trans_dur = QLabel(); self.lbl_trans_dur.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_trans_dur.setStyleSheet("font-size: 12px; font-weight: bold;")
        b_td_p = QPushButton("+"); b_td_p.setFixedSize(34, 30); b_td_p.setStyleSheet(btn_step); b_td_p.clicked.connect(lambda: self.change_trans_duration(0.4))
        row_tdur.addWidget(b_td_m); row_tdur.addWidget(self.lbl_trans_dur); row_tdur.addWidget(b_td_p)
        l0_2.addLayout(row_tdur)

        row_pauto = QHBoxLayout()
        b_pa_m = QPushButton("−"); b_pa_m.setFixedSize(34, 30); b_pa_m.setStyleSheet(btn_step); b_pa_m.clicked.connect(lambda: self.change_panel_autoclose(-1))
        self.lbl_panel_auto = QLabel(); self.lbl_panel_auto.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_panel_auto.setStyleSheet("font-size: 12px; font-weight: bold; color: #a29bfe;")
        b_pa_p = QPushButton("+"); b_pa_p.setFixedSize(34, 30); b_pa_p.setStyleSheet(btn_step); b_pa_p.clicked.connect(lambda: self.change_panel_autoclose(1))
        row_pauto.addWidget(b_pa_m); row_pauto.addWidget(self.lbl_panel_auto); row_pauto.addWidget(b_pa_p)
        l0_2.addLayout(row_pauto)
        p0_lay.addWidget(c0_2, stretch=1)

        c0_3 = QFrame(); c0_3.setProperty("class", "SubPanel")
        l0_3 = QVBoxLayout(c0_3); l0_3.setContentsMargins(8, 6, 8, 6); l0_3.setSpacing(5)
        t_sc = QLabel("🖥️ Veille Écran Tactile"); t_sc.setStyleSheet("font-size: 13px; font-weight: bold; color: #1abc9c;"); t_sc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l0_3.addWidget(t_sc)

        self.btn_screen_mode = QPushButton()
        self.btn_screen_mode.setStyleSheet("background-color: #16a085; " + btn_base)
        self.btn_screen_mode.clicked.connect(self.cycle_screen_mode)
        l0_3.addWidget(self.btn_screen_mode)

        row_tsc = QHBoxLayout()
        b_ts_m = QPushButton("−"); b_ts_m.setFixedSize(34, 30); b_ts_m.setStyleSheet(btn_step); b_ts_m.clicked.connect(lambda: self.change_screen_timeout(-1))
        self.lbl_screen_timeout = QLabel(); self.lbl_screen_timeout.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_screen_timeout.setStyleSheet("font-size: 12px; font-weight: bold;")
        b_ts_p = QPushButton("+"); b_ts_p.setFixedSize(34, 30); b_ts_p.setStyleSheet(btn_step); b_ts_p.clicked.connect(lambda: self.change_screen_timeout(1))
        row_tsc.addWidget(b_ts_m); row_tsc.addWidget(self.lbl_screen_timeout); row_tsc.addWidget(b_ts_p)
        l0_3.addLayout(row_tsc)

        btn_test_saver = QPushButton("🌙 Tester veille maintenant")
        btn_test_saver.setStyleSheet("background-color: #34495e; " + btn_base)
        btn_test_saver.clicked.connect(self.trigger_screen_saver_now)
        l0_3.addWidget(btn_test_saver)
        p0_lay.addWidget(c0_3, stretch=1)

        self.stack.addWidget(page0)

        # ONGLET 1 : CALIBRATION DES BLANCS (R/G/B) & VENTILATEUR
        page1 = QWidget()
        p1_lay = QHBoxLayout(page1)
        p1_lay.setContentsMargins(0, 0, 0, 0)
        p1_lay.setSpacing(8)

        c1_cal = QFrame(); c1_cal.setProperty("class", "SubPanel")
        l1_cal = QVBoxLayout(c1_cal); l1_cal.setContentsMargins(10, 6, 10, 6); l1_cal.setSpacing(5)
        t_cal = QLabel("🎨 Calibration Optique des Blancs (Gains R / G / B)")
        t_cal.setStyleSheet("font-size: 13px; font-weight: bold; color: #f39c12;")
        t_cal.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l1_cal.addWidget(t_cal)

        row_r = QHBoxLayout()
        lbl_r = QLabel("Canal ROUGE :"); lbl_r.setStyleSheet("color: #ff6b6b; font-weight: bold; font-size: 12px;")
        b_rm = QPushButton("−"); b_rm.setFixedSize(34, 28); b_rm.setStyleSheet(btn_step); b_rm.clicked.connect(lambda: self.adjust_calib('r', -0.04))
        self.lbl_cal_r = QLabel(); self.lbl_cal_r.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_cal_r.setStyleSheet("font-weight: bold; font-size: 12px;")
        b_rp = QPushButton("+"); b_rp.setFixedSize(34, 28); b_rp.setStyleSheet(btn_step); b_rp.clicked.connect(lambda: self.adjust_calib('r', 0.04))
        row_r.addWidget(lbl_r, stretch=1); row_r.addWidget(b_rm); row_r.addWidget(self.lbl_cal_r, stretch=1); row_r.addWidget(b_rp)
        l1_cal.addLayout(row_r)

        row_g = QHBoxLayout()
        lbl_g = QLabel("Canal VERT (G) :"); lbl_g.setStyleSheet("color: #2ecc71; font-weight: bold; font-size: 12px;")
        b_gm = QPushButton("−"); b_gm.setFixedSize(34, 28); b_gm.setStyleSheet(btn_step); b_gm.clicked.connect(lambda: self.adjust_calib('g', -0.04))
        self.lbl_cal_g = QLabel(); self.lbl_cal_g.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_cal_g.setStyleSheet("font-weight: bold; font-size: 12px;")
        b_gp = QPushButton("+"); b_gp.setFixedSize(34, 28); b_gp.setStyleSheet(btn_step); b_gp.clicked.connect(lambda: self.adjust_calib('g', 0.04))
        row_g.addWidget(lbl_g, stretch=1); row_g.addWidget(b_gm); row_g.addWidget(self.lbl_cal_g, stretch=1); row_g.addWidget(b_gp)
        l1_cal.addLayout(row_g)

        row_b = QHBoxLayout()
        lbl_b = QLabel("Canal BLEU (Froid) :"); lbl_b.setStyleSheet("color: #3498db; font-weight: bold; font-size: 12px;")
        b_bm = QPushButton("−"); b_bm.setFixedSize(34, 28); b_bm.setStyleSheet(btn_step); b_bm.clicked.connect(lambda: self.adjust_calib('b', -0.04))
        self.lbl_cal_b = QLabel(); self.lbl_cal_b.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_cal_b.setStyleSheet("font-weight: bold; font-size: 12px;")
        b_bp = QPushButton("+"); b_bp.setFixedSize(34, 28); b_bp.setStyleSheet(btn_step); b_bp.clicked.connect(lambda: self.adjust_calib('b', 0.04))
        row_b.addWidget(lbl_b, stretch=1); row_b.addWidget(b_bm); row_b.addWidget(self.lbl_cal_b, stretch=1); row_b.addWidget(b_bp)
        l1_cal.addLayout(row_b)

        p1_lay.addWidget(c1_cal, stretch=2)

        c1_fan = QFrame(); c1_fan.setProperty("class", "SubPanel")
        l1_fan = QVBoxLayout(c1_fan); l1_fan.setContentsMargins(8, 6, 8, 6); l1_fan.setSpacing(5)
        t_f = QLabel("🌡️ Thermique & Fan (Kickstart)"); t_f.setStyleSheet("font-size: 13px; font-weight: bold; color: #e67e22;"); t_f.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l1_fan.addWidget(t_f)

        self.lbl_cpu_status = QLabel(); self.lbl_cpu_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l1_fan.addWidget(self.lbl_cpu_status)

        self.btn_fan_mode = QPushButton()
        self.btn_fan_mode.clicked.connect(self.toggle_fan_mode)
        l1_fan.addWidget(self.btn_fan_mode)

        fan_box = QHBoxLayout()
        self.btn_f_minus = QPushButton("−"); self.btn_f_minus.setFixedSize(34, 30); self.btn_f_minus.setStyleSheet(btn_step); self.btn_f_minus.clicked.connect(lambda: self.change_fan_speed(-25))
        self.lbl_fan_set = QLabel(); self.lbl_fan_set.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_fan_set.setStyleSheet("font-size: 12px; font-weight: bold;")
        self.btn_f_plus = QPushButton("+"); self.btn_f_plus.setFixedSize(34, 30); self.btn_f_plus.setStyleSheet(btn_step); self.btn_f_plus.clicked.connect(lambda: self.change_fan_speed(25))
        fan_box.addWidget(self.btn_f_minus); fan_box.addWidget(self.lbl_fan_set); fan_box.addWidget(self.btn_f_plus)
        l1_fan.addLayout(fan_box)
        p1_lay.addWidget(c1_fan, stretch=1)

        self.stack.addWidget(page1)

        # ONGLET 2 : DOMOTIQUE BUREAU (DAIKIN / SONOFF), SYNC INTER-BUREAUX & SYSTÈME
        page2 = QWidget()
        p2_lay = QHBoxLayout(page2)
        p2_lay.setContentsMargins(0, 0, 0, 0)
        p2_lay.setSpacing(8)

        c2_pwr = QFrame(); c2_pwr.setProperty("class", "SubPanel")
        l2_pwr = QVBoxLayout(c2_pwr); l2_pwr.setContentsMargins(8, 6, 8, 6); l2_pwr.setSpacing(5)
        t_pw = QLabel("🔌 Relais 12V & Sync Bureaux"); t_pw.setStyleSheet("font-size: 13px; font-weight: bold; color: #3498db;"); t_pw.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l2_pwr.addWidget(t_pw)
        self.btn_manual_power = QPushButton()
        self.btn_manual_power.clicked.connect(self.toggle_manual_led)
        l2_pwr.addWidget(self.btn_manual_power)

        self.btn_sync_mode = QPushButton()
        self.btn_sync_mode.setStyleSheet("background-color: #16a085; " + btn_base)
        self.btn_sync_mode.clicked.connect(self.cycle_sync_mode)
        l2_pwr.addWidget(self.btn_sync_mode)

        self.lbl_relay_info = QLabel()
        self.lbl_relay_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_relay_info.setStyleSheet("color: #95a5a6; font-size: 11px;")
        l2_pwr.addWidget(self.lbl_relay_info)
        p2_lay.addWidget(c2_pwr, stretch=1)

        c2_dom = QFrame(); c2_dom.setProperty("class", "SubPanel")
        l2_dom = QVBoxLayout(c2_dom); l2_dom.setContentsMargins(8, 6, 8, 6); l2_dom.setSpacing(5)
        t_dm = QLabel("❄️ Clim Daikin & Sonoff Zigbee"); t_dm.setStyleSheet("font-size: 13px; font-weight: bold; color: #1abc9c;"); t_dm.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l2_dom.addWidget(t_dm)

        clim_box = QHBoxLayout()
        b_cm = QPushButton("−"); b_cm.setFixedSize(34, 30); b_cm.setStyleSheet(btn_step); b_cm.clicked.connect(lambda: self.change_clim_temp(-1))
        self.lbl_clim_cfg = QLabel(); self.lbl_clim_cfg.setAlignment(Qt.AlignmentFlag.AlignCenter); self.lbl_clim_cfg.setStyleSheet("font-size: 12px; font-weight: bold;")
        b_cp = QPushButton("+"); b_cp.setFixedSize(34, 30); b_cp.setStyleSheet(btn_step); b_cp.clicked.connect(lambda: self.change_clim_temp(1))
        clim_box.addWidget(b_cm); clim_box.addWidget(self.lbl_clim_cfg); clim_box.addWidget(b_cp)
        l2_dom.addLayout(clim_box)

        self.btn_sonoff_appt = QPushButton()
        self.btn_sonoff_appt.clicked.connect(self.app_ref.toggle_sonoff_appoint)
        l2_dom.addWidget(self.btn_sonoff_appt)
        l2_dom.addStretch()
        p2_lay.addWidget(c2_dom, stretch=1)

        c2_sys = QFrame(); c2_sys.setProperty("class", "SubPanel")
        l2_sys = QVBoxLayout(c2_sys); l2_sys.setContentsMargins(8, 6, 8, 6); l2_sys.setSpacing(5)
        t_sy = QLabel("⚙️ Système Raspberry Pi"); t_sy.setStyleSheet("font-size: 13px; font-weight: bold; color: #95a5a6;"); t_sy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l2_sys.addWidget(t_sy)
        btn_quit = QPushButton("❌ Quitter (LED + Relais OFF)"); btn_quit.setStyleSheet("background-color: #57606f; " + btn_base); btn_quit.clicked.connect(self.app_ref.close)
        btn_reboot = QPushButton("🔄 Redémarrer le RPi"); btn_reboot.setStyleSheet("background-color: #d35400; " + btn_base); btn_reboot.clicked.connect(self.do_reboot)
        btn_shutdown = QPushButton("🛑 Éteindre le RPi"); btn_shutdown.setStyleSheet("background-color: #c0392b; " + btn_base); btn_shutdown.clicked.connect(self.do_shutdown)
        l2_sys.addWidget(btn_quit); l2_sys.addWidget(btn_reboot); l2_sys.addWidget(btn_shutdown)
        p2_lay.addWidget(c2_sys, stretch=1)

        self.stack.addWidget(page2)

        self.switch_tab(0)

        self.fade_anim = QVariantAnimation(self)
        self.fade_anim.setDuration(140)
        self.fade_anim.valueChanged.connect(self._on_fade)

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self.refresh_ui)

    def _on_fade(self, val):
        self.bg_alpha = int(val)
        self.update()

    def open_overlay(self):
        self.setGeometry(self.app_ref.rect())
        self.app_ref.overlay_open = True
        self.refresh_ui()
        self.setVisible(True)
        self.raise_()
        self.fade_anim.stop()
        self.fade_anim.setStartValue(0)
        self.fade_anim.setEndValue(180)
        self.fade_anim.start()
        self.ui_timer.start(500)

    def close_overlay(self):
        self.app_ref.overlay_open = False
        self.ui_timer.stop()
        self.setVisible(False)

    def switch_tab(self, idx: int):
        self.stack.setCurrentIndex(idx)
        for i, btn in enumerate((self.btn_tab0, self.btn_tab1, self.btn_tab2)):
            if i == idx:
                btn.setStyleSheet("background-color: #2980b9; color: white; font-weight: bold; font-size: 12px; border-radius: 7px; border: 1px solid #3498db;")
            else:
                btn.setStyleSheet("background-color: #242832; color: #95a5a6; font-weight: bold; font-size: 12px; border-radius: 7px; border: 1px solid #323846;")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, self.bg_alpha))

    def mousePressEvent(self, event):
        if not self.container.geometry().contains(event.pos()):
            self.close_overlay()
        else:
            super().mousePressEvent(event)

    def refresh_ui(self):
        btn_base = "font-size: 12px; font-weight: bold; min-height: 30px; max-height: 30px; border: none; border-radius: 6px; color: white;"
        
        if self.app_ref.radar_enabled:
            self.btn_radar_mode.setText("Radar : AUTO (Actif)")
            self.btn_radar_mode.setStyleSheet("background-color: #2980b9; " + btn_base)
        else:
            self.btn_radar_mode.setText("Radar : MANUEL (Permanent)")
            self.btn_radar_mode.setStyleSheet("background-color: #57606f; " + btn_base)

        t_led = self.app_ref.radar_timeout
        t_led_str = f"{t_led} s" if t_led < 60 else f"{t_led // 60} min"
        self.lbl_led_timeout.setText(f"Extinction LED : {t_led_str}")
        self.lbl_relay_delay.setText(f"Relais OFF : +{self.app_ref.relay_delay_min} min")

        self.btn_trans_style.setText(f"Style : {self.app_ref.transition_style}")
        self.lbl_trans_dur.setText(f"Durée : {self.app_ref.transition_duration:.1f} s")

        p_auto = self.app_ref.panel_autoclose_s
        self.lbl_panel_auto.setText("Repli bandeau : Non" if p_auto == 0 else f"Repli bandeau : {p_auto} s")

        self.btn_screen_mode.setText(f"Mode : {self.app_ref.screen_sleep_mode}")
        t_sc = self.app_ref.screen_timeout
        t_sc_str = "Jamais" if t_sc == 0 else (f"{t_sc} s" if t_sc < 60 else f"{t_sc // 60} min")
        self.lbl_screen_timeout.setText(f"Inactivité : {t_sc_str}")

        self.lbl_cal_r.setText(f"{int(round(ColorCalibration.gain_r * 100))}%")
        self.lbl_cal_g.setText(f"{int(round(ColorCalibration.gain_g * 100))}%")
        self.lbl_cal_b.setText(f"{int(round(ColorCalibration.gain_b * 100))}%")

        temp_c = self.app_ref.current_cpu_temp
        fan_pct = int(round(self.app_ref.actual_fan_output * 100))
        boost_txt = " (BOOST)" if self.app_ref.kickstart_timer.isActive() else ""
        color_t = "#2ecc71" if temp_c < 55 else "#f39c12" if temp_c < 65 else "#e74c3c"
        self.lbl_cpu_status.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {color_t}; min-height: 26px;")
        self.lbl_cpu_status.setText(f"CPU: {temp_c:.1f}°C | Fan: {fan_pct}%{boost_txt}")

        if self.app_ref.fan_auto:
            self.btn_fan_mode.setText("Ventilateur : AUTO")
            self.btn_fan_mode.setStyleSheet("background-color: #d35400; " + btn_base)
            self.lbl_fan_set.setText("45°C → 65°C")
            self.btn_f_minus.setEnabled(False); self.btn_f_plus.setEnabled(False)
        else:
            self.btn_fan_mode.setText("Ventilateur : MANUEL")
            self.btn_fan_mode.setStyleSheet("background-color: #8e44ad; " + btn_base)
            self.lbl_fan_set.setText(f"Consigne : {self.app_ref.fan_manual_pct}%")
            self.btn_f_minus.setEnabled(True); self.btn_f_plus.setEnabled(True)

        if self.app_ref.led_power_state:
            self.btn_manual_power.setText("Relais 12V : ARMÉ (ON) 🟢")
            self.btn_manual_power.setStyleSheet("background-color: #27ae60; " + btn_base)
            if self.app_ref.relay_cutoff_timer.isActive():
                rem_min = int(math.ceil(self.app_ref.relay_cutoff_timer.remainingTime() / 60000.0))
                self.lbl_relay_info.setText(f"Veille — Coupure relais dans ~{rem_min} min")
            else:
                self.lbl_relay_info.setText("Alimentation active et stabilisée")
        else:
            self.btn_manual_power.setText("Relais 12V : COUPÉ (OFF) 🔴")
            self.btn_manual_power.setStyleSheet("background-color: #c0392b; " + btn_base)
            self.lbl_relay_info.setText("Alimentation 12V isolée (Haute-Z)")

        self.btn_sync_mode.setText(f"Mode : {self.app_ref.sync_mode}")
        self.lbl_clim_cfg.setText(f"Consigne Daikin : {self.app_ref.clim_target_temp}°C")

        if self.app_ref.sonoff_appt_state:
            self.btn_sonoff_appt.setText("Sonoff Appoint : ON 🟢")
            self.btn_sonoff_appt.setStyleSheet("background-color: #27ae60; " + btn_base)
        else:
            self.btn_sonoff_appt.setText("Sonoff Appoint : OFF ⚫")
            self.btn_sonoff_appt.setStyleSheet("background-color: #353b48; " + btn_base)

    def toggle_radar_mode(self):
        self.app_ref.toggle_radar_mode_quick()
        self.refresh_ui()

    def change_led_timeout(self, delta):
        curr = self.app_ref.radar_timeout
        idx = LED_TIMEOUT_STEPS.index(curr) if curr in LED_TIMEOUT_STEPS else 1
        self.app_ref.radar_timeout = LED_TIMEOUT_STEPS[max(0, min(len(LED_TIMEOUT_STEPS) - 1, idx + delta))]
        self.app_ref.save_config()
        self.refresh_ui()

    def change_relay_delay(self, delta):
        curr = self.app_ref.relay_delay_min
        idx = RELAY_DELAY_MINS.index(curr) if curr in RELAY_DELAY_MINS else 2
        self.app_ref.relay_delay_min = RELAY_DELAY_MINS[max(0, min(len(RELAY_DELAY_MINS) - 1, idx + delta))]
        self.app_ref.save_config()
        self.refresh_ui()

    def cycle_transition_style(self):
        curr = self.app_ref.transition_style
        idx = TRANSITION_STYLES.index(curr) if curr in TRANSITION_STYLES else 0
        self.app_ref.transition_style = TRANSITION_STYLES[(idx + 1) % len(TRANSITION_STYLES)]
        self.app_ref.save_config()
        self.refresh_ui()

    def change_trans_duration(self, delta_s):
        self.app_ref.transition_duration = round(max(0.4, min(4.0, self.app_ref.transition_duration + delta_s)), 1)
        self.app_ref.save_config()
        self.refresh_ui()

    def change_panel_autoclose(self, delta):
        curr = self.app_ref.panel_autoclose_s
        idx = PANEL_AUTO_CLOSE_S.index(curr) if curr in PANEL_AUTO_CLOSE_S else 2
        self.app_ref.panel_autoclose_s = PANEL_AUTO_CLOSE_S[max(0, min(len(PANEL_AUTO_CLOSE_S) - 1, idx + delta))]
        self.app_ref.reset_panel_idle_timer()
        self.app_ref.save_config()
        self.refresh_ui()

    def cycle_screen_mode(self):
        curr = self.app_ref.screen_sleep_mode
        idx = SCREEN_MODES.index(curr) if curr in SCREEN_MODES else 0
        self.app_ref.screen_sleep_mode = SCREEN_MODES[(idx + 1) % len(SCREEN_MODES)]
        self.app_ref.save_config()
        self.refresh_ui()

    def change_screen_timeout(self, delta):
        curr = self.app_ref.screen_timeout
        idx = SCREEN_TIMEOUT_STEPS.index(curr) if curr in SCREEN_TIMEOUT_STEPS else 2
        self.app_ref.screen_timeout = SCREEN_TIMEOUT_STEPS[max(0, min(len(SCREEN_TIMEOUT_STEPS) - 1, idx + delta))]
        self.app_ref.reset_user_activity_timer()
        self.app_ref.save_config()
        self.refresh_ui()

    def trigger_screen_saver_now(self):
        self.close_overlay()
        QTimer.singleShot(150, lambda: self.app_ref.enter_screen_sleep(force=True))

    def adjust_calib(self, channel: str, delta: float):
        if channel == 'r':
            ColorCalibration.gain_r = round(max(0.30, min(1.0, ColorCalibration.gain_r + delta)), 2)
        elif channel == 'g':
            ColorCalibration.gain_g = round(max(0.30, min(1.0, ColorCalibration.gain_g + delta)), 2)
        elif channel == 'b':
            ColorCalibration.gain_b = round(max(0.20, min(1.0, ColorCalibration.gain_b + delta)), 2)
        ColorCalibration.rebuild_lut()
        self.app_ref.slider.invalidate_cache()
        self.app_ref.track.update()
        self.app_ref.request_hw_update()
        self.app_ref.save_config()
        self.refresh_ui()

    def toggle_manual_led(self):
        if self.app_ref.led_power_state:
            self.app_ref.cut_led_and_relay_safely()
        else:
            self.app_ref.wake_up_leds()
        self.refresh_ui()

    def cycle_sync_mode(self):
        curr = self.app_ref.sync_mode
        idx = SYNC_MODES.index(curr) if curr in SYNC_MODES else 0
        self.app_ref.sync_mode = SYNC_MODES[(idx + 1) % len(SYNC_MODES)]
        self.app_ref.save_config()
        self.refresh_ui()

    def change_clim_temp(self, delta: int):
        self.app_ref.clim_target_temp = max(18, min(28, self.app_ref.clim_target_temp + delta))
        self.app_ref.update_clim_btn_style()
        self.app_ref.send_homey_capability(ID_DEVICE_CLIM_BUREAU, "target_temperature", self.app_ref.clim_target_temp)
        self.refresh_ui()

    def toggle_fan_mode(self):
        self.app_ref.fan_auto = not self.app_ref.fan_auto
        self.app_ref.apply_fan_speed(force_kickstart=True)
        self.app_ref.save_config()
        self.refresh_ui()

    def change_fan_speed(self, delta_pct):
        old_pct = self.app_ref.fan_manual_pct
        self.app_ref.fan_manual_pct = max(0, min(100, old_pct + delta_pct))
        self.app_ref.apply_fan_speed(force_kickstart=(self.app_ref.fan_manual_pct > 0))
        self.app_ref.save_config()
        self.refresh_ui()

    def do_reboot(self):
        self.app_ref.shutdown_hardware()
        subprocess.run(["sudo", "reboot"])
        
    def do_shutdown(self):
        self.app_ref.shutdown_hardware()
        subprocess.run(["sudo", "shutdown", "-h", "now"])

# ============================================================================
# 8. ÉCRAN DE VEILLE ANIMÉ (Zéro Flash Curseur)
# ============================================================================
class ScreenSaverOverlay(QWidget):
    wakeRequested      = pyqtSignal()
    ledToggleRequested = pyqtSignal()
    fadeOutFinished    = pyqtSignal()
    fadeInFinished     = pyqtSignal()

    def __init__(self, app_ref):
        super().__init__(app_ref)
        self.app_ref = app_ref
        self.mode = "ICÔNE VEILLE"
        self.pulse = 0.0
        self.fade_factor = 0.0
        self.fading_in = False
        self.led_btn_rect = QRectF()
        
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setVisible(True)

        self.timer = QTimer(self)
        self.timer.setInterval(65)
        self.timer.timeout.connect(self._tick)

        self.fade_anim = QVariantAnimation(self)
        self.fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self.fade_anim.valueChanged.connect(self._on_fade_step)
        self.fade_anim.finished.connect(self._on_fade_done)

    def start_saver(self, mode: str):
        self.mode = mode
        self.fading_in = True
        self.setGeometry(self.app_ref.rect())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.raise_()
        self.timer.start()

        self.fade_anim.stop()
        self.fade_anim.setDuration(900)
        self.fade_anim.setStartValue(self.fade_factor)
        self.fade_anim.setEndValue(1.0)
        self.fade_anim.start()

    def stop_saver(self):
        if not self.fading_in and self.fade_factor <= 0.001:
            return
        self.fading_in = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.fade_anim.stop()
        self.fade_anim.setDuration(300)
        self.fade_anim.setStartValue(self.fade_factor)
        self.fade_anim.setEndValue(0.0)
        self.fade_anim.start()

    def _on_fade_step(self, val):
        self.fade_factor = float(val)
        self.update()

    def _on_fade_done(self):
        if self.fading_in:
            self.fadeInFinished.emit()
        else:
            self.timer.stop()
            self.fade_factor = 0.0
            self.update()
            self.fadeOutFinished.emit()

    def _tick(self):
        self.pulse = (self.pulse + 0.09) % (2.0 * math.pi)
        self.update()

    def paintEvent(self, event):
        if self.fade_factor <= 0.005:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        f = self.fade_factor

        if self.mode == "ASSOMBRIR":
            bg_alpha = int(222 * f)
        elif self.mode == "HDMI OFF":
            bg_alpha = int(255 * f)
        else:
            bg_alpha = int(248 * f)
        painter.fillRect(self.rect(), QColor(0, 0, 0, bg_alpha))

        if f < 0.05:
            return

        cx, cy = w * 0.5, h * 0.5
        pulse_val = 0.5 * (1.0 + math.sin(self.pulse))
        alpha_main = int((70 + 55 * pulse_val) * f)

        if self.mode == "ASSOMBRIR":
            painter.setPen(QColor(236, 240, 241, alpha_main + int(30 * f)))
            t_str = QTime.currentTime().toString("HH:mm")
            painter.drawText(QRectF(cx - 170, cy - 55, 340, 38), Qt.AlignmentFlag.AlignCenter, f"🔆  {t_str}  —  Toucher l'écran pour réveiller")
        else:
            painter.setPen(QPen(QColor(52, 152, 219, alpha_main), 1.8))
            painter.setBrush(QColor(20, 26, 36, int(150 * f)))
            painter.drawEllipse(QPointF(cx, cy - 34), 34, 34)

            painter.setPen(QColor(236, 240, 241, min(255, alpha_main + int(45 * f))))
            t_str = QTime.currentTime().toString("HH:mm")
            painter.drawText(QRectF(cx - 120, cy - 52, 240, 36), Qt.AlignmentFlag.AlignCenter, f"🔆  {t_str}")

            painter.setPen(QColor(149, 165, 166, int(95 * f)))
            painter.drawText(QRectF(cx - 170, cy + 4, 340, 24), Qt.AlignmentFlag.AlignCenter, "Effleurer l'écran pour sortir de veille")

        btn_w, btn_h = 195.0, 44.0
        self.led_btn_rect = QRectF(cx - btn_w * 0.5, h - 62.0, btn_w, btn_h)
        leds_active = (not getattr(self.app_ref, 'is_led_asleep', False)) and getattr(self.app_ref, 'led_power_state', False)

        if leds_active:
            border_c = QColor(46, 204, 113, int(115 * f))
            fill_c   = QColor(20, 52, 36, int(135 * f))
            text_c   = QColor(210, 250, 225, int(165 * f))
            lbl_txt  = "💡 LED : ON  (Éteindre)"
        else:
            border_c = QColor(127, 140, 141, int(85 * f))
            fill_c   = QColor(28, 32, 40, int(125 * f))
            text_c   = QColor(170, 180, 190, int(140 * f))
            lbl_txt  = "🌑 LED : OFF (Allumer)"

        painter.setPen(QPen(border_c, 1.5))
        painter.setBrush(fill_c)
        painter.drawRoundedRect(self.led_btn_rect, 11, 11)
        painter.setPen(text_c)
        painter.drawText(self.led_btn_rect, Qt.AlignmentFlag.AlignCenter, lbl_txt)

    def mousePressEvent(self, event):
        if self.fade_factor > 0.4 and self.led_btn_rect.contains(QPointF(event.pos())):
            self.ledToggleRequested.emit()
            self.update()
            event.accept()
            return

        self.wakeRequested.emit()
        event.accept()

# ============================================================================
# 9. COMPOSANTS GRAPHIQUES OPTIMISÉS (Cache QPixmap + Magnétisme + HUD)
# ============================================================================
class MemoryButton(QPushButton):
    saveRequested = pyqtSignal(int)
    loadRequested = pyqtSignal(int)

    def __init__(self, slot_id):
        super().__init__(f"M{slot_id}")
        self.slot_id = slot_id
        self.setFixedSize(48, 44)
        self.hold_progress = 0.0
        self.is_long_press = False
        self.is_active_slot = False

        self.hold_anim = QVariantAnimation(self)
        self.hold_anim.setDuration(1100)
        self.hold_anim.setStartValue(0.0)
        self.hold_anim.setEndValue(1.0)
        self.hold_anim.valueChanged.connect(self._on_hold_progress)
        self.hold_anim.finished.connect(self._on_hold_finished)
        self.update_style()

    def set_active_slot(self, active: bool):
        if self.is_active_slot != active:
            self.is_active_slot = active
            self.update_style()

    def update_style(self):
        border_col = "#3498db" if self.is_active_slot else "#4a5264"
        bg_col = "#2c3444" if self.is_active_slot else "#252932"
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg_col}; color: white;
                border: 2px solid {border_col}; border-radius: 8px;
                font-weight: bold; font-size: 14px;
            }}
        """)

    def _on_hold_progress(self, val):
        self.hold_progress = float(val)
        self.update()

    def _on_hold_finished(self):
        if self.hold_progress >= 0.99:
            self.is_long_press = True
            self.hold_progress = 0.0
            self.setText("✔")
            self.setStyleSheet("background-color: #27ae60; color: white; border: 2px solid #fff; border-radius: 8px; font-weight: bold; font-size: 16px;")
            self.saveRequested.emit(self.slot_id)
            QTimer.singleShot(800, self.reset_style)

    def mousePressEvent(self, e):
        self.is_long_press = False
        self.hold_anim.start()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        self.hold_anim.stop()
        self.hold_progress = 0.0
        self.update()
        if not self.is_long_press:
            self.loadRequested.emit(self.slot_id)
        super().mouseReleaseEvent(e)

    def reset_style(self):
        self.setText(f"M{self.slot_id}")
        self.update_style()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.hold_progress > 0.02:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            bar_w = int((w - 8) * self.hold_progress)
            painter.setBrush(QColor(46, 204, 113, 220))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(4, h - 7, bar_w, 4, 2, 2)

class SmartSlider(QWidget):
    valueChanged   = pyqtSignal(float)
    userInteracted = pyqtSignal()

    def __init__(self, mode='spectrum'):
        super().__init__()
        self.mode = mode
        self.value = 0.5
        self.vis_value = 0.5
        self.is_dragging = False
        self.knob_scale = 0.0
        self._cached_pixmap = None
        self._cached_mode = None
        self.setMinimumHeight(46)

        self.knob_anim = QVariantAnimation(self)
        self.knob_anim.setDuration(130)
        self.knob_anim.valueChanged.connect(self._on_knob_anim)

        self.val_anim = QVariantAnimation(self)
        self.val_anim.setDuration(180)
        self.val_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.val_anim.valueChanged.connect(self._on_val_anim)

    def set_value_animated(self, target_val: float):
        self.value = float(target_val)
        self.val_anim.stop()
        self.val_anim.setStartValue(self.vis_value)
        self.val_anim.setEndValue(self.value)
        self.val_anim.start()

    def _on_val_anim(self, val):
        self.vis_value = float(val)
        self.update()

    def _on_knob_anim(self, val):
        self.knob_scale = float(val)
        self.update()

    def invalidate_cache(self):
        self._cached_pixmap = None
        self.update()

    def set_mode(self, mode):
        if self.mode != mode:
            self.mode = mode
            self.invalidate_cache()

    def resizeEvent(self, event):
        self.invalidate_cache()
        super().resizeEvent(event)

    def _build_cache(self):
        w, h = self.width(), self.height()
        if w <= 10 or h <= 10:
            return
        pix = QPixmap(w, h)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(6, 5, w - 12, h - 10)
        
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        p.setClipPath(path)

        gradient = QLinearGradient(rect.left(), 0, rect.right(), 0)
        if self.mode == 'spectrum':
            for i in range(13): 
                hue = i / 12.0
                r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
                gradient.setColorAt(hue, QColor(int(r*255), int(g*255), int(b*255)))
        else:
            for i in range(11):
                pos = i / 10.0
                gradient.setColorAt(pos, QColor(*kelvin_to_rgb_calibrated(pos, apply_hw_calib=False)))

        p.fillRect(rect, QBrush(gradient))
        p.setClipping(False)
        p.setPen(QPen(QColor(90, 95, 110), 1.5))
        p.drawRoundedRect(rect, 9, 9)
        p.end()
        self._cached_pixmap = pix
        self._cached_mode = self.mode

    def paintEvent(self, event):
        if self._cached_pixmap is None or self._cached_mode != self.mode:
            self._build_cache()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._cached_pixmap:
            painter.drawPixmap(0, 0, self._cached_pixmap)

        rect = QRectF(self.rect()).adjusted(6, 5, -6, -5)
        if self.mode != 'spectrum':
            kelvin_val = int(1800 + self.vis_value * (6500 - 1800))
            painter.setPen(QColor(20, 20, 25, 220))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{kelvin_val} K")

        pos_x = rect.left() + self.vis_value * rect.width()
        extra_w = 5.0 * self.knob_scale
        knob_rect = QRectF(pos_x - 6 - extra_w * 0.5, rect.top() - 3, 12 + extra_w, rect.height() + 6)
        
        if self.knob_scale > 0.05:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, int(95 * self.knob_scale)))
            painter.drawRoundedRect(knob_rect.adjusted(-4, -2, 4, 2), 8, 8)

        painter.setPen(QPen(QColor(20, 20, 25, 210), 2))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawRoundedRect(knob_rect, 6, 6)

    def mousePressEvent(self, e):
        self.is_dragging = True
        self.val_anim.stop()
        self.knob_anim.stop()
        self.knob_anim.setStartValue(self.knob_scale)
        self.knob_anim.setEndValue(1.0)
        self.knob_anim.start()
        self.update_val(e)

    def mouseMoveEvent(self, e):
        self.update_val(e)

    def mouseReleaseEvent(self, e):
        self.is_dragging = False
        self.knob_anim.stop()
        self.knob_anim.setStartValue(self.knob_scale)
        self.knob_anim.setEndValue(0.0)
        self.knob_anim.start()
        self.userInteracted.emit()

    def update_val(self, e):
        w = max(1, self.width() - 12)
        self.value = max(0.0, min(1.0, (e.pos().x() - 6) / w))
        self.vis_value = self.value
        self.update()
        self.userInteracted.emit()
        self.valueChanged.emit(self.value)

class AnimatedControlPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.accent_color = QColor(52, 152, 219)
        self.open_ratio = 0.0
        self.idle_ratio = 1.0

    def set_accent_color(self, r: int, g: int, b: int):
        self.accent_color = QColor(r, g, b)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = float(self.width())
        painter.fillRect(self.rect(), QColor(24, 27, 34))

        line_w = w * max(0.08, self.open_ratio * self.idle_ratio)
        cx = w * 0.5
        grad = QLinearGradient(cx - line_w * 0.5, 0, cx + line_w * 0.5, 0)
        c_edge = QColor(self.accent_color.red(), self.accent_color.green(), self.accent_color.blue(), 40)
        c_mid  = QColor(self.accent_color.red(), self.accent_color.green(), self.accent_color.blue(), 240)
        grad.setColorAt(0.0, c_edge)
        grad.setColorAt(0.5, c_mid)
        grad.setColorAt(1.0, c_edge)
        painter.fillRect(QRectF(cx - line_w * 0.5, 0, line_w, 3), QBrush(grad))

class TrackWidget(QWidget):
    zonesChanged     = pyqtSignal()
    selectionChanged = pyqtSignal()
    userInteracted   = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.zones = []
        self.active_zone = None
        self.dragging_action = None
        self.drag_start_pos = QPointF()
        self.initial_brightness = 0.0
        self.drag_offset_idx = 0.0
        self.anim_phase = 0.0
        self.ui_pulse = 0.0
        self.transition_progress = 1.0
        self._rail_cache = None

        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(25)
        self.ui_timer.timeout.connect(self._tick_ui)

    def ensure_ui_timer(self):
        if not self.ui_timer.isActive():
            self.ui_timer.start()

    def resizeEvent(self, event):
        self._rail_cache = None
        super().resizeEvent(event)

    def _build_rail_cache(self):
        w, h = self.width(), self.height()
        if w <= 20 or h <= 20:
            return
        pix = QPixmap(w, h)
        pix.fill(Qt.GlobalColor.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin_x = 14.0
        track_w = w - 2 * margin_x
        rail_y, rail_h = h * 0.12, h * 0.76
        rail_rect = QRectF(margin_x, rail_y, track_w, rail_h)

        bg_grad = QLinearGradient(0, rail_y, 0, rail_y + rail_h)
        bg_grad.setColorAt(0, QColor(24, 27, 34))
        bg_grad.setColorAt(1, QColor(34, 38, 48))
        p.setPen(QPen(QColor(55, 62, 76), 1.5))
        p.setBrush(QBrush(bg_grad))
        p.drawRoundedRect(rail_rect, 12, 12)

        px_per_led = track_w / TOTAL_LEDS
        p.setPen(QPen(QColor(70, 78, 95, 105), 1, Qt.PenStyle.DashLine))
        for led_mark in range(36, TOTAL_LEDS, 36):
            mx = margin_x + led_mark * px_per_led
            p.drawLine(int(mx), int(rail_y + 6), int(mx), int(rail_y + rail_h - 6))
        p.end()
        self._rail_cache = pix

    def _tick_ui(self):
        needs_repaint = False
        hw_needs_sync = False
        dead_zones = []

        for z in self.zones:
            if z.step_visual_lerp():
                needs_repaint = True
                hw_needs_sync = True
            if z.is_dead():
                dead_zones.append(z)

        if dead_zones:
            for dz in dead_zones:
                if dz in self.zones:
                    self.zones.remove(dz)
            needs_repaint = True
            hw_needs_sync = True

        if self.active_zone:
            self.ui_pulse = (self.ui_pulse + 0.12) % (2.0 * math.pi)
            needs_repaint = True

        if hw_needs_sync:
            self.zonesChanged.emit()
        if needs_repaint and not self.has_active_animations():
            self.update()

        if not needs_repaint and self.active_zone is None:
            self.ui_timer.stop()

    def _find_best_free_span(self, desired_len: float = 48.0):
        live_zones = sorted([z for z in self.zones if not z.is_closing], key=lambda x: x.start)
        if not live_zones:
            return 0.0, min(float(TOTAL_LEDS), desired_len)

        last_end = live_zones[-1].end
        if last_end + desired_len + 4.0 <= TOTAL_LEDS:
            s = min(float(TOTAL_LEDS) - desired_len, last_end + 4.0)
            return s, s + desired_len

        gaps = []
        prev_end = 0.0
        for z in live_zones:
            if z.start - prev_end >= 12.0:
                gaps.append((z.start - prev_end, prev_end, z.start))
            prev_end = max(prev_end, z.end)
        if TOTAL_LEDS - prev_end >= 12.0:
            gaps.append((TOTAL_LEDS - prev_end, prev_end, float(TOTAL_LEDS)))

        if gaps:
            gaps.sort(reverse=True, key=lambda x: x[0])
            best_len, g_start, g_end = gaps[0]
            span = min(desired_len, best_len)
            mid = (g_start + g_end) * 0.5
            return max(0.0, mid - span * 0.5), min(float(TOTAL_LEDS), mid + span * 0.5)

        mid = TOTAL_LEDS * 0.5
        return mid - 24.0, mid + 24.0

    def add_zone(self):
        s, e = self._find_best_free_span(48.0)
        z = Zone(s, e, animate_spawn=True)
        self.zones.append(z)
        self.select_zone(z)
        self.ensure_ui_timer()
        self.userInteracted.emit()
        self.zonesChanged.emit()

    def expand_or_clone_active_zone(self):
        z = self.active_zone
        if not z or z.is_closing:
            return
        others = [o for o in self.zones if o is not z and not o.is_closing]
        left_bound = max([0.0] + [o.end for o in others if o.end <= z.start + 1.0])
        right_bound = min([float(TOTAL_LEDS)] + [o.start for o in others if o.start >= z.end - 1.0])

        if abs(z.start - left_bound) > 2.0 or abs(z.end - right_bound) > 2.0:
            z.start = left_bound
            z.end   = right_bound
            self.ensure_ui_timer()
            self.userInteracted.emit()
            self.zonesChanged.emit()
        else:
            s, e = self._find_best_free_span(max(24.0, z.end - z.start))
            clone_data = z.to_dict()
            clone_data['start'] = s
            clone_data['end'] = e
            nz = Zone(data=clone_data, animate_spawn=True)
            self.zones.append(nz)
            self.select_zone(nz)
            self.ensure_ui_timer()
            self.userInteracted.emit()
            self.zonesChanged.emit()

    def delete_zone(self):
        if self.active_zone and self.active_zone in self.zones:
            target = self.active_zone
            target.is_closing = True
            target.selected = False
            self.select_zone(None)
            self.ensure_ui_timer()
            self.zonesChanged.emit()

    def select_zone(self, zone):
        if zone and zone.is_closing:
            zone = None
        self.active_zone = zone
        for z in self.zones:
            z.selected = (z == zone)
        if zone is not None:
            self.ensure_ui_timer()
        self.update()
        self.selectionChanged.emit()

    def load_zones(self, data_list):
        old_zones = [z for z in self.zones if not z.is_closing]
        self.zones = []
        for i, d in enumerate(data_list):
            nz = Zone(data=d, animate_spawn=(i >= len(old_zones)))
            if i < len(old_zones):
                nz.vis_start  = old_zones[i].vis_start
                nz.vis_end    = old_zones[i].vis_end
                nz.vis_bright = old_zones[i].vis_bright
            self.zones.append(nz)
        self.select_zone(None)
        self.ensure_ui_timer()
        self.zonesChanged.emit()

    def has_active_animations(self):
        return any(z.effect != "FIXE" for z in self.zones if not z.is_closing)

    def _snap_led_pos(self, val: float, ignore_zone) -> float:
        snap_points = [0.0, float(TOTAL_LEDS)] + [float(m) for m in range(36, TOTAL_LEDS, 36)]
        for o in self.zones:
            if o is not ignore_zone and not o.is_closing:
                snap_points.append(o.start)
                snap_points.append(o.end)
        for sp in snap_points:
            if abs(val - sp) <= 3.0:
                return sp
        return val

    def paintEvent(self, event):
        if self._rail_cache is None:
            self._build_rail_cache()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._rail_cache:
            painter.drawPixmap(0, 0, self._rail_cache)

        w, h = self.width(), self.height()
        margin_x = 14.0
        track_w = w - 2 * margin_x
        rail_y, rail_h = h * 0.12, h * 0.76
        rail_rect = QRectF(margin_x, rail_y, track_w, rail_h)
        px_per_led = track_w / TOTAL_LEDS

        clip_path = QPainterPath()
        clip_path.addRoundedRect(rail_rect, 11, 11)
        painter.setClipPath(clip_path)

        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)

        for z in self.zones:
            x = margin_x + z.vis_start * px_per_led
            wd = max(4.0, (z.vis_end - z.vis_start) * px_per_led)
            fill_h = rail_h * z.vis_bright * self.transition_progress
            active_rect = QRectF(x, rail_y + (rail_h - fill_h), wd, fill_h)
            full_rect = QRectF(x, rail_y, wd, rail_h)
            
            if z.effect == "FIXE" or z.is_closing:
                r, g, b = (255, 70, 50) if z.is_closing else z.get_rgb(for_hardware=False)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(int(r), int(g), int(b), int(45 * (z.vis_bright / max(0.05, z.brightness)) * self.transition_progress)))
                painter.drawRoundedRect(full_rect, 8, 8)
                painter.setBrush(QColor(int(r), int(g), int(b), 215))
                painter.drawRoundedRect(active_rect, 8, 8)
            else:
                grad = QLinearGradient(x, 0, x + wd, 0)
                px_colors = z.compute_pixels(self.anim_phase, for_hardware=False)
                n_c = len(px_colors)
                steps = min(12, n_c)
                for step in range(steps):
                    idx = int(step * (n_c - 1) / max(1, steps - 1))
                    cr, cg, cb = px_colors[idx]
                    grad.setColorAt(step / max(1.0, float(steps - 1)), QColor(cr, cg, cb, 230))
                painter.setPen(Qt.PenStyle.NoPen)
                r, g, b = z.get_rgb(for_hardware=False)
                painter.setBrush(QColor(int(r), int(g), int(b), int(45 * self.transition_progress)))
                painter.drawRoundedRect(full_rect, 8, 8)
                painter.setBrush(QBrush(grad))
                painter.drawRoundedRect(active_rect, 8, 8)

            if z.spawn_glow > 0.03:
                glow_col = QColor(255, 90, 70, int(160 * z.spawn_glow)) if z.is_closing else QColor(180, 245, 255, int(170 * z.spawn_glow))
                painter.setBrush(glow_col)
                painter.drawRoundedRect(active_rect, 8, 8)

        painter.setClipping(False)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        for z in self.zones:
            if z.is_closing:
                continue
            x = margin_x + z.vis_start * px_per_led
            wd = (z.vis_end - z.vis_start) * px_per_led
            if wd > 52:
                pct = int(round(z.brightness * 100))
                led_cnt = int(round(z.end - z.start))
                label_txt = f"{pct}% ({led_cnt})" if z.effect == "FIXE" else f"{pct}% • {z.effect[:4]}"
                painter.setPen(QColor(255, 255, 255, 215))
                painter.drawText(QRectF(x + 4, rail_y + 4, wd - 8, 20), Qt.AlignmentFlag.AlignCenter, label_txt)

        if self.active_zone and not self.active_zone.is_closing:
            z = self.active_zone
            x = margin_x + z.vis_start * px_per_led
            wd = max(8.0, (z.vis_end - z.vis_start) * px_per_led)
            sel_rect = QRectF(x, rail_y, wd, rail_h)
            pulse_alpha = int(185 + 70 * math.sin(self.ui_pulse))
            
            r, g, b = z.get_rgb(for_hardware=False)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(r, g, b, 110))
            painter.drawRoundedRect(QRectF(x + 6, rail_y - 5, max(4.0, wd - 12), 4), 2, 2)

            painter.setPen(QPen(QColor(255, 215, 0, pulse_alpha), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(sel_rect, 9, 9)
            
            handle_h = rail_h * 0.42
            hy = rail_y + (rail_h - handle_h) / 2.0
            
            col_left = QColor(0, 229, 255, 255) if self.dragging_action == 'left' else QColor(255, 215, 0, pulse_alpha)
            col_right = QColor(0, 229, 255, 255) if self.dragging_action == 'right' else QColor(255, 215, 0, pulse_alpha)
            
            w_l = 9.0 if self.dragging_action == 'left' else 6.0
            w_r = 9.0 if self.dragging_action == 'right' else 6.0

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(col_left)
            painter.drawRoundedRect(QRectF(x - w_l * 0.5, hy, w_l, handle_h), 3, 3)
            painter.setBrush(col_right)
            painter.drawRoundedRect(QRectF(x + wd - w_r * 0.5, hy, w_r, handle_h), 3, 3)

            if self.dragging_action in ('left', 'right', 'move', 'brightness'):
                s_idx, e_idx = int(round(z.start)), int(round(z.end))
                pct = int(round(z.brightness * 100))
                hud_txt = f"LED {s_idx} → {e_idx}  ({e_idx - s_idx} px)  •  Intensité {pct}%"
                hud_w = 260.0
                hud_x = max(margin_x, min(w - margin_x - hud_w, x + wd * 0.5 - hud_w * 0.5))
                hud_rect = QRectF(hud_x, max(2.0, rail_y - 24.0), hud_w, 20.0)
                painter.setBrush(QColor(18, 22, 30, 235))
                painter.setPen(QPen(QColor(0, 229, 255, 200), 1.2))
                painter.drawRoundedRect(hud_rect, 6, 6)
                painter.setPen(QColor(255, 255, 255))
                painter.drawText(hud_rect, Qt.AlignmentFlag.AlignCenter, hud_txt)

    def _x_to_led(self, x_pos: float) -> float:
        margin_x = 14.0
        track_w = max(1.0, self.width() - 2 * margin_x)
        return max(0.0, min(float(TOTAL_LEDS), (x_pos - margin_x) * TOTAL_LEDS / track_w))

    def mousePressEvent(self, event):
        self.userInteracted.emit()
        click_x = float(event.pos().x())
        idx = self._x_to_led(click_x)
        margin_x = 14.0
        ppl = max(0.1, (self.width() - 2 * margin_x) / TOTAL_LEDS)
        handle_tol_px = 32.0

        live_zones = [z for z in self.zones if not z.is_closing]
        target = None

        if self.active_zone and not self.active_zone.is_closing:
            zs_px = margin_x + self.active_zone.start * ppl
            ze_px = margin_x + self.active_zone.end * ppl
            if (zs_px - handle_tol_px) <= click_x <= (ze_px + handle_tol_px):
                target = self.active_zone

        if not target:
            for z in reversed(live_zones):
                zs_px = margin_x + z.start * ppl
                ze_px = margin_x + z.end * ppl
                if (zs_px - handle_tol_px * 0.65) <= click_x <= (ze_px + handle_tol_px * 0.65):
                    target = z
                    break

        if not target and self.active_zone is None and live_zones:
            if len(live_zones) == 1:
                target = live_zones[0]
            else:
                best_d = 70.0
                for z in live_zones:
                    zs_px = margin_x + z.start * ppl
                    ze_px = margin_x + z.end * ppl
                    d = min(abs(click_x - zs_px), abs(click_x - ze_px))
                    if d < best_d:
                        best_d = d
                        target = z
        
        if target:
            self.select_zone(target)
            self.drag_start_pos = event.pos()
            
            z_s = margin_x + target.start * ppl
            z_e = margin_x + target.end * ppl
            dist_left  = abs(click_x - z_s)
            dist_right = abs(click_x - z_e)
            
            if min(dist_left, dist_right) <= handle_tol_px:
                self.dragging_action = 'right' if dist_right <= dist_left else 'left'
            else:
                self.dragging_action = 'undecided' 
                self.drag_offset_idx = idx - target.start
                self.initial_brightness = target.brightness
            self.ensure_ui_timer()
        else:
            self.select_zone(None)
            self.dragging_action = None

    def mouseMoveEvent(self, event):
        if not self.active_zone or not self.dragging_action or self.active_zone.is_closing:
            return
        
        self.userInteracted.emit()
        curr = self._x_to_led(float(event.pos().x()))
        z = self.active_zone

        if self.dragging_action == 'undecided':
            dx = abs(event.pos().x() - self.drag_start_pos.x())
            dy = abs(event.pos().y() - self.drag_start_pos.y())
            if dx > 5 or dy > 5:
                self.dragging_action = 'brightness' if dy > dx else 'move'

        if self.dragging_action == 'left':
            snapped = self._snap_led_pos(curr, z)
            z.start = max(0.0, min(z.end - 4.0, snapped))
            z.vis_start = z.start
        elif self.dragging_action == 'right':
            snapped = self._snap_led_pos(curr, z)
            z.end = min(float(TOTAL_LEDS), max(z.start + 4.0, snapped))
            z.vis_end = z.end
        elif self.dragging_action == 'move':
            l = z.end - z.start
            raw_s = max(0.0, min(float(TOTAL_LEDS) - l, curr - self.drag_offset_idx))
            snapped_s = self._snap_led_pos(raw_s, z)
            if abs(snapped_s - raw_s) <= 2.5 and snapped_s + l <= TOTAL_LEDS:
                ns = snapped_s
            else:
                snapped_e = self._snap_led_pos(raw_s + l, z)
                ns = (snapped_e - l) if (abs(snapped_e - (raw_s + l)) <= 2.5 and snapped_e - l >= 0.0) else raw_s
            z.start = ns
            z.end = ns + l
            z.vis_start = z.start
            z.vis_end = z.end
        elif self.dragging_action == 'brightness':
            dy = self.drag_start_pos.y() - event.pos().y()
            z.brightness = max(0.02, min(1.0, self.initial_brightness + dy / (self.height() * 0.55)))
            z.vis_bright = z.brightness

        self.ensure_ui_timer()
        self.zonesChanged.emit()

    def mouseReleaseEvent(self, e):
        if self.dragging_action:
            self.dragging_action = None
            self.userInteracted.emit()
            self.ensure_ui_timer()

class PresenceMonitor(QThread):
    statusChanged = pyqtSignal(bool) 
    def run(self):
        if not HARDWARE_ACTIVE or radar_sensor is None:
            return
        last_state = -1
        while True:
            try:
                state = int(radar_sensor.value)
                if state != last_state:
                    time.sleep(0.12)
                    if int(radar_sensor.value) == state:
                        self.statusChanged.emit(bool(state))
                        last_state = state
            except Exception:
                pass
            time.sleep(0.25)

# ============================================================================
# 10. APPLICATION PRINCIPALE (Initialisation Séquentielle & Arrêt Synchrone)
# ============================================================================
class LedApp(QMainWindow):
    PANEL_TARGET_HEIGHT = 86

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Barre LED & Domotique")
        self.node_id = f"desk_{os.getpid()}"
        self._hw_shutting_down  = False
        
        self.overlay_open       = False
        self.current_cpu_temp   = 40.0
        self.current_fan_speed  = 0.0
        self.actual_fan_output  = 0.0
        self.radar_detected_now = True
        self.led_power_state    = False
        self.is_led_asleep      = False
        self.is_screen_asleep   = False
        self._last_radar_style  = None
        self.panel_target_open  = False
        self.t0_anim            = time.monotonic()
        self.trans_progress     = 1.0
        self.trans_opening      = True

        self.full_config = self.load_json_file()
        sys_cfg = self.full_config.get("settings", {})
        self.radar_enabled       = sys_cfg.get("radar_enabled", True)
        self.radar_timeout       = sys_cfg.get("radar_timeout", 60)
        self.relay_delay_min     = sys_cfg.get("relay_delay_min", 15)
        self.transition_style    = sys_cfg.get("transition_style", "FONDU")
        self.transition_duration = sys_cfg.get("transition_duration", 1.2)
        self.screen_timeout      = sys_cfg.get("screen_timeout", 60)
        self.screen_sleep_mode   = sys_cfg.get("screen_sleep_mode", "ICÔNE VEILLE")
        self.panel_autoclose_s   = sys_cfg.get("panel_autoclose_s", 8)
        self.fan_auto            = sys_cfg.get("fan_auto", True)
        self.fan_manual_pct      = sys_cfg.get("fan_manual_pct", 75)
        self.sync_mode           = sys_cfg.get("sync_mode", "INDÉPENDANT")

        ColorCalibration.gain_r  = sys_cfg.get("cal_r", 1.00)
        ColorCalibration.gain_g  = sys_cfg.get("cal_g", 0.82)
        ColorCalibration.gain_b  = sys_cfg.get("cal_b", 0.58)
        ColorCalibration.rebuild_lut()

        self.led_worker = LedHardwareWorker()
        self.led_worker.start()

        self.sync_worker = DeskSyncWorker()
        self.sync_worker.remoteZonesReceived.connect(self.on_remote_sync_received)
        self.sync_worker.start()

        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet("background-color: #111318;")
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # --- HEADER COMPACT ---
        header = QHBoxLayout()
        header.setContentsMargins(8, 6, 8, 2)
        header.setSpacing(6)
        
        self.mem_buttons = {}
        for i in range(1, 4):
            btn = MemoryButton(i)
            btn.saveRequested.connect(self.save_memory)
            btn.loadRequested.connect(self.load_memory)
            header.addWidget(btn)
            self.mem_buttons[i] = btn

        self.btn_temp_mem = QPushButton("↺ TEMP")
        self.btn_temp_mem.setFixedSize(68, 44)
        self.btn_temp_mem.clicked.connect(self.restore_temp_memory)
        self.update_temp_btn_style("temp_backup" in self.full_config and bool(self.full_config["temp_backup"]))
        header.addWidget(self.btn_temp_mem)

        self.btn_led_master = QPushButton("🔆 LED ON")
        self.btn_led_master.setFixedSize(94, 44)
        self.btn_led_master.clicked.connect(self.toggle_master_leds)
        header.addWidget(self.btn_led_master)
            
        header.addStretch() 
        
        self.btn_radar_status = QPushButton("📡 RADAR ●")
        self.btn_radar_status.setFixedHeight(38)
        self.btn_radar_status.clicked.connect(self.toggle_radar_mode_quick)
        header.addWidget(self.btn_radar_status)

        badge_style = """
            QLabel {
                background-color: #1c2029; color: #ecf0f1;
                font-size: 13px; font-weight: bold;
                padding: 0px 8px; border-radius: 8px;
                border: 1px solid #2d3342;
            }
        """
        self.lbl_unraid = QLabel("UNRAID ●")
        self.lbl_unraid.setFixedHeight(38)
        self.lbl_unraid.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_unraid.setStyleSheet(badge_style)
        header.addWidget(self.lbl_unraid)
        
        self.lbl_awair = QLabel("CO2: -- ppm | --°C")
        self.lbl_awair.setFixedHeight(38)
        self.lbl_awair.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_awair.setStyleSheet(badge_style)
        header.addWidget(self.lbl_awair)

        self.lbl_clock = QLabel("00:00")
        self.lbl_clock.setFixedHeight(38)
        self.lbl_clock.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_clock.setStyleSheet("color: white; font-size: 22px; font-weight: bold; margin: 0 4px;")
        header.addWidget(self.lbl_clock)

        self.clim_bureau_state = False
        self.clim_target_temp  = 22
        self.btn_clim = QPushButton("❄️ Clim")
        self.btn_clim.setFixedSize(82, 44)
        self.btn_clim.clicked.connect(self.toggle_clim_bureau)
        self.update_clim_btn_style()
        header.addWidget(self.btn_clim)
        
        self.hue_bureau_state = False
        self.btn_hue = QPushButton("💡 Hue")
        self.btn_hue.setFixedSize(80, 44)
        self.btn_hue.clicked.connect(self.toggle_hue_bureau)
        self.update_hue_btn_style()
        header.addWidget(self.btn_hue)

        self.nanoleaf_state = False
        self.btn_nanoleaf = QPushButton("Nanoleaf")
        self.btn_nanoleaf.setFixedSize(84, 44)
        self.btn_nanoleaf.clicked.connect(self.toggle_nanoleaf)
        self.update_nanoleaf_btn_style()
        header.addWidget(self.btn_nanoleaf)

        self.sonoff_appt_state = False

        btn_settings = QPushButton("⚙")
        btn_settings.setFixedSize(44, 44)
        btn_settings.setStyleSheet("background-color:#252932; color:white; font-size:20px; border-radius:8px; border: 1px solid #3d4659;")
        btn_settings.clicked.connect(self.open_settings)
        header.addWidget(btn_settings)
        
        main_layout.addLayout(header)

        # --- PISTE LED ---
        self.track = TrackWidget()
        self.track.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        main_layout.addWidget(self.track, stretch=1)

        # --- PANNEAU DE CONTRÔLE DE ZONE ANIMÉ (BAS) ---
        self.panel = AnimatedControlPanel()
        self.panel.setFixedHeight(0)
        self.panel.setVisible(False)
        panel_layout = QHBoxLayout(self.panel)
        panel_layout.setContentsMargins(10, 6, 10, 6)
        panel_layout.setSpacing(8)
        
        vbox_left = QVBoxLayout()
        vbox_left.setSpacing(4)
        self.btn_mode = QPushButton("RGB")
        self.btn_mode.setFixedSize(86, 34)
        self.btn_mode.setStyleSheet("background-color:#353b48; color:white; font-weight:bold; font-size:12px; border-radius:6px;")
        self.btn_mode.clicked.connect(self.toggle_mode)
        
        btn_del = QPushButton("🗑 SUPPR")
        btn_del.setFixedSize(86, 34)
        btn_del.setStyleSheet("background-color:#a82020; color:white; font-weight:bold; font-size:12px; border-radius:6px;")
        btn_del.clicked.connect(self.track.delete_zone)
        vbox_left.addWidget(self.btn_mode)
        vbox_left.addWidget(btn_del)
        panel_layout.addLayout(vbox_left)

        vbox_fx = QVBoxLayout()
        vbox_fx.setSpacing(4)
        self.btn_effect = QPushButton("✨ FIXE")
        self.btn_effect.setFixedSize(130, 34)
        self.btn_effect.setStyleSheet("background-color:#2980b9; color:white; font-weight:bold; font-size:12px; border-radius:6px;")
        self.btn_effect.clicked.connect(self.cycle_effect)

        self.btn_speed = QPushButton("⚡ NORMAL")
        self.btn_speed.setFixedSize(130, 34)
        self.btn_speed.setStyleSheet("background-color:#2c3e50; color:white; font-weight:bold; font-size:12px; border-radius:6px;")
        self.btn_speed.clicked.connect(self.cycle_speed)
        vbox_fx.addWidget(self.btn_effect)
        vbox_fx.addWidget(self.btn_speed)
        panel_layout.addLayout(vbox_fx)

        vbox_tools = QVBoxLayout()
        vbox_tools.setSpacing(4)
        self.btn_expand_clone = QPushButton("↔ ÉTENDRE")
        self.btn_expand_clone.setFixedSize(98, 72)
        self.btn_expand_clone.setStyleSheet("background-color:#16a085; color:white; font-weight:bold; font-size:12px; border-radius:6px;")
        self.btn_expand_clone.clicked.connect(self.on_expand_or_clone_clicked)
        vbox_tools.addWidget(self.btn_expand_clone)
        panel_layout.addLayout(vbox_tools)
        
        self.slider = SmartSlider()
        self.slider.valueChanged.connect(self.on_slider_change)
        self.slider.userInteracted.connect(self.reset_panel_idle_timer)
        panel_layout.addWidget(self.slider, stretch=1)

        btn_hide_panel = QPushButton("✖")
        btn_hide_panel.setFixedSize(36, 72)
        btn_hide_panel.setStyleSheet("background-color:#252932; color:#95a5a6; font-weight:bold; font-size:16px; border-radius:6px; border: 1px solid #323846;")
        btn_hide_panel.clicked.connect(lambda: self.track.select_zone(None))
        panel_layout.addWidget(btn_hide_panel)

        main_layout.addWidget(self.panel)

        self.btn_add = QPushButton("+", self.track)
        self.btn_add.setGeometry(22, 14, 50, 50)
        self.btn_add.setStyleSheet("""
            QPushButton {
                background-color: #27ae60; color: white;
                border-radius: 25px; font-size: 26px; font-weight: bold;
                border: 2px solid #2ecc71;
            }
            QPushButton:pressed { background-color: #1e8449; }
        """)
        self.btn_add.clicked.connect(self.track.add_zone)
        self.btn_add.show()

        self.settings_overlay = SettingsOverlay(self)
        self.screensaver = ScreenSaverOverlay(self)
        self.screensaver.wakeRequested.connect(self.wake_up_screen)
        self.screensaver.ledToggleRequested.connect(self.toggle_master_leds)
        self.screensaver.fadeInFinished.connect(self._on_screensaver_faded_in)

        self.panel_anim = QVariantAnimation(self)
        self.panel_anim.setDuration(210)
        self.panel_anim.valueChanged.connect(self._on_panel_anim)
        self.panel_anim.finished.connect(self._on_panel_anim_finished)

        self.panel_idle_timer = QTimer(self)
        self.panel_idle_timer.setSingleShot(True)
        self.panel_idle_timer.timeout.connect(self._auto_collapse_panel)

        self.panel_gauge_timer = QTimer(self)
        self.panel_gauge_timer.setInterval(80)
        self.panel_gauge_timer.timeout.connect(self._tick_panel_gauge)

        # --- TIMERS & THREADS ---
        self.track.zonesChanged.connect(self.on_zones_modified)
        self.track.selectionChanged.connect(self.update_gui_from_selection)
        self.track.userInteracted.connect(self.reset_panel_idle_timer)
        
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hw_update)
        self.dirty = False

        self.trans_anim = QVariantAnimation(self)
        self.trans_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.trans_anim.valueChanged.connect(self._on_transition_step)
        self.trans_anim.finished.connect(self._on_transition_finished)

        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.autosave_timer.setInterval(400)
        self.autosave_timer.timeout.connect(self.perform_autosave)

        self.anim_timer = QTimer(self)
        self.anim_timer.setInterval(30)
        self.anim_timer.timeout.connect(self.anim_step)
        
        self.standby_timer = QTimer(self)
        self.standby_timer.setSingleShot(True)
        self.standby_timer.timeout.connect(self.enter_led_sleep)

        self.relay_cutoff_timer = QTimer(self)
        self.relay_cutoff_timer.setSingleShot(True)
        self.relay_cutoff_timer.timeout.connect(self.cut_relay_power)

        self.screen_idle_timer = QTimer(self)
        self.screen_idle_timer.setSingleShot(True)
        self.screen_idle_timer.timeout.connect(self.enter_screen_sleep)

        self.kickstart_timer = QTimer(self)
        self.kickstart_timer.setSingleShot(True)
        self.kickstart_timer.timeout.connect(self._finish_fan_kickstart)
        
        if "temp_live" in self.full_config and self.full_config["temp_live"]:
            self.track.load_zones(self.full_config["temp_live"])
        elif "1" in self.full_config:
            self.track.load_zones(self.full_config["1"])

        if HARDWARE_ACTIVE and radar_sensor is not None:
            try:
                self.radar_detected_now = bool(radar_sensor.value)
            except Exception:
                pass

        self.set_led_power(True)
        self.update_led_master_btn_style()
        self.update_radar_badge()
        self.reset_user_activity_timer()

        QApplication.instance().installEventFilter(self)

        self.radar = PresenceMonitor()
        self.radar.statusChanged.connect(self.on_presence)
        self.radar.start()

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000) 
        self.update_clock() 

        self.api_worker = HomeyAPIWorker()
        self.api_worker.data_updated.connect(self.process_homey_data)
        self.api_worker.start()

        self.thermal_timer = QTimer(self)
        self.thermal_timer.timeout.connect(self.update_thermals)
        self.thermal_timer.start(3000)
        self.update_thermals()

        self.resize(1480, 320)
        self.settings_overlay.setGeometry(self.rect())
        self.screensaver.setGeometry(self.rect())
        if HARDWARE_ACTIVE:
            self.setCursor(Qt.CursorShape.BlankCursor)
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'screensaver') and self.screensaver is not None:
            self.screensaver.setGeometry(self.rect())
        if hasattr(self, 'settings_overlay') and self.settings_overlay is not None:
            self.settings_overlay.setGeometry(self.rect())

    def on_remote_sync_received(self, payload):
        sender_id, remote_zones = payload
        if sender_id != self.node_id and self.sync_mode == "SYNC ESCLAVE":
            self.track.load_zones(remote_zones)

    def update_led_master_btn_style(self):
        leds_on = (not self.is_led_asleep) and self.led_power_state
        if leds_on:
            self.btn_led_master.setText("🔆 LED ON")
            self.btn_led_master.setStyleSheet("""
                QPushButton {
                    background-color: #1e8449; color: white;
                    border: 2px solid #2ecc71; border-radius: 8px;
                    font-weight: bold; font-size: 13px;
                }
            """)
        else:
            self.btn_led_master.setText("🌑 LED OFF")
            self.btn_led_master.setStyleSheet("""
                QPushButton {
                    background-color: #252932; color: #95a5a6;
                    border: 2px solid #4a5264; border-radius: 8px;
                    font-weight: bold; font-size: 13px;
                }
            """)
        if hasattr(self, 'screensaver') and self.screensaver.fade_factor > 0.01:
            self.screensaver.update()

    def toggle_master_leds(self):
        leds_on = (not self.is_led_asleep) and self.led_power_state
        if leds_on:
            self.enter_led_sleep(manual=True)
        else:
            self.wake_up_leds()

    def update_radar_badge(self):
        if not self.radar_enabled:
            state_key = "MANUEL"
            txt = "📡 MANUEL ○"
            style = "background-color: #252932; color: #95a5a6; border: 1px solid #3d4659;"
        elif self.is_led_asleep:
            state_key = "VEILLE"
            txt = "📡 VEILLE ☾"
            style = "background-color: #1c2029; color: #3498db; border: 1px solid #2980b9;"
        elif self.radar_detected_now:
            state_key = "PRESENT"
            txt = "📡 RADAR ●"
            style = "background-color: #143625; color: #2ecc71; border: 1px solid #27ae60;"
        else:
            state_key = "ABSENT"
            rem_s = int(math.ceil(self.standby_timer.remainingTime() / 1000.0)) if self.standby_timer.isActive() else 0
            txt = f"📡 ABSENT ({rem_s}s)"
            style = "background-color: #3d2814; color: #f39c12; border: 1px solid #e67e22;"

        self.btn_radar_status.setText(txt)
        if self._last_radar_style != state_key:
            self._last_radar_style = state_key
            base = "font-size: 12px; font-weight: bold; padding: 0px 9px; border-radius: 8px;"
            self.btn_radar_status.setStyleSheet(f"{style} {base}")

    def toggle_radar_mode_quick(self):
        self.radar_enabled = not self.radar_enabled
        if not self.radar_enabled:
            self.standby_timer.stop()
            self.relay_cutoff_timer.stop()
            if self.is_led_asleep:
                self.wake_up_leds()
        else:
            if not self.radar_detected_now and not self.is_led_asleep:
                self.standby_timer.start(self.radar_timeout * 1000)
        self.update_radar_badge()
        self.save_config()

    def update_thermals(self):
        if HARDWARE_ACTIVE and cpu:
            self.current_cpu_temp = cpu.temperature
        self.apply_fan_speed(force_kickstart=False)

    def apply_fan_speed(self, force_kickstart: bool = False):
        if self.fan_auto:
            if self.current_cpu_temp < 45.0:
                target = 0.0
            elif self.current_cpu_temp > 65.0:
                target = 1.0
            else:
                target = 0.50 + 0.50 * ((self.current_cpu_temp - 45.0) / 20.0)
        else:
            target = self.fan_manual_pct / 100.0

        self.current_fan_speed = target
        needs_boost = (target > 0.0 and target < 1.0) and (self.actual_fan_output == 0.0 or force_kickstart)

        if needs_boost:
            self.actual_fan_output = 1.0
            if HARDWARE_ACTIVE and fan:
                fan.value = 1.0
            self.kickstart_timer.start(2000)
            return

        if self.kickstart_timer.isActive():
            if target == 0.0:
                self.kickstart_timer.stop()
                self.actual_fan_output = 0.0
                if HARDWARE_ACTIVE and fan:
                    fan.value = 0.0
            return

        self.actual_fan_output = target
        if HARDWARE_ACTIVE and fan:
            fan.value = target

    def _finish_fan_kickstart(self):
        self.actual_fan_output = self.current_fan_speed
        if HARDWARE_ACTIVE and fan:
            fan.value = self.current_fan_speed

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove, QEvent.Type.TouchBegin):
            if not self.is_screen_asleep:
                self.reset_user_activity_timer()
        return super().eventFilter(obj, event)

    def reset_user_activity_timer(self):
        if self.screen_timeout > 0 and not self.is_screen_asleep:
            self.screen_idle_timer.start(self.screen_timeout * 1000)
        else:
            self.screen_idle_timer.stop()

    def enter_screen_sleep(self, force: bool = False):
        if self.is_screen_asleep or (self.screen_timeout == 0 and not force):
            return
        self.is_screen_asleep = True
        self.track.select_zone(None)
        self.screensaver.start_saver(self.screen_sleep_mode)

    def _on_screensaver_faded_in(self):
        if self.is_screen_asleep and self.screen_sleep_mode == "HDMI OFF":
            self.set_screen_power(False)

    def wake_up_screen(self):
        if not self.is_screen_asleep:
            return
        self.is_screen_asleep = False
        self.set_screen_power(True)
        self.screensaver.stop_saver()
        self.reset_user_activity_timer()

    def set_screen_power(self, on: bool):
        def _power_worker():
            uid = os.environ.get("SUDO_UID", "1000")
            env = os.environ.copy()
            env.setdefault("DISPLAY", ":0")
            env.setdefault("WAYLAND_DISPLAY", "wayland-0")
            env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
            if on:
                cmds = [
                    ["wlopm", "--on", "*"],
                    ["xset", "-display", ":0", "dpms", "force", "on"],
                    ["wlr-randr", "--output", "HDMI-A-1", "--on"],
                    ["vcgencmd", "display_power", "1"]
                ]
            else:
                cmds = [
                    ["wlopm", "--off", "*"],
                    ["xset", "-display", ":0", "+dpms"],
                    ["xset", "-display", ":0", "dpms", "force", "off"],
                    ["wlr-randr", "--output", "HDMI-A-1", "--off"],
                    ["vcgencmd", "display_power", "0"]
                ]
            for cmd in cmds:
                try:
                    subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1.5)
                except Exception:
                    pass
        threading.Thread(target=_power_worker, daemon=True).start()

    # ========================================================================
    # PILOTAGE DU RELAIS 12V & SÉQUENCE D'EXTINCTION PRÉALABLE DES LEDS
    # ========================================================================
    def set_led_power(self, on: bool):
        was_off = not self.led_power_state
        self.led_power_state = on
        set_relay_hw_pin(on)

        if on:
            self.relay_cutoff_timer.stop()
            if was_off:
                # Rafale d'amorçage pendant que le driver 12V stabilise son rail
                for delay_ms in (180, 480, 900, 1400):
                    QTimer.singleShot(delay_ms, self.force_hw_refresh)
                QTimer.singleShot(250, lambda: self.start_led_transition(opening=True))
            else:
                self.start_led_transition(opening=True)
        self.update_led_master_btn_style()

    def cut_led_and_relay_safely(self):
        """Éteint d'abord toutes les LEDs (noir), attend 180 ms, puis ouvre le relais en Haute-Impédance"""
        self.anim_timer.stop()
        self.timer.stop()
        self.trans_anim.stop()
        if LEDS_DMA_READY and pixels is not None:
            try:
                pixels.fill((0, 0, 0))
                pixels.show()
            except Exception:
                pass
        QTimer.singleShot(180, lambda: self.set_led_power(False))

    def start_led_transition(self, opening: bool):
        self.trans_opening = opening
        self.trans_anim.stop()
        self.trans_anim.setDuration(int(self.transition_duration * 1000))
        self.trans_anim.setStartValue(self.trans_progress)
        self.trans_anim.setEndValue(1.0 if opening else 0.0)
        self.trans_anim.start()

    def _on_transition_step(self, val):
        self.trans_progress = float(val)
        self.track.transition_progress = self.trans_progress
        self.track.update()
        self.dirty = True
        self.hw_update()

    def _on_transition_finished(self):
        if not self.trans_opening and self.is_led_asleep:
            self.led_worker.push_frame([(0, 0, 0)] * TOTAL_LEDS)
        else:
            self.on_zones_modified()

    def compute_spatial_mask(self, led_idx: int) -> float:
        p = self.trans_progress
        if p >= 0.999:
            return 1.0
        if p <= 0.001:
            return 0.0

        style = self.transition_style
        if style == "FONDU":
            return p

        pos = led_idx / float(max(1, TOTAL_LEDS - 1))
        if style == "BALAYAGE G→D":
            norm_pos = pos if self.trans_opening else (1.0 - pos)
        elif style == "BALAYAGE D→G":
            norm_pos = (1.0 - pos) if self.trans_opening else pos
        elif style == "RIDEAU CENTRE":
            dist_center = abs(pos - 0.5) * 2.0
            norm_pos = dist_center if self.trans_opening else (1.0 - dist_center)
        else:
            return p

        width = 0.18
        edge = p * (1.0 + width)
        return max(0.0, min(1.0, (edge - norm_pos) / width))

    def on_presence(self, detected: bool):
        self.radar_detected_now = detected
        if not self.radar_enabled:
            self.update_radar_badge()
            return
        if detected:
            if self.standby_timer.isActive():
                self.standby_timer.stop()
            if self.relay_cutoff_timer.isActive():
                self.relay_cutoff_timer.stop()
            if self.is_led_asleep:
                self.wake_up_leds()
            if self.is_screen_asleep:
                self.wake_up_screen()
        else:
            if not self.is_led_asleep and not self.standby_timer.isActive():
                self.standby_timer.start(self.radar_timeout * 1000)
        self.update_radar_badge()

    def wake_up_leds(self):
        self.is_led_asleep = False
        self.relay_cutoff_timer.stop()
        if not self.led_power_state:
            self.set_led_power(True)
        else:
            self.start_led_transition(opening=True)
        self.update_led_master_btn_style()
        self.update_radar_badge()

    def enter_led_sleep(self, manual: bool = False):
        if (not self.radar_enabled and not manual) or self.is_led_asleep:
            return
        self.is_led_asleep = True
        self.anim_timer.stop()
        self.track.select_zone(None)
        self.start_led_transition(opening=False)
        self.relay_cutoff_timer.start(self.relay_delay_min * 60 * 1000)
        self.update_led_master_btn_style()
        self.update_radar_badge()

    def cut_relay_power(self):
        if self.is_led_asleep:
            self.cut_led_and_relay_safely()
            self.enter_screen_sleep(force=True)

    def force_hw_refresh(self):
        if not self.is_led_asleep and self.led_power_state:
            self.dirty = True
            self.hw_update()

    def on_zones_modified(self):
        self.autosave_timer.start()
        self.update_expand_clone_btn_label()
        if self.sync_mode == "SYNC MAÎTRE":
            live = [z.to_dict() for z in self.track.zones if not z.is_closing]
            self.sync_worker.broadcast_zones(live, self.node_id)
        if self.track.has_active_animations() and not self.is_led_asleep:
            if not self.anim_timer.isActive():
                self.anim_timer.start()
        else:
            self.anim_timer.stop()
            self.request_hw_update()

    def anim_step(self):
        if self.is_led_asleep or not self.led_power_state:
            return
        self.track.anim_phase = time.monotonic() - self.t0_anim
        if not self.is_screen_asleep:
            self.track.update()
        self.dirty = True
        self.hw_update()

    def request_hw_update(self):
        self.dirty = True
        if not self.timer.isActive():
            self.timer.start(16)

    def hw_update(self):
        if not self.dirty or not self.led_power_state or self._hw_shutting_down:
            return 
        phase = time.monotonic() - self.t0_anim
        buf = [(0, 0, 0)] * TOTAL_LEDS
        
        for z in self.track.zones:
            s = max(0, int(round(z.vis_start)))
            e = min(TOTAL_LEDS, int(round(z.vis_end)))
            if e <= s:
                continue
            px_list = z.compute_pixels(phase, for_hardware=True)
            for idx, (r, g, b) in enumerate(px_list):
                pos = s + idx
                if pos < e and pos < TOTAL_LEDS:
                    cr, cg, cb = buf[pos]
                    buf[pos] = (min(255, cr + r), min(255, cg + g), min(255, cb + b))

        if self.trans_progress < 0.999:
            for i in range(TOTAL_LEDS):
                r, g, b = buf[i]
                if r or g or b:
                    m = self.compute_spatial_mask(i)
                    buf[i] = (int(r * m), int(g * m), int(b * m))
                    
        self.led_worker.push_frame(buf)
        self.dirty = False

    def send_homey_capability(self, dev_id: str, capability: str, value):
        def _worker():
            conn = None
            try:
                conn = http.client.HTTPConnection(HOMEY_IP, timeout=2.5)
                payload = json.dumps({"value": value}).encode('utf-8')
                headers = {
                    "Authorization": f"Bearer {HOMEY_API_KEY}",
                    "Content-Type": "application/json"
                }
                conn.request("PUT", f"/api/manager/devices/device/{dev_id}/capability/{capability}", body=payload, headers=headers)
                conn.getresponse().read()
            except Exception as e:
                print(f"Erreur API Homey ({capability}) : {e}")
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass
        threading.Thread(target=_worker, daemon=True).start()

    def process_homey_data(self, data):
        if "awair" in data:
            aw = data["awair"]
            co2 = aw.get("measure_co2", {}).get("value")
            tmp = aw.get("measure_temperature", {}).get("value")
            if co2 is not None:
                c_dot = "●" if co2 < 800 else "▲" if co2 < 1200 else "■"
                t_str = f" | {tmp:.1f}°C" if tmp is not None else ""
                self.lbl_awair.setText(f"CO2: {int(co2)} ppm {c_dot}{t_str}")
                
        if "unraid" in data:
            u_data = data["unraid"]
            arr_status = u_data.get("array_status", {}).get("value")
            dsk_health = u_data.get("disk_health", {}).get("value")
            cpu_temp   = u_data.get("cpu_temperature", {}).get("value")
            
            temp_str = f" {int(cpu_temp)}°C" if cpu_temp is not None else ""
            if arr_status == "started" and dsk_health == "healthy":
                self.lbl_unraid.setText(f"UNRAID{temp_str} ●")
            elif arr_status == "started":
                self.lbl_unraid.setText(f"UNRAID{temp_str} ▲")
            else:
                self.lbl_unraid.setText("UNRAID ■")

        if "clim_bureau" in data:
            c_data = data["clim_bureau"]
            new_clim = bool(c_data.get("onoff", {}).get("value"))
            t_target = c_data.get("target_temperature", {}).get("value")
            if t_target is not None:
                self.clim_target_temp = int(round(t_target))
            self.clim_bureau_state = new_clim
            self.update_clim_btn_style()

        if "nanoleaf" in data and "onoff" in data["nanoleaf"]:
            new_nano = bool(data["nanoleaf"]["onoff"].get("value"))
            if new_nano != self.nanoleaf_state:
                self.nanoleaf_state = new_nano
                self.update_nanoleaf_btn_style()

        if "hue_bureau" in data and "onoff" in data["hue_bureau"]:
            new_hue = bool(data["hue_bureau"]["onoff"].get("value"))
            if new_hue != self.hue_bureau_state:
                self.hue_bureau_state = new_hue
                self.update_hue_btn_style()

        if "sonoff_appt" in data and "onoff" in data["sonoff_appt"]:
            self.sonoff_appt_state = bool(data["sonoff_appt"]["onoff"].get("value"))

    def update_clim_btn_style(self):
        bg = "#2980b9" if self.clim_bureau_state else "#252932"
        border = "#3498db" if self.clim_bureau_state else "#3d4659"
        self.btn_clim.setText(f"❄️ {self.clim_target_temp}°C" if self.clim_bureau_state else "❄️ Clim")
        self.btn_clim.setStyleSheet(f"background-color: {bg}; color: white; font-weight: bold; font-size: 13px; border-radius: 8px; border: 2px solid {border};")

    def update_nanoleaf_btn_style(self):
        bg = "#8e44ad" if self.nanoleaf_state else "#252932"
        border = "#9b59b6" if self.nanoleaf_state else "#3d4659"
        self.btn_nanoleaf.setStyleSheet(f"background-color: {bg}; color: white; font-weight: bold; font-size: 13px; border-radius: 8px; border: 2px solid {border};")

    def update_hue_btn_style(self):
        bg = "#f39c12" if self.hue_bureau_state else "#252932"
        border = "#f1c40f" if self.hue_bureau_state else "#3d4659"
        self.btn_hue.setStyleSheet(f"background-color: {bg}; color: white; font-weight: bold; font-size: 13px; border-radius: 8px; border: 2px solid {border};")

    def toggle_clim_bureau(self):
        self.clim_bureau_state = not self.clim_bureau_state
        self.update_clim_btn_style()
        self.send_homey_capability(ID_DEVICE_CLIM_BUREAU, "onoff", self.clim_bureau_state)

    def toggle_nanoleaf(self):
        self.nanoleaf_state = not self.nanoleaf_state
        self.update_nanoleaf_btn_style()
        self.send_homey_capability(ID_DEVICE_NANOLEAF, "onoff", self.nanoleaf_state)

    def toggle_hue_bureau(self):
        self.hue_bureau_state = not self.hue_bureau_state
        self.update_hue_btn_style()
        self.send_homey_capability(ID_DEVICE_HUE_BUREAU, "onoff", self.hue_bureau_state)

    def toggle_sonoff_appoint(self):
        self.sonoff_appt_state = not self.sonoff_appt_state
        self.send_homey_capability(ID_DEVICE_SONOFF_APPT, "onoff", self.sonoff_appt_state)
        self.settings_overlay.refresh_ui()

    def update_clock(self):
        self.lbl_clock.setText(QTime.currentTime().toString("HH:mm"))
        self.update_radar_badge()

    def open_settings(self):
        self.settings_overlay.open_overlay()

    def reset_panel_idle_timer(self):
        self.panel.idle_ratio = 1.0
        self.panel.update()
        if self.panel_target_open and self.panel_autoclose_s > 0:
            self.panel_idle_timer.start(self.panel_autoclose_s * 1000)
            if not self.panel_gauge_timer.isActive():
                self.panel_gauge_timer.start()
        else:
            self.panel_idle_timer.stop()
            self.panel_gauge_timer.stop()

    def _tick_panel_gauge(self):
        if self.track.dragging_action is not None or self.slider.is_dragging:
            self.reset_panel_idle_timer()
            return
        if not self.panel_idle_timer.isActive():
            self.panel_gauge_timer.stop()
            return
        rem_ms = self.panel_idle_timer.remainingTime()
        if rem_ms < 2500:
            self.panel.idle_ratio = max(0.05, rem_ms / 2500.0)
            self.panel.update()

    def _auto_collapse_panel(self):
        if self.track.dragging_action is not None or self.slider.is_dragging:
            self.reset_panel_idle_timer()
            return
        self.track.select_zone(None)

    def _on_panel_anim(self, val):
        h = max(0, int(round(float(val))))
        self.panel.open_ratio = h / float(self.PANEL_TARGET_HEIGHT)
        self.panel.setFixedHeight(h)

    def _on_panel_anim_finished(self):
        if not self.panel_target_open:
            self.panel.setFixedHeight(0)
            self.panel.setVisible(False)
            self.panel_idle_timer.stop()
            self.panel_gauge_timer.stop()
        else:
            self.panel.setFixedHeight(self.PANEL_TARGET_HEIGHT)

    def update_expand_clone_btn_label(self):
        z = self.track.active_zone
        if not z or z.is_closing:
            return
        others = [o for o in self.track.zones if o is not z and not o.is_closing]
        left_bound = max([0.0] + [o.end for o in others if o.end <= z.start + 1.0])
        right_bound = min([float(TOTAL_LEDS)] + [o.start for o in others if o.start >= z.end - 1.0])
        if abs(z.start - left_bound) > 2.0 or abs(z.end - right_bound) > 2.0:
            self.btn_expand_clone.setText("↔ ÉTENDRE")
        else:
            self.btn_expand_clone.setText("⧉ CLONER")

    def on_expand_or_clone_clicked(self):
        self.reset_panel_idle_timer()
        self.track.expand_or_clone_active_zone()
        self.update_expand_clone_btn_label()

    def update_gui_from_selection(self):
        z = self.track.active_zone
        if z and not z.is_closing:
            self.panel.set_accent_color(*z.get_rgb(for_hardware=False))
            self.update_expand_clone_btn_label()
            self.slider.blockSignals(True)
            if z.mode == 'RGB':
                self.btn_mode.setText("🎨 RGB")
                self.slider.set_mode('spectrum')
                self.slider.set_value_animated(z.hue)
            else:
                self.btn_mode.setText("☀️ BLANC")
                self.slider.set_mode('temp')
                self.slider.set_value_animated(z.temp)
            self.btn_effect.setText(f"✨ {z.effect}")
            
            spd_label = "NORMAL"
            for name, val in SPEED_PRESETS:
                if abs(z.speed - val) < 0.1:
                    spd_label = name
                    break
            self.btn_speed.setText(f"⚡ {spd_label}")
            self.btn_speed.setEnabled(z.effect != "FIXE")
            self.slider.blockSignals(False)

            if not self.panel_target_open:
                self.panel_target_open = True
                start_h = self.panel.height() if self.panel.isVisible() else 0
                self.panel.setVisible(True)
                self.panel_anim.stop()
                self.panel_anim.setEasingCurve(QEasingCurve.Type.OutBack)
                self.panel_anim.setStartValue(float(start_h))
                self.panel_anim.setEndValue(float(self.PANEL_TARGET_HEIGHT))
                self.panel_anim.start()
            self.reset_panel_idle_timer()
        else:
            if self.panel_target_open:
                self.panel_target_open = False
                self.panel_idle_timer.stop()
                self.panel_gauge_timer.stop()
                self.panel_anim.stop()
                self.panel_anim.setEasingCurve(QEasingCurve.Type.InCubic)
                self.panel_anim.setStartValue(float(self.panel.height()))
                self.panel_anim.setEndValue(0.0)
                self.panel_anim.start()

    def toggle_mode(self):
        self.reset_panel_idle_timer()
        z = self.track.active_zone
        if z and not z.is_closing:
            z.mode = 'WHITE' if z.mode == 'RGB' else 'RGB'
            self.update_gui_from_selection()
            self.track.update()
            self.on_zones_modified()

    def cycle_effect(self):
        self.reset_panel_idle_timer()
        z = self.track.active_zone
        if z and not z.is_closing:
            idx = EFFECT_MODES.index(z.effect) if z.effect in EFFECT_MODES else 0
            z.effect = EFFECT_MODES[(idx + 1) % len(EFFECT_MODES)]
            self.update_gui_from_selection()
            self.track.update()
            self.on_zones_modified()

    def cycle_speed(self):
        self.reset_panel_idle_timer()
        z = self.track.active_zone
        if z and not z.is_closing:
            curr_idx = 1
            for i, (_, val) in enumerate(SPEED_PRESETS):
                if abs(z.speed - val) < 0.1:
                    curr_idx = i
                    break
            next_name, next_val = SPEED_PRESETS[(curr_idx + 1) % len(SPEED_PRESETS)]
            z.speed = next_val
            self.btn_speed.setText(f"⚡ {next_name}")
            self.on_zones_modified()

    def on_slider_change(self, val):
        z = self.track.active_zone
        if z and not z.is_closing:
            if z.mode == 'RGB':
                z.hue = val
            else:
                z.temp = val
            self.panel.set_accent_color(*z.get_rgb(for_hardware=False))
            self.track.update()
            self.on_zones_modified()

    def update_temp_btn_style(self, has_backup: bool):
        if has_backup:
            self.btn_temp_mem.setStyleSheet("""
                QPushButton {
                    background-color: #16a085; color: white;
                    border: 2px solid #1abc9c; border-radius: 8px;
                    font-weight: bold; font-size: 13px;
                }
            """)
        else:
            self.btn_temp_mem.setStyleSheet("""
                QPushButton {
                    background-color: #252932; color: #95a5a6;
                    border: 2px solid #3d4659; border-radius: 8px;
                    font-weight: bold; font-size: 13px;
                }
            """)

    def load_json_file(self):
        if os.path.exists(MEMORY_FILE):
            try:
                with open(MEMORY_FILE, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_config(self):
        self.full_config["settings"] = {
            "radar_enabled": self.radar_enabled,
            "radar_timeout": self.radar_timeout,
            "relay_delay_min": self.relay_delay_min,
            "transition_style": self.transition_style,
            "transition_duration": self.transition_duration,
            "screen_timeout": self.screen_timeout,
            "screen_sleep_mode": self.screen_sleep_mode,
            "panel_autoclose_s": self.panel_autoclose_s,
            "fan_auto": self.fan_auto,
            "fan_manual_pct": self.fan_manual_pct,
            "sync_mode": self.sync_mode,
            "cal_r": ColorCalibration.gain_r,
            "cal_g": ColorCalibration.gain_g,
            "cal_b": ColorCalibration.gain_b
        }
        tmp_file = MEMORY_FILE + ".tmp"
        try:
            with open(tmp_file, 'w') as f:
                json.dump(self.full_config, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_file, MEMORY_FILE)
        except Exception as e:
            print(f"Erreur sauvegarde atomique : {e}")

    def perform_autosave(self):
        live = [z.to_dict() for z in self.track.zones if not z.is_closing]
        self.full_config["temp_live"] = live
        self.save_config()

    def save_memory(self, slot):
        data = [z.to_dict() for z in self.track.zones if not z.is_closing]
        self.full_config[str(slot)] = data
        self.full_config["temp_live"] = data
        self.save_config()
        for s_id, btn in self.mem_buttons.items():
            btn.set_active_slot(s_id == slot)

    def load_memory(self, slot):
        key = str(slot)
        if key in self.full_config:
            current_state = [z.to_dict() for z in self.track.zones if not z.is_closing]
            if current_state and current_state != self.full_config[key]:
                self.full_config["temp_backup"] = current_state
                self.update_temp_btn_style(True)
            
            self.track.load_zones(self.full_config[key])
            for s_id, btn in self.mem_buttons.items():
                btn.set_active_slot(s_id == slot)

    def restore_temp_memory(self):
        if "temp_backup" in self.full_config and self.full_config["temp_backup"]:
            current_state = [z.to_dict() for z in self.track.zones if not z.is_closing]
            backup_to_load = self.full_config["temp_backup"]
            self.full_config["temp_backup"] = current_state
            self.track.load_zones(backup_to_load)
            for btn in self.mem_buttons.values():
                btn.set_active_slot(False)
            self.update_temp_btn_style(True)

    # ========================================================================
    # SÉQUENCE D'ARRÊT SYNCHRONE : 1. EXTINCTION LED -> 2. PAUSE -> 3. RELAIS OFF
    # ========================================================================
    def shutdown_hardware(self):
        if self._hw_shutting_down:
            return
        self._hw_shutting_down = True
        print("🛑 Arrêt : 1/3 Extinction des LEDs...")

        try:
            self.perform_autosave()
            self.anim_timer.stop()
            self.timer.stop()
            self.trans_anim.stop()
            self.standby_timer.stop()
            self.relay_cutoff_timer.stop()
            self.kickstart_timer.stop()
        except Exception:
            pass

        try:
            self.led_worker._running = False
            self.led_worker._event.set()
            self.led_worker.wait(600)
            self.sync_worker._running = False
        except Exception:
            pass

        # Envoi de 2 trames noires successives avant de désactiver le DMA
        global LEDS_DMA_READY, pixels, dotstars
        if LEDS_DMA_READY:
            LEDS_DMA_READY = False
            try:
                if pixels is not None:
                    for _ in range(2):
                        pixels.fill((0, 0, 0))
                        pixels.show()
                        time.sleep(0.04)
                    pixels.deinit()
                if dotstars is not None:
                    dotstars.fill((0, 0, 0))
                    dotstars.show()
                    dotstars.deinit()
            except Exception as e:
                print(f"Erreur extinction LEDs : {e}")

        # Verrouille GPIO18 (Data LED) à 0V (Drive Low) pour empêcher tout clignotement parasite
        subprocess.run(["pinctrl", "set", "18", "op", "dl"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Pause de 220 ms pour que le ruban soit 100% éteint AVANT d'ouvrir le relais
        print("🛑 Arrêt : 2/3 Pause de décharge avant ouverture relais...")
        time.sleep(0.22)

        # Ouverture franche du relais via passage de GPIO20 en Haute Impédance (ip pn)
        print("🛑 Arrêt : 3/3 Ouverture du relais (Haute Impédance) et arrêt ventilateur...")
        set_relay_hw_pin(False)

        if HARDWARE_ACTIVE and fan is not None:
            try:
                fan.value = 0.0
                fan.close()
            except Exception:
                pass
        subprocess.run(["pinctrl", "set", str(PIN_FAN), "op", "dl"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ Système arrêté proprement (LEDs noires, relais ouvert).")

    def closeEvent(self, event):
        self.shutdown_hardware()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    if HARDWARE_ACTIVE:
        app.setOverrideCursor(QCursor(Qt.CursorShape.BlankCursor))
    window = LedApp()

    def _sig_handler(sig, frame):
        window.shutdown_hardware()
        app.quit()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    window.show()
    sys.exit(app.exec())