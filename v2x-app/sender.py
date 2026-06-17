# -*- coding: utf-8 -*-
"""
sender.py — CARLA V2X sender 

Uso:
    python sender.py --host 192.168.56.101 --port 5555 --num-v2x 1
    python sender.py --host 192.168.56.101 --port 5555 --num-v2x 2

"""

import time
import math
import argparse
import threading
import random
import zmq
import carla
import asn1tools

parser = argparse.ArgumentParser()
parser.add_argument("--host", default="192.168.56.101")
parser.add_argument("--port", type=int, default=5555)
parser.add_argument("--carla", default="localhost")
parser.add_argument("--carla-port", type=int, default=2000)
parser.add_argument("--interval", type=float, default=0.1)
parser.add_argument("--hwm", type=int, default=1000)
parser.add_argument("--num-v2x", type=int, default=2)
parser.add_argument("--selection", choices=["first", "nearest", "random"], default="first")
args = parser.parse_args()

import pathlib
ASN_PATH = pathlib.Path(__file__).parent / "v2x.asn"
schema = asn1tools.compile_files(str(ASN_PATH), "uper")

zmq_ctx = zmq.Context()
zmq_sock = zmq_ctx.socket(zmq.PUSH)
zmq_sock.setsockopt(zmq.SNDHWM, args.hwm)
zmq_sock.setsockopt(zmq.LINGER, 500)
zmq_sock.setsockopt(zmq.RECONNECT_IVL, 100)
zmq_sock.setsockopt(zmq.RECONNECT_IVL_MAX, 5000)

ZMQ_ENDPOINT = f"tcp://{args.host}:{args.port}"
zmq_sock.connect(ZMQ_ENDPOINT)

seq_counters = {}
seq_lock = threading.Lock()

collision_flags = {}
obstacle_flags = {}


def next_seq(vehicle_id: int) -> int:
    with seq_lock:
        seq_counters[vehicle_id] = (seq_counters.get(vehicle_id, -1) + 1) % 65536
        return seq_counters[vehicle_id]


def select_v2x_vehicles(world, num_v2x, mode="first"):
    vehicles = list(world.get_actors().filter("vehicle.*"))

    if not vehicles:
        raise RuntimeError("No hay vehículos en CARLA. Ejecuta primero generate_traffic.py.")

    num_v2x = min(num_v2x, len(vehicles))

    if mode == "first":
        return vehicles[:num_v2x]

    if mode == "nearest":
        origin = carla.Location(x=0.0, y=0.0, z=0.0)
        return sorted(
            vehicles,
            key=lambda v: v.get_transform().location.distance(origin)
        )[:num_v2x]

    if mode == "random":
        return random.sample(vehicles, num_v2x)

    return vehicles[:num_v2x]


def update_spectator_camera(world, vehicle):
    spectator = world.get_spectator()
    transform = vehicle.get_transform()

    spectator.set_transform(
        carla.Transform(
            transform.location
            + transform.get_forward_vector() * -8
            + carla.Location(z=4),
            carla.Rotation(
                pitch=-15,
                yaw=transform.rotation.yaw,
                roll=0
            )
        )
    )


def encode_vehicle(vehicle, col_flag, obs_flag):
    try:
        vel = vehicle.get_velocity()
        accel = vehicle.get_acceleration()
        trans = vehicle.get_transform()
        ctrl = vehicle.get_control()
        angv = vehicle.get_angular_velocity()

        speed_ms = math.sqrt(vel.x**2 + vel.y**2 + vel.z**2)
        speed_enc = int(round(speed_ms * 3.6 * 10))

        accel_signed = accel.x if abs(accel.x) > 0.01 else math.sqrt(
            accel.x**2 + accel.y**2 + accel.z**2
        )
        accel_enc = max(-2000, min(2000, int(round(accel_signed * 100))))

        yaw_deg = trans.rotation.yaw % 360
        heading = int(round(yaw_deg * 10)) % 3601

        loc_x = max(-2000000, min(2000000, int(round(trans.location.x * 100))))
        loc_y = max(-2000000, min(2000000, int(round(trans.location.y * 100))))

        ang_vel = max(-36000, min(36000, int(round(angv.z * 100))))
        throttle = max(0, min(100, int(round(ctrl.throttle * 100))))
        brake = max(0, min(100, int(round(ctrl.brake * 100))))
        steer = max(-100, min(100, int(round(ctrl.steer * 100))))
        gear = max(0, min(10, int(ctrl.gear) + 1))

        ts_ms = int(time.time() * 1000) & 0xFFFFFFFF

        msg = {
            "vehicleID": int(vehicle.id),
            "seqNum": next_seq(int(vehicle.id)),
            "timestamp": ts_ms,
            "speed": speed_enc,
            "acceleration": accel_enc,
            "heading": heading,
            "locationX": loc_x,
            "locationY": loc_y,
            "angularVelocity": ang_vel,
            "throttle": throttle,
            "brake": brake,
            "steer": steer,
            "gear": gear,
            "collision": col_flag,
            "obstacle": obs_flag,
        }

        payload = schema.encode("VehicleMessage", msg)
        return payload, msg

    except Exception as e:
        print(f"[ERROR] encode_vehicle({vehicle.id}): {e}")
        return None, None


def setup_collision_sensor(world, vehicle):
    bp = world.get_blueprint_library().find("sensor.other.collision")
    sensor = world.spawn_actor(bp, carla.Transform(), attach_to=vehicle)
    sensor.listen(lambda event: collision_flags.__setitem__(vehicle.id, True))
    return sensor


def setup_obstacle_sensor(world, vehicle):
    bp = world.get_blueprint_library().find("sensor.other.obstacle")
    bp.set_attribute("distance", "5.0")

    if bp.has_attribute("only_dynamics"):
        bp.set_attribute("only_dynamics", "true")

    transform = carla.Transform(carla.Location(x=2.5, z=1.0))
    sensor = world.spawn_actor(bp, transform, attach_to=vehicle)
    sensor.listen(lambda event: obstacle_flags.__setitem__(vehicle.id, True))
    return sensor


def main():
    print("=" * 60)
    print(f" V2X Sender | ZMQ PUSH → {ZMQ_ENDPOINT}")
    print(f" Vehículos V2X : {args.num_v2x} | {1 / args.interval:.0f} Hz")
    print("=" * 60)

    client = carla.Client(args.carla, args.carla_port)
    client.set_timeout(30.0)

    world = client.get_world()
    print(f"[+] Conectado a CARLA: {world.get_map().name}")

    v2x_vehicles = select_v2x_vehicles(world, args.num_v2x, args.selection)
    v2x_vehicle_ids = {v.id for v in v2x_vehicles}

    camera_vehicle = v2x_vehicles[0]

    print(f"[+] Vehículos V2X: {sorted(v2x_vehicle_ids)}")
    print(f"[+] Cámara siguiendo al vehículo V2X: {camera_vehicle.id}")

    col_sensors = [setup_collision_sensor(world, v) for v in v2x_vehicles]
    obs_sensors = [setup_obstacle_sensor(world, v) for v in v2x_vehicles]

    print("[+] Sensores adjuntados. Enviando...")

    packets_sent = 0

    try:
        while True:
            t0 = time.time()

            try:
                update_spectator_camera(world, camera_vehicle)
            except Exception as e:
                print(f"[WARN] No se pudo actualizar la cámara: {e}")

            for vehicle in world.get_actors().filter("vehicle.*"):
                if vehicle.id not in v2x_vehicle_ids:
                    continue

                col = collision_flags.pop(vehicle.id, False)
                obs = obstacle_flags.pop(vehicle.id, False)

                payload, msg = encode_vehicle(vehicle, col, obs)

                if payload is None:
                    continue

                zmq_sock.send(payload)
                packets_sent += 1

                if packets_sent % 100 == 0:
                    print(
                        f"[{packets_sent}] "
                        f"vID={vehicle.id} "
                        f"seq={msg['seqNum']} "
                        f"speed={msg['speed'] / 10:.1f}km/h "
                        f"hdg={msg['heading'] / 10:.1f}° "
                        f"brake={msg['brake']}% "
                        f"col={msg['collision']} "
                        f"obs={msg['obstacle']}"
                    )

            time.sleep(max(0.0, args.interval - (time.time() - t0)))

    except KeyboardInterrupt:
        print(f"\n[+] Detenido. Paquetes enviados: {packets_sent}")

    finally:
        for sensor in col_sensors + obs_sensors:
            try:
                sensor.destroy()
            except Exception:
                pass

        zmq_sock.close()
        zmq_ctx.term()


if __name__ == "__main__":
    main()

