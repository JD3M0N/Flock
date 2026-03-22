#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def pytest_command():
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists():
        return [str(venv_python), "-m", "pytest"]
    return [sys.executable, "-m", "pytest"]


def collect_tests():
    command = pytest_command() + ["tests", "--collect-only", "-q"]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("No se pudieron descubrir los tests.")
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        sys.exit(result.returncode)

    return [line.strip() for line in result.stdout.splitlines() if "::" in line]


def run_test(nodeid):
    command = pytest_command() + ["-q", nodeid]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    success = result.returncode == 0
    status = "PASS" if success else "FAIL"
    print(f"[{status}] {nodeid}")

    output = (result.stdout + result.stderr).strip()
    if output and not success:
        print(output)
        print("-" * 80)

    return success


def main():
    tests = collect_tests()
    if not tests:
        print("No se encontraron tests.")
        return 1

    passed = 0
    failed = 0

    print(f"Se encontraron {len(tests)} tests.")
    print("=" * 80)

    for nodeid in tests:
        if run_test(nodeid):
            passed += 1
        else:
            failed += 1

    print("=" * 80)
    print("Resumen final:")
    print(f"  Pasaron con exito: {passed}")
    print(f"  Fallaron: {failed}")
    print(f"  Total: {len(tests)}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
