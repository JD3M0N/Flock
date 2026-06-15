# Informe Tecnico Final - Flock

Proyecto de Sistemas Distribuidos - Ciencia de la Computacion

## 1. Arquitectura y Organizacion del Sistema

Flock es una aplicacion de mensajeria distribuida donde los servidores no retransmiten mensajes: mantienen una capa distribuida de identidad, presencia y localizacion, mientras los clientes intercambian mensajes directamente por UDP. La arquitectura combina un anillo DHT inspirado en Chord para los servidores y comunicacion peer-to-peer cifrada entre clientes.

El problema principal es evitar un servidor central de mensajeria y, al mismo tiempo, permitir que un usuario pueda descubrir donde se encuentra otro usuario. Para resolverlo, cada servidor posee un rango del espacio de hashes de nombres de usuario. El cliente puede contactar cualquier servidor conocido; si el dato pertenece a otro rango, la solicitud se enruta hacia el nodo correcto.

Roles principales:

- **Servidor DHT** (`server/server.py`): mantiene rango de hashes, predecessor, successor, lista de sucesores de respaldo, usuarios propios y replicas.
- **Gestor de persistencia de servidor** (`server/db_manager.py`): guarda usuarios propios y registros replicados en SQLite.
- **Cliente Flock** (`client/client.py`): descubre servidores, registra presencia, resuelve contactos, envia mensajes P2P y reintenta mensajes pendientes.
- **Gestor criptografico** (`client/crypto_manager.py`): genera, carga, cifra y migra claves RSA; aplica cifrado hibrido RSA + AES-GCM.
- **Frontend Flask** (`client/ui_flask.py` y `client/templates/`): interfaz web principal para seleccion de nodo, autenticacion, chats y diagnostico.
- **Proxy multicast opcional** (`router/multicast_proxy.py`): apoyo para descubrimiento en redes donde broadcast/multicast necesita puente.

En Docker Swarm, `docker-stack.yml` despliega `flock-server` en modo global y tres clientes web de ejemplo. Se usa red `host` porque Flock depende de broadcast/multicast UDP y puertos fijos para descubrimiento y health checks. Los volumenes persisten bases SQLite de servidores y datos locales de clientes.

## 2. Procesos y Modelo de Concurrencia

Cada servidor ejecuta varios hilos de fondo:

- listener UDP de comandos en el puerto `12345`;
- listener UDP de health checks en `12346`;
- descubrimiento multicast;
- reparacion periodica del anillo;
- mantenimiento de sucesores;
- gestion de replicas y sincronizacion completa periodica;
- logging de estado.

El modelo es multihilo con sockets UDP bloqueantes y timeouts cortos. Esta decision reduce complejidad frente a un event loop completo y encaja con el patron del proyecto: operaciones pequenas, mensajes independientes y tolerancia a fallos parciales por timeout.

Cada cliente tambien usa hilos de fondo para recibir mensajes P2P, reintentar mensajes pendientes y reconectar con otro servidor si el gestor activo cae. La interfaz web usa Flask + Socket.IO en modo `threading`, con eventos WebSocket para actualizar chats, diagnostico y estados de envio.

## 3. Comunicacion y Protocolos

Flock usa tres planos de comunicacion:

- **Servidor-servidor UDP**: `DISCOVER`, `RANGE`, `JOIN`, `PRED_CHANGE`, `SUCC`, `FIX`, `REPLIC`, `TAKEOVER`, `DROP_REPLICS`, `STATUS`, `SNAPSHOT`, `CHECKSUM`, `SYNC_FROM`.
- **Cliente-servidor UDP**: `REGISTER` para publicar presencia e identidad; `RESOLVE` para localizar contactos.
- **Cliente-cliente UDP**: `MESSAGE` cifrado extremo a extremo; `PING/PONG` para detectar disponibilidad.

La serializacion es textual para comandos operativos y JSON compacto para respuestas administrativas (`STATUS`, `SNAPSHOT`, `CHECKSUM`, `SYNC_FROM`). Los timeouts son parte del protocolo: una falta de respuesta se interpreta como fallo parcial probable y activa reparacion, reintento o reconexion.

El flujo normal es:

1. El cliente descubre servidores.
2. Se conecta a un nodo inicial.
3. Registra `username`, IP, puerto, version, clave publica y firma.
4. Para enviar a un contacto, ejecuta `RESOLVE`.
5. Con la IP, puerto y clave publica resuelta, cifra y envia el mensaje directamente al cliente destino.

## 4. Coordinacion y Sincronizacion

La coordinacion se basa en estructura de anillo y versionado de registros:

- el anillo mantiene relaciones `predecessor` y `successor`;
- cada nodo propaga una lista de sucesores de respaldo;
- `FIX` fuerza reparacion cuando se detectan fallos;
- los registros de presencia llevan version para rechazar escrituras obsoletas;
- las firmas atan identidad, direccion, puerto, version y clave publica.

No se implementa consenso fuerte tipo Raft o Paxos. Para este dominio, la decision distribuida principal es determinar el responsable de un hash y reparar el anillo ante fallos. El sistema usa consistencia eventual y reconciliacion practica en lugar de quorum estricto. Esta decision es coherente con el objetivo de mensajeria P2P: los servidores localizan identidades, pero no almacenan el contenido de los mensajes.

Referencias de apoyo: Conf6 para coordinacion y sincronizacion; Conf3 para P2P estructurado.

## 5. Nombrado y Localizacion

El nombrado de usuarios es plano: cada `username` se transforma en un hash dentro del espacio definido en `server/server.py`. Ese hash determina el rango responsable dentro del anillo. La localizacion fisica se resuelve mediante `RESOLVE`, que devuelve IP, puerto P2P, clave publica y version.

Los servicios se localizan mediante:

- UDP broadcast/multicast para descubrir nodos iniciales;
- DNS/host networking de Docker cuando se usa Swarm;
- tablas de predecessor/successor y rangos de hash para enrutar dentro del anillo;
- cache local de contactos en el cliente para acelerar envios posteriores.

Referencias de apoyo: Conf7 para nombrado plano, hashing y localizacion; Conf3 para DHT y factor de replicacion.

## 6. Consistencia y Replicacion

Modelo adoptado: **consistencia eventual para presencia distribuida**. Los mensajes no se replican en servidores; se guardan en la base local de cada cliente y, si el destino esta offline, se insertan en una cola local de reintento.

Cada servidor guarda:

- usuarios propios del rango que administra;
- replicas de usuarios cuyo propietario es otro nodo;
- metadatos de propietario para reconciliacion.

La replicacion se realiza con `REPLIC` hacia nodos de respaldo. Si un propietario cae, un nodo que posee replicas puede asimilar registros mediante `replicants_manager`, reinsertarlos con `place_user_record` y volver a replicarlos. `SYNC_FROM` permite reconciliar replicas de un propietario conocido.

Manejo de particion y recuperacion:

- **Deteccion**: `PING/PONG` con timeouts en puerto `12346`; fallos generan eventos `node_unreachable`.
- **Prevencion de conflictos**: versiones monotonicamente crecientes y validacion de firma; registros obsoletos o con identidad conflictiva se rechazan.
- **Split-brain**: no hay consenso fuerte; el proyecto evita escrituras contradictorias aceptando solo registros con identidad firmada y version valida. La limitacion queda documentada como consistencia eventual, no fuerte.
- **Reconciliacion**: `FIX`, promocion de sucesor, `TAKEOVER`, `DROP_REPLICS`, `SYNC_FROM` y sincronizacion completa periodica.
- **Evidencia**: `SNAPSHOT` lista hashes deterministas de registros propios y replicas; `CHECKSUM` produce un hash estable y conteo de registros para comparar estado.

La prueba oficial esta en `scripts/acceptance_failure_recovery.py` y ejecuta el escenario critico: 3 nodos arriba, apagar 2, levantar 1 nuevo, apagar el inicial restante y verificar resolucion, snapshots y checksums.

Referencias de apoyo: Conf8 para modelos de consistencia, replicacion y trade-off disponibilidad/consistencia.

## 7. Tolerancia a Fallos

Flock tolera fallos no bizantinos de nodos que dejan de responder o desaparecen abruptamente. La estrategia combina:

- sucesores de respaldo;
- reparacion del anillo con `FIX`;
- reconexion automatica de clientes;
- replicas configurables por `FLOCK_FAIL_TOLERANCE`;
- reintento de mensajes offline desde el cliente;
- logs estructurados JSON Lines para auditar eventos.

El nivel esperado es disponibilidad practica con replicacion `n+1` sobre registros de presencia, no tolerancia bizantina. Los clientes siguen conservando su historial local incluso si los servidores fallan, y los mensajes pendientes se reintentan cuando el contacto vuelve a estar disponible.

El escenario obligatorio de tolerancia se valida con:

```bash
python3 scripts/acceptance_failure_recovery.py
```

Para guardar la evidencia JSON de la ejecucion:

```bash
python3 scripts/acceptance_failure_recovery.py --report-file Documentation/acceptance_failure_recovery_report.json
```

El script construye una imagen Docker, levanta nodos, registra usuarios con firma real, captura `SNAPSHOT`/`CHECKSUM`, detiene contenedores y comprueba que el nuevo nodo pueda resolver todos los usuarios registrados.

Referencias de apoyo: Conf9 para fallos parciales y tolerancia.

## 8. Seguridad

La seguridad se concentra en identidad local, autenticacion de presencia y cifrado de mensajes:

- Las contrasenas locales se derivan con PBKDF2 y salt aleatorio (`client/client.py`).
- Las claves privadas RSA se cifran con la contrasena del usuario; claves antiguas sin cifrar se migran automaticamente (`client/crypto_manager.py`).
- Los servidores reciben clave publica, version y firma, pero nunca la clave privada.
- Los mensajes se cifran con esquema hibrido RSA-2048-OAEP + AES-256-GCM.
- Flask usa cookies `HttpOnly`, `SameSite=Lax` y token CSRF para eventos sensibles.
- Los logs sanitizan campos sensibles mediante `shared_logging_utils.py`.

Limitaciones documentadas:

- No se despliega TLS/mTLS entre nodos en la version academica.
- La seguridad de transporte depende de la red de laboratorio o del aislamiento Docker.
- `SESSION_COOKIE_SECURE` esta desactivado para ejecucion local HTTP; debe activarse si se publica sobre HTTPS.

Referencias de apoyo: Conf4 para seguridad, privacidad y fronteras de confianza.

## 9. Ejecucion del Proyecto

### Requisitos previos

- Python 3.10 o superior.
- Docker para pruebas de aceptacion y despliegue.
- Docker Swarm si se despliega en varias maquinas.
- Puertos UDP `12345`, `12346`, `10003` abiertos.
- Puertos TCP `5000`, `5001`, `5002`, `5003` segun clientes web usados.
- En pruebas entre maquinas, definir `FLOCK_NODE_IP=<IP_LAN_SERVIDOR>` en servidores y `FLOCK_PUBLIC_IP=<IP_LAN_CLIENTE>` en clientes.

### Ejecucion local

```bash
git clone <URL_DEL_REPOSITORIO>
cd Flock
python -m venv venv
venv/bin/pip install -r requirements.txt
```

Terminal 1:

```bash
cd server
../venv/bin/python server.py node1
```

Si el servidor corre en otra PC:

```bash
cd server
FLOCK_NODE_IP=<IP_LAN_SERVIDOR> ../venv/bin/python server.py node1
```

Terminal 2:

```bash
cd server
../venv/bin/python server.py node2
```

Terminal 3:

```bash
cd client
../venv/bin/python ui_flask.py
```

Si el cliente corre en otra PC:

```bash
cd client
FLOCK_PUBLIC_IP=<IP_LAN_CLIENTE> ../venv/bin/python ui_flask.py
```

Abrir:

```text
http://localhost:5000
```

Para un segundo cliente local:

```bash
cd client
../venv/bin/python -c "from ui_flask import app, socketio; socketio.run(app, host='0.0.0.0', port=5001, debug=False, allow_unsafe_werkzeug=True)"
```

### Logs y verificacion

```bash
./tail_logs.sh
./tail_logs.sh server
./tail_logs.sh client
.venv/bin/python -m pytest -q
```

Comandos administrativos UDP disponibles:

```text
STATUS
SNAPSHOT
CHECKSUM
SYNC_FROM <owner_ip>
```

### Operacion local en una sola PC

Para grabar la defensa sin VMs ni Swarm, se usa una red bridge local de Docker.
Cada servidor corre en su propio contenedor con IP propia y puede usar los
puertos UDP fijos `12345` y `12346`. Los logs quedan montados en
`logs/prueba-local/`.

Si Docker requiere permisos:

```bash
export FLOCK_DOCKER_CMD="sudo docker"
```

Levantar tres servidores y dos clientes web:

```bash
python3 scripts/flock_local.py limpiar --yes --logs
python3 scripts/flock_local.py preparar
python3 scripts/flock_local.py montar-nodo nodo1
python3 scripts/flock_local.py montar-nodo nodo2
python3 scripts/flock_local.py montar-nodo nodo3
python3 scripts/flock_local.py montar-cliente cliente1
python3 scripts/flock_local.py montar-cliente cliente2
```

Abrir:

```text
http://localhost:5001
http://localhost:5002
```

Sembrar datos firmados para la prueba de consistencia:

```bash
python3 scripts/flock_local.py sembrar-estado --nodo nodo1
python3 scripts/flock_local.py verificar --nodo nodo1
```

Ejecutar manualmente el escenario critico:

```bash
python3 scripts/flock_local.py parar-nodo nodo2
python3 scripts/flock_local.py parar-nodo nodo3
python3 scripts/flock_local.py montar-nodo nodo4
python3 scripts/flock_local.py parar-nodo nodo1
```

Verificar el nodo nuevo:

```bash
python3 scripts/flock_local.py verificar --nodo nodo4 --reporte Documentation/prueba_local_estado.json
python3 scripts/flock_local.py admin STATUS --nodo nodo4
python3 scripts/flock_local.py admin SNAPSHOT --nodo nodo4
python3 scripts/flock_local.py admin CHECKSUM --nodo nodo4
```

Ver logs legibles:

```bash
python3 scripts/flock_local.py logs nodo4
FLOCK_LOG_DIR=logs/prueba-local/nodo4 ./tail_logs.sh server
```

La guia detallada de grabacion esta en `Documentation/guion_prueba_local.md`.

### Prueba oficial de tolerancia

```bash
python3 scripts/acceptance_failure_recovery.py --report-file Documentation/acceptance_failure_recovery_report.json
```

Salida esperada:

- JSON con snapshots iniciales;
- checksum final del nodo nuevo;
- respuestas `OK` para todos los usuarios resueltos;
- mensaje `Acceptance scenario passed.`

### Docker Swarm

Construir imagen:

```bash
docker build -t flock:latest .
```

Inicializar Swarm:

```bash
docker swarm init --advertise-addr <IP_DEL_MANAGER>
docker swarm join-token worker
```

Desplegar:

```bash
docker stack deploy -c docker-stack.yml flock
docker service ls
docker service ps flock_flock-server
docker service logs -f flock_flock-server
```

Clientes de ejemplo:

```text
http://<ip-del-nodo>:5001
http://<ip-del-nodo>:5002
http://<ip-del-nodo>:5003
```

Detener:

```bash
docker stack rm flock
```

## 10. Matriz de Cumplimiento de la Rubrica Final

| Requisito de `informe_final_mundial.pdf` | Cumplimiento en Flock | Evidencia |
| --- | --- | --- |
| Informe tecnico en Markdown/PDF | Cubierto | `informe_tecnico_final.md` en la raiz es el entregable principal; `Documentation/informe_tecnico_final.md` mantiene una copia sincronizada. |
| Codigo fuente y configuracion funcional | Cubierto | `server/`, `client/`, `router/`, `Dockerfile`, `docker-stack.yml`, `requirements.txt`. |
| Instrucciones reproducibles | Cubierto | Seccion 9, `README.md` y `Documentation/guion_prueba_local.md` incluyen ejecucion local, Docker bridge, logs y pruebas. |
| Video en `minube.uh.cu` | Pendiente externo | `video_link.txt` mantiene la marca `PENDIENTE` hasta reemplazarla por el enlace real. |
| Arquitectura y organizacion | Cubierto | DHT tipo Chord, gestores de identidad distribuidos, clientes P2P, proxy multicast opcional. |
| Procesos y concurrencia | Cubierto | Servidor multihilo, cliente con workers de recepcion, reintento y reconexion, Flask Socket.IO. |
| Comunicacion y protocolos | Cubierto | UDP cliente-servidor, UDP servidor-servidor, UDP P2P cifrado, comandos administrativos JSON. |
| Coordinacion y sincronizacion | Cubierto con limitacion declarada | Anillo, predecessor/successor, sucesores de respaldo, versiones monotonicamente crecientes, sin consenso fuerte. |
| Nombrado y localizacion | Cubierto | Hash de `username`, rangos DHT, broadcast/multicast, cache local de contactos. |
| Consistencia y replicacion | Cubierto | Consistencia eventual de presencia, replicas configurables, `SNAPSHOT`, `CHECKSUM`, `SYNC_FROM`. |
| Tolerancia a fallos | Cubierto | Reparacion con `FIX`, health checks, promocion de sucesores, asimilacion de replicas y reconexion cliente. |
| Seguridad | Cubierto con limitacion declarada | PBKDF2, clave privada cifrada, firmas de presencia, RSA+AES-GCM, cookies HttpOnly/SameSite, sin TLS academico local. |
| Prueba 3 up -> 2 down -> 1 up -> 1 down | Cubierto por scripts | `scripts/flock_local.py` para ejecucion manual y `scripts/acceptance_failure_recovery.py --report-file Documentation/acceptance_failure_recovery_report.json` como validacion automatizada. |
| Logs/metricas para defensa | Cubierto | JSONL en `logs/prueba-local/`, `tail_logs.sh`, panel `/diagnostics` con STATUS/SNAPSHOT/CHECKSUM, IP P2P, resolve, ping y cola. |

## 11. Relacion con Principios de las Conferencias

| Tema del curso | Decision de diseno | Trade-off |
| --- | --- | --- |
| Sistemas distribuidos y fallos parciales | No hay relay central de mensajes; los servidores solo resuelven identidad y presencia. | Mayor complejidad de red P2P, menor dependencia de un punto central. |
| Arquitecturas y DHT | Anillo Chord simplificado para particionar el espacio de usuarios. | Busqueda sencilla por rangos; no implementa finger table completa. |
| Procesos | Hilos por servicio de fondo y contenedores para despliegue. | Implementacion comprensible para defensa; requiere cuidado con locks y sockets. |
| Comunicacion | Mensajes UDP con timeouts y comandos autocontenidos. | Baja sobrecarga; la fiabilidad se maneja con reintentos, logs y cola local. |
| Coordinacion | Reparacion del anillo, health checks y versionado de registros. | Consistencia eventual en vez de consenso fuerte. |
| Nombrado | Nombres planos `username` -> hash -> nodo responsable. | Localizacion distribuida simple y cacheable. |
| Consistencia y replicacion | Escritura en nodo propietario y replicas de respaldo. | Disponibilidad practica; convergencia tras reconciliacion. |
| Tolerancia a fallas | Redundancia fisica de nodos y recuperacion hacia adelante. | Tolera crash/omision no bizantina, no respuestas arbitrarias maliciosas. |
| Seguridad | Identidad firmada y cifrado extremo a extremo. | Los servidores no leen contenido; la red local academica no usa TLS/mTLS. |


