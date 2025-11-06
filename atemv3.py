#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import print_function
import sys
import time
import socket
import PyATEMMax
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.parse
import subprocess
import ipaddress
import platform
import logging
import concurrent.futures
import os
import json
import queue
from datetime import datetime, timedelta

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# =======================================================
# Constants
# =======================================================
Live = 2
Preview = 1
Clear = 0
TallySendInterval = 0.25
MCAST_GRP = "224.0.0.20"
MCAST_PORT = 3000
TTL = 2
FallbackIP = "192.168.2.200"
CONFIG_FILE = "/home/samuele/Desktop/tally_config.json"
ATEM_TIMEOUT = 5.0  # Timeout per le operazioni ATEM
ATEM_RECONNECT_DELAY = 5  # Secondi prima di tentare riconnessione
ATEM_DATA_STALE_TIMEOUT = 10  # Secondi prima di considerare i dati obsoleti

# =======================================================
# Configuration Management
# =======================================================
def load_config():
    """Carica la configurazione dal file JSON"""
    default_config = {
        'atem_ip': None,
        'wifi_ap_mode': False,
        'last_successful_ip': None
    }
    
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                config = json.load(f)
            logger.info("Configurazione caricata da file")
            return config
    except Exception as e:
        logger.warning(f"Errore caricamento config: {e}")
    
    return default_config

def save_config(config):
    """Salva la configurazione nel file JSON"""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
        logger.info("Configurazione salvata")
    except Exception as e:
        logger.error(f"Errore salvataggio config: {e}")

# =======================================================
# Variables
# =======================================================
config = load_config()
TallyState = [0 for _ in range(256)]
dict_state = {'Live': 0, 'Preview': 0, 'Autolive': 0, 'isActive': True}
ATEM_IP = config.get('atem_ip') or config.get('last_successful_ip')
wifi_mode_ap = config.get('wifi_ap_mode', False)
lock = threading.Lock()
atem_lock = threading.Lock()  # Lock dedicato per l'oggetto ATEM
atem_data_queue = queue.Queue()  # Queue per passare dati tra thread
atem_status = {
    'connected': False, 
    'ip': None, 
    'last_error': None,
    'connection_attempts': 0,
    'last_connection_time': None,
    'data_updates': 0,
    'last_live': None,
    'last_preview': None,
    'last_data_time': None,  # Timestamp ultimo dato ricevuto
    'reconnect_in_progress': False
}
system_stats = {
    'start_time': time.time(),
    'total_packets_sent': 0,
    'scan_requests': 0,
    'reconnection_count': 0
}
scan_in_progress = False
atem_reader_thread = None
atem_reader_stop_event = threading.Event()

# =======================================================
# ATEM Connection Management Class
# =======================================================
class ATEMConnectionManager:
    """Gestisce la connessione ATEM in modo robusto"""
    
    def __init__(self):
        self.atem = None
        self.ip = None
        self.connected = False
        self.last_error = None
        self.reconnect_timer = None
        
    def connect(self, ip):
        """Connette all'ATEM con timeout e gestione errori"""
        with atem_lock:
            try:
                # Chiudi connessione precedente se esistente
                if self.atem is not None:
                    try:
                        self.atem.disconnect()
                    except:
                        pass
                    self.atem = None
                
                # Crea nuova connessione
                self.atem = PyATEMMax.ATEMMax()
                self.atem.connect(ip)
                
                # Attendi connessione con timeout
                if not self.atem.waitForConnection(timeout=ATEM_TIMEOUT):
                    raise Exception(f"Timeout connessione a {ip}")
                
                # Verifica che possiamo leggere i dati
                time.sleep(0.5)  # Stabilizzazione
                test_live = self.atem.programInput[0].videoSource
                test_preview = self.atem.previewInput[0].videoSource
                
                if test_live is None or test_preview is None:
                    raise Exception("Impossibile leggere dati iniziali dall'ATEM")
                
                self.ip = ip
                self.connected = True
                self.last_error = None
                logger.info(f"Connesso con successo ad ATEM {ip}")
                return True
                
            except Exception as e:
                self.last_error = str(e)
                self.connected = False
                logger.error(f"Errore connessione ATEM {ip}: {e}")
                
                # Cleanup
                if self.atem is not None:
                    try:
                        self.atem.disconnect()
                    except:
                        pass
                    self.atem = None
                
                return False
    
    def disconnect(self):
        """Disconnette dall'ATEM in modo sicuro"""
        with atem_lock:
            if self.atem is not None:
                try:
                    self.atem.disconnect()
                except:
                    pass
                self.atem = None
            self.connected = False
            logger.info("Disconnesso da ATEM")
    
    def read_data(self):
        """Legge i dati dall'ATEM con gestione errori"""
        with atem_lock:
            if self.atem is None or not self.connected:
                return None, None
            
            try:
                live = self.atem.programInput[0].videoSource
                preview = self.atem.previewInput[0].videoSource
                
                if live is None or preview is None:
                    raise Exception("Dati ATEM None")
                
                # Parse dei valori
                live_num = self._parse_input_value(live)
                preview_num = self._parse_input_value(preview)
                
                return live_num, preview_num
                
            except Exception as e:
                logger.error(f"Errore lettura dati ATEM: {e}")
                self.last_error = str(e)
                # Non disconnettiamo subito, lasciamo che il retry lo gestisca
                return None, None
    
    def _parse_input_value(self, value):
        """Parse del valore input ATEM"""
        try:
            value_str = str(value)
            if value_str.startswith("input"):
                if value_str == "input0":
                    return 0
                return int(value_str.replace("input", ""))
            elif value_str.isdigit():
                return int(value_str)
            else:
                return 0
        except:
            return 0
    
    def is_connection_alive(self):
        """Verifica se la connessione è ancora attiva"""
        with atem_lock:
            if self.atem is None or not self.connected:
                return False
            
            try:
                # Prova a leggere un valore per verificare la connessione
                test = self.atem.programInput[0].videoSource
                return test is not None
            except:
                return False

# Istanza globale del connection manager
atem_manager = ATEMConnectionManager()

# =======================================================
# ATEM Reader Thread Function
# =======================================================
def atem_reader_thread_func():
    """Thread dedicato per la lettura dati ATEM"""
    global atem_status, dict_state
    
    logger.info("Thread lettore ATEM avviato")
    consecutive_errors = 0
    max_consecutive_errors = 5
    last_reconnect_attempt = time.time()
    
    while not atem_reader_stop_event.is_set():
        try:
            # Se non connesso, tenta connessione
            if not atem_manager.connected and ATEM_IP:
                current_time = time.time()
                if current_time - last_reconnect_attempt >= ATEM_RECONNECT_DELAY:
                    logger.info(f"Tentativo riconnessione ad ATEM {ATEM_IP}")
                    atem_status['reconnect_in_progress'] = True
                    
                    if atem_manager.connect(ATEM_IP):
                        atem_status['connected'] = True
                        atem_status['ip'] = ATEM_IP
                        atem_status['last_connection_time'] = time.strftime("%H:%M:%S")
                        atem_status['connection_attempts'] += 1
                        system_stats['reconnection_count'] += 1
                        consecutive_errors = 0
                    else:
                        atem_status['connected'] = False
                        atem_status['last_error'] = atem_manager.last_error
                    
                    atem_status['reconnect_in_progress'] = False
                    last_reconnect_attempt = current_time
                
                # Se non connesso, attendi prima di riprovare
                if not atem_manager.connected:
                    time.sleep(1)
                    continue
            
            # Leggi dati dall'ATEM
            if atem_manager.connected:
                live_value, preview_value = atem_manager.read_data()
                
                if live_value is not None and preview_value is not None:
                    # Aggiorna stato con lock
                    with lock:
                        if dict_state['Live'] != live_value or dict_state['Preview'] != preview_value:
                            logger.info(f"Stato ATEM aggiornato - Live: {live_value}, Preview: {preview_value}")
                        
                        dict_state['Live'] = live_value
                        dict_state['Preview'] = preview_value
                        dict_state['Autolive'] = 0
                        
                        atem_status['last_live'] = live_value
                        atem_status['last_preview'] = preview_value
                        atem_status['last_data_time'] = time.time()
                        atem_status['data_updates'] += 1
                    
                    consecutive_errors = 0
                    
                    # Log periodico
                    if atem_status['data_updates'] % 100 == 0:
                        logger.debug(f"ATEM dati OK - Updates: {atem_status['data_updates']}")
                    
                else:
                    consecutive_errors += 1
                    logger.warning(f"Dati ATEM non validi, errori consecutivi: {consecutive_errors}")
                    
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error("Troppi errori consecutivi, disconnessione ATEM")
                        atem_manager.disconnect()
                        atem_status['connected'] = False
                        atem_status['last_error'] = "Troppi errori di lettura"
                        consecutive_errors = 0
                        time.sleep(ATEM_RECONNECT_DELAY)
            
            # Breve pausa tra letture
            time.sleep(0.1)  # 100ms tra le letture
            
        except Exception as e:
            logger.error(f"Errore critico nel thread lettore ATEM: {e}")
            time.sleep(1)
    
    logger.info("Thread lettore ATEM terminato")

# =======================================================
# Helper functions
# =======================================================
def get_system_info():
    """Raccoglie informazioni di sistema dettagliate"""
    try:
        # CPU Temperature (Raspberry Pi specific)
        cpu_temp = "N/A"
        if os.path.exists("/sys/class/thermal/thermal_zone0/temp"):
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                cpu_temp = f"{int(f.read()) / 1000:.1f}°C"
        
        # Memory info
        try:
            with open("/proc/meminfo") as f:
                mem_info = f.readlines()
                mem_total = int([x for x in mem_info if 'MemTotal' in x][0].split()[1]) // 1024
                mem_free = int([x for x in mem_info if 'MemAvailable' in x][0].split()[1]) // 1024
                mem_usage = f"{mem_total - mem_free}MB / {mem_total}MB ({((mem_total - mem_free) / mem_total * 100):.1f}%)"
        except:
            mem_usage = "N/A"
        
        # System uptime
        uptime_seconds = time.time() - system_stats['start_time']
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        uptime_str = f"{hours}h {minutes}m"
        
        # Network interface info
        try:
            local_ip, _ = get_local_ip_and_subnet()
        except:
            local_ip = "N/A"
        
        # Controlla se i dati ATEM sono obsoleti
        data_freshness = "N/A"
        if atem_status.get('last_data_time'):
            age = time.time() - atem_status['last_data_time']
            if age < 1:
                data_freshness = "Tempo reale"
            elif age < 10:
                data_freshness = f"{age:.1f}s fa"
            else:
                data_freshness = f"OBSOLETI ({age:.0f}s fa)"
        
        return {
            'cpu_temp': cpu_temp,
            'memory_usage': mem_usage,
            'uptime': uptime_str,
            'local_ip': local_ip,
            'total_packets_sent': system_stats['total_packets_sent'],
            'scan_requests': system_stats['scan_requests'],
            'reconnections': system_stats['reconnection_count'],
            'data_freshness': data_freshness
        }
    except Exception as e:
        logger.error(f"Errore raccolta info sistema: {e}")
        return {
            'cpu_temp': 'N/A',
            'memory_usage': 'N/A', 
            'uptime': 'N/A',
            'local_ip': 'N/A',
            'total_packets_sent': system_stats.get('total_packets_sent', 0),
            'scan_requests': system_stats.get('scan_requests', 0),
            'reconnections': system_stats.get('reconnection_count', 0),
            'data_freshness': 'N/A'
        }

def get_local_ip_and_subnet():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        
        # Network detection for Linux
        try:
            if platform.system().lower() == "linux":
                result = subprocess.check_output(
                    ["ip", "route", "show", "default"], 
                    universal_newlines=True,
                    timeout=5
                )
                network = ipaddress.IPv4Interface(f"{ip}/24").network
                return ip, network
        except:
            pass
            
        network = ipaddress.IPv4Interface(f"{ip}/24").network
        return ip, network
        
    except Exception as e:
        logger.error(f"Errore recupero IP locale: {e}")
        return None, ipaddress.IPv4Network("192.168.1.0/24", strict=False)

def ping_host(ip_str):
    """Ping function optimized for threading"""
    try:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        command = ["ping", param, "1", "-W", "200", ip_str]  # Timeout ridotto a 200ms
        result = subprocess.call(
            command, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            timeout=0.5
        )
        return (ip_str, result == 0)
    except:
        return (ip_str, False)

def test_atem_connection(ip_str):
    """Test rapido della connessione ATEM"""
    try:
        # Usa una connessione temporanea per il test
        test_manager = ATEMConnectionManager()
        result = test_manager.connect(ip_str)
        if result:
            test_manager.disconnect()
        return result
    except Exception as e:
        logger.debug(f"Test connessione ATEM fallito per {ip_str}: {e}")
        return False

def find_atem(network, force_scan=False):
    """Find ATEM on network with option to force full scan"""
    global ATEM_IP, scan_in_progress
    
    scan_in_progress = True
    system_stats['scan_requests'] += 1
    
    try:
        logger.info(f"Ricerca ATEM sulla rete {network}")
        
        # First check if we have a known working IP (unless forced)
        if not force_scan and ATEM_IP:
            logger.info(f"Test IP precedentemente salvato: {ATEM_IP}")
            if test_atem_connection(ATEM_IP):
                logger.info(f"ATEM confermato su IP salvato: {ATEM_IP}")
                config['last_successful_ip'] = ATEM_IP
                save_config(config)
                return True
            else:
                logger.info("IP salvato non risponde, avvio scansione completa")
        
        # Phase 1: Parallel ping scan
        logger.info("Fase 1: Scansione ping parallela...")
        
        alive_hosts = []
        all_ips = [str(ip) for ip in network.hosts()]
        total_ips = len(all_ips)
        logger.info(f"Scansione {total_ips} IP in parallelo...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
            future_to_ip = {executor.submit(ping_host, ip): ip for ip in all_ips}
            
            completed = 0
            for future in concurrent.futures.as_completed(future_to_ip, timeout=10):
                completed += 1
                try:
                    ip_str, is_alive = future.result()
                    if is_alive:
                        alive_hosts.append(ip_str)
                        logger.info(f"Host attivo trovato: {ip_str} ({completed}/{total_ips})")
                    elif completed % 50 == 0:
                        logger.info(f"Progresso ping: {completed}/{total_ips}")
                except Exception as e:
                    logger.debug(f"Errore ping: {e}")
        
        logger.info(f"Ping completato. Host attivi: {len(alive_hosts)}")
        
        if not alive_hosts:
            logger.warning("Nessun host risponde al ping")
            return False
        
        # Phase 2: Test ATEM on alive hosts
        logger.info("Fase 2: Test ATEM sugli host attivi...")
        
        for i, ip_str in enumerate(alive_hosts, 1):
            logger.info(f"Test ATEM {i}/{len(alive_hosts)}: {ip_str}")
            
            if test_atem_connection(ip_str):
                ATEM_IP = ip_str
                config['atem_ip'] = ATEM_IP
                config['last_successful_ip'] = ATEM_IP
                save_config(config)
                logger.info(f"ATEM trovato e salvato: {ATEM_IP}")
                return True
        
        logger.warning("Nessun ATEM trovato tra gli host attivi")
        return False
        
    finally:
        scan_in_progress = False

def setWifiMode(ap_mode=False):
    """Set WiFi mode (AP or Station)"""
    global wifi_mode_ap
    wifi_mode_ap = ap_mode
    config['wifi_ap_mode'] = ap_mode
    save_config(config)
    
    script_path = "/home/samuele/Desktop/wifi_ap.sh"
    
    if not os.path.exists(script_path):
        logger.warning(f"Script WiFi AP non trovato: {script_path}")
        return False
    
    try:
        action = "on" if ap_mode else "off"
        subprocess.run(["sudo", script_path, action], timeout=10, check=True)
        logger.info(f"WiFi AP mode {'attivato' if ap_mode else 'disattivato'}")
        return True
    except subprocess.TimeoutExpired:
        logger.error("Timeout esecuzione script WiFi")
        return False
    except subprocess.CalledProcessError as e:
        logger.error(f"Errore esecuzione script WiFi: {e}")
        return False
    except Exception as e:
        logger.error(f"Errore WiFi script: {e}")
        return False

# =======================================================
# Enhanced Web Server
# =======================================================
class ConfigHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress HTTP server logs
        
    def do_GET(self):
        """Handle GET requests"""
        if self.path == "/api/status":
            # JSON API endpoint for status
            system_info = get_system_info()
            status_data = {
                'atem': atem_status,
                'tally_state': dict_state,
                'wifi_ap': wifi_mode_ap,
                'scan_in_progress': scan_in_progress,
                'system': system_info,
                'config': {
                    'current_atem_ip': ATEM_IP,
                    'last_successful_ip': config.get('last_successful_ip'),
                    'wifi_ap_mode': wifi_mode_ap
                }
            }
            
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(status_data, indent=2).encode('utf-8'))
            return
        
        # Main web interface
        system_info = get_system_info()
        
        # Status indicators with colors
        atem_status_color = "green" if atem_status['connected'] else "red"
        atem_status_text = "Connesso" if atem_status['connected'] else "Disconnesso"
        
        if atem_status.get('reconnect_in_progress'):
            atem_status_text = "Riconnessione..."
            atem_status_color = "orange"
        
        wifi_status_color = "orange" if wifi_mode_ap else "blue"
        wifi_status_text = "Access Point" if wifi_mode_ap else "Station Mode"
        
        scan_status = "IN CORSO..." if scan_in_progress else "Pronto"
        scan_button_disabled = "disabled" if scan_in_progress else ""
        
        # Calcola freshness dei dati
        data_status = "N/A"
        data_color = "gray"
        if atem_status.get('last_data_time'):
            age = time.time() - atem_status['last_data_time']
            if age < 2:
                data_status = "Tempo reale"
                data_color = "green"
            elif age < 10:
                data_status = f"Aggiornato {age:.1f}s fa"
                data_color = "orange"
            else:
                data_status = f"OBSOLETO ({age:.0f}s fa)"
                data_color = "red"
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Tally System Controller</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }}
                .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
                .header {{ text-align: center; color: #333; margin-bottom: 30px; }}
                .status-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 30px; }}
                .status-card {{ background: #f8f9fa; padding: 15px; border-radius: 6px; border-left: 4px solid #007bff; }}
                .status-value {{ font-size: 18px; font-weight: bold; margin-top: 5px; }}
                .status-green {{ color: green; }}
                .status-red {{ color: red; }}
                .status-orange {{ color: orange; }}
                .status-blue {{ color: blue; }}
                .status-gray {{ color: gray; }}
                .form-section {{ background: #f8f9fa; padding: 20px; border-radius: 6px; margin-bottom: 20px; }}
                .form-group {{ margin-bottom: 15px; }}
                .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
                .form-group input[type="text"] {{ width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
                .form-group input[type="checkbox"] {{ margin-right: 8px; }}
                .btn {{ padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; margin-right: 10px; margin-bottom: 10px; }}
                .btn-primary {{ background-color: #007bff; color: white; }}
                .btn-success {{ background-color: #28a745; color: white; }}
                .btn-warning {{ background-color: #ffc107; color: black; }}
                .btn-danger {{ background-color: #dc3545; color: white; }}
                .btn:hover {{ opacity: 0.8; }}
                .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
                .tally-display {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 10px; }}
                .tally-indicator {{ width: 60px; height: 40px; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; border-radius: 4px; }}
                .tally-live {{ background-color: red; }}
                .tally-preview {{ background-color: green; }}
                .tally-clear {{ background-color: gray; }}
                .log-section {{ background: #f8f9fa; padding: 15px; border-radius: 6px; margin-top: 20px; }}
                .refresh-info {{ font-size: 12px; color: #666; text-align: center; margin-top: 20px; }}
                .debug-info {{ font-size: 11px; color: #888; margin-top: 5px; }}
                .alert {{ padding: 10px; background-color: #f44336; color: white; margin-bottom: 15px; border-radius: 4px; }}
                .alert.warning {{ background-color: #ff9800; }}
                .alert.info {{ background-color: #2196F3; }}
                @keyframes pulse {{
                    0% {{ opacity: 1; }}
                    50% {{ opacity: 0.5; }}
                    100% {{ opacity: 1; }}
                }}
                .pulse {{ animation: pulse 1s infinite; }}
            </style>
            <script>
                let statusUpdateInterval;
                
                function updateStatus() {{
                    fetch('/api/status')
                        .then(response => response.json())
                        .then(data => {{
                            // Aggiorna display tally
                            updateTallyDisplay(data.tally_state);
                            
                            // Aggiorna stato connessione
                            updateConnectionStatus(data.atem);
                            
                            // Aggiorna freshness dati
                            updateDataFreshness(data.atem.last_data_time);
                        }})
                        .catch(error => {{
                            console.error('Error updating status:', error);
                        }});
                }}
                
                function updateTallyDisplay(state) {{
                    const liveEl = document.getElementById('live-value');
                    const previewEl = document.getElementById('preview-value');
                    if (liveEl) liveEl.textContent = state.Live || '-';
                    if (previewEl) previewEl.textContent = state.Preview || '-';
                }}
                
                function updateConnectionStatus(atem) {{
                    const statusEl = document.getElementById('connection-status');
                    if (statusEl) {{
                        if (atem.connected) {{
                            statusEl.textContent = 'Connesso';
                            statusEl.className = 'status-value status-green';
                        }} else if (atem.reconnect_in_progress) {{
                            statusEl.textContent = 'Riconnessione...';
                            statusEl.className = 'status-value status-orange pulse';
                        }} else {{
                            statusEl.textContent = 'Disconnesso';
                            statusEl.className = 'status-value status-red';
                        }}
                    }}
                }}
                
                function updateDataFreshness(lastDataTime) {{
                    const freshnessEl = document.getElementById('data-freshness');
                    if (freshnessEl && lastDataTime) {{
                        const age = Date.now()/1000 - lastDataTime;
                        let text, className;
                        
                        if (age < 2) {{
                            text = 'Tempo reale';
                            className = 'status-green';
                        }} else if (age < 10) {{
                            text = `Aggiornato ${{age.toFixed(1)}}s fa`;
                            className = 'status-orange';
                        }} else {{
                            text = `OBSOLETO (${{age.toFixed(0)}}s fa)`;
                            className = 'status-red';
                        }}
                        
                        freshnessEl.textContent = text;
                        freshnessEl.className = className;
                    }}
                }}
                
                function confirmScan() {{
                    return confirm('Avviare la scansione della rete per cercare ATEM? Questo potrebbe richiedere alcuni minuti.');
                }}
                
                function restartReader() {{
                    if (confirm('Riavviare il lettore ATEM? Questo forzerà una riconnessione.')) {{
                        fetch('/api/restart_reader', {{ method: 'POST' }})
                            .then(() => alert('Lettore ATEM riavviato'))
                            .catch(error => alert('Errore: ' + error));
                    }}
                }}
                
                // Avvia aggiornamento automatico
                window.onload = function() {{
                    updateStatus();
                    statusUpdateInterval = setInterval(updateStatus, 1000);  // Ogni secondo
                }};
                
                window.onunload = function() {{
                    if (statusUpdateInterval) {{
                        clearInterval(statusUpdateInterval);
                    }}
                }};
            </script>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>📹 Tally System Controller</h1>
                    <h2>Powered by Wired-shop</h2>
                    <p>Sistema di controllo tally per mixer ATEM - v3.0 FIXED</p>
                </div>
                
                {'<div class="alert warning">⚠️ Riconnessione ATEM in corso...</div>' if atem_status.get('reconnect_in_progress') else ''}
                {'<div class="alert">❌ ATEM disconnesso - verificare connessione</div>' if not atem_status['connected'] and not atem_status.get('reconnect_in_progress') else ''}
                
                <div class="status-grid">
                    <div class="status-card">
                        <div>Stato ATEM</div>
                        <div id="connection-status" class="status-value status-{atem_status_color.lower()} {'pulse' if atem_status.get('reconnect_in_progress') else ''}">{atem_status_text}</div>
                        <small>IP: {atem_status['ip'] or ATEM_IP or 'N/A'}<br>
                        Aggiornamenti: {atem_status.get('data_updates', 0)}</small>
                        {f'<br><small style="color:red">Errore: {atem_status["last_error"]}</small>' if atem_status.get('last_error') else ''}
                    </div>
                    
                    <div class="status-card">
                        <div>Freshness Dati</div>
                        <div id="data-freshness" class="status-value status-{data_color}">{data_status}</div>
                        <small>Stato aggiornamento dati ATEM</small>
                    </div>
                    
                    <div class="status-card">
                        <div>Modalità WiFi</div>
                        <div class="status-value status-{wifi_status_color.lower()}">{wifi_status_text}</div>
                        <small>Configurazione di rete attuale</small>
                    </div>
                    
                    <div class="status-card">
                        <div>Sistema</div>
                        <div class="status-value status-green">Online</div>
                        <small>Uptime: {system_info['uptime']}<br>
                        CPU: {system_info['cpu_temp']}<br>
                        RAM: {system_info['memory_usage']}</small>
                    </div>
                </div>
                
                <div class="form-section">
                    <h3>Configurazione ATEM</h3>
                    <form method="POST">
                        <div class="form-group">
                            <label for="atem_ip">Indirizzo IP ATEM:</label>
                            <input type="text" id="atem_ip" name="atem_ip" value="{ATEM_IP or ''}" 
                                   placeholder="es. 192.168.2.200" pattern="^(?:[0-9]{{1,3}}\\.?){{4}}$">
                            <small>IP attuale: {ATEM_IP or 'Non configurato'}</small>
                            {f'<br><small>Ultimo IP funzionante: {config.get("last_successful_ip", "Nessuno")}</small>' if config.get("last_successful_ip") else ''}
                        </div>
                        
                        <div class="form-group">
                            <label>
                                <input type="checkbox" name="wifi_ap" {'checked' if wifi_mode_ap else ''}>
                                Abilita modalità Access Point WiFi
                            </label>
                            <small>Crea una rete WiFi dedicata per i dispositivi tally</small>
                        </div>
                        
                        <button type="submit" class="btn btn-primary">Salva Configurazione</button>
                        <button type="submit" name="action" value="scan" class="btn btn-warning" {scan_button_disabled} 
                                onclick="return confirmScan()">Ricerca ATEM</button>
                        <button type="button" onclick="restartReader()" class="btn btn-danger">Riavvia Lettore</button>
                        <button type="button" onclick="location.reload()" class="btn btn-success">Aggiorna Pagina</button>
                    </form>
                </div>
                
                <div class="status-card">
                    <h3>Stato Tally Corrente</h3>
                    <div class="tally-display">
                        <div class="tally-indicator tally-live">LIVE<br><span id="live-value">{dict_state['Live'] if dict_state['Live'] > 0 else '-'}</span></div>
                        <div class="tally-indicator tally-preview">PVW<br><span id="preview-value">{dict_state['Preview'] if dict_state['Preview'] > 0 else '-'}</span></div>
                    </div>
                    <div class="debug-info" style="margin-top: 10px;">
                        Sistema Attivo: {'Sì' if dict_state['isActive'] else 'No'}<br>
                        Ultima lettura: {atem_status.get('last_live', 'N/A')} / {atem_status.get('last_preview', 'N/A')}<br>
                        Totale aggiornamenti: {atem_status.get('data_updates', 0)}
                    </div>
                </div>
                
                <div class="status-card">
                    <h3>Statistiche Sistema</h3>
                    <div><strong>Pacchetti multicast inviati:</strong> {system_info['total_packets_sent']}</div>
                    <div><strong>Riconnessioni totali:</strong> {system_info['reconnections']}</div>
                    <div><strong>Scansioni rete:</strong> {system_info['scan_requests']}</div>
                    <div><strong>Tentativi connessione:</strong> {atem_status['connection_attempts']}</div>
                    {f'<div><strong>Ultima connessione:</strong> {atem_status["last_connection_time"]}</div>' if atem_status.get('last_connection_time') else ''}
                </div>
                
                <div class="log-section">
                    <h4>Informazioni Tecniche</h4>
                    <p><strong>Multicast:</strong> {MCAST_GRP}:{MCAST_PORT}</p>
                    <p><strong>Intervallo invio:</strong> {TallySendInterval}s</p>
                    <p><strong>Thread lettore ATEM:</strong> {'Attivo' if atem_reader_thread and atem_reader_thread.is_alive() else 'Non attivo'}</p>
                    <p><strong>File configurazione:</strong> {CONFIG_FILE}</p>
                </div>
                
                <div class="refresh-info">
                    Aggiornamento automatico ogni secondo<br>
                    Sistema Tally RB v3.0 - Thread-safe Architecture
                </div>
            </div>
        </body>
        </html>"""
        
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        """Handle POST requests"""
        try:
            # Handle API restart reader
            if self.path == "/api/restart_reader":
                restart_atem_reader()
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "ok"}).encode('utf-8'))
                return
            
            length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(length).decode('utf-8')
            params = urllib.parse.parse_qs(post_data)
            
            global ATEM_IP
            
            # Handle ATEM scan request
            if "action" in params and params["action"][0] == "scan":
                logger.info("Richiesta scansione ATEM dal web interface")
                
                def run_scan():
                    localIP, network = get_local_ip_and_subnet()
                    if localIP:
                        find_atem(network, force_scan=True)
                        # Riavvia il reader thread se trova un ATEM
                        if ATEM_IP:
                            restart_atem_reader()
                
                # Run scan in background thread
                scan_thread = threading.Thread(target=run_scan, daemon=True)
                scan_thread.start()
                
            else:
                # Handle configuration update
                if "atem_ip" in params and params["atem_ip"][0].strip():
                    new_ip = params["atem_ip"][0].strip()
                    if new_ip != ATEM_IP:
                        ATEM_IP = new_ip
                        config['atem_ip'] = ATEM_IP
                        save_config(config)
                        logger.info(f"ATEM IP aggiornato manualmente: {ATEM_IP}")
                        
                        # Riavvia il reader thread con il nuovo IP
                        restart_atem_reader()
                
                # Handle WiFi mode change
                new_wifi_mode = "wifi_ap" in params
                if new_wifi_mode != wifi_mode_ap:
                    setWifiMode(ap_mode=new_wifi_mode)
            
            # Redirect back to main page
            self.send_response(303)
            self.send_header('Location', '/')
            self.end_headers()
            
        except Exception as e:
            logger.error(f"Errore elaborazione POST: {e}")
            self.send_error(500, "Errore interno server")

def run_webserver():
    """Run the web server"""
    try:
        server = HTTPServer(('', 8080), ConfigHandler)
        logger.info("Web server avviato su porta 8080")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Errore web server: {e}")

def restart_atem_reader():
    """Riavvia il thread lettore ATEM"""
    global atem_reader_thread, atem_reader_stop_event
    
    logger.info("Riavvio thread lettore ATEM...")
    
    # Ferma il thread esistente
    if atem_reader_thread and atem_reader_thread.is_alive():
        atem_reader_stop_event.set()
        atem_reader_thread.join(timeout=5)
        logger.info("Thread lettore ATEM fermato")
    
    # Disconnetti ATEM esistente
    atem_manager.disconnect()
    
    # Reset evento stop
    atem_reader_stop_event.clear()
    
    # Avvia nuovo thread
    atem_reader_thread = threading.Thread(target=atem_reader_thread_func, daemon=True, name="ATEMReader")
    atem_reader_thread.start()
    logger.info("Thread lettore ATEM riavviato")

# =======================================================
# Main Program
# =======================================================
if __name__ == "__main__":
    logger.info("=== Avvio Tally System RB v3.0 FIXED ===")
    logger.info("Architettura migliorata con thread dedicato per ATEM")
    
    # Phase 1: Start web server
    logger.info("Fase 1: Avvio web server...")
    webserver_thread = threading.Thread(target=run_webserver, daemon=True, name="WebServer")
    webserver_thread.start()
    time.sleep(2)
    logger.info("Web server avviato - Configurazione disponibile su http://[IP]:8080")
    
    # Phase 2: Check for saved ATEM IP
    if ATEM_IP:
        logger.info(f"Fase 2: IP ATEM salvato trovato: {ATEM_IP}")
    else:
        logger.info("Fase 2: Nessun IP ATEM salvato, tentativo ricerca automatica...")
        localIP, network = get_local_ip_and_subnet()
        if localIP:
            logger.info(f"IP Locale: {localIP}, Rete: {network}")
            if find_atem(network):
                logger.info("ATEM trovato automaticamente!")
            else:
                logger.warning("ATEM non trovato, usa configurazione manuale via web")
                ATEM_IP = FallbackIP
    
    # Phase 3: Start ATEM reader thread
    logger.info("Fase 3: Avvio thread lettore ATEM...")
    atem_reader_thread = threading.Thread(target=atem_reader_thread_func, daemon=True, name="ATEMReader")
    atem_reader_thread.start()
    
    # Phase 4: Setup multicast socket
    try:
        mcastSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        mcastSock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, TTL)
        logger.info(f"Socket multicast configurato: {MCAST_GRP}:{MCAST_PORT}")
    except Exception as e:
        logger.error(f"Errore setup socket multicast: {e}")
        sys.exit(1)

    # Phase 5: Main tally multicast loop
    logger.info("=== Avvio loop multicast principale ===")
    
    last_state_check = time.time()
    data_timeout_warned = False
    
    try:
        while True:
            try:
                # Check data freshness
                current_time = time.time()
                data_age = None
                if atem_status.get('last_data_time'):
                    data_age = current_time - atem_status['last_data_time']
                    
                    # Avvisa se i dati sono obsoleti
                    if data_age > ATEM_DATA_STALE_TIMEOUT:
                        if not data_timeout_warned:
                            logger.warning(f"Dati ATEM obsoleti da {data_age:.0f} secondi")
                            data_timeout_warned = True
                    else:
                        data_timeout_warned = False
                
                # Reset tally array
                for i in range(len(TallyState)):
                    TallyState[i] = Clear
                
                # Set tally states if system is active
                if dict_state['isActive']:
                    # Solo se i dati sono freschi (meno di 10 secondi)
                    if data_age is None or data_age > ATEM_DATA_STALE_TIMEOUT:
                        # Dati troppo vecchi, non inviare tally
                        pass
                    else:
                        with lock:
                            live_val = dict_state['Live']
                            preview_val = dict_state['Preview']
                            autolive_val = dict_state['Autolive']
                        
                        # Preview state
                        if autolive_val == 0 and preview_val > 0:
                            if preview_val <= len(TallyState):
                                TallyState[preview_val - 1] = Preview
                        
                        # Live state
                        if live_val > 0 and live_val <= len(TallyState):
                            TallyState[live_val - 1] = Live
                
                # Send multicast packet
                mcastSock.sendto(bytearray(TallyState), (MCAST_GRP, MCAST_PORT))
                system_stats['total_packets_sent'] += 1
                
                # Log periodico
                if system_stats['total_packets_sent'] % 100 == 0:
                    active_tallies = [i+1 for i, state in enumerate(TallyState) if state != Clear]
                    if active_tallies:
                        logger.debug(f"Multicast #{system_stats['total_packets_sent']}, Tally attive: {active_tallies}")
                
                # Check thread health ogni 10 secondi
                if current_time - last_state_check > 10:
                    if not atem_reader_thread.is_alive():
                        logger.error("Thread lettore ATEM non attivo! Riavvio...")
                        restart_atem_reader()
                    last_state_check = current_time
                
                # Sleep fino al prossimo ciclo
                time.sleep(TallySendInterval)
                
            except Exception as e:
                logger.error(f"Errore nel loop multicast: {e}")
                time.sleep(1)
                
    except KeyboardInterrupt:
        logger.info("Interruzione manuale ricevuta...")
    
    # Cleanup
    logger.info("Avvio procedura di chiusura...")
    
    # Ferma thread lettore ATEM
    atem_reader_stop_event.set()
    if atem_reader_thread:
        atem_reader_thread.join(timeout=5)
    
    # Disconnetti ATEM
    atem_manager.disconnect()
    
    # Chiudi socket
    mcastSock.close()
    
    logger.info("=== Sistema chiuso correttamente ===")