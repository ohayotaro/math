import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from spectra.cli import init_directory
from spectra.ledger import (
    MAX_QUANTITY,
    Ledger,
    apply_transactions,
    block_hash,
    create_transaction,
    make_certificate,
    quorum_size,
    state_root,
    strict_json_loads,
    validate_genesis,
    validate_transaction,
)


@pytest.fixture(scope="module")
def devnet(tmp_path_factory):
    root = tmp_path_factory.mktemp("ledger-devnet")
    genesis = init_directory(str(root))
    return root, genesis


def _private_key(root: Path, node_id: int) -> str:
    return (root / "keys" / ("node-%d.key" % node_id)).read_text().strip()


def _tx(root: Path, genesis, amount=5, nonce=1, recipient=None):
    return create_transaction(
        (root / "keys" / "faucet.key").read_text().strip(),
        validate_genesis(genesis),
        recipient or genesis["validators"][1]["public_key"],
        amount,
        nonce,
    )


def test_signatures_are_bound_to_full_genesis_and_transaction_fields(devnet):
    root, genesis = devnet
    transaction = _tx(root, genesis)
    digest = validate_genesis(genesis)
    assert validate_transaction(transaction, digest) == transaction

    other_genesis = json.loads(json.dumps(genesis))
    other_genesis["chain_id"] = "different-local-chain"
    assert validate_genesis(other_genesis) != digest
    with pytest.raises(ValueError, match="signature"):
        validate_transaction(transaction, validate_genesis(other_genesis))

    changed_supply = json.loads(json.dumps(genesis))
    changed_supply["balances"] = {changed_supply["faucet"]: 999_999}
    assert changed_supply["chain_id"] == genesis["chain_id"]
    with pytest.raises(ValueError, match="signature"):
        validate_transaction(transaction, validate_genesis(changed_supply))

    changed_spectral = json.loads(json.dumps(genesis))
    changed_spectral["topology"]["spectral"]["strict_ramanujan"] = 1
    with pytest.raises(ValueError, match="spectral"):
        validate_genesis(changed_spectral)
    changed_resilience = json.loads(json.dumps(genesis))
    changed_resilience["topology"]["resilience"]["connected"] = 1
    with pytest.raises(ValueError, match="resilience"):
        validate_genesis(changed_resilience)

    changed = dict(transaction, amount=transaction["amount"] + 1)
    with pytest.raises(ValueError, match="signature"):
        validate_transaction(changed, digest)


def test_strict_schemas_reject_bool_float_overflow_and_unknown_fields(devnet):
    root, genesis = devnet
    digest = validate_genesis(genesis)
    transaction = _tx(root, genesis)
    for invalid in (
        dict(transaction, amount=True),
        dict(transaction, amount=1.5),
        dict(transaction, amount=MAX_QUANTITY + 1),
        dict(transaction, unexpected="field"),
    ):
        with pytest.raises(ValueError):
            validate_transaction(invalid, digest)
    with pytest.raises(ValueError):
        create_transaction(
            (root / "keys" / "faucet.key").read_text().strip(),
            digest,
            genesis["validators"][1]["public_key"],
            True,
            1,
        )
    with pytest.raises(ValueError, match="duplicate"):
        strict_json_loads(b'{"amount":1,"amount":2}')
    with pytest.raises(ValueError, match="non-finite"):
        strict_json_loads(b'{"amount":NaN}')


def test_nonce_overspend_self_transfer_and_supply_invariants(devnet):
    root, genesis = devnet
    digest = validate_genesis(genesis)
    initial = dict(genesis["balances"])
    recipient = genesis["validators"][1]["public_key"]
    with pytest.raises(ValueError, match="insufficient"):
        apply_transactions(
            initial, {}, [_tx(root, genesis, amount=1_000_001)], digest, 1_000_000
        )
    with pytest.raises(ValueError, match="nonce"):
        apply_transactions(
            initial, {}, [_tx(root, genesis, nonce=2)], digest, 1_000_000
        )

    self_send = _tx(root, genesis, amount=7, recipient=genesis["faucet"])
    balances, nonces = apply_transactions(initial, {}, [self_send], digest, 1_000_000)
    assert balances == initial
    assert nonces == {genesis["faucet"]: 1}
    assert sum(balances.values()) == 1_000_000
    assert state_root(balances, nonces) != state_root(initial, {})
    assert recipient not in balances


def test_quorum_certificate_validation_vote_lock_and_restart(devnet, tmp_path):
    root, genesis = devnet
    ledgers = {}
    paths = {}
    for node_id in range(8):
        paths[node_id] = tmp_path / ("node-%d.sqlite3" % node_id)
        ledgers[node_id] = Ledger(
            str(paths[node_id]), genesis, node_id, _private_key(root, node_id)
        )
    try:
        proposal_a = ledgers[0].propose([_tx(root, genesis, amount=5)])
        proposal_b = ledgers[0].propose([_tx(root, genesis, amount=6)])
        votes = []
        for node_id in range(quorum_size(8)):
            votes.append(ledgers[node_id].vote(proposal_a))

        prior_vote = votes[1]
        ledgers[1].close()
        ledgers[1] = Ledger(
            str(paths[1]), genesis, 1, _private_key(root, 1)
        )
        assert ledgers[1].vote(proposal_a) == prior_vote
        with pytest.raises(ValueError, match="locked"):
            ledgers[1].vote(proposal_b)

        certificate = make_certificate(proposal_a, votes)
        duplicate = make_certificate(proposal_a, votes + [votes[0]])
        with pytest.raises(ValueError, match="duplicate"):
            ledgers[7].commit_certificate(duplicate)
        outsider = make_certificate(
            proposal_a, votes[:-1] + [{"validator_id": 8, "signature": votes[-1]["signature"]}]
        )
        with pytest.raises(ValueError, match="unknown"):
            ledgers[7].commit_certificate(outsider)
        with pytest.raises(ValueError, match="quorum"):
            ledgers[7].commit_certificate(make_certificate(proposal_a, votes[:5]))

        ledgers[7].commit_certificate(certificate)
        assert ledgers[7].height == 1
        assert ledgers[7].head == block_hash(proposal_a)
        assert ledgers[7].balances[genesis["validators"][1]["public_key"]] == 5
        ledgers[7].close()
        ledgers[7] = Ledger(
            str(paths[7]), genesis, 7, _private_key(root, 7)
        )
        assert ledgers[7].height == 1
        assert ledgers[7].head == block_hash(proposal_a)
    finally:
        for ledger in ledgers.values():
            ledger.close()


def test_restart_rejects_tampered_snapshot_and_certificate(devnet, tmp_path):
    root, genesis = devnet
    paths = {
        node_id: tmp_path / ("node-%d.sqlite3" % node_id)
        for node_id in range(8)
    }
    ledgers = {
        node_id: Ledger(
            str(paths[node_id]), genesis, node_id, _private_key(root, node_id)
        )
        for node_id in range(8)
    }
    try:
        proposal = ledgers[0].propose([_tx(root, genesis, amount=5)])
        votes = [ledgers[node_id].vote(proposal) for node_id in range(6)]
        certificate = make_certificate(proposal, votes)
        for ledger in ledgers.values():
            ledger.commit_certificate(certificate)
    finally:
        for ledger in ledgers.values():
            ledger.close()

    snapshot_path = tmp_path / "tampered-snapshot.sqlite3"
    certificate_path = tmp_path / "tampered-certificate.sqlite3"
    shutil.copyfile(paths[7], snapshot_path)
    shutil.copyfile(paths[7], certificate_path)
    connection = sqlite3.connect(str(snapshot_path))
    connection.execute("UPDATE snapshot SET balances=?", ("{}",))
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="snapshot"):
        Ledger(str(snapshot_path), genesis, 7, _private_key(root, 7))

    connection = sqlite3.connect(str(certificate_path))
    raw_certificate = connection.execute(
        "SELECT certificate FROM certificates WHERE height=1"
    ).fetchone()[0]
    tampered = json.loads(raw_certificate)
    tampered["block"]["state_root"] = "0" * 64
    connection.execute(
        "UPDATE certificates SET certificate=? WHERE height=1",
        (json.dumps(tampered, sort_keys=True, separators=(",", ":")),),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="signature"):
        Ledger(str(certificate_path), genesis, 7, _private_key(root, 7))
