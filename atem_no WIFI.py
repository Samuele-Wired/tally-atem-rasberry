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

# =======================================================
# Variables
# =======================================================
TallyState = [0 for _ in range(256)]
dict_state = {'Live': 0, 'Preview': 0, 'Autolive': 0, 'isActive': True}  # FIX: isActive ora è True
ATEM_IP = None
wifi_mode_ap = False
lock = threading.Lock()
atem_status = {'connected': False, 'ip': None}


# =======================================================
# Helper functions
# =======================================================
def get_local_ip_and_subnet():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        
        # Migliore gestione della subnet
        try:
            if platform.system().lower() == "linux":
                result = subprocess.check_output(
                    ["ip", "route", "show", "default"], 
                    universal_newlines=True,
                    timeout=5
                )
                # Estrai la rete dalla route di default
                lines = result.strip().split('\n')
                for line in lines:
                    if 'dev' in line:
                        # Assumiamo /24 per semplicita
                        network = ipaddress.IPv4Interface(f"{ip}/24").network
                        return ip, network
        except:
            pass
            
        # Fallback: assumiamo /24
        network = ipaddress.IPv4Interface(f"{ip}/24").network
        return ip, network
        
    except Exception as e:
        logger.error(f"Errore nel recupero IP locale: {e}")
        return None, ipaddress.IPv4Network("192.168.1.0/24", strict=False)


def ping(host):
    """Ping singolo per uso generale - manteniamo per compatibilita"""
    try:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        command = ["ping", param, "1", "-W", "500", host]  # timeout 500ms
        return subprocess.call(
            command, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            timeout=1
        ) == 0
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def ping_host(ip_str):
    """Ping singolo host - funzione per threading"""
    try:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        command = ["ping", param, "1", "-W", "500", ip_str]  # timeout 500ms
        result = subprocess.call(
            command, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            timeout=1
        )
        return (ip_str, result == 0)
    except:
        return (ip_str, False)


def find_atem(network):
    global ATEM_IP
    logger.info(f"Scansione rete {network} per ATEM...")

    # Prima prova con IP gia noto se esiste
    if ATEM_IP and test_atem_connection(ATEM_IP):
        logger.info(f"ATEM gia configurato e funzionante: {ATEM_IP}")
        return True

    # Fase 1: Ping parallelo di tutti gli IP della rete
    logger.info("Fase 1: Ping parallelo della rete...")
    
    alive_hosts = []
    
    # Crea lista di tutti gli IP da testare
    all_ips = [str(ip) for ip in network.hosts()]
    total_ips = len(all_ips)
    logger.info(f"Scansione {total_ips} IP in parallelo...")
    
    # Ping parallelo con ThreadPoolExecutor
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        # Avvia tutti i ping in parallelo
        future_to_ip = {executor.submit(ping_host, ip): ip for ip in all_ips}
        
        completed = 0
        for future in concurrent.futures.as_completed(future_to_ip, timeout=10):
            completed += 1
            try:
                ip_str, is_alive = future.result()
                if is_alive:
                    alive_hosts.append(ip_str)
                    logger.info(f"Host vivo trovato: {ip_str} ({completed}/{total_ips})")
                elif completed % 20 == 0:  # Log progresso ogni 20 IP
                    logger.info(f"Progresso ping: {completed}/{total_ips}")
            except Exception as e:
                logger.debug(f"Errore ping: {e}")
    
    logger.info(f"Ping completato. Host vivi trovati: {len(alive_hosts)}")
    
    if not alive_hosts:
        logger.warning("Nessun host risponde al ping")
        return False
    
    # Fase 2: Test ATEM sequenziale sugli host vivi
    logger.info("Fase 2: Test ATEM sugli host che rispondono...")
    
    for i, ip_str in enumerate(alive_hosts, 1):
        logger.info(f"Test ATEM {i}/{len(alive_hosts)}: {ip_str}")
        
        if test_atem_connection(ip_str):
            ATEM_IP = ip_str
            logger.info(f"?? ATEM trovato a {ATEM_IP}!")
            return True
    
    logger.warning("Nessun ATEM trovato tra gli host vivi")
    return False


def test_atem_connection(ip_str):
    """Test specifico per connessione ATEM"""
    try:
        test_atem = PyATEMMax.ATEMMax()
        test_atem.connect(ip_str)
        
        # Attendi connessione con timeout ridotto
        if not test_atem.waitForConnection(timeout=2.0):
            test_atem.disconnect()
            return False
            
        # Verifica che possiamo leggere i dati
        live_val = str(test_atem.programInput[0].videoSource)
        preview_val = str(test_atem.previewInput[0].videoSource)
        
        logger.info(f"? ATEM confermato {ip_str} - Live: {live_val}, Preview: {preview_val}")
        test_atem.disconnect()
        return True
        
    except Exception as e:
        logger.debug(f"? Test ATEM fallito per {ip_str}: {e}")
        try:
            test_atem.disconnect()
        except:
            pass
        return False


def getAtemData():
    global atem_status
    try:
        if not atem.connected:
            logger.info(f"Connessione a ATEM {ATEM_IP}...")
            atem.connect(ATEM_IP)
            if not atem.waitForConnection(timeout=2.0):
                raise Exception("Timeout connessione ATEM")

        # Leggi i dati dall'ATEM
        live_source = str(atem.programInput[0].videoSource)
        preview_source = str(atem.previewInput[0].videoSource)
        
        # Converti da "inputX" a numero
        dict_state['Live'] = int(live_source.replace("input", "")) if live_source != "input0" else 0
        dict_state['Preview'] = int(preview_source.replace("input", "")) if preview_source != "input0" else 0
        dict_state['Autolive'] = 0

        atem_status['connected'] = True
        atem_status['ip'] = ATEM_IP
        
        logger.debug(f"ATEM Data - Live: {dict_state['Live']}, Preview: {dict_state['Preview']}")
        return True
        
    except Exception as e:
        logger.error(f"Errore lettura dati ATEM: {e}")
        atem_status['connected'] = False
        atem_status['ip'] = None
        
        # Prova a riconnettersi
        try:
            if atem.connected:
                atem.disconnect()
        except:
            pass
            
        return False


def setWifiMode(ap_mode=False):
    global wifi_mode_ap
    wifi_mode_ap = ap_mode
    
    script_path = "/home/samuele/Desktop/wifi_ap.sh"
    
    # Verifica che lo script esista
    import os
    if not os.path.exists(script_path):
        logger.warning(f"Script WiFi AP non trovato: {script_path}")
        return
    
    try:
        if ap_mode:
            subprocess.run(["sudo", script_path, "on"], timeout=10, check=True)
            logger.info("WiFi AP mode attivato")
        else:
            subprocess.run(["sudo", script_path, "off"], timeout=10, check=True)
            logger.info("WiFi AP mode disattivato")
    except subprocess.TimeoutExpired:
        logger.error("Timeout esecuzione script WiFi")
    except subprocess.CalledProcessError as e:
        logger.error(f"Errore esecuzione script WiFi: {e}")
    except Exception as e:
        logger.error(f"Errore imprevisto WiFi script: {e}")


# =======================================================
# Webserver per configurazione
# =======================================================
class ConfigHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Sopprimi log HTTP del server
        pass
        
    def do_GET(self):
        html = f"""
        <!DOCTYPE html>
        <html><head><title>Tally Config RB</title></head><body>
        <h2>Tally Config RB</h2>
        <p><strong>Stato ATEM:</strong> {"Connesso" if atem_status['connected'] else "Non Connesso"}</p>
        <p><strong>Indirizzo ATEM:</strong> {atem_status['ip'] if atem_status['ip'] else "N/A"}</p>
        <p><strong>Live Input:</strong> {dict_state['Live']}</p>
        <p><strong>Preview Input:</strong> {dict_state['Preview']}</p>
        <p><strong>WiFi AP Mode:</strong> {"Attivo" if wifi_mode_ap else "Disattivo"}</p>
        
        <form method="POST">
            <label>ATEM IP: <input type="text" name="atem_ip" value="{ATEM_IP if ATEM_IP else ''}" required></label><br><br>
            <label>Wi-Fi AP: <input type="checkbox" name="wifi_ap" {'checked' if wifi_mode_ap else ''}></label><br><br>
            <input type="submit" value="Applica Configurazione">
        </form>
        
        <h3>Log Sistema</h3>
        <p>Controlla i log con: <code>journalctl -f</code> o <code>python3 script.py</code></p>
        </body></html>"""
        
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(length).decode('utf-8')
            params = urllib.parse.parse_qs(post_data)
            
            global ATEM_IP
            if "atem_ip" in params and params["atem_ip"][0].strip():
                new_ip = params["atem_ip"][0].strip()
                if new_ip != ATEM_IP:
                    ATEM_IP = new_ip
                    logger.info(f"ATEM IP aggiornato a: {ATEM_IP}")
                    # Reset connessione per forzare riconnessione
                    atem_status['connected'] = False
            
            new_wifi_mode = "wifi_ap" in params
            if new_wifi_mode != wifi_mode_ap:
                setWifiMode(ap_mode=new_wifi_mode)
            
            self.send_response(303)
            self.send_header('Location', '/')
            self.end_headers()
            
        except Exception as e:
            logger.error(f"Errore elaborazione POST: {e}")
            self.send_error(500, "Errore interno")


def run_webserver():
    try:
        server = HTTPServer(('', 8080), ConfigHandler)
        logger.info("Web server avviato su porta 8080")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Errore web server: {e}")


# =======================================================
# Main
# =======================================================
if __name__ == "__main__":
    logger.info("=== Avvio Tally System RB ===")
    
    atem = PyATEMMax.ATEMMax()
    
    # Fase 1: Trova ATEM
    max_retries = 3
    for attempt in range(max_retries):
        logger.info(f"Tentativo {attempt + 1}/{max_retries} di ricerca ATEM")
        
        localIP, network = get_local_ip_and_subnet()
        if not localIP:
            logger.error("Impossibile determinare IP locale")
            time.sleep(5)
            continue
            
        logger.info(f"IP Locale: {localIP}, Rete: {network}")
        
        if find_atem(network):
            logger.info("ATEM trovato, avvio sistema...")
            break
        
        if attempt < max_retries - 1:
            logger.warning(f"ATEM non trovato, riprovo tra 10 secondi...")
            time.sleep(10)
    else:
        logger.error("ATEM non trovato dopo tutti i tentativi")
        # Continua comunque per permettere configurazione manuale
        ATEM_IP = FallbackIP
        logger.info(f"Uso IP fallback: {ATEM_IP}")

    # Fase 2: Avvia web server
    webserver_thread = threading.Thread(target=run_webserver, daemon=True)
    webserver_thread.start()
    
    # Fase 3: Setup socket multicast
    try:
        mcastSock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        mcastSock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, TTL)
        logger.info(f"Socket multicast configurato: {MCAST_GRP}:{MCAST_PORT}")
    except Exception as e:
        logger.error(f"Errore setup socket multicast: {e}")
        sys.exit(1)

    # Fase 4: Loop principale
    logger.info("=== Avvio loop principale ===")
    error_count = 0
    max_errors = 10
    
    while True:
        try:
            # Ottieni dati da ATEM
            success = getAtemData()
            
            if success:
                error_count = 0  # Reset contatore errori
            else:
                error_count += 1
                if error_count >= max_errors:
                    logger.error(f"Troppi errori consecutivi ({error_count}), riavvio ricerca ATEM")
                    # Riprova a trovare ATEM
                    localIP, network = get_local_ip_and_subnet()
                    if localIP:
                        find_atem(network)
                    error_count = 0
            
            # Reset array tally
            for i in range(len(TallyState)):
                TallyState[i] = Clear
            
            # Imposta stati tally se attivo
            if dict_state['isActive'] and success:
                # Preview (solo se non in Autolive mode)
                if dict_state['Autolive'] == 0 and dict_state['Preview'] > 0:
                    if dict_state['Preview'] <= len(TallyState):
                        TallyState[dict_state['Preview'] - 1] = Preview
                
                # Live
                if dict_state['Live'] > 0 and dict_state['Live'] <= len(TallyState):
                    TallyState[dict_state['Live'] - 1] = Live
            
            # Invia dati tally
            try:
                mcastSock.sendto(bytearray(TallyState), (MCAST_GRP, MCAST_PORT))
            except Exception as e:
                logger.error(f"Errore invio multicast: {e}")
            
            time.sleep(TallySendInterval)
            
        except KeyboardInterrupt:
            logger.info("Interruzione manuale, chiusura...")
            break
        except Exception as e:
            logger.error(f"Errore nel loop principale: {e}")
            error_count += 1
            time.sleep(1)
    
    # Cleanup
    try:
        if atem.connected:
            atem.disconnect()
        mcastSock.close()
        logger.info("Cleanup completato")
    except:
        pass