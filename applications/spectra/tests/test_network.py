import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from spectra.cli import APPLICATION_ROOT, init_directory, transfer
from spectra.ledger import (
    Ledger,
    create_transaction,
    make_certificate,
    new_keypair,
    validate_genesis,
)
from spectra.node import (
    BoundedThreadingHTTPServer,
    NodeService,
    QuorumUnavailable,
    RequestHandler,
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


def test_incomplete_body_times_out_and_handler_slot_recovers(tmp_path, monkeypatch):
    root = tmp_path / "timeout"
    init_directory(str(root), _free_base_port())
    service = NodeService(str(root), 0)
    inbound_timeout = 0.2
    monkeypatch.setattr("spectra.node.MAX_HANDLERS", 1)
    monkeypatch.setattr("spectra.node.INBOUND_TIMEOUT", inbound_timeout)
    server = BoundedThreadingHTTPServer(("127.0.0.1", 0), RequestHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = server.server_address
    partial = socket.create_connection(address, timeout=1)
    partial.sendall(
        b"POST /submit HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Length: 2\r\n"
        b"Connection: close\r\n\r\n"
        b"x"
    )
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                with urlopen(
                    "http://127.0.0.1:%d/status" % address[1], timeout=0.2
                ):
                    pytest.fail("handler slot was not occupied by incomplete request")
            except HTTPError as exc:
                if exc.code == 503:
                    break
            except OSError:
                pass
            time.sleep(0.01)
        else:
            pytest.fail("handler slot did not reject a concurrent request")

        partial.settimeout(1)
        started = time.monotonic()
        while partial.recv(4096):
            pass
        assert time.monotonic() - started < inbound_timeout + 0.5

        with urlopen("http://127.0.0.1:%d/status" % address[1], timeout=1) as response:
            assert response.status == 200
    finally:
        partial.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
        service.ledger.close()


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


def test_concurrent_submits_are_serialized(tmp_path, monkeypatch):
    root = tmp_path / "concurrent"
    genesis = init_directory(str(root), _free_base_port())
    service = NodeService(str(root), 0)
    voter_ledgers = {
        node_id: Ledger(
            str(root / "data" / ("mock-node-%d.sqlite3" % node_id)),
            genesis,
            node_id,
            _private_key(root, node_id),
        )
        for node_id in range(1, 8)
    }

    def overlay(path, message, received=False):
        node_id = path[-1]
        if message["kind"] == "vote":
            return {"vote": voter_ledgers[node_id].vote(message["block"])}
        voter_ledgers[node_id].commit_certificate(message["certificate"])
        return {"committed": voter_ledgers[node_id].height}

    monkeypatch.setattr(service, "_post_overlay", overlay)
    faucet = (root / "keys" / "faucet.key").read_text().strip()
    digest = validate_genesis(genesis)
    sender_one_private, sender_one = new_keypair()
    sender_two_private, sender_two = new_keypair()
    try:
        service.submit({
            "transactions": [
                create_transaction(faucet, digest, sender_one, 10, 1)
            ]
        })
        service.submit({
            "transactions": [
                create_transaction(faucet, digest, sender_two, 10, 2)
            ]
        })
        transactions = [
            create_transaction(
                sender_one_private, digest, genesis["validators"][3]["public_key"], 3, 1
            ),
            create_transaction(
                sender_two_private, digest, genesis["validators"][4]["public_key"], 4, 1
            ),
        ]
        results = []
        errors = []

        def submit(transaction):
            try:
                results.append(service.submit({"transactions": [transaction]}))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(transaction,)) for transaction in transactions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not errors
        assert sorted(result["height"] for result in results) == [3, 4]
        assert service.ledger.height == 4
        assert all(ledger.height == 4 for ledger in voter_ledgers.values())
    finally:
        service.ledger.close()
        for ledger in voter_ledgers.values():
            ledger.close()


def test_neighbor_sync_paginates_history(tmp_path, monkeypatch):
    root = tmp_path / "pagination"
    genesis = init_directory(str(root), _free_base_port())
    neighbor = next(
        edge[1] if edge[0] == 0 else edge[0]
        for edge in genesis["topology"]["edges"]
        if 0 in edge
    )
    service = NodeService(str(root), neighbor)
    ledgers = {
        0: Ledger(
            str(root / "data" / "source.sqlite3"),
            genesis,
            0,
            _private_key(root, 0),
        ),
    }
    for node_id in range(1, 8):
        ledgers[node_id] = Ledger(
            str(root / "data" / ("source-voter-%d.sqlite3" % node_id)),
            genesis,
            node_id,
            _private_key(root, node_id),
        )
    faucet = (root / "keys" / "faucet.key").read_text().strip()
    digest = validate_genesis(genesis)
    try:
        for nonce in range(1, 18):
            transaction = create_transaction(
                faucet, digest, genesis["validators"][1]["public_key"], 1, nonce
            )
            block = ledgers[0].propose([transaction])
            votes = [ledgers[node_id].vote(block) for node_id in range(6)]
            certificate = make_certificate(block, votes)
            for ledger in ledgers.values():
                ledger.commit_certificate(certificate)

        requests = []

        def request(method, host, port, path, payload=None):
            requests.append(path)
            if path == "/status":
                return ledgers[0].status()
            first_height = int(path.split("=")[1])
            return ledgers[0].certificates_page(first_height)

        monkeypatch.setattr("spectra.node._request_json", request)
        result = service.sync_from_neighbor(0)
        assert result == {"height": 17, "replayed": 17, "target_height": 17}
        assert len([path for path in requests if path.startswith("/chain")]) == 2
    finally:
        for ledger in ledgers.values():
            ledger.close()
        service.ledger.close()


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
