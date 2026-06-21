
import socket
import asn1tools
import time
import subprocess
import argparse
import sys
import zmq

# Argumentos
parser = argparse.ArgumentParser(description="V2X Forwarder — VM (Open5GS/srsRAN)")
parser.add_argument("--zmq-port",   type=int, default=5555,   help="Puerto ZMQ PULL de entrada (desde sender)")
parser.add_argument("--ue-port",    type=int, default=5006,   help="Puerto UDP de salida hacia los receivers en UEs")
parser.add_argument("--mode",       choices=["single", "multi"], default="multi",
                    help="Modo: 'single' (un UE) o 'multi' (varios UEs)")
parser.add_argument("--ue1-netns",  default="ue1",         help="Nombre del netns del UE1")
parser.add_argument("--ue2-netns",  default="ue2",         help="Nombre del netns del UE2")
parser.add_argument("--ue1-ip",     default="",             help="IP fija UE1 (auto-detecta si está vacío)")
parser.add_argument("--ue2-ip",     default="",             help="IP fija UE2 (auto-detecta si está vacío)")
parser.add_argument("--iface",      default="tun_srsue",   help="Interfaz TUN dentro del netns")
parser.add_argument("--hwm",        type=int, default=2000, help="High-water mark ZMQ PULL (default 2000)")
args = parser.parse_args()

import pathlib
ASN_PATH = pathlib.Path(__file__).parent / "v2x.asn"
schema = asn1tools.compile_files(str(ASN_PATH), "uper")


# Detección automática de IP de UEs
def detect_ue_ip(netns: str, iface: str, retries: int = 10, delay: float = 2.0) -> str | None:
    """
    Ejecuta 'ip netns exec <netns> ip addr show <iface>' y extrae la IP.
    Reintenta hasta 'retries' veces esperando 'delay' segundos entre intentos.
    Devuelve la IP como string o None si no se detecta.
    """
    cmd = ["sudo", "ip", "netns", "exec", netns, "ip", "addr", "show", iface]
    for attempt in range(1, retries + 1):
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True)
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    ip = line.split()[1].split("/")[0]
                    return ip
        except subprocess.CalledProcessError:
            pass
        if attempt < retries:
            print(f"  [netns/{netns}] IP no detectada aún (intento {attempt}/{retries}), reintentando en {delay}s...")
            time.sleep(delay)
    return None


# Configuración de UEs
def setup_ues() -> dict[str, tuple[str, int]]:
    """
    Devuelve un dict {"ue1": (ip, port), "ue2": (ip, port), ...}
    Auto-detecta IPs desde los netns si no se proporcionan por argumento.
    """
    ues: dict[str, tuple[str, int]] = {}

    if args.mode == "single":
        ip = args.ue1_ip
        if not ip:
            print(f"[+] Detectando IP de UE1 (netns={args.ue1_netns}, iface={args.iface})...")
            ip = detect_ue_ip(args.ue1_netns, args.iface)
        if not ip:
            print("[ERROR] No se pudo detectar la IP de UE1. Verifica que srsUE esté conectado.")
            sys.exit(1)
        ues["ue1"] = (ip, args.ue_port)
        print(f"  UE1 → {ip}:{args.ue_port}  (single-UE mode)")

    else:  # multi
        for label, netns, forced_ip in [
            ("ue1", args.ue1_netns, args.ue1_ip),
            ("ue2", args.ue2_netns, args.ue2_ip),
        ]:
            ip = forced_ip
            if not ip:
                print(f"[+] Detectando IP de {label} (netns={netns}, iface={args.iface})...")
                ip = detect_ue_ip(netns, args.iface)
            if not ip:
                print(f"[ERROR] No se pudo detectar la IP de {label}. Verifica srsUE.")
                sys.exit(1)
            ues[label] = (ip, args.ue_port)
            print(f"  {label} → {ip}:{args.ue_port}")

    return ues


#  Lógica de orquestación
VEHICLE_UE_MAP: dict[int, str] = {}


def resolve_ue(decoded: dict, ues: dict) -> tuple[str, int] | None:
    """
    Devuelve (ip, port) del UE destino aplicando la lógica de Slicing de Red 5G:
      - UE1 para tráfico crítico 
      - UE2 para telemetría ordinaria 
    """
    vehicle_id = decoded["vehicleID"]
    if vehicle_id in VEHICLE_UE_MAP:
        label = VEHICLE_UE_MAP[vehicle_id]
        return ues.get(label)

    if args.mode == "single":
        return ues.get("ue1")

    # Lógica de clasificacion:
    is_critical = (
        decoded["collision"] or 
        decoded["obstacle"] or 
        decoded.get("brake", 0) > 50 or 
        decoded.get("acceleration", 0) < -300
    )

    label = "ue1" if is_critical else "ue2"
    return ues.get(label)


# Estadísticas
class Stats:
    def __init__(self):
        self.total   = 0
        self.by_ue: dict[str, int] = {}
        self.unknown = 0
        self.errors  = 0
        self.t0 = time.time()

    def record(self, label: str | None):
        self.total += 1
        if label is None:
            self.unknown += 1
        else:
            self.by_ue[label] = self.by_ue.get(label, 0) + 1

    def report(self):
        elapsed = time.time() - self.t0
        rate = self.total / elapsed if elapsed > 0 else 0
        ue_str = "  ".join(f"{k}={v}" for k, v in sorted(self.by_ue.items()))
        print(
            f"\n[STATS] total={self.total}  {ue_str}  "
            f"unknown={self.unknown}  errors={self.errors}  "
            f"rate={rate:.1f} pkt/s\n"
        )


# Main
def main():
    print("=" * 55)
    print(" V2X Forwarder  |  VM Ubuntu  |  Open5GS + srsRAN")
    print(f" Transporte entrada : ZMQ PULL  (puerto {args.zmq_port})")
    print(f" Transporte salida  : UDP       (puerto {args.ue_port} en netns UE)")
    print(f" Modo               : {'Single-UE' if args.mode == 'single' else 'Multi-UE'}")
    print("=" * 55)

    ues = setup_ues()

    zmq_ctx  = zmq.Context()
    zmq_sock = zmq_ctx.socket(zmq.PULL)
    zmq_sock.setsockopt(zmq.RCVHWM, args.hwm)  
    zmq_sock.setsockopt(zmq.LINGER, 0)
    zmq_sock.bind(f"tcp://0.0.0.0:{args.zmq_port}")
    print(f"\n[+] ZMQ PULL escuchando en tcp://0.0.0.0:{args.zmq_port}")

    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print("-" * 55)

    stats = Stats()
    last_report = time.time()
    REPORT_INTERVAL = 10.0

    try:
        while True:
            data = zmq_sock.recv()

            # Decodificar
            try:
                decoded = schema.decode("VehicleMessage", data)
            except Exception as e:
                stats.errors += 1
                print(f"[ERROR] Decodificación fallida: {e}")
                continue

            vid  = decoded["vehicleID"]
            dest = resolve_ue(decoded, ues)

            label = None
            for lbl, (ip, port) in ues.items():
                if dest == (ip, port):
                    label = lbl
                    break

            stats.record(label)

            if dest is None:
                print(f"[WARN] vehicleID={vid} sin UE asignado, descartando")
                continue

            send_sock.sendto(data, dest)

            if stats.total % 50 == 1:
                print(
                    f"[{label}] vID={vid} seq={decoded['seqNum']} "
                    f"speed={decoded['speed']/10:.1f}km/h "
                    f"hdg={decoded['heading']/10:.1f}° "
                    f"col={decoded['collision']} "
                    f"→ {dest[0]}:{dest[1]}"
                )

            # Informe de estadísticas
            now = time.time()
            if now - last_report >= REPORT_INTERVAL:
                stats.report()
                last_report = now

    except KeyboardInterrupt:
        print("\n[+] Forwarder detenido.")
    finally:
        zmq_sock.close()
        zmq_ctx.term()
        send_sock.close()


if __name__ == "__main__":
    main()

