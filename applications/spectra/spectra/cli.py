"""Command-line interface for the local Spectra development network."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from .ledger import (
    MAX_QUANTITY,
    create_transaction,
    new_keypair,
    strict_json_loads,
    validate_genesis,
)
from .node import _request_json, serve_node, wait_for_node
from .topology import make_topology

DEFAULT_BASE_PORT = 18400
INITIAL_SUPPLY = 1_000_000
APPLICATION_ROOT = Path(__file__).resolve().parent.parent


def _write_secret(path: Path, secret: str) -> None:
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as output:
        output.write(secret + "\n")
    os.chmod(str(path), 0o600)


def init_directory(directory: str, base_port: int = DEFAULT_BASE_PORT) -> Dict[str, Any]:
    if type(base_port) is not int or not 1 <= base_port <= 65528:
        raise ValueError("base port must leave room for eight validators")
    root = Path(directory).expanduser().resolve()
    if (root / "genesis.json").exists():
        raise FileExistsError("devnet already initialized at " + str(root))
    root.mkdir(parents=True, exist_ok=True)
    keys_dir = root / "keys"
    data_dir = root / "data"
    keys_dir.mkdir(mode=0o700, exist_ok=True)
    data_dir.mkdir(mode=0o700, exist_ok=True)
    validators = []
    public_keys = set()
    for node_id in range(8):
        private_key, public_key = new_keypair()
        while public_key in public_keys:
            private_key, public_key = new_keypair()
        public_keys.add(public_key)
        _write_secret(keys_dir / ("node-%d.key" % node_id), private_key)
        validators.append({
            "id": node_id,
            "public_key": public_key,
            "host": "127.0.0.1",
            "port": base_port + node_id,
        })
    faucet_private, faucet_public = new_keypair()
    while faucet_public in public_keys:
        faucet_private, faucet_public = new_keypair()
    _write_secret(keys_dir / "faucet.key", faucet_private)
    topology_result = make_topology(n=8, degree=4)
    genesis = {
        "version": 1,
        "chain_id": "spectra-local-v1",
        "validators": validators,
        "faucet": faucet_public,
        "balances": {faucet_public: INITIAL_SUPPLY},
        "topology": {
            "n": 8,
            "degree": 4,
            "edges": topology_result["edges"],
            "spectral": topology_result["spectral"],
            "resilience": topology_result["resilience"],
        },
    }
    validate_genesis(genesis)
    encoded = json.dumps(
        genesis, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    with (root / "genesis.json").open("x", encoding="ascii") as output:
        output.write(encoded + "\n")
    os.chmod(str(root / "genesis.json"), 0o644)
    return genesis


def read_genesis(directory: str) -> Dict[str, Any]:
    genesis = strict_json_loads((Path(directory).expanduser() / "genesis.json").read_bytes())
    validate_genesis(genesis)
    return cast(Dict[str, Any], genesis)


def _get_status(genesis: Dict[str, Any], node_id: int = 0) -> Dict[str, Any]:
    validator = genesis["validators"][node_id]
    result = _request_json(
        "GET", validator["host"], validator["port"], "/status"
    )
    if (
        type(result) is not dict
        or result.get("genesis_hash") != validate_genesis(genesis)
        or result.get("node_id") != node_id
    ):
        raise ValueError("node status does not match configured genesis")
    return result


def transfer(directory: str, recipient: str, amount: int) -> Dict[str, Any]:
    if type(amount) is not int or not 1 <= amount <= MAX_QUANTITY:
        raise ValueError("amount must be a positive bounded integer")
    genesis = read_genesis(directory)
    status = _get_status(genesis, 0)
    faucet_private = (
        Path(directory).expanduser() / "keys" / "faucet.key"
    ).read_text(encoding="ascii").strip()
    transaction = create_transaction(
        faucet_private, validate_genesis(genesis), recipient, amount,
        status["nonces"].get(genesis["faucet"], 0) + 1,
    )
    validator = genesis["validators"][0]
    payload = json.dumps(
        {"transactions": [transaction]}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _request_json(
        "POST", validator["host"], validator["port"], "/submit", payload
    )


def _demo() -> Dict[str, Any]:
    processes: List[subprocess.Popen[bytes]] = []
    log_files = []
    with tempfile.TemporaryDirectory(prefix="spectra-devnet-") as temp:
        root = Path(temp)
        genesis = init_directory(str(root))
        recipient_private, recipient = new_keypair()
        _write_secret(root / "keys" / "demo-recipient.key", recipient_private)
        (root / "logs").mkdir(mode=0o700)
        try:
            for node_id in range(8):
                log_file = (root / "logs" / ("node-%d.log" % node_id)).open("wb")
                log_files.append(log_file)
                processes.append(subprocess.Popen(
                    [
                        sys.executable, "-m", "spectra", "node",
                        "--directory", str(root), "--id", str(node_id),
                    ],
                    cwd=str(APPLICATION_ROOT),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                ))
            for validator in genesis["validators"]:
                if not wait_for_node(validator["host"], validator["port"]):
                    raise RuntimeError("devnet validator did not become ready")
            amount = 137
            transfer(str(root), recipient, amount)
            statuses = [_get_status(genesis, node_id) for node_id in range(8)]
            roots = {status["state_root"] for status in statuses}
            heights = {status["height"] for status in statuses}
            if len(roots) != 1 or heights != {1}:
                raise RuntimeError("validators did not agree on committed state")
            for status in statuses:
                if status["balances"].get(recipient) != amount:
                    raise RuntimeError("recipient balance does not match transfer")
                if status["balances"].get(genesis["faucet"]) != INITIAL_SUPPLY - amount:
                    raise RuntimeError("faucet balance does not match transfer")
            return {
                "validators": len(statuses),
                "height": 1,
                "amount": amount,
                "recipient": recipient,
                "state_root": statuses[0]["state_root"],
                "independent_databases": len({
                    str(root / "data" / ("node-%d.sqlite3" % i)) for i in range(8)
                }),
            }
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for log_file in log_files:
                log_file.close()


def _sync(directory: str, node_id: int, neighbor: int) -> Dict[str, Any]:
    genesis = read_genesis(directory)
    validator = genesis["validators"][node_id]
    payload = json.dumps({"neighbor": neighbor}, separators=(",", ":")).encode("utf-8")
    return _request_json(
        "POST", validator["host"], validator["port"], "/sync", payload
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spectra")
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="create a local eight-validator devnet")
    initialize.add_argument("--directory", required=True)
    initialize.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT)

    node = commands.add_parser("node", help="run one independent validator process")
    node.add_argument("--directory", required=True)
    node.add_argument("--id", type=int, required=True)

    send = commands.add_parser("transfer", help="send a faucet-funded local transfer")
    send.add_argument("--directory", required=True)
    send.add_argument("--to", required=True)
    send.add_argument("--amount", required=True, type=int)

    sync = commands.add_parser("sync", help="replay certificates from a direct neighbor")
    sync.add_argument("--directory", required=True)
    sync.add_argument("--id", required=True, type=int)
    sync.add_argument("--neighbor", required=True, type=int)

    commands.add_parser("demo", help="run and verify an isolated eight-process transfer")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "init":
            genesis = init_directory(arguments.directory, arguments.base_port)
            result = {
                "directory": str(Path(arguments.directory).expanduser().resolve()),
                "validators": len(genesis["validators"]),
                "ports": [
                    item["port"] for item in genesis["validators"]
                ],
                "genesis_hash": validate_genesis(genesis),
            }
        elif arguments.command == "node":
            serve_node(arguments.directory, arguments.id)
            return 0
        elif arguments.command == "transfer":
            result = transfer(arguments.directory, arguments.to, arguments.amount)
        elif arguments.command == "sync":
            result = _sync(arguments.directory, arguments.id, arguments.neighbor)
        else:
            result = _demo()
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print("spectra: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
