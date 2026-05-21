# Docker Swarm para Flock

Esta guia explica como ejecutar Flock en Docker Swarm usando una imagen Docker comun para servidores y clientes.

Flock usa descubrimiento por UDP broadcast/multicast y puertos UDP fijos (`12345`, `12346` y `10003`). Por eso el stack usa la red `host`: una red overlay de Swarm no se comporta igual que una LAN fisica para este tipo de descubrimiento.

## Requisitos

- Docker instalado en todas las maquinas.
- Todas las maquinas en la misma LAN, o con conectividad directa que permita broadcast/multicast UDP.
- Puertos permitidos en el firewall:
  - UDP `12345`: comandos entre servidores y busqueda de servidores.
  - UDP `12346`: health checks entre servidores.
  - UDP `10003`: descubrimiento multicast.
  - TCP `5001`, `5002`, `5003`: clientes Flask de ejemplo.
- La imagen `flock:latest` debe existir en cada nodo, o debe publicarse en un registry accesible por todo el Swarm.

## Construir la imagen

Desde la raiz del proyecto:

```bash
docker build -t flock:latest .
```

Importante: `docker stack deploy` no construye imagenes. En un Swarm de varias maquinas tienes dos opciones:

1. Construir `flock:latest` manualmente en cada nodo.
2. Publicar la imagen en un registry y cambiar `image: flock:latest` en `docker-stack.yml` por el nombre publicado, por ejemplo `usuario/flock:latest`.

## Inicializar Docker Swarm

En la maquina que sera manager:

```bash
docker swarm init --advertise-addr <IP_DEL_MANAGER>
```

Obtiene el comando para unir workers:

```bash
docker swarm join-token worker
```

Ejecuta en cada otra maquina el comando que imprime Docker. Tiene esta forma:

```bash
docker swarm join --token <TOKEN> <IP_DEL_MANAGER>:2377
```

Comprueba los nodos desde el manager:

```bash
docker node ls
```

## Desplegar Flock

Desde el manager, en la raiz del proyecto:

```bash
docker stack deploy -c docker-stack.yml flock
```

El servicio `flock-server` esta en modo `global`, asi que Swarm intenta levantar un servidor Flock en cada nodo. Los clientes de ejemplo se levantan como servicios separados:

- `client-alice`: escucha en `http://<ip-del-nodo>:5001`
- `client-bob`: escucha en `http://<ip-del-nodo>:5002`
- `client-ana`: escucha en `http://<ip-del-nodo>:5003`

Como se usa red `host`, no se usa la seccion `ports` de Docker. Cada cliente arranca Flask directamente en el puerto del host que le corresponde.

## Comandos de operacion

Ver servicios:

```bash
docker service ls
```

Ver en que nodos estan corriendo las tareas:

```bash
docker service ps flock_flock-server
docker service ps flock_client-alice
docker service ps flock_client-bob
docker service ps flock_client-ana
```

Ver logs de servidores:

```bash
docker service logs -f flock_flock-server
```

Ver logs de un cliente:

```bash
docker service logs -f flock_client-alice
```

Eliminar el stack:

```bash
docker stack rm flock
```

## Probar la aplicacion

1. Abre uno de los clientes:

   ```text
   http://<ip-del-nodo>:5001
   ```

2. Pulsa la opcion para buscar servidores.
3. Registra un usuario.
4. Abre otro cliente, por ejemplo:

   ```text
   http://<ip-del-nodo>:5002
   ```

5. Busca servidores, registra otro usuario y prueba enviar mensajes entre ambos.

## Diagnostico rapido

Si los clientes no encuentran servidores:

```bash
docker service logs -f flock_flock-server
```

Comprueba tambien conectividad entre maquinas:

```bash
ping <IP_DE_OTRO_NODO>
```

Y revisa que el firewall no bloquee UDP `12345`, UDP `12346` ni UDP `10003`.

Si un cliente no abre en el navegador, revisa en que nodo quedo ejecutandose:

```bash
docker service ps flock_client-alice
```

Luego abre el puerto en la IP de ese nodo:

```text
http://<ip-del-nodo-donde-corre-client-alice>:5001
```
