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
atem_status = {
    'connected': False, 
    'ip': None, 
    'last_error': None,
    'connection_attempts': 0,
    'last_connection_time': None,
    'data_updates': 0,
    'last_live': None,
    'last_preview': None
}
system_stats = {
    'start_time': time.time(),
    'total_packets_sent': 0,
    'scan_requests': 0,
    'reconnection_count': 0
}
scan_in_progress = False

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
                cpu_temp = f"{int(f.read()) / 1000:.1f}C"
        
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
        
        return {
            'cpu_temp': cpu_temp,
            'memory_usage': mem_usage,
            'uptime': uptime_str,
            'local_ip': local_ip,
            'total_packets_sent': system_stats['total_packets_sent'],
            'scan_requests': system_stats['scan_requests'],
            'reconnections': system_stats['reconnection_count']
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
            'reconnections': system_stats.get('reconnection_count', 0)
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
        command = ["ping", param, "1", "-W", "500", ip_str]
        result = subprocess.call(
            command, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            timeout=1
        )
        return (ip_str, result == 0)
    except:
        return (ip_str, False)

def test_atem_connection(ip_str):
    """Test ATEM connection with timeout - VERSIONE MIGLIORATA"""
    try:
        test_atem = PyATEMMax.ATEMMax()
        test_atem.connect(ip_str)
        
        if not test_atem.waitForConnection(timeout=5.0):  # Aumentato timeout
            test_atem.disconnect()
            return False
        
        # Attendi stabilizzazione dati - IMPORTANTE
        time.sleep(0.5)
        
        # Verifica dati multipli per essere sicuri
        valid_reads = 0
        for attempt in range(3):
            try:
                live_val = test_atem.programInput[0].videoSource
                preview_val = test_atem.previewInput[0].videoSource
                
                # Verifica che i dati siano validi (non None e non vuoti)
                if live_val is not None and preview_val is not None:
                    live_num = int(str(live_val).replace("input", "")) if str(live_val) != "input0" else 0
                    preview_num = int(str(preview_val).replace("input", "")) if str(preview_val) != "input0" else 0
                    
                    logger.debug(f"ATEM test {ip_str} - tentativo {attempt+1}: Live={live_num}, Preview={preview_num}")
                    valid_reads += 1
                
                time.sleep(0.2)  # Piccola pausa tra le letture
                
            except Exception as e:
                logger.debug(f"Errore lettura dati ATEM test: {e}")
                time.sleep(0.2)
        
        test_atem.disconnect()
        
        # Considera valido se almeno 2 letture su 3 sono andate a buon fine
        if valid_reads >= 2:
            logger.info(f"ATEM confermato su {ip_str} - {valid_reads}/3 letture valide")
            return True
        else:
            logger.warning(f"ATEM su {ip_str} - solo {valid_reads}/3 letture valide")
            return False
            
    except Exception as e:
        logger.debug(f"ATEM test failed for {ip_str}: {e}")
        try:
            test_atem.disconnect()
        except:
            pass
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
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            future_to_ip = {executor.submit(ping_host, ip): ip for ip in all_ips}
            
            completed = 0
            for future in concurrent.futures.as_completed(future_to_ip, timeout=12):
                completed += 1
                try:
                    ip_str, is_alive = future.result()
                    if is_alive:
                        alive_hosts.append(ip_str)
                        logger.info(f"Host attivo trovato: {ip_str} ({completed}/{total_ips})")
                    elif completed % 20 == 0:
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

def getAtemData():
    """Get data from ATEM mixer - VERSIONE CORRETTA E MIGLIORATA"""
    global atem_status, system_stats
    
    try:
        # Verifica e gestione connessione
        if not atem.connected:
            logger.info(f"Connessione ATEM {ATEM_IP}...")
            atem.connect(ATEM_IP)
            atem_status['connection_attempts'] += 1
            
            if not atem.waitForConnection(timeout=3.0):
                raise Exception("Timeout connessione ATEM")
            
            system_stats['reconnection_count'] += 1
            atem_status['last_connection_time'] = time.strftime("%H:%M:%S")
            logger.info("Connessione ATEM stabilita")
            
            # Attendi stabilizzazione dopo connessione - IMPORTANTE
            time.sleep(0.5)

        # LETTURA DATI MIGLIORATA - con retry e validazione
        live_source = None
        preview_source = None
        
        # Prova fino a 3 volte per ottenere dati validi
        for attempt in range(3):
            try:
                # Leggi i dati dall'ATEM
                live_source = atem.programInput[0].videoSource
                preview_source = atem.previewInput[0].videoSource
                
                # Verifica che i dati non siano None
                if live_source is not None and preview_source is not None:
                    break
                    
                logger.debug(f"Dati ATEM None - tentativo {attempt + 1}")
                time.sleep(0.1)  # Breve pausa prima di riprovare
                
            except Exception as e:
                logger.debug(f"Errore lettura dati ATEM - tentativo {attempt + 1}: {e}")
                time.sleep(0.1)
        
        # Se dopo 3 tentativi ancora None, solleva eccezione
        if live_source is None or preview_source is None:
            raise Exception(f"Dati ATEM non validi: Live={live_source}, Preview={preview_source}")
        
        # Conversione e parsing MIGLIORATO
        try:
            live_str = str(live_source)
            preview_str = str(preview_source)
            
            # Parsing più robusto
            if live_str.startswith("input"):
                live_num = int(live_str.replace("input", "")) if live_str != "input0" else 0
            else:
                # Gestisci altri formati possibili
                live_num = int(live_str) if live_str.isdigit() else 0
                
            if preview_str.startswith("input"):
                preview_num = int(preview_str.replace("input", "")) if preview_str != "input0" else 0
            else:
                # Gestisci altri formati possibili
                preview_num = int(preview_str) if preview_str.isdigit() else 0
            
            # Validazione range valori
            live_num = max(0, min(live_num, 255))  # Limita a range valido
            preview_num = max(0, min(preview_num, 255))
            
        except (ValueError, AttributeError) as e:
            logger.error(f"Errore parsing dati ATEM: Live='{live_str}', Preview='{preview_str}' - {e}")
            raise Exception(f"Errore parsing: {e}")
        
        # Aggiorna lo stato SOLO se i dati sono cambiati o ogni 10 letture
        with lock:
            changed = False
            if dict_state['Live'] != live_num or dict_state['Preview'] != preview_num:
                changed = True
                dict_state['Live'] = live_num
                dict_state['Preview'] = preview_num
                atem_status['last_live'] = live_num
                atem_status['last_preview'] = preview_num
                logger.info(f"STATO CAMBIATO - Live: {live_num}, Preview: {preview_num}")
            
            dict_state['Autolive'] = 0  # Reset autolive
            atem_status['data_updates'] += 1
        
        # Log periodico per debug (ogni 40 letture = ~10 secondi)
        if atem_status['data_updates'] % 40 == 0 or changed:
            logger.debug(f"ATEM Data - Live: {live_num}, Preview: {preview_num} (Updates: {atem_status['data_updates']})")
        
        # Aggiorna status
        atem_status['connected'] = True
        atem_status['ip'] = ATEM_IP
        atem_status['last_error'] = None
        
        return True
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Errore lettura dati ATEM: {error_msg}")
        
        # Aggiorna status errore
        atem_status['connected'] = False
        atem_status['last_error'] = error_msg
        
        # Disconnetti per forzare riconnessione
        try:
            if atem.connected:
                atem.disconnect()
                logger.debug("ATEM disconnesso dopo errore")
        except:
            pass
            
        return False

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
            self.end_headers()
            self.wfile.write(json.dumps(status_data, indent=2).encode('utf-8'))
            return
        
        # Main web interface
        system_info = get_system_info()
        
        # Status indicators with colors
        atem_status_color = "green" if atem_status['connected'] else "red"
        atem_status_text = "Connesso" if atem_status['connected'] else "Disconnesso"
        
        wifi_status_color = "orange" if wifi_mode_ap else "blue"
        wifi_status_text = "Access Point" if wifi_mode_ap else "Station Mode"
        
        scan_status = "IN CORSO..." if scan_in_progress else "Pronto"
        scan_button_disabled = "disabled" if scan_in_progress else ""
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Tally System Controller</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
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
                .form-section {{ background: #f8f9fa; padding: 20px; border-radius: 6px; margin-bottom: 20px; }}
                .form-group {{ margin-bottom: 15px; }}
                .form-group label {{ display: block; margin-bottom: 5px; font-weight: bold; }}
                .form-group input[type="text"] {{ width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }}
                .form-group input[type="checkbox"] {{ margin-right: 8px; }}
                .btn {{ padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; margin-right: 10px; margin-bottom: 10px; }}
                .btn-primary {{ background-color: #007bff; color: white; }}
                .btn-success {{ background-color: #28a745; color: white; }}
                .btn-warning {{ background-color: #ffc107; color: black; }}
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
            </style>
            <script>
                function refreshStatus() {{
                    fetch('/api/status')
                        .then(response => response.json())
                        .then(data => {{
                            console.log('Status updated:', data);
                            // Aggiorna elementi specifici se necessario
                            updateTallyDisplay(data.tally_state);
                        }})
                        .catch(error => console.error('Error:', error));
                }}
                
                function updateTallyDisplay(state) {{
                    // Questa funzione può essere espansa per aggiornare la UI in tempo reale
                }}
                
                setInterval(refreshStatus, 3000);  // Ogni 3 secondi
                
                function confirmScan() {{
                    return confirm('Avviare la scansione della rete per cercare ATEM? Questo potrebbe richiedere alcuni minuti.');
                }}
            </script>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>?? Tally System Controller</h1>
                    <h2>Powered by Wired-shop</h2>
                    <p>Sistema di controllo tally per mixer ATEM - Versione Corretta v2.1</p>
                </div>
                
                <div class="status-grid">
                    <div class="status-card">
                        <div>Stato ATEM</div>
                        <div class="status-value status-{atem_status_color.lower()}">{atem_status_text}</div>
                        <small>IP: {atem_status['ip'] or 'N/A'}<br>
                        Aggiornamenti dati: {atem_status.get('data_updates', 0)}</small>
                        {f'<br><small style="color:red">Errore: {atem_status["last_error"]}</small>' if atem_status.get('last_error') else ''}
                        <div class="debug-info">
                            Tentativi: {atem_status['connection_attempts']}<br>
                            {f'Ultima connessione: {atem_status["last_connection_time"]}' if atem_status.get('last_connection_time') else 'Mai connesso'}
                        </div>
                    </div>
                    
                    <div class="status-card">
                        <div>Modalita WiFi</div>
                        <div class="status-value status-{wifi_status_color.lower()}">{wifi_status_text}</div>
                        <small>Configurazione di rete attuale</small>
                    </div>
                    
                    <div class="status-card">
                        <div>Stato Scansione</div>
                        <div class="status-value">{scan_status}</div>
                        <small>Ricerca automatica ATEM<br>
                        Scansioni effettuate: {system_info['scan_requests']}</small>
                    </div>
                    
                    <div class="status-card">
                        <div>Sistema</div>
                        <div class="status-value status-green">Online</div>
                        <small>Uptime: {system_info['uptime']}<br>
                        CPU: {system_info['cpu_temp']}<br>
                        RAM: {system_info['memory_usage']}<br>
                        IP: {system_info['local_ip']}</small>
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
                                Abilita modalita Access Point WiFi
                            </label>
                            <small>Crea una rete WiFi dedicata per i dispositivi tally</small>
                        </div>
                        
                        <button type="submit" class="btn btn-primary">Salva Configurazione</button>
                        <button type="submit" name="action" value="scan" class="btn btn-warning" {scan_button_disabled} 
                                onclick="return confirmScan()">Ricerca ATEM</button>
                        <button type="button" onclick="location.reload()" class="btn btn-success">Aggiorna Pagina</button>
                    </form>
                </div>
                
                <div class="status-card">
                    <h3>Stato Tally Corrente - MIGLIORATO</h3>
                    <div><strong>Input Live:</strong> 
                        <span style="color: red; font-weight: bold;">{dict_state['Live'] if dict_state['Live'] > 0 else 'Nessuno'}</span>
                    </div>
                    <div><strong>Input Preview:</strong> 
                        <span style="color: green; font-weight: bold;">{dict_state['Preview'] if dict_state['Preview'] > 0 else 'Nessuno'}</span>
                    </div>
                    <div><strong>Sistema Attivo:</strong> {'Si' if dict_state['isActive'] else 'No'}</div>
                    
                    <div class="tally-display">
                        <div class="tally-indicator tally-live">LIVE<br>{dict_state['Live'] if dict_state['Live'] > 0 else '-'}</div>
                        <div class="tally-indicator tally-preview">PVW<br>{dict_state['Preview'] if dict_state['Preview'] > 0 else '-'}</div>
                    </div>
                    
                    <div class="debug-info">
                        Ultima lettura LIVE: {atem_status.get('last_live', 'N/A')}<br>
                        Ultima lettura PREVIEW: {atem_status.get('last_preview', 'N/A')}<br>
                        Aggiornamenti totali: {atem_status.get('data_updates', 0)}
                    </div>
                </div>
                
                <div class="status-card">
                    <h3>Statistiche Sistema</h3>
                    <div><strong>Pacchetti inviati:</strong> {system_info['total_packets_sent']}</div>
                    <div><strong>Riconnessioni:</strong> {system_info['reconnections']}</div>
                    <div><strong>Scansioni effettuate:</strong> {system_info['scan_requests']}</div>
                    <div><strong>Tentativi connessione:</strong> {atem_status['connection_attempts']}</div>
                    {f'<div><strong>Ultima connessione:</strong> {atem_status["last_connection_time"]}</div>' if atem_status.get('last_connection_time') else ''}
                </div>
                
                <div class="log-section">
                    <h4>Informazioni Tecniche</h4>
                    <p><strong>Multicast:</strong> {MCAST_GRP}:{MCAST_PORT}</p>
                    <p><strong>Intervallo invio:</strong> {TallySendInterval}s</p>
                    <p><strong>File configurazione:</strong> {CONFIG_FILE}</p>
                    <p><strong>Script WiFi:</strong> /home/samuele/Desktop/wifi_ap.sh</p>
                    <p><strong>Debugging:</strong> Attivato logging migliorato per ATEM</p>
                </div>
                
                <div class="refresh-info">
                    Pagina aggiornata automaticamente ogni 3 secondi via JavaScript<br>
                    Versione: Sistema Tally RB v2.1 - Versione Corretta
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
                        # Forza disconnessione per riconnessione con nuovo IP
                        atem_status['connected'] = False
                        try:
                            if atem.connected:
                                atem.disconnect()
                        except:
                            pass
                
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

# =======================================================
# Main Program - VERSIONE MIGLIORATA
# =======================================================
if __name__ == "__main__":
    logger.info("=== Avvio Tally System RB v2.1 - VERSIONE CORRETTA ===")
    
    # Phase 1: Start web server first (as requested)
    logger.info("Fase 1: Avvio web server...")
    webserver_thread = threading.Thread(target=run_webserver, daemon=True)
    webserver_thread.start()
    time.sleep(2)  # Give web server time to start
    logger.info("Web server avviato - Configurazione disponibile su http://[IP]:8080")
    
    # Initialize ATEM object
    atem = PyATEMMax.ATEMMax()
    
    # Phase 2: Try to connect to known ATEM IP first
    if ATEM_IP:
        logger.info(f"Fase 2: Test IP ATEM salvato: {ATEM_IP}")
        if test_atem_connection(ATEM_IP):
            logger.info(f"ATEM confermato su IP salvato: {ATEM_IP}")
            config['last_successful_ip'] = ATEM_IP
            save_config(config)
        else:
            logger.info("IP salvato non risponde, sara necessaria una scansione")
            ATEM_IP = None
    
    # Phase 3: If no working ATEM IP, try to find one
    if not ATEM_IP:
        logger.info("Fase 3: Ricerca ATEM sulla rete...")
        max_retries = 2
        for attempt in range(max_retries):
            logger.info(f"Tentativo {attempt + 1}/{max_retries} ricerca ATEM")
            
            localIP, network = get_local_ip_and_subnet()
            if not localIP:
                logger.error("Impossibile determinare IP locale")
                time.sleep(5)
                continue
                
            logger.info(f"IP Locale: {localIP}, Rete: {network}")
            
            if find_atem(network):
                logger.info("ATEM trovato automaticamente!")
                break
            
            if attempt < max_retries - 1:
                logger.warning(f"ATEM non trovato, riprovo tra 10 secondi...")
                time.sleep(10)
        else:
            logger.warning("ATEM non trovato automaticamente")
            # Use fallback IP but don't save it as successful
            ATEM_IP = FallbackIP
            logger.info(f"Uso IP fallback per permettere configurazione manuale: {ATEM_IP}")

    # Phase 4: Setup multicast socket
    try:
        mcastSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        mcastSock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, TTL)
        logger.info(f"Socket multicast configurato: {MCAST_GRP}:{MCAST_PORT}")
    except Exception as e:
        logger.error(f"Errore setup socket multicast: {e}")
        sys.exit(1)

    # Phase 5: Main tally loop - VERSIONE MIGLIORATA
    logger.info("=== Avvio loop principale tally - VERSIONE CORRETTA ===")
    error_count = 0
    consecutive_errors = 0
    max_consecutive_errors = 10  # Ridotto per reagire più velocemente
    last_successful_read = time.time()
    
    # Variabili per debugging
    loop_count = 0
    last_log_time = time.time()
    
    while True:
        try:
            loop_count += 1
            current_time = time.time()
            
            # Skip ATEM operations if scan is in progress
            if scan_in_progress:
                time.sleep(TallySendInterval)
                continue
            
            # Get data from ATEM
            atem_success = False
            if ATEM_IP:
                atem_success = getAtemData()
            
            # Gestione errori migliorata
            if atem_success:
                consecutive_errors = 0  # Reset error counter on success
                last_successful_read = current_time
                
                # Log periodico di successo (ogni 2 minuti)
                if current_time - last_log_time > 120:
                    logger.info(f"Sistema operativo - Loop: {loop_count}, Live: {dict_state['Live']}, Preview: {dict_state['Preview']}")
                    last_log_time = current_time
                    
            else:
                consecutive_errors += 1
                error_count += 1
                
                # Log più frequente in caso di errori
                if consecutive_errors % 5 == 0:
                    logger.warning(f"Errori consecutivi: {consecutive_errors}/{max_consecutive_errors}")
                
                # Se troppi errori consecutivi, tenta riconnessione
                if consecutive_errors >= max_consecutive_errors:
                    logger.warning(f"Troppi errori consecutivi ({consecutive_errors}), tentativo recovery...")
                    
                    # Disconnetti completamente
                    try:
                        if atem.connected:
                            atem.disconnect()
                            logger.info("ATEM disconnesso per recovery")
                    except:
                        pass
                    
                    # Se l'ultimo successo e troppo vecchio, prova ricerca ATEM
                    if current_time - last_successful_read > 60:  # 1 minuto
                        logger.info("Ultimo successo troppo vecchio, avvio ricerca ATEM...")
                        try:
                            localIP, network = get_local_ip_and_subnet()
                            if localIP and not scan_in_progress:
                                threading.Thread(
                                    target=lambda: find_atem(network, force_scan=False), 
                                    daemon=True
                                ).start()
                        except Exception as e:
                            logger.error(f"Errore avvio ricerca automatica: {e}")
                    
                    consecutive_errors = 0  # Reset counter after recovery attempt
                    time.sleep(2)  # Pausa più lunga dopo recovery
            
            # Reset tally array
            for i in range(len(TallyState)):
                TallyState[i] = Clear
            
            # Set tally states if system is active and ATEM connected
            if dict_state['isActive'] and (atem_success or consecutive_errors < 5):
                # Use thread-safe access to dict_state
                with lock:
                    live_val = dict_state['Live']
                    preview_val = dict_state['Preview']
                    autolive_val = dict_state['Autolive']
                
                # Preview state (only if not in Autolive mode)
                if autolive_val == 0 and preview_val > 0:
                    if preview_val <= len(TallyState):
                        TallyState[preview_val - 1] = Preview
                
                # Live state
                if live_val > 0 and live_val <= len(TallyState):
                    TallyState[live_val - 1] = Live
            
            # Send tally data via multicast
            try:
                mcastSock.sendto(bytearray(TallyState), (MCAST_GRP, MCAST_PORT))
                system_stats['total_packets_sent'] += 1
                
                # Debug periodico del multicast (ogni 5 minuti)
                if system_stats['total_packets_sent'] % 1200 == 0:  # 1200 * 0.25s = 5 min
                    active_tallies = [i+1 for i, state in enumerate(TallyState) if state != Clear]
                    logger.debug(f"Multicast inviato - Pacchetto #{system_stats['total_packets_sent']}, Tally attive: {active_tallies}")
                    
            except Exception as e:
                logger.error(f"Errore invio multicast: {e}")
            
            # Sleep until next cycle
            time.sleep(TallySendInterval)
            
        except KeyboardInterrupt:
            logger.info("Interruzione manuale ricevuta, chiusura sistema...")
            break
        except Exception as e:
            logger.error(f"Errore critico nel loop principale: {e}")
            error_count += 1
            consecutive_errors += 1
            time.sleep(1)
    
    # Cleanup on exit
    logger.info("Avvio procedura di chiusura...")
    try:
        if atem.connected:
            logger.info("Disconnessione ATEM...")
            atem.disconnect()
        
        logger.info("Chiusura socket multicast...")
        mcastSock.close()
        
        logger.info("Sistema chiuso correttamente")
    except Exception as e:
        logger.error(f"Errore durante cleanup: {e}")
    
    logger.info("=== Fine esecuzione Tally System RB v2.1 ===")

# =======================================================
# FINE DEL CODICE CORRETTO
# ======================================================= 

# CHANGELOG v2.1:
# - Funzione test_atem_connection() migliorata con retry e validazione
# - Funzione getAtemData() completamente riscritta con:
#   * Lettura dati con retry (fino a 3 tentativi)
#   * Validazione robusta dei dati ricevuti
#   * Parsing migliorato per gestire formati diversi
#   * Limiti sui valori per evitare overflow
#   * Logging dettagliato per debugging
# - Loop principale ottimizzato con:
#   * Gestione errori più intelligente
#   * Recovery automatico più efficace  
#   * Logging periodico per monitoraggio
#   * Thread-safe access ai dati condivisi
# - Web interface migliorato con:
#   * Informazioni di debug aggiuntive
#   * Stato più dettagliato dell'ATEM
#   * Refresh automatico ogni 3 secondi
# - Aggiunta validazione range per Live/Preview (0-255)
# - Migliorata stabilita connessione ATEM
# - Ottimizzato logging per ridurre spam nei log
