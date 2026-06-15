#!/usr/bin/env python3
"""Operacion local de Flock para defensa tecnica en una sola PC."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("FLOCK_LOCAL_IMAGE", "flock:local")
NETWORK = os.environ.get("FLOCK_LOCAL_NETWORK", "flock-local-net")
OLD_NETWORKS = ["flock-manual-net"]
LOG_ROOT = Path(os.environ.get("FLOCK_LOCAL_LOG_ROOT", ROOT_DIR / "logs" / "prueba-local")).resolve()
FAIL_TOLERANCE = os.environ.get("FLOCK_FAIL_TOLERANCE", "3")
LOG_LEVEL = os.environ.get("FLOCK_LOG_LEVEL", "INFO")
STATUS_LOG_INTERVAL = os.environ.get("FLOCK_STATUS_LOG_INTERVAL", "10")
REPLICA_SYNC_INTERVAL = os.environ.get("FLOCK_REPLICA_FULL_SYNC_INTERVAL", "8")
SERVER_PORT = 12345
PING_PORT = 12346

SERVER_NODES = {
    "nodo1": "flock-nodo1",
    "nodo2": "flock-nodo2",
    "nodo3": "flock-nodo3",
    "nodo4": "flock-nodo4",
}
LEGACY_SERVER_CONTAINERS = {
    "flock-node1",
    "flock-node2",
    "flock-node3",
    "flock-node4",
}
CLIENTS = {
    "cliente1": ("flock-cliente1", 5001, "cliente1-local"),
    "cliente2": ("flock-cliente2", 5002, "cliente2-local"),
}
LEGACY_CLIENT_CONTAINERS = {"flock-alice", "flock-bob"}
CONTROL_RECORDS = [
    ("control_uno", "10.90.0.11", 6101),
    ("control_dos", "10.90.0.12", 6102),
    ("control_tres", "10.90.0.13", 6103),
]


class CommandError(RuntimeError):
    pass


def docker_base_cmd() -> list[str]:
    configured = os.environ.get("FLOCK_DOCKER_CMD")
    if configured:
        return shlex.split(configured)
    return ["docker"]


def docker_result(args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        docker_base_cmd() + args,
        cwd=ROOT_DIR,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def docker_cmd(args: list[str], *, check: bool = True, capture: bool = False) -> str:
    result = docker_result(args, capture=capture)
    if check and result.returncode != 0:
        output = (result.stdout or "").strip()
        printable = " ".join(docker_base_cmd() + args)
        hint = (
            "\nSugerencia: si Docker devuelve permission denied, ejecuta "
            'export FLOCK_DOCKER_CMD="sudo docker" o lanza el script con sudo.'
        )
        raise CommandError(f"Fallo el comando: {printable}\n{output}{hint}")
    return (result.stdout or "").strip()


def run_local(args: list[str], *, check: bool = True) -> None:
    result = subprocess.run(args, cwd=ROOT_DIR, check=False)
    if check and result.returncode != 0:
        raise CommandError(f"Fallo el comando: {' '.join(args)}")


def require_docker() -> None:
    docker_cmd(["info"], capture=True)


def ensure_log_dirs() -> None:
    for name in [*SERVER_NODES, *CLIENTS]:
        (LOG_ROOT / name).mkdir(parents=True, exist_ok=True)


def build_image() -> None:
    print(f"[flock] Construyendo imagen {IMAGE} ...")
    docker_cmd(["build", "-t", IMAGE, "."])


def ensure_network() -> None:
    result = docker_result(["network", "inspect", NETWORK], capture=True)
    if result.returncode == 0:
        print(f"[flock] Red disponible: {NETWORK}")
        return
    print(f"[flock] Creando red local: {NETWORK}")
    docker_cmd(["network", "create", NETWORK], capture=True)


def container_exists(container: str) -> bool:
    return docker_result(["inspect", container], capture=True).returncode == 0


def container_running(container: str) -> bool:
    output = docker_cmd(
        ["inspect", "-f", "{{.State.Running}}", container],
        check=False,
        capture=True,
    )
    return output.strip() == "true"


def container_ip(container: str) -> str:
    return docker_cmd(
        [
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            container,
        ],
        capture=True,
    )


def udp_command(ip: str, command: str, timeout: float = 3.0) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(command.encode(), (ip, SERVER_PORT))
        data, _ = sock.recvfrom(65535)
        return data.decode()


def ping(ip: str, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(b"PING", (ip, PING_PORT))
            data, _ = sock.recvfrom(1024)
            return data.decode() == "PONG"
        except Exception:
            return False


def wait_for_server(container: str, timeout: float = 25.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = container_ip(container) if container_exists(container) else ""
        if ip and ping(ip):
            print(f"[flock] {container} listo en {ip}")
            return ip
        time.sleep(0.5)
    raise TimeoutError(f"{container} no respondio PING en UDP {PING_PORT}")


def start_server(node: str) -> None:
    container = SERVER_NODES[node]
    if container_exists(container):
        if container_running(container):
            print(f"[flock] {container} ya esta corriendo")
        else:
            print(f"[flock] Montando contenedor existente: {container}")
            docker_cmd(["start", container], capture=True)
        wait_for_server(container)
        return

    log_dir = LOG_ROOT / node
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[flock] Montando {node} como {container}")
    docker_cmd(
        [
            "run",
            "-d",
            "--name",
            container,
            "--network",
            NETWORK,
            "-e",
            f"FLOCK_FAIL_TOLERANCE={FAIL_TOLERANCE}",
            "-e",
            "FLOCK_LOG_DIR=/logs",
            "-e",
            f"FLOCK_LOG_LEVEL={LOG_LEVEL}",
            "-e",
            f"FLOCK_STATUS_LOG_INTERVAL={STATUS_LOG_INTERVAL}",
            "-e",
            f"FLOCK_REPLICA_FULL_SYNC_INTERVAL={REPLICA_SYNC_INTERVAL}",
            "-v",
            f"{log_dir}:/logs",
            IMAGE,
            "python",
            "server/server.py",
            node,
        ],
        capture=True,
    )
    wait_for_server(container)


def start_client(client: str) -> None:
    container, port, secret = CLIENTS[client]
    if container_exists(container):
        if container_running(container):
            print(f"[flock] {container} ya esta corriendo en http://localhost:{port}")
            return
        print(f"[flock] Montando cliente existente: {container}")
        docker_cmd(["start", container], capture=True)
        print(f"[flock] Abrir http://localhost:{port}")
        return

    log_dir = LOG_ROOT / client
    log_dir.mkdir(parents=True, exist_ok=True)
    command = (
        "from ui_flask import app, socketio; "
        f"socketio.run(app, host='0.0.0.0', port={port}, debug=False, allow_unsafe_werkzeug=True)"
    )
    print(f"[flock] Montando {client} en http://localhost:{port}")
    docker_cmd(
        [
            "run",
            "-d",
            "--name",
            container,
            "--network",
            NETWORK,
            "-p",
            f"{port}:{port}",
            "-e",
            f"FLOCK_SECRET_KEY={secret}",
            "-e",
            "FLOCK_LOG_DIR=/logs",
            "-e",
            f"FLOCK_LOG_LEVEL={LOG_LEVEL}",
            "-v",
            f"{log_dir}:/logs",
            "-w",
            "/app/client",
            IMAGE,
            "python",
            "-c",
            command,
        ],
        capture=True,
    )


def stop_containers(containers: list[str]) -> None:
    existing = [name for name in containers if container_exists(name)]
    if not existing:
        print("[flock] No hay contenedores coincidentes para parar")
        return
    print(f"[flock] Parando: {', '.join(existing)}")
    docker_cmd(["stop", *existing], capture=True)


def running_default_node() -> str:
    for node in ("nodo4", "nodo1", "nodo2", "nodo3"):
        container = SERVER_NODES[node]
        if container_exists(container) and container_running(container):
            return container
    raise CommandError("No hay nodos Flock corriendo")


def resolve_node_container(node_or_container: str | None) -> str:
    if not node_or_container:
        return running_default_node()
    return SERVER_NODES.get(node_or_container, node_or_container)


def print_admin(command: str, node_or_container: str | None = None) -> str:
    container = resolve_node_container(node_or_container)
    ip = container_ip(container)
    if not ip:
        raise CommandError(f"No se pudo resolver IP para {container}")
    response = udp_command(ip, command)
    print(response)
    return response


def admin_json(ip: str, command: str) -> dict:
    response = udp_command(ip, command)
    if not response.startswith("OK "):
        raise CommandError(f"{command} fallo en {ip}: {response}")
    return json.loads(response[3:])


def load_crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ModuleNotFoundError as exc:
        raise CommandError("cryptography es necesario para sembrar-estado") from exc
    return hashes, serialization, padding, rsa


def make_identity():
    _, serialization, _, rsa = load_crypto()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    public_key_b64 = base64.b64encode(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode()
    return private_key, public_key_b64


def sign_registration(private_key, username: str, ip: str, port: int, version: int, public_key_b64: str) -> str:
    hashes, _, padding, _ = load_crypto()
    payload = f"{username}|{ip}|{port}|{version}|{public_key_b64}"
    signature = private_key.sign(
        payload.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def register_record(server_ip: str, username: str, message_ip: str, message_port: int, version: int) -> str:
    private_key, public_key_b64 = make_identity()
    signature = sign_registration(private_key, username, message_ip, message_port, version, public_key_b64)
    command = f"REGISTER {username} {message_ip} {message_port} {version} {public_key_b64} {signature}"
    return udp_command(server_ip, command)


def seed_state(node_or_container: str | None = None, wait_seconds: float = 8.0) -> None:
    container = resolve_node_container(node_or_container)
    ip = container_ip(container)
    print(f"[flock] Escribiendo registros de control mediante {container} ({ip})")
    for offset, (username, message_ip, port) in enumerate(CONTROL_RECORDS):
        version = time.time_ns() + offset
        response = register_record(ip, username, message_ip, port, version)
        print(f"{username}: {response}")
        if not response.startswith("OK "):
            raise CommandError(f"No se pudo registrar {username}: {response}")
    if wait_seconds > 0:
        print(f"[flock] Esperando {wait_seconds:g}s para propagacion de replicas")
        time.sleep(wait_seconds)


def resolve_control_records(node_or_container: str | None = None) -> dict[str, str]:
    container = resolve_node_container(node_or_container)
    ip = container_ip(container)
    return {username: udp_command(ip, f"RESOLVE {username}") for username, _, _ in CONTROL_RECORDS}


def collect_state(node_or_container: str | None) -> dict:
    container = resolve_node_container(node_or_container)
    ip = container_ip(container)
    return {
        "nodo": container,
        "ip": ip,
        "estado": admin_json(ip, "STATUS"),
        "snapshot": admin_json(ip, "SNAPSHOT"),
        "checksum": admin_json(ip, "CHECKSUM"),
        "registros_resueltos": resolve_control_records(container),
    }


def verify_state(node_or_container: str | None, report_file: str | None, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    report = None
    missing = {}
    while time.time() < deadline:
        report = collect_state(node_or_container)
        missing = {
            name: response
            for name, response in report["registros_resueltos"].items()
            if not response.startswith("OK ")
        }
        if not missing:
            break
        time.sleep(2)

    if report is None:
        raise CommandError("No se pudo recolectar estado")

    print(json.dumps(report, indent=2, sort_keys=True))
    if report_file:
        path = Path(report_file)
        if not path.is_absolute():
            path = ROOT_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"[flock] Reporte escrito en {path}")
    if missing:
        raise CommandError(f"Registros no encontrados tras recuperacion: {sorted(missing)}")


def prepare(skip_build: bool) -> None:
    require_docker()
    ensure_log_dirs()
    if not skip_build:
        build_image()
    ensure_network()


def status() -> None:
    docker_cmd(
        [
            "ps",
            "-a",
            "--filter",
            "name=flock-",
            "--format",
            "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
        ]
    )


def follow_logs(target: str) -> None:
    container = SERVER_NODES.get(target) or CLIENTS.get(target, (target, None, None))[0]
    docker_cmd(["logs", "-f", container], check=False)


def clean(yes: bool, remove_logs: bool) -> None:
    if not yes:
        answer = input("Parar y eliminar contenedores/redes locales de Flock? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("[flock] Limpieza cancelada")
            return
    containers = [
        *SERVER_NODES.values(),
        *(value[0] for value in CLIENTS.values()),
        *LEGACY_SERVER_CONTAINERS,
        *LEGACY_CLIENT_CONTAINERS,
    ]
    docker_cmd(["rm", "-f", *containers], check=False, capture=True)
    for network in [NETWORK, *OLD_NETWORKS]:
        docker_cmd(["network", "rm", network], check=False, capture=True)
    if remove_logs:
        if LOG_ROOT.exists():
            shutil.rmtree(LOG_ROOT)
    print("[flock] Entorno local limpio")


def run_acceptance(report_file: str, skip_build: bool) -> None:
    command = [sys.executable, "scripts/acceptance_failure_recovery.py", "--report-file", report_file]
    if skip_build:
        command.insert(2, "--skip-build")
    run_local(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operacion local de Flock en Docker.")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("preparar", help="Construir imagen, red local y carpetas de logs.")
    prepare_parser.add_argument("--sin-build", action="store_true")

    start_node_parser = sub.add_parser("montar-nodo", help="Montar un nodo servidor.")
    start_node_parser.add_argument("nodo", choices=sorted(SERVER_NODES))

    stop_node_parser = sub.add_parser("parar-nodo", help="Parar un nodo servidor.")
    stop_node_parser.add_argument("nodo", choices=sorted(SERVER_NODES))

    start_client_parser = sub.add_parser("montar-cliente", help="Montar un cliente web.")
    start_client_parser.add_argument("cliente", choices=sorted(CLIENTS))

    seed_parser = sub.add_parser("sembrar-estado", help="Escribir registros firmados de control.")
    seed_parser.add_argument("--nodo", default="nodo1")
    seed_parser.add_argument("--espera", type=float, default=8.0)

    verify_parser = sub.add_parser("verificar", help="Leer estado y resolver registros de control.")
    verify_parser.add_argument("--nodo", default="nodo4")
    verify_parser.add_argument("--reporte")
    verify_parser.add_argument("--timeout", type=float, default=30.0)

    admin_parser = sub.add_parser("admin", help="Enviar STATUS, SNAPSHOT, CHECKSUM u otro comando UDP.")
    admin_parser.add_argument("admin_command", nargs="+")
    admin_parser.add_argument("--nodo")

    logs_parser = sub.add_parser("logs", help="Seguir logs de un nodo, cliente o contenedor.")
    logs_parser.add_argument("objetivo", nargs="?", default="nodo4")

    sub.add_parser("estado", help="Listar contenedores Flock locales.")

    clean_parser = sub.add_parser("limpiar", help="Eliminar contenedores y red local.")
    clean_parser.add_argument("--yes", action="store_true")
    clean_parser.add_argument("--logs", action="store_true", help="Tambien borrar logs locales.")

    acceptance_parser = sub.add_parser("acceptance", help="Ejecutar la prueba automatizada oficial.")
    acceptance_parser.add_argument(
        "--reporte",
        default="Documentation/acceptance_failure_recovery_report.json",
    )
    acceptance_parser.add_argument("--sin-build", action="store_true")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "preparar":
            prepare(skip_build=args.sin_build)
        elif args.command == "montar-nodo":
            prepare(skip_build=True)
            start_server(args.nodo)
        elif args.command == "parar-nodo":
            stop_containers([SERVER_NODES[args.nodo]])
        elif args.command == "montar-cliente":
            prepare(skip_build=True)
            start_client(args.cliente)
        elif args.command == "sembrar-estado":
            seed_state(args.nodo, args.espera)
        elif args.command == "verificar":
            verify_state(args.nodo, args.reporte, args.timeout)
        elif args.command == "admin":
            print_admin(" ".join(args.admin_command), args.nodo)
        elif args.command == "logs":
            follow_logs(args.objetivo)
        elif args.command == "estado":
            status()
        elif args.command == "limpiar":
            clean(args.yes, args.logs)
        elif args.command == "acceptance":
            run_acceptance(args.reporte, args.sin_build)
        else:
            parser.error(f"Comando desconocido: {args.command}")
    except (CommandError, TimeoutError, OSError) as exc:
        print(f"[flock] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
