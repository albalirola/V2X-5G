# Diseño, Orquestación y Validación de Servicios V2X sobre 5G (con CARLA, srsRAN y Open5GS)

[![CARLA Simulator](https://img.shields.io/badge/Simulator-CARLA-blue?style=for-the-badge&logo=unrealengine&logoColor=white)](https://carla.org/)
[![srsRAN](https://img.shields.io/badge/5G_RAN-srsRAN_Project-orange?style=for-the-badge)](https://github.com/srsran/srsRAN_Project)
[![Open5GS](https://img.shields.io/badge/5G_Core-Open5GS-red?style=for-the-badge)](https://open5gs.org/)
[![Prometheus](https://img.shields.io/badge/Monitor-Prometheus-E6522C?style=for-the-badge&logo=prometheus&logoColor=white)](https://prometheus.io/)
[![Grafana](https://img.shields.io/badge/Dashboard-Grafana-F46800?style=for-the-badge&logo=grafana&logoColor=white)](https://grafana.com/)
[![Python](https://img.shields.io/badge/Language-Python_3-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)

Este repositorio contiene los scripts y archivos de configuración utilizados en el proyecto de **Simulación, Orquestación y Validación de Servicios V2X sobre 5G**.

La plataforma integra el simulador de conducción **CARLA**, ejecutado en Windows, con una red 5G Standalone emulada mediante **srsRAN**, **Open5GS** y **GNU Radio**, ejecutados en una máquina virtual Ubuntu. Sobre esta infraestructura se implementa una lógica de orquestación que clasifica los mensajes V2X según su criticidad y los redirige hacia dos caminos lógicos diferenciados: uno asociado al tráfico crítico y otro al tráfico ordinario.

El sistema se monitoriza en tiempo real mediante métricas expuestas a **Prometheus** y visualizadas en un panel de control personalizado de **Grafana**.

---

## Arquitectura general del sistema

La comunicación entre el entorno de simulación y la infraestructura 5G emulada se realiza entre el host Windows y la máquina virtual Ubuntu mediante una red local **Host-Only** de VirtualBox. El flujo de datos sigue el siguiente esquema general:


![Arquitectura del Sistema](images/system_architecture.png)

---

## Panel de Control de Grafana (Monitorización en Tiempo Real)

El panel monitoriza la velocidad de los vehículos, los estados de los mandos físicos (acelerador/freno), detección de colisiones, latencia de ida y vuelta (E2E), paquetes recibidos por segundo y pérdidas acumuladas.

![Dashboard de Grafana](images/grafana_telemetry.png)

---

## Estructura del Directorio y Descripción de Archivos

El proyecto se encuentra organizado de forma modular en los siguientes directorios especializados:

```
├── core-5g/ # Configuración del núcleo 5G Open5GS
├── srsran/ # Configuración del gNB y de los terminales srsUE
├── gnuradio/ # Puente ZMQ multi-UE para la emulación radio
├── v2x-app/ # Scripts de envío, recepción y orquestación V2X
├── monitoring/ # Configuración de Prometheus y dashboard de Grafana
├── host-setup/ # Ficheros de soporte para el entorno Windows
└── launch.sh # Script principal de arranque del sistema
```

### Componentes e infraestructura

| Directorio | Archivo | Descripción |
| :--- | :--- | :--- |
| **[`v2x-app/`](v2x-app/)** | **[`sender.py`](v2x-app/sender.py)** | Aplicación Python ejecutada en Windows junto a CARLA. Se conecta a la API de CARLA, selecciona los vehículos V2X, extrae sus variables cinemáticas y eventos de sensores, codifica la información en ASN.1 uPER y la envía a la máquina virtual mediante ZeroMQ PUSH. |
| | **[`forwarder.py`](v2x-app/forwarder.py)** | Módulo de orquestación ejecutado en la máquina virtual Ubuntu. Recibe la telemetría mediante ZeroMQ PULL, decodifica los mensajes `VehicleMessage`, clasifica el tráfico según su criticidad y lo reenvía mediante UDP hacia el camino lógico correspondiente. |
| | **[`receiver.py`](v2x-app/receiver.py)** | Receptor ejecutado dentro del `network namespace` de cada UE virtual. Escucha mensajes UDP en el puerto 5006, decodifica las tramas ASN.1, calcula métricas como latencia extremo a extremo y pérdida estimada de paquetes, y las expone para Prometheus. |
| | **[`v2x.asn`](v2x-app/v2x.asn)** | Esquema ASN.1 definido para este trabajo. Modela el mensaje binario `VehicleMessage`, incluyendo identificación del vehículo, número de secuencia, marca temporal, telemetría, controles y eventos de riesgo. |
| **[`core-5g/`](core-5g/)** | **[`amf.yaml`](core-5g/amf.yaml)** / **[`smf.yaml`](core-5g/smf.yaml)** / **[`upf.yaml`](core-5g/upf.yaml)** | Archivos de configuración principales del núcleo 5G Open5GS. Definen los parámetros de acceso y movilidad, gestión de sesiones PDU, plano de usuario, interfaces de red y conectividad con la red de acceso. |
| | **[`descargar_open5gs.txt`](core-5g/descargar_open5gs.txt)** | Guía básica con comandos de instalación, configuración y arranque de Open5GS en la máquina virtual Ubuntu. |
| **[`srsran/`](srsran/)** | **[`gnb_zmq.yaml`](srsran/gnb_zmq.yaml)** | Configuración principal del gNB de srsRAN Project utilizada en las pruebas. Define los parámetros de la estación base 5G y la conexión con el AMF de Open5GS, configurado en la dirección local `127.0.0.5`. Este es el archivo utilizado por `launch.sh` para arrancar el gNB. |
| | **[`ue1_zmq.conf`](srsran/ue1_zmq.conf)** / **[`ue2_zmq.conf`](srsran/ue2_zmq.conf)** | Ficheros de configuración de los terminales virtuales srsUE. Cada UE se ejecuta en un `network namespace` independiente y se utiliza como extremo de recepción para un camino lógico del sistema. |
| **[`gnuradio/`](gnuradio/)** | **[`multi_ue_scenario.grc`](gnuradio/multi_ue_scenario.grc)** / **[`multi_ue_scenario.py`](gnuradio/multi_ue_scenario.py)** | `Flowgraph` de GNU Radio y script Python generado a partir de él. Se utiliza como puente ZMQ para permitir la conexión simultánea de varios UEs virtuales al mismo gNB, replicando el flujo descendente y combinando el flujo ascendente mediante conexiones punto a punto. |
| **[`monitoring/`](monitoring/)** | **[`prometheus_v2x.yml`](monitoring/prometheus_v2x.yml)** | Configuración de Prometheus para recoger las métricas exportadas por los receptores V2X. Los receptores publican métricas HTTP y Prometheus las consulta periódicamente para almacenarlas. |
| | **[`v2x_dashboard.json`](monitoring/v2x_dashboard.json)** | Dashboard exportado de Grafana. Permite visualizar en tiempo real métricas de red, latencia extremo a extremo, pérdida estimada de paquetes, distribución del tráfico y telemetría vehicular. |
| **Raíz (`/`)** | **[`launch.sh`](launch.sh)** | Script Bash de arranque del sistema en la máquina virtual Ubuntu. Automatiza la creación de `network namespaces`, el lanzamiento del gNB, los UEs virtuales, GNU Radio, el `forwarder`, los receptores y los puentes `socat` necesarios para que Prometheus acceda a las métricas. |

---

## Guía de Despliegue y Ejecución

Sigue detalladamente el orden que se describe a continuación para arrancar el sistema completo:

### 1. Preparación en Windows (CARLA)
1. Inicia CARLA en Windows.
2. Pulsa **Play** para iniciar el entorno de simulación.
3. Abre una consola cmd y genera tráfico vehicular controlado por IA:
   ```cmd
   python C:\carla\PythonAPI\examples\generate_traffic.py -n 10
   ```

### 2. Arranque de la Red 5G en Ubuntu (VM)
1. Accede al directorio del proyecto en la máquina virtual y dale permisos de ejecución al script principal:
   ```bash
   chmod +x launch.sh
   ./launch.sh
   ```
2. Introduce la contraseña de administrador si se te solicita. El script abrirá varias ventanas de terminal para lanzar los componentes del sistema: gNB, GNU Radio, los srsUEs e interfaces virtuales.
3. Comprueba que las consolas de los UEs completan la conexión con la red e indican:
   `RRC Connected` y `PDU Session Establishment successful`.
4. Una vez los terminales se hayan asociado correctamente al núcleo 5G, **pulsa Enter en la terminal principal de `launch.sh`**. A partir de ese momento, el script detectará las IPs asignadas a los UEs, lanzará los receptores dentro de sus network namespaces y configurará los puentes necesarios para la monitorización.

### 3. Conexión y Envío de Telemetría (Windows)
1. Abre una terminal de comandos en Windows en `C:\v2x-5g`.
2. Lanza el transmisor apuntando a la IP de la Máquina Virtual de Ubuntu (por ejemplo, `192.168.56.101`):
   ```cmd
   python v2x-app/sender.py --host 192.168.56.101 --port 5555 --num-v2x 2
   ```
   *El script se conecta a CARLA, selecciona automáticamente los vehículos V2X, extrae su telemetría, codifica los mensajes en ASN.1 uPER y los envía a la máquina virtual mediante ZeroMQ.*
   Para enviar telemetría de un único vehiculo V2X:
   ```cmd
   python v2x-app/sender.py --host 192.168.56.101 --port 5555 --num-v2x 1
   ```
   
### 4. Acceso a las Gráficas
Desde el navegador de la máquina host (Windows), se puede acceder a las siguientes URLs:
*   **Métricas de Prometheus**: `http://192.168.56.101:9091/classic/targets` (verificar que los targets `ue1` y `ue2` se muestran en verde **UP**).
*   **Dashboard de Grafana**: `http://192.168.56.101:3000` (inicia sesión con tus credenciales y abre el panel V2X).

![Configuración de Targets en Prometheus](images/prometheus_targets.png)

---

## Prueba de congestión de tráfico de fondo (iperf3)

Además del tráfico V2X generado desde CARLA, el sistema permite inyectar tráfico UDP de fondo mediante `iperf3` para analizar el comportamiento de los dos caminos lógicos definidos por el orquestador: 
- **Camino crítico (UE1)**: recibe los mensajes V2X clasificados como críticos.
- **Camino ordinario (UE2)**: recibe la telemetría ordinaria y se utiliza para introducir tráfico adicional de fondo.

### 1. Iniciar el Servidor iperf3 en el namespace de UE2
En la terminal de la VM de Ubuntu, lanza el servidor iperf3 escuchando dentro de la red aislada de UE2:
```bash
sudo ip netns exec ue2 iperf3 -s
```

### 2. Inyectar Tráfico UDP hacia UE2
Desde una terminal normal de la VM de Ubuntu, genera tráfico UDP dirigido a la dirección IP asignada al UE2 (por ejemplo, si UE2 tiene la IP `10.45.0.5`):
*   **Carga Ligera(1 Mbps):**
    ```bash
    iperf3 -c 10.45.0.5 -b 1M -t 60
    ```
*   **Carga Moderada (5 Mbps):**
    ```bash
    iperf3 -c 10.45.0.5 -b 5M -t 60
    ```

### 3. Resultados Observados

El comportamiento del sistema bajo tráfico de fondo puede visualizarse en el dashboard de Grafana:

![Saturación del canal eMBB](images/grafana_congestion.png)

Esta prueba no pretende demostrar un aislamiento QoS completo propio de un despliegue 5G comercial, sino evaluar cómo responde el prototipo ante tráfico concurrente y comprobar la utilidad de separar los mensajes V2X por criticidad dentro del banco de pruebas.
