#!/bin/bash
# WiFi AP Manager Script (Bridge Mode)
# Uso: ./wifi_ap.sh [on|off|status]

# =======================================================
# Configurazione
# =======================================================
AP_SSID="WS-REGIA VIDEO"
AP_PASS="6019371144"
AP_INTERFACE="wlan0"
BRIDGE_INTERFACE="br0"

# File di configurazione hostapd
HOSTAPD_CONF="/etc/hostapd/hostapd_tally.conf"
HOSTAPD_DEFAULT="/etc/default/hostapd"

# Colori per output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# =======================================================
# Funzioni helper
# =======================================================
log_info()    { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_debug()   { echo -e "${BLUE}[DEBUG]${NC} $1"; }

check_root() {
    [[ $EUID -ne 0 ]] && { log_error "Esegui come root"; exit 1; }
}

check_interface() {
    ip link show $AP_INTERFACE &>/dev/null || { log_error "Interface $AP_INTERFACE non trovata"; exit 1; }
}

install_dependencies() {
    log_info "Controllo dipendenze..."
    for pkg in hostapd bridge-utils; do
        dpkg -l | grep -q "^ii  $pkg " || { log_info "Installazione $pkg"; apt update; apt install -y $pkg; }
    done
}

# =======================================================
# Configurazione hostapd
# =======================================================
create_hostapd_config() {
    log_debug "Creazione hostapd config..."
    cat > $HOSTAPD_CONF << EOF
interface=$AP_INTERFACE
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=7
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=2
wpa_passphrase=$AP_PASS
wpa_key_mgmt=WPA-PSK
wpa_pairwise=TKIP
rsn_pairwise=CCMP
bridge=$BRIDGE_INTERFACE
EOF

    grep -q "DAEMON_CONF=" $HOSTAPD_DEFAULT && \
        sed -i "s|#*DAEMON_CONF=.*|DAEMON_CONF=\"$HOSTAPD_CONF\"|" $HOSTAPD_DEFAULT || \
        echo "DAEMON_CONF=\"$HOSTAPD_CONF\"" >> $HOSTAPD_DEFAULT
}

# =======================================================
# Configurazione bridge
# =======================================================
setup_bridge() {
    if ! ip link show $BRIDGE_INTERFACE &>/dev/null; then
        log_info "Creazione bridge $BRIDGE_INTERFACE..."
        ip link add name $BRIDGE_INTERFACE type bridge
        ip link set eth0 master $BRIDGE_INTERFACE
        ip link set $AP_INTERFACE master $BRIDGE_INTERFACE
        ip link set $BRIDGE_INTERFACE up
        log_info "Bridge $BRIDGE_INTERFACE creato e interfacce aggiunte"
    else
        log_debug "Bridge $BRIDGE_INTERFACE già presente"
        ip link set $BRIDGE_INTERFACE up
    fi
}

# =======================================================
# Funzioni principali
# =======================================================
start_ap() {
    log_info "Avvio WiFi Access Point in modalità BRIDGE..."

    systemctl stop hostapd 2>/dev/null
    command -v nmcli &>/dev/null && nmcli device disconnect $AP_INTERFACE 2>/dev/null

    setup_bridge

    log_debug "Avvio hostapd..."
    systemctl start hostapd
    systemctl is-active --quiet hostapd || { log_error "Errore avvio hostapd"; return 1; }

    log_info "? Access Point attivo in BRIDGE!"
    log_info "   SSID: $AP_SSID"
    log_info "   Password: $AP_PASS"
}

stop_ap() {
    log_info "Arresto WiFi AP..."
    systemctl stop hostapd 2>/dev/null
    ip link set $BRIDGE_INTERFACE down 2>/dev/null
    log_info "? Access Point disattivato"
}

get_status() {
    echo -e "\n${BLUE}=== Stato WiFi AP ===${NC}"
    ip link show $BRIDGE_INTERFACE &>/dev/null && echo -e "Bridge: ${GREEN}$BRIDGE_INTERFACE attivo${NC}" || echo -e "Bridge: ${YELLOW}non presente${NC}"
    systemctl is-active --quiet hostapd && echo -e "Hostapd: ${GREEN}Attivo${NC}" || echo -e "Hostapd: ${RED}Inattivo${NC}"
    echo -e "\nConfigurazione:"
    echo -e "  SSID: ${YELLOW}$AP_SSID${NC}"
}

# =======================================================
# Main
# =======================================================
main() {
    check_root
    check_interface
    install_dependencies
    create_hostapd_config

    case "${1:-status}" in
        "on"|"start") start_ap ;;
        "off"|"stop") stop_ap ;;
        "status") get_status ;;
        *) echo "Uso: $0 [on|off|status]"; exit 1 ;;
    esac
}

main "$@"
