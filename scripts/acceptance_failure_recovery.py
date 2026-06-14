#!/usr/bin/env python3
import argparse
import base64
import json
import socket
import subprocess
import sys
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


IMAGE = "flock:acceptance"
NETWORK = "flock-acceptance-net"
SERVER_PORT = 12345
PING_PORT = 12346
CONTAINERS = ["flock-acc-node1", "flock-acc-node2", "flock-acc-node3", "flock-acc-node4"]


def run(cmd, check=True, capture=False):
    result = subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if check and result.returncode != 0:
        output = result.stdout or ""
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{output}")
    return result.stdout.strip() if capture and result.stdout else ""


def cleanup():
    for name in CONTAINERS:
        run(["docker", "rm", "-f", name], check=False)
    run(["docker", "network", "rm", NETWORK], check=False)


def container_ip(name):
    return run(
        [
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            name,
        ],
        capture=True,
    )


def udp_command(ip, command, timeout=3.0):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(command.encode(), (ip, SERVER_PORT))
        data, _ = sock.recvfrom(65535)
        return data.decode()


def ping(ip, timeout=1.0):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(b"PING", (ip, PING_PORT))
            data, _ = sock.recvfrom(1024)
            return data.decode() == "PONG"
        except Exception:
            return False


def wait_for_server(name, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = container_ip(name)
        if ip and ping(ip):
            return ip
        time.sleep(0.5)
    raise TimeoutError(f"{name} did not become ready")


def wait_for_admin(ip, command, timeout=20):
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            response = udp_command(ip, command)
            if response.startswith("OK "):
                return json.loads(response[3:])
        except Exception as exc:
            last_error = exc
        time.sleep(0.5)
    raise TimeoutError(f"{command} did not succeed on {ip}: {last_error}")


def start_server(container_name, node_name):
    run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--network",
            NETWORK,
            "-e",
            "FLOCK_FAIL_TOLERANCE=3",
            IMAGE,
            "python",
            "server/server.py",
            node_name,
        ],
        capture=True,
    )
    return wait_for_server(container_name)


def make_identity():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    public_key_b64 = base64.b64encode(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode()
    return private_key, public_key_b64


def sign_registration(private_key, username, ip, port, version, public_key_b64):
    payload = f"{username}|{ip}|{port}|{version}|{public_key_b64}"
    signature = private_key.sign(
        payload.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


def register_user(server_ip, username, message_ip, message_port, version):
    private_key, public_key_b64 = make_identity()
    signature = sign_registration(private_key, username, message_ip, message_port, version, public_key_b64)
    command = f"REGISTER {username} {message_ip} {message_port} {version} {public_key_b64} {signature}"
    response = udp_command(server_ip, command)
    if not response.startswith("OK "):
        raise RuntimeError(f"REGISTER {username} failed: {response}")
    return {"username": username, "ip": message_ip, "port": message_port, "public_key": public_key_b64, "version": version}


def resolve_user(server_ip, username):
    return udp_command(server_ip, f"RESOLVE {username}")


def main():
    parser = argparse.ArgumentParser(description="Run Flock failure-recovery acceptance scenario in Docker.")
    parser.add_argument("--skip-build", action="store_true", help="Use the existing flock:acceptance image.")
    parser.add_argument(
        "--report-file",
        help="Optional path where the JSON evidence report will be written after a successful run.",
    )
    args = parser.parse_args()

    cleanup()
    try:
        if not args.skip_build:
            print("Building Docker image...")
            run(["docker", "build", "-t", IMAGE, "."])

        run(["docker", "network", "create", NETWORK], capture=True)

        print("Starting initial 3 servers...")
        node1_ip = start_server("flock-acc-node1", "node1")
        node2_ip = start_server("flock-acc-node2", "node2")
        node3_ip = start_server("flock-acc-node3", "node3")
        time.sleep(8)

        users = [
            register_user(node1_ip, "alice_acc", "10.10.0.1", 5001, time.time_ns()),
            register_user(node2_ip, "bob_acc", "10.10.0.2", 5002, time.time_ns()),
            register_user(node3_ip, "carol_acc", "10.10.0.3", 5003, time.time_ns()),
        ]
        time.sleep(8)

        initial = {
            "node1": {"snapshot": wait_for_admin(node1_ip, "SNAPSHOT"), "checksum": wait_for_admin(node1_ip, "CHECKSUM")},
            "node2": {"snapshot": wait_for_admin(node2_ip, "SNAPSHOT"), "checksum": wait_for_admin(node2_ip, "CHECKSUM")},
            "node3": {"snapshot": wait_for_admin(node3_ip, "SNAPSHOT"), "checksum": wait_for_admin(node3_ip, "CHECKSUM")},
        }

        print("Stopping 2 servers...")
        run(["docker", "stop", "flock-acc-node2", "flock-acc-node3"], capture=True)
        time.sleep(8)

        print("Starting replacement server...")
        node4_ip = start_server("flock-acc-node4", "node4")
        time.sleep(10)

        print("Stopping remaining original server...")
        run(["docker", "stop", "flock-acc-node1"], capture=True)
        time.sleep(8)

        final_snapshot = wait_for_admin(node4_ip, "SNAPSHOT")
        final_checksum = wait_for_admin(node4_ip, "CHECKSUM")
        resolved = {user["username"]: resolve_user(node4_ip, user["username"]) for user in users}
        missing = {name: response for name, response in resolved.items() if not response.startswith("OK ")}

        report = {
            "initial": initial,
            "final": {"node4": {"snapshot": final_snapshot, "checksum": final_checksum}},
            "resolved": resolved,
            "missing": missing,
        }
        report_json = json.dumps(report, indent=2, sort_keys=True)
        print(report_json)

        if missing:
            raise RuntimeError(f"Missing users after failure scenario: {sorted(missing)}")
        if final_checksum["records"] < len(users):
            raise RuntimeError("Final checksum reports fewer records than registered users")

        if args.report_file:
            with open(args.report_file, "w", encoding="utf-8") as handle:
                handle.write(report_json)
                handle.write("\n")

        print("Acceptance scenario passed.")
        return 0
    finally:
        cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Acceptance scenario failed: {exc}", file=sys.stderr)
        cleanup()
        raise SystemExit(1)
