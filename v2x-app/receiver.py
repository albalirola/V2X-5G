# -*- coding: utf-8 -*-

import socket
import asn1tools
import os
import time
import pathlib
from prometheus_client import start_http_server, Gauge, Counter

UE_IP     = os.environ.get("UE_IP",     "0.0.0.0")
UE_PORT   = int(os.environ.get("UE_PORT",   "5006"))
PROM_PORT = int(os.environ.get("PROM_PORT", "8000"))
UE_LABEL  = os.environ.get("UE_LABEL",  "ue_unknown")

ASN_PATH = pathlib.Path(__file__).parent / "v2x.asn"
schema   = asn1tools.compile_files(str(ASN_PATH), "uper")

LABELS = ["ue", "vehicle_id"]

g_speed        = Gauge("v2x_speed_kmh",        "Velocidad (km/h)",          LABELS)
g_acceleration = Gauge("v2x_acceleration_ms2", "Aceleración (m/s²)",        LABELS)
g_heading      = Gauge("v2x_heading_deg",       "Rumbo (grados)",            LABELS)
g_throttle     = Gauge("v2x_throttle_pct",      "Acelerador (0-100%)",       LABELS)
g_brake        = Gauge("v2x_brake_pct",         "Freno (0-100%)",            LABELS)
g_steer        = Gauge("v2x_steer_pct",         "Dirección (-100..100)",     LABELS)
g_location_x   = Gauge("v2x_location_x_m",     "Posición X (m)",            LABELS)
g_location_y   = Gauge("v2x_location_y_m",     "Posición Y (m)",            LABELS)
g_angular_vel  = Gauge("v2x_angular_vel_dps",  "Vel. angular (deg/s)",      LABELS)
g_gear         = Gauge("v2x_gear",             "Marcha",                    LABELS)
g_collision    = Gauge("v2x_collision",         "Colisión (0/1)",            LABELS)
g_obstacle     = Gauge("v2x_obstacle",          "Obstáculo (0/1)",           LABELS)
g_latency_ms   = Gauge("v2x_latency_ms",        "Latencia E2E (ms)",         LABELS)
g_seq          = Gauge("v2x_seq_num",           "Número de secuencia",       LABELS)

c_packets_recv = Counter("v2x_packets_received_total", "Paquetes recibidos",  ["ue"])
c_packets_lost = Counter("v2x_packets_lost_total",     "Paquetes perdidos",   ["ue", "vehicle_id"])

# Seguimiento de secuencia y tiempos por vehículo
last_seq: dict = {}
last_time: dict = {}      # Registra el timestamp del último paquete recibido en este receptor
MAX_LOSS_THRESHOLD = 100  


def detect_loss(vehicle_id: int, seq: int):
    now = time.time()
    
    if vehicle_id in last_seq:
        # Calcular el tiempo transcurrido desde el último mensaje recibido de este coche
        time_diff = now - last_time.get(vehicle_id, 0)
        
        if time_diff < 0.25:
            expected = (last_seq[vehicle_id] + 1) % 65536
            if seq != expected:
                lost = (seq - last_seq[vehicle_id] - 1) % 65536
                if 0 < lost < MAX_LOSS_THRESHOLD:
                    c_packets_lost.labels(
                        ue=UE_LABEL, vehicle_id=str(vehicle_id)
                    ).inc(lost)
                    print(
                        f"[WARN] vID={vehicle_id} ~{lost} pkt(s) perdido(s) REALES en red "
                        f"(last={last_seq[vehicle_id]} recv={seq})"
                    )
        else:
            print(
                f"[INFO] vID={vehicle_id} retorno de slice detectado "
                f"(inactivo durante {time_diff:.2f}s). Reajustando secuencia a {seq} sin contar pérdidas."
            )
            
    last_seq[vehicle_id] = seq
    last_time[vehicle_id] = now

min_offset = None

def compute_latency(sent_ts_ms: int) -> float:
    """
    Calcula la latencia E2E en ms estimando y restando el desfase de reloj
    de manera dinámica entre el host Windows y la VM.
    """
    global min_offset
    
    recv_ts = int(time.time() * 1000) & 0xFFFFFFFF
    
    diff_unsigned = (recv_ts - sent_ts_ms) & 0xFFFFFFFF
    
    if diff_unsigned > 0x7FFFFFFF:
        diff_signed = diff_unsigned - 0x100000000
    else:
        diff_signed = diff_unsigned

    if min_offset is None or diff_signed < min_offset:
        min_offset = diff_signed
        print(f"[RELOJ] Nueva calibración de desfase base detectada: {min_offset} ms")

    # La latencia real es la diferencia actual menos el desfase de los relojes
    latency_ms = diff_signed - min_offset

    if latency_ms < 0:
        return 0.0
        
    return float(latency_ms)


def main():
    start_http_server(PROM_PORT)
    print(f"[{UE_LABEL}] Prometheus en http://0.0.0.0:{PROM_PORT}/metrics")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UE_IP, UE_PORT))
    print(f"[{UE_LABEL}] Escuchando en {UE_IP}:{UE_PORT}")

    # Mostrar offset de reloj al arrancar para detectar desincronía
    print(f"[{UE_LABEL}] Timestamp local al arranque: {int(time.time())} "
          f"(compara con el sender para verificar sincronía)")
    print("-" * 55)

    packets = 0

    while True:
        data, _addr = sock.recvfrom(4096)

        try:
            d = schema.decode("VehicleMessage", data)
        except Exception as e:
            print(f"[ERROR] Decodificación fallida: {e}")
            continue

        vid     = d["vehicleID"]
        vid_str = str(vid)
        lbl     = {"ue": UE_LABEL, "vehicle_id": vid_str}

        latency_ms = compute_latency(d["timestamp"])
        detect_loss(vid, d["seqNum"])

        g_speed.labels(**lbl).set(d["speed"] / 10.0)
        g_acceleration.labels(**lbl).set(d["acceleration"] / 100.0)
        g_heading.labels(**lbl).set(d["heading"] / 10.0)
        g_throttle.labels(**lbl).set(d["throttle"])
        g_brake.labels(**lbl).set(d["brake"])
        g_steer.labels(**lbl).set(d["steer"])
        g_location_x.labels(**lbl).set(d["locationX"] / 100.0)
        g_location_y.labels(**lbl).set(d["locationY"] / 100.0)
        g_angular_vel.labels(**lbl).set(d["angularVelocity"] / 100.0)
        g_gear.labels(**lbl).set(d["gear"])
        g_collision.labels(**lbl).set(int(d["collision"]))
        g_obstacle.labels(**lbl).set(int(d["obstacle"]))
        g_latency_ms.labels(**lbl).set(latency_ms)
        g_seq.labels(**lbl).set(d["seqNum"])
        c_packets_recv.labels(ue=UE_LABEL).inc()

        packets += 1
        if packets % 50 == 1:
            print(
                f"[{UE_LABEL}] vID={vid} seq={d['seqNum']} "
                f"speed={d['speed']/10:.1f}km/h "
                f"hdg={d['heading']/10:.1f}° "
                f"lat={latency_ms:.0f}ms "
                f"col={d['collision']} [total={packets}]"
            )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[{UE_LABEL}] Receiver detenido.")
