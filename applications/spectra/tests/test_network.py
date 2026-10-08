import json
import socket
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from spectra.cli import APPLICATION_ROOT, init_directory, transfer
from spectra.ledger import Ledger, validate_genesis
from spectra.node import (
    NodeService,
    QuorumUnavailable,
    _shortest_path,
    wait_for_node,
)


def _private_key(root: Path, node_id: int) -> str:
    return (root / "keys" / ("node-%d.key" % node_id)).read_text().strip()


def _free_base_port() -> int:
    for _ in range(30):
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            base = candidate.getsockname()[1]
        if base > 65528:
            continue
        sockets = []
        try:
            for port in range(base, base + 8):
                listener = socket.socket()
                listener.bind(("127.0.0.1", port))
                sockets.append(listener)
            return base
        except OSError:
            continue
        finally:
            for listener in sockets:
                listener.close()
    raise RuntimeError("could not reserve a contiguous local port range")


def _status(validator):
    with urlopen(
        "http://%s:%d/status" % (validator["host"], validator["port"]), timeout=2
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_sync(validator, neighbor):
    payload = json.dumps({"neighbor": neighbor}).encode("utf-8")
    request = Request(
        "http://%s:%d/sync" % (validator["host"], validator["port"]),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=4) as response:
        return json.loads(response.read().decode("utf-8"))


def test_no_quorum_does_not_finalize_a_block(tmp_path, monkeypatch):
    root = tmp_path / "no-quorum"
    genesis = init_directory(str(root), _free_base_port())
    service = NodeService(str(root), 0)
    voter_ledgers = {
        node_id: Ledger(
            str(root / "data" / ("mock-node-%d.sqlite3" % node_id)),
            genesis,
            node_id,
            _private_key(root, node_id),
        )
        for node_id in range(1, 5)
    }

    def limited_votes(path, message, received=False):
        node_id = path[-1]
        if node_id not in voter_ledgers:
            raise RuntimeError("simulated unavailable validator")
        return {"vote": voter_ledgers[node_id].vote(message["block"])}

    monkeypatch.setattr(service, "_post_overlay", limited_votes)
    recipient = genesis["validators"][1]["public_key"]
    from spectra.ledger import create_transaction

    transaction = create_transaction(
        (root / "keys" / "faucet.key").read_text().strip(),
        validate_genesis(genesis),
        recipient,
        3,
        1,
    )
    try:
        with pytest.raises(QuorumUnavailable):
            service.submit({"transactions": [transaction]})
        assert service.ledger.height == 0
        assert service.ledger.status()["balances"] == genesis["balances"]
        assert service.ledger.certificates_from(1) == []
        assert all(ledger.height == 0 for ledger in voter_ledgers.values())
    finally:
        service.ledger.close()
        for ledger in voter_ledgers.values():
            ledger.close()


def test_eight_process_overlay_transfer_and_neighbor_catchup(tmp_path):
    root = tmp_path / "network"
    genesis = init_directory(str(root), _free_base_port())
    processes = []
    logs = []
    try:
        for node_id in range(8):
            log = (tmp_path / ("node-%d.log" % node_id)).open("wb")
            logs.append(log)
            processes.append(subprocess.Popen(
                [
                    sys.executable, "-m", "spectra", "node",
                    "--directory", str(root), "--id", str(node_id),
                ],
                cwd=str(APPLICATION_ROOT),
                stdout=log,
                stderr=subprocess.STDOUT,
            ))
        for validator in genesis["validators"]:
            assert wait_for_node(validator["host"], validator["port"], timeout=15)

        topology = genesis["topology"]
        routed_path = next(
            _shortest_path(8, topology["edges"], 0, node_id)
            for node_id in range(1, 8)
            if len(_shortest_path(8, topology["edges"], 0, node_id)) > 2
        )
        first_result = transfer(
            str(root), genesis["validators"][1]["public_key"], 37
        )
        assert first_result["votes"] == 8
        assert set(first_result["delivered"]) == set(range(8))
        statuses = [_status(validator) for validator in genesis["validators"]]
        assert {status["height"] for status in statuses} == {1}
        assert len({status["state_root"] for status in statuses}) == 1
        assert len({
            str(root / "data" / ("node-%d.sqlite3" % node_id))
            for node_id in range(8)
        }) == 8
        assert statuses[routed_path[1]]["relayed_messages"] > 0
        for status in statuses:
            assert status["balances"][genesis["validators"][1]["public_key"]] == 37

        processes[7].terminate()
        processes[7].wait(timeout=5)
        processes[7] = None
        transfer(str(root), genesis["validators"][2]["public_key"], 11)
        assert _status(genesis["validators"][0])["height"] == 2

        log = (tmp_path / "node-7-restart.log").open("wb")
        logs.append(log)
        processes[7] = subprocess.Popen(
            [
                sys.executable, "-m", "spectra", "node",
                "--directory", str(root), "--id", "7",
            ],
            cwd=str(APPLICATION_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        validator = genesis["validators"][7]
        assert wait_for_node(validator["host"], validator["port"], timeout=15)
        neighbor = next(
            vertex for edge in topology["edges"] for vertex in edge
            if 7 in edge and vertex != 7
        )
        result = _post_sync(validator, neighbor)
        assert result["height"] == 2
        final_statuses = [_status(item) for item in genesis["validators"]]
        assert {status["height"] for status in final_statuses} == {2}
        assert len({status["state_root"] for status in final_statuses}) == 1
        assert final_statuses[7]["balances"][genesis["validators"][2]["public_key"]] == 11
    finally:
        for process in processes:
            if process is not None and process.poll() is None:
                process.terminate()
        for process in processes:
            if process is not None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        for log in logs:
            log.close()
