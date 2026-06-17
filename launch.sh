#!/bin/bash

set -euo pipefail

# Rutas en mi entorno
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRSRAN_GNB_DIR="/home/alba/srsRAN_Project/build/apps/gnb"
SRSUE_DIR="/home/alba/srsRAN/build/srsue/src"
MULTIUE_DIR="/home/alba/multiue"
GNB_CONF="$MULTIUE_DIR/srsran/gnb_zmq.yaml"
UE1_CONF="$MULTIUE_DIR/srsran/ue1_zmq.conf"
UE2_CONF="$MULTIUE_DIR/srsran/ue2_zmq.conf"
GRC_SCRIPT="$MULTIUE_DIR/gnuradio/multi_ue_scenario.py" 

# Parámetros 
TUN_IFACE="tun_srsue"
FORWARDER_ZMQ_PORT=5555      # Puerto ZMQ PUSH/PULL
UE_PORT=5006                 # Puerto UDP de los receivers 
PROM_PORT_UE1=8001           # Puerto Prometheus UE1
PROM_PORT_UE2=8002           # Puerto Prometheus UE2
IP_DETECT_RETRIES=60         
IP_DETECT_DELAY=5            # Segundos entre intentos

# Modo single/multi 
SINGLE_UE=false
for arg in "$@"; do
    [[ "$arg" == "--single-ue" ]] && SINGLE_UE=true
done

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
BLU='\033[0;34m'; CYN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${GRN}[+]${NC} $*"; }
info() { echo -e "${BLU}[i]${NC} $*"; }
warn() { echo -e "${YLW}[!]${NC} $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

open_term() {
    local title="$1"; shift
    if command -v gnome-terminal &>/dev/null; then
        gnome-terminal --title="$title" -- bash -c "$*; exec bash" &
    elif command -v xterm &>/dev/null; then
        xterm -title "$title" -e bash -c "$*; exec bash" &
    else
        # Sin entorno gráfico: lanzar en segundo plano con log
        local logfile="/tmp/v2x_${title// /_}.log"
        bash -c "$*" > "$logfile" 2>&1 &
        info "Sin terminal gráfica — log: $logfile"
    fi
}

# Función: detectar IP de UE 
detect_ue_ip() {
    local netns="$1"
    local iface="$2"
    local retries="${3:-$IP_DETECT_RETRIES}"
    local delay="${4:-$IP_DETECT_DELAY}"

    for ((i=1; i<=retries; i++)); do
        ip_out=$(sudo ip netns exec "$netns" ip addr show "$iface" 2>/dev/null || true)
        ip=$(echo "$ip_out" | awk '/inet /{print $2}' | cut -d/ -f1)
        if [[ -n "$ip" ]]; then
            echo "$ip"
            return 0
        fi
        warn "  netns/$netns: IP no detectada aún (intento $i/$retries), esperando ${delay}s..."
        sleep "$delay"
    done
    return 1
}

echo -e "\n${CYN}══════════════════════════════════════════${NC}"
echo -e "${CYN}  V2X sobre 5G — Sistema de orquestación  ${NC}"
if $SINGLE_UE; then
    echo -e "${CYN}  Modo: Single-UE                          ${NC}"
else
    echo -e "${CYN}  Modo: Multi-UE (GNU Radio + UE1 + UE2)  ${NC}"
fi
echo -e "${CYN}══════════════════════════════════════════${NC}\n"

# 0. Limpieza de procesos anteriores 
log "Limpiando procesos anteriores..."
sudo killall -q socat 2>/dev/null || true
sudo rm -f /tmp/prom_ue1.sock /tmp/prom_ue2.sock
# Matar forwarder/receiver anteriores si los hay
pkill -f "python3.*forwarder.py" 2>/dev/null || true
pkill -f "python3.*receiver.py" 2>/dev/null || true
log "Limpieza completada"

# Verificar dependencias 
log "Verificando dependencias..."
command -v python3 >/dev/null || die "python3 no encontrado"
command -v socat >/dev/null   || die "socat no encontrado. Instálalo con: sudo apt install socat"
python3 -c "import asn1tools" 2>/dev/null || die "asn1tools no instalado: pip install asn1tools"
python3 -c "import zmq"       2>/dev/null || die "pyzmq no instalado: pip install pyzmq"
python3 -c "import prometheus_client" 2>/dev/null || die "prometheus-client no instalado"
[[ -f "$SCRIPT_DIR/v2x-app/v2x.asn" ]]     || die "v2x.asn no encontrado en $SCRIPT_DIR/v2x-app"
[[ -f "$SCRIPT_DIR/v2x-app/forwarder.py" ]] || die "forwarder.py no encontrado en $SCRIPT_DIR/v2x-app"
[[ -f "$SCRIPT_DIR/v2x-app/receiver.py" ]]  || die "receiver.py no encontrado en $SCRIPT_DIR/v2x-app"
[[ -f "$GNB_CONF" ]]                || die "Configuración gNB no encontrada: $GNB_CONF"
[[ -f "$UE1_CONF" ]]                || die "Configuración UE1 no encontrada: $UE1_CONF"
if ! $SINGLE_UE; then
    [[ -f "$UE2_CONF" ]]   || die "Configuración UE2 no encontrada: $UE2_CONF"
    [[ -f "$GRC_SCRIPT" ]] || die "Flowgraph GNU Radio no encontrado: $GRC_SCRIPT"
fi
log "Dependencias OK"

# 1. Crear network namespaces 
log "Configurando network namespaces..."
for ns in ue1 $( $SINGLE_UE || echo ue2 ); do
    if ! ip netns list 2>/dev/null | grep -q "^${ns}\b"; then
        sudo ip netns add "$ns"
        log "  Creado netns: $ns"
    else
        info "  netns $ns ya existe"
    fi
    sudo ip netns exec "$ns" ip link set lo up
done
log "Loopback activado en todos los namespaces"

# 2. Lanzar Open5GS 
log "Verificando Open5GS..."
if systemctl is-active --quiet open5gs-amfd 2>/dev/null; then
    info "  Open5GS ya está activo (systemd)"
else
    warn "  Open5GS no detectado en systemd."
    warn "  Asegúrate de arrancarlo manualmente antes de continuar."
    warn "  Puedes usar: sudo systemctl start open5gs-*"
    read -r -p "  ¿Continuar de todos modos? [s/N] " ans
    [[ "$ans" =~ ^[sS]$ ]] || exit 0
fi

# 3. Lanzar gNB
log "Lanzando gNB (srsRAN Project)..."
open_term "gNB" "cd '$SRSRAN_GNB_DIR' && sudo ./gnb -c '$GNB_CONF'"
sleep 3

# 4. Lanzar GNU Radio (solo en modo multi-UE)
if ! $SINGLE_UE; then
    log "Lanzando GNU Radio Companion (multi-UE ZMQ bridge)..."
    open_term "GNU Radio — Multi-UE" "python3 '$GRC_SCRIPT'"
    sleep 5
fi

# 5. Lanzar UE1
log "Lanzando UE1 (srsUE)..."
open_term "UE1 — srsUE" "cd '$SRSUE_DIR' && sudo ./srsue '$UE1_CONF'"
sleep 5

# 6. Lanzar UE2 (solo en modo multi-UE)
if ! $SINGLE_UE; then
    log "Lanzando UE2 (srsUE)..."
    open_term "UE2 — srsUE" "cd '$SRSUE_DIR' && sudo ./srsue '$UE2_CONF'"
    sleep 5
fi

# 7. Detectar IPs asignadas por el UPF
echo ""
warn "Introduce la contraseña en las terminales de los UEs si te la piden."
warn "Espera a ver 'RRC Connected' y 'PDU Session Establishment successful' en los UEs."
read -r -p "  ► Pulsa ENTER cuando los UEs estén conectados... "
echo ""
sudo -v
log "Detectando IPs asignadas por Open5GS UPF..."

UE1_IP=$(detect_ue_ip "ue1" "$TUN_IFACE") \
    || die "No se detectó la IP de UE1 tras $IP_DETECT_RETRIES intentos. ¿Se conectó srsUE?"
log "  UE1 IP: $UE1_IP"

if ! $SINGLE_UE; then
    UE2_IP=$(detect_ue_ip "ue2" "$TUN_IFACE") \
        || die "No se detectó la IP de UE2. ¿Se conectó srsUE correctamente?"
    log "  UE2 IP: $UE2_IP"
fi

# 8. Lanzar forwarder 
log "Lanzando Forwarder (orquestador V2X)..."
FMODE=$( $SINGLE_UE && echo "single" || echo "multi" )
FWD_CMD="cd '$SCRIPT_DIR/v2x-app' && python3 forwarder.py \
    --mode $FMODE \
    --zmq-port $FORWARDER_ZMQ_PORT \
    --ue-port $UE_PORT \
    --ue1-ip '$UE1_IP'"
if ! $SINGLE_UE; then
    FWD_CMD+=" --ue2-ip '$UE2_IP'"
fi
open_term "Forwarder — Orquestador V2X" "$FWD_CMD"
sleep 2

# 9. Lanzar Receivers dentro de sus netns
log "Lanzando Receiver en netns/ue1..."
RCV_CMD1="sudo ip netns exec ue1 bash -c \
    'UE_IP=$UE1_IP UE_PORT=$UE_PORT PROM_PORT=$PROM_PORT_UE1 UE_LABEL=ue1 \
     python3 $SCRIPT_DIR/v2x-app/receiver.py'"
open_term "Receiver — UE1" "$RCV_CMD1"
sleep 2

if ! $SINGLE_UE; then
    log "Lanzando Receiver en netns/ue2..."
    RCV_CMD2="sudo ip netns exec ue2 bash -c \
        'UE_IP=$UE2_IP UE_PORT=$UE_PORT PROM_PORT=$PROM_PORT_UE2 UE_LABEL=ue2 \
         python3 $SCRIPT_DIR/v2x-app/receiver.py'"
    open_term "Receiver — UE2" "$RCV_CMD2"
    sleep 2
fi

# 10. Crear puentes socat para Prometheus
# Actualizar IPs en prometheus.yml
log "Actualizando IPs en prometheus.yml..."
sudo sed -i "s|'10\.45\.[0-9]*\.[0-9]*:8001'|'${UE1_IP}:8001'|g" /etc/prometheus/prometheus.yml
sudo sed -i "s|'10\.45\.[0-9]*\.[0-9]*:8002'|'${UE2_IP}:8002'|g" /etc/prometheus/prometheus.yml
sudo systemctl restart prometheus
log "Prometheus actualizado → UE1=${UE1_IP}:8001  UE2=${UE2_IP}:8002"

# 11. Resumen final 
echo ""
echo -e "${CYN}══════════════════════════════════════════${NC}"
echo -e "${GRN}  Sistema V2X ACTIVO${NC}"
echo -e "${CYN}══════════════════════════════════════════${NC}"
echo -e "  UE1: ${UE1_IP}:${UE_PORT}   (Prometheus → localhost:${PROM_PORT_UE1})"
if ! $SINGLE_UE; then
    echo -e "  UE2: ${UE2_IP}:${UE_PORT}   (Prometheus → localhost:${PROM_PORT_UE2})"
fi
echo ""
echo -e "  ${YLW}Ahora lanza el sender en Windows:${NC}"
echo -e "  python sender.py --host <IP_VM> --port $FORWARDER_ZMQ_PORT"
echo ""
echo -e "  ${YLW}Grafana:${NC}  http://localhost:3000"
echo -e "  ${YLW}Prometheus:${NC}  http://localhost:9091"
echo -e "  ${YLW}Prometheus targets:${NC}  http://localhost:9091/targets"
echo -e "${CYN}══════════════════════════════════════════${NC}"
