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
python scripts/acceptance_failure_recovery.py
```

Para guardar la evidencia JSON de la ejecucion:

```bash
python scripts/acceptance_failure_recovery.py --report-file Documentation/acceptance_failure_recovery_report.json
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
venv/bin/pytest -q
```

Comandos administrativos UDP disponibles:

```text
STATUS
SNAPSHOT
CHECKSUM
SYNC_FROM <owner_ip>
```

### Prueba oficial de tolerancia

```bash
python scripts/acceptance_failure_recovery.py --report-file Documentation/acceptance_failure_recovery_report.json
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

## 10. Video de Demostracion

El video debe subirse a `minube.uh.cu`, durar como maximo 15 minutos y estar referenciado tambien en `video_link.txt`.

Guion recomendado:

1. Mostrar repositorio, informe y `video_link.txt`.
2. Ejecutar instalacion o despliegue Docker.
3. Abrir UI web y descubrir servidores.
4. Autenticar dos usuarios.
5. Enviar mensajes P2P cifrados.
6. Mostrar panel de diagnostico y cola offline.
7. Ejecutar `scripts/acceptance_failure_recovery.py`.
8. Mostrar `SNAPSHOT`, `CHECKSUM`, logs de deteccion de fallos y resoluciones `OK`.
9. Cerrar con la explicacion del modelo: DHT + replicacion eventual + mensajes P2P cifrados.

## 11. Checklist Pre-entrega

- [ ] Informe final actualizado y versionado.
- [ ] `video_link.txt` con enlace real de minube.uh.cu.
- [ ] `venv/bin/pytest -q` pasa.
- [ ] `python scripts/acceptance_failure_recovery.py` pasa en Docker.
- [ ] Video muestra UI, logs y prueba 3 up -> 2 down -> 1 up -> 1 down.
- [ ] README o informe contiene comandos reproducibles desde cero.
- [ ] No quedan credenciales reales ni datos locales innecesarios en la entrega.
