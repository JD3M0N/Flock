# Guion de Prueba Local en una Sola PC

Esta guia muestra una ejecucion local de Flock con Docker bridge. Cada servidor
corre en su propio contenedor con IP propia, por lo que todos pueden usar los
puertos UDP fijos `12345` y `12346` sin conflicto.

## 1. Levantar el Sistema Desde Cero

Si Docker requiere permisos:

```bash
export FLOCK_DOCKER_CMD="sudo docker"
```

Preparar el entorno:

```bash
python3 scripts/flock_local.py limpiar --yes --logs
python3 scripts/flock_local.py preparar
```

Montar tres nodos servidores:

```bash
python3 scripts/flock_local.py montar-nodo nodo1
python3 scripts/flock_local.py montar-nodo nodo2
python3 scripts/flock_local.py montar-nodo nodo3
python3 scripts/flock_local.py estado
```

Montar dos clientes web:

```bash
python3 scripts/flock_local.py montar-cliente cliente1
python3 scripts/flock_local.py montar-cliente cliente2
```

Abrir:

```text
http://localhost:5001
http://localhost:5002
```

## 2. Funcionalidad Principal

En los clientes web:

1. Buscar servidores.
2. Registrar `Alice` en `cliente1`.
3. Registrar `Bob` en `cliente2`.
4. Enviar mensajes entre ambos.
5. Abrir `/diagnostics`.
6. Mostrar `STATUS`, `SNAPSHOT` y `CHECKSUM`.

## 3. Escribir Estado de Control

Antes de tumbar nodos, escribir registros firmados para poder comprobar que se
mantienen despues de la recuperacion:

```bash
python3 scripts/flock_local.py sembrar-estado --nodo nodo1
python3 scripts/flock_local.py verificar --nodo nodo1
```

La salida debe resolver correctamente:

```text
control_uno
control_dos
control_tres
```

## 4. Escenario Obligatorio de Tolerancia

Ejecutar la secuencia pedida por la rubrica:

```bash
python3 scripts/flock_local.py parar-nodo nodo2
python3 scripts/flock_local.py parar-nodo nodo3
python3 scripts/flock_local.py montar-nodo nodo4
python3 scripts/flock_local.py parar-nodo nodo1
```

Esto equivale a:

```text
3 nodos arriba -> apagar 2 -> levantar 1 nodo nuevo -> apagar el inicial restante
```

## 5. Verificacion de Consistencia

Verificar que el nodo nuevo mantiene estado coherente:

```bash
python3 scripts/flock_local.py verificar --nodo nodo4 --reporte Documentation/prueba_local_estado.json
python3 scripts/flock_local.py admin STATUS --nodo nodo4
python3 scripts/flock_local.py admin SNAPSHOT --nodo nodo4
python3 scripts/flock_local.py admin CHECKSUM --nodo nodo4
```

La verificacion debe mostrar:

- registros de control resueltos con `OK`;
- registros presentes en `SNAPSHOT`;
- conteo y hash estable en `CHECKSUM`;
- reporte escrito en `Documentation/prueba_local_estado.json`.

## 6. Logs Para Mostrar

Logs del nodo nuevo:

```bash
python3 scripts/flock_local.py logs nodo4
```

Vista tabular desde los archivos persistentes:

```bash
FLOCK_LOG_DIR=logs/prueba-local/nodo4 ./tail_logs.sh server
```

Eventos utiles para senalar:

```text
node_joined
replica_written
node_unreachable
fix_started
replica_assimilated
sync_completed
checksum_generated
```

## 7. Prueba Automatizada Opcional

Si queda tiempo, ejecutar la validacion automatizada:

```bash
python3 scripts/flock_local.py acceptance
```

El reporte queda en:

```text
Documentation/acceptance_failure_recovery_report.json
```

## 8. Cierre

Mostrar:

- `Documentation/prueba_local_estado.json`;
- logs del nodo nuevo;
- `video_link.txt`, recordando que el video final debe subirse a `minube.uh.cu`.
