"""Genesis-bound Ed25519 ledger and durable quorum state."""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .topology import (
    degrees,
    resilience_certificate,
    spectral_certificate,
    validate_graph,
)

MAX_QUANTITY = 2**63 - 1
MAX_TRANSACTIONS = 100
MAX_BLOCK_BYTES = 128 * 1024
MAX_CHAIN_PAGE_CERTIFICATES = 16
MAX_CHAIN_PAGE_BYTES = 256 * 1024
TRANSACTION_DOMAIN = b"spectra/transaction/v1\x00"
PROPOSAL_DOMAIN = b"spectra/proposal/v1\x00"
VOTE_DOMAIN = b"spectra/vote/v1\x00"


def canonical_bytes(value: Any) -> bytes:
    def reject_floats(item: Any) -> None:
        if isinstance(item, float):
            raise ValueError("floating-point JSON values are not allowed")
        if type(item) is dict:
            for key, child in item.items():
                reject_floats(key)
                reject_floats(child)
        elif type(item) in (list, tuple):
            for child in item:
                reject_floats(child)

    reject_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _reject_constant(value: str) -> None:
    raise ValueError("non-finite JSON number is not allowed: " + value)


def _reject_float(value: str) -> None:
    raise ValueError("floating-point JSON values are not allowed: " + value)


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: " + key)
        result[key] = value
    return result


def strict_json_loads(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON") from exc


def _require_object(value: Any, fields: Iterable[str], name: str) -> Dict[str, Any]:
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(name + " has an invalid schema")
    return value


def _quantity(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > MAX_QUANTITY:
        raise ValueError(name + " must be a bounded integer")
    return value


def _hex_bytes(value: Any, size: int, name: str) -> bytes:
    if type(value) is not str or len(value) != size * 2:
        raise ValueError(name + " must be hexadecimal")
    try:
        result = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(name + " must be hexadecimal") from exc
    if result.hex() != value:
        raise ValueError(name + " must use lowercase canonical hexadecimal")
    return result


def public_key_hex(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def new_keypair() -> Tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return raw.hex(), public_key_hex(private_key)


def load_private_key(private_hex: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_hex_bytes(private_hex, 32, "private key"))


def _validator_keys(genesis: Mapping[str, Any]) -> List[str]:
    validators = genesis["validators"]
    return [item["public_key"] for item in validators]


def quorum_size(validators: int) -> int:
    if type(validators) is not int or validators < 1:
        raise ValueError("validator count must be positive")
    return (2 * validators) // 3 + 1


def validate_genesis(genesis: Any) -> str:
    fields = {
        "version", "chain_id", "validators", "faucet", "balances", "topology"
    }
    genesis = _require_object(genesis, fields, "genesis")
    if genesis["version"] != 1 or type(genesis["version"]) is not int:
        raise ValueError("unsupported genesis version")
    if type(genesis["chain_id"]) is not str or not genesis["chain_id"]:
        raise ValueError("chain_id must be a nonempty string")
    validators = genesis["validators"]
    if type(validators) is not list or len(validators) != 8:
        raise ValueError("genesis must contain exactly eight validators")
    keys = []
    ports = []
    for expected_id, validator in enumerate(validators):
        validator = _require_object(
            validator, {"id", "public_key", "host", "port"}, "validator"
        )
        if type(validator["id"]) is not int or validator["id"] != expected_id:
            raise ValueError("validator ids must be contiguous from zero")
        _hex_bytes(validator["public_key"], 32, "validator public key")
        if validator["host"] != "127.0.0.1":
            raise ValueError("validators may listen only on 127.0.0.1")
        port = _quantity(validator["port"], "validator port", 1)
        if port > 65535:
            raise ValueError("validator port is invalid")
        keys.append(validator["public_key"])
        ports.append(port)
    if len(set(keys)) != len(keys):
        raise ValueError("validator public keys must be unique")
    if len(set(ports)) != len(ports):
        raise ValueError("validator ports must be unique")
    if ports != list(range(ports[0], ports[0] + len(validators))):
        raise ValueError("validator ports must be a contiguous range")

    _hex_bytes(genesis["faucet"], 32, "faucet public key")
    if genesis["faucet"] in keys:
        raise ValueError("faucet key must be distinct from validator keys")
    balances = genesis["balances"]
    if type(balances) is not dict or set(balances) != {genesis["faucet"]}:
        raise ValueError("genesis must fund only the faucet account")
    amount = _quantity(balances[genesis["faucet"]], "genesis balance", 1)
    if amount > MAX_QUANTITY:
        raise ValueError("genesis supply exceeds the quantity limit")

    topology = _require_object(
        genesis["topology"], {"n", "degree", "edges", "spectral", "resilience"},
        "topology",
    )
    if type(topology["n"]) is not int or topology["n"] != len(validators):
        raise ValueError("topology vertex count does not match validators")
    if type(topology["degree"]) is not int or topology["degree"] != 4:
        raise ValueError("devnet topology must have degree four")
    edges = topology["edges"]
    if type(edges) is not list:
        raise ValueError("topology edges must be a list")
    graph = validate_graph(8, [tuple(edge) if type(edge) is list else edge for edge in edges])
    if len(graph) != len(edges) or degrees(8, graph) != (4,) * 8:
        raise ValueError("genesis graph must be simple and four-regular")
    if edges != [list(edge) for edge in sorted(graph)]:
        raise ValueError("genesis edges must be sorted and canonical")
    expected_spectral = spectral_certificate(8, graph)
    expected_resilience = resilience_certificate(8, graph, 2)
    if canonical_bytes(topology["spectral"]) != canonical_bytes(expected_spectral):
        raise ValueError("genesis spectral metadata does not match its graph")
    if canonical_bytes(topology["resilience"]) != canonical_bytes(expected_resilience):
        raise ValueError("genesis resilience metadata does not match its graph")
    return hashlib.sha256(canonical_bytes(genesis)).hexdigest()


def transaction_signing_bytes(genesis_hash: str, body: Mapping[str, Any]) -> bytes:
    return TRANSACTION_DOMAIN + bytes.fromhex(genesis_hash) + canonical_bytes(body)


def create_transaction(
    private_key_hex: str, genesis_hash: str, recipient: str, amount: int, nonce: int
) -> Dict[str, Any]:
    private_key = load_private_key(private_key_hex)
    _hex_bytes(recipient, 32, "recipient")
    _quantity(amount, "amount", 1)
    _quantity(nonce, "nonce", 1)
    body = {
        "sender": public_key_hex(private_key),
        "recipient": recipient,
        "amount": amount,
        "nonce": nonce,
    }
    signature = private_key.sign(transaction_signing_bytes(genesis_hash, body)).hex()
    return dict(body, signature=signature)


def validate_transaction(transaction: Any, genesis_hash: str) -> Dict[str, Any]:
    transaction = _require_object(
        transaction, {"sender", "recipient", "amount", "nonce", "signature"},
        "transaction",
    )
    _hex_bytes(transaction["sender"], 32, "sender")
    _hex_bytes(transaction["recipient"], 32, "recipient")
    _quantity(transaction["amount"], "amount", 1)
    _quantity(transaction["nonce"], "nonce", 1)
    _hex_bytes(transaction["signature"], 64, "transaction signature")
    body = {key: transaction[key] for key in ("sender", "recipient", "amount", "nonce")}
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(body["sender"])).verify(
            bytes.fromhex(transaction["signature"]),
            transaction_signing_bytes(genesis_hash, body),
        )
    except InvalidSignature as exc:
        raise ValueError("invalid transaction signature") from exc
    return dict(transaction)


def state_root(balances: Mapping[str, int], nonces: Mapping[str, int]) -> str:
    state = {
        "balances": {key: balances[key] for key in sorted(balances) if balances[key] != 0},
        "nonces": {key: nonces[key] for key in sorted(nonces) if nonces[key] != 0},
    }
    return hashlib.sha256(canonical_bytes(state)).hexdigest()


def apply_transactions(
    balances_in: Mapping[str, int],
    nonces_in: Mapping[str, int],
    transactions: Any,
    genesis_hash: str,
    supply: int,
) -> Tuple[Dict[str, int], Dict[str, int]]:
    if type(transactions) is not list or len(transactions) > MAX_TRANSACTIONS:
        raise ValueError("block transaction list is invalid or too large")
    balances = dict(balances_in)
    nonces = dict(nonces_in)
    for raw_transaction in transactions:
        transaction = validate_transaction(raw_transaction, genesis_hash)
        sender = transaction["sender"]
        recipient = transaction["recipient"]
        amount = transaction["amount"]
        expected_nonce = nonces.get(sender, 0) + 1
        if expected_nonce > MAX_QUANTITY or transaction["nonce"] != expected_nonce:
            raise ValueError("transaction nonce is not the sender's next nonce")
        if balances.get(sender, 0) < amount:
            raise ValueError("insufficient balance")
        if sender != recipient:
            new_recipient_balance = balances.get(recipient, 0) + amount
            if new_recipient_balance > MAX_QUANTITY:
                raise ValueError("recipient balance exceeds the quantity limit")
            balances[sender] -= amount
            balances[recipient] = new_recipient_balance
        nonces[sender] = expected_nonce
    if sum(balances.values()) != supply:
        raise ValueError("transaction execution changed total supply")
    return balances, nonces


def _proposal_body(block: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: block[key] for key in (
        "height", "parent_hash", "transactions", "state_root", "proposer"
    )}


def _proposal_signing_bytes(genesis_hash: str, body: Mapping[str, Any]) -> bytes:
    return PROPOSAL_DOMAIN + bytes.fromhex(genesis_hash) + canonical_bytes(body)


def block_hash(block: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(block)).hexdigest()


def _validate_block_signature(block: Any, genesis_hash: str, validators: List[str]) -> Dict[str, Any]:
    block = _require_object(
        block,
        {"height", "parent_hash", "transactions", "state_root", "proposer", "signature"},
        "block",
    )
    _quantity(block["height"], "block height", 1)
    _hex_bytes(block["parent_hash"], 32, "parent hash")
    _hex_bytes(block["state_root"], 32, "state root")
    if type(block["proposer"]) is not int or block["proposer"] != 0:
        raise ValueError("only fixed leader zero may propose")
    _hex_bytes(block["signature"], 64, "proposal signature")
    if type(block["transactions"]) is not list or len(block["transactions"]) > MAX_TRANSACTIONS:
        raise ValueError("block transaction list is invalid or too large")
    if len(canonical_bytes(block)) > MAX_BLOCK_BYTES:
        raise ValueError("block exceeds the maximum encoded size")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(validators[0])).verify(
            bytes.fromhex(block["signature"]),
            _proposal_signing_bytes(genesis_hash, _proposal_body(block)),
        )
    except InvalidSignature as exc:
        raise ValueError("invalid proposal signature") from exc
    return dict(block)


def vote_signing_bytes(genesis_hash: str, height: int, digest: str) -> bytes:
    body = {"height": height, "block_hash": digest}
    return VOTE_DOMAIN + bytes.fromhex(genesis_hash) + canonical_bytes(body)


def make_certificate(block: Mapping[str, Any], votes: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    return {"block": dict(block), "votes": [dict(vote) for vote in votes]}


class Ledger:
    """One validator's durable ledger, bound to genesis and validator identity."""

    def __init__(
        self,
        db_path: str,
        genesis: Mapping[str, Any],
        node_id: int,
        private_key_hex: str,
    ) -> None:
        self.genesis = dict(genesis)
        self.genesis_hash = validate_genesis(self.genesis)
        self.validators = _validator_keys(self.genesis)
        if type(node_id) is not int or not 0 <= node_id < len(self.validators):
            raise ValueError("invalid validator id")
        self.node_id = node_id
        self.private_key = load_private_key(private_key_hex)
        if public_key_hex(self.private_key) != self.validators[node_id]:
            raise ValueError("private key does not match validator identity")
        self.supply = sum(self.genesis["balances"].values())
        self._lock = threading.RLock()
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        self._db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        if not existed:
            self._initialize_database()
        self._load_database()

    def _initialize_database(self) -> None:
        with self._db:
            self._db.execute("CREATE TABLE identity (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.execute(
                "CREATE TABLE certificates (height INTEGER PRIMARY KEY, certificate TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE vote_locks (height INTEGER PRIMARY KEY, block_hash TEXT NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE snapshot (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "height INTEGER NOT NULL, head TEXT NOT NULL, state_root TEXT NOT NULL, "
                "balances TEXT NOT NULL, nonces TEXT NOT NULL)"
            )
            self._db.executemany(
                "INSERT INTO identity(key, value) VALUES (?, ?)",
                [("genesis_hash", self.genesis_hash), ("node_id", str(self.node_id))],
            )
            balances = dict(self.genesis["balances"])
            nonces: Dict[str, int] = {}
            self._db.execute(
                "INSERT INTO snapshot VALUES (1, 0, ?, ?, ?, ?)",
                (
                    self.genesis_hash,
                    state_root(balances, nonces),
                    canonical_bytes(balances).decode("ascii"),
                    canonical_bytes(nonces).decode("ascii"),
                ),
            )

    def _load_database(self) -> None:
        with self._lock:
            try:
                identity = dict(self._db.execute("SELECT key, value FROM identity"))
                if identity != {
                    "genesis_hash": self.genesis_hash,
                    "node_id": str(self.node_id),
                }:
                    raise ValueError("database identity does not match genesis and node")
                self._db.execute("SELECT height, block_hash FROM vote_locks").fetchall()
                rows = self._db.execute(
                    "SELECT height, certificate FROM certificates ORDER BY height"
                ).fetchall()
                balances = dict(self.genesis["balances"])
                nonces: Dict[str, int] = {}
                height = 0
                head = self.genesis_hash
                for row_height, raw_certificate in rows:
                    certificate = strict_json_loads(raw_certificate.encode("utf-8"))
                    block = certificate["block"]
                    if row_height != height + 1:
                        raise ValueError("stored certificate heights are not contiguous")
                    balances, nonces, head = self._validate_and_apply_certificate(
                        certificate, height, head, balances, nonces
                    )
                    height = row_height
                    if block["height"] != height:
                        raise ValueError("stored certificate key does not match block height")
                snapshot = self._db.execute(
                    "SELECT height, head, state_root, balances, nonces FROM snapshot WHERE singleton=1"
                ).fetchone()
                if snapshot is None:
                    raise ValueError("database snapshot is missing")
                snapshot_balances = strict_json_loads(snapshot[3].encode("utf-8"))
                snapshot_nonces = strict_json_loads(snapshot[4].encode("utf-8"))
                if (
                    snapshot[0] != height
                    or snapshot[1] != head
                    or snapshot[2] != state_root(balances, nonces)
                    or snapshot_balances != balances
                    or snapshot_nonces != nonces
                ):
                    raise ValueError("database snapshot does not match replayed certificates")
                self.height = height
                self.head = head
                self.balances = balances
                self.nonces = nonces
            except sqlite3.DatabaseError as exc:
                raise ValueError("ledger database is corrupt or incomplete") from exc

    def _validate_and_apply_certificate(
        self,
        certificate: Any,
        current_height: int,
        current_head: str,
        balances: Mapping[str, int],
        nonces: Mapping[str, int],
    ) -> Tuple[Dict[str, int], Dict[str, int], str]:
        certificate = _require_object(certificate, {"block", "votes"}, "certificate")
        block = _validate_block_signature(certificate["block"], self.genesis_hash, self.validators)
        if block["height"] != current_height + 1:
            raise ValueError("certificate height is not the next height")
        if block["parent_hash"] != current_head:
            raise ValueError("certificate parent does not match ledger head")
        next_balances, next_nonces = apply_transactions(
            balances, nonces, block["transactions"], self.genesis_hash, self.supply
        )
        expected_root = state_root(next_balances, next_nonces)
        if block["state_root"] != expected_root:
            raise ValueError("block state root does not match executed state")
        votes = certificate["votes"]
        if type(votes) is not list:
            raise ValueError("certificate votes must be a list")
        seen = set()
        digest = block_hash(block)
        for vote in votes:
            vote = _require_object(vote, {"validator_id", "signature"}, "vote")
            validator_id = vote["validator_id"]
            if type(validator_id) is not int or not 0 <= validator_id < len(self.validators):
                raise ValueError("certificate contains an unknown validator")
            if validator_id in seen:
                raise ValueError("certificate contains a duplicate validator")
            seen.add(validator_id)
            _hex_bytes(vote["signature"], 64, "vote signature")
            try:
                Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(self.validators[validator_id])
                ).verify(
                    bytes.fromhex(vote["signature"]),
                    vote_signing_bytes(self.genesis_hash, block["height"], digest),
                )
            except InvalidSignature as exc:
                raise ValueError("invalid certificate vote") from exc
        if len(seen) < quorum_size(len(self.validators)):
            raise ValueError("certificate does not contain a quorum")
        return next_balances, next_nonces, digest

    def propose(self, transactions: Any) -> Dict[str, Any]:
        with self._lock:
            if self.node_id != 0:
                raise ValueError("only validator zero may propose")
            next_balances, next_nonces = apply_transactions(
                self.balances, self.nonces, transactions, self.genesis_hash, self.supply
            )
            body = {
                "height": self.height + 1,
                "parent_hash": self.head,
                "transactions": transactions,
                "state_root": state_root(next_balances, next_nonces),
                "proposer": 0,
            }
            block = dict(body)
            block["signature"] = self.private_key.sign(
                _proposal_signing_bytes(self.genesis_hash, body)
            ).hex()
            _validate_block_signature(block, self.genesis_hash, self.validators)
            if len(canonical_bytes(block)) > MAX_BLOCK_BYTES:
                raise ValueError("block exceeds the maximum encoded size")
            return block

    def vote(self, raw_block: Any) -> Dict[str, Any]:
        with self._lock:
            block = _validate_block_signature(raw_block, self.genesis_hash, self.validators)
            digest = block_hash(block)
            height = block["height"]
            existing = self._db.execute(
                "SELECT block_hash FROM vote_locks WHERE height=?", (height,)
            ).fetchone()
            if height <= self.height:
                committed = self._db.execute(
                    "SELECT certificate FROM certificates WHERE height=?", (height,)
                ).fetchone()
                if committed is None or block_hash(strict_json_loads(
                    committed[0].encode("utf-8")
                )["block"]) != digest:
                    raise ValueError("cannot vote at an already committed height")
                if existing is not None and existing[0] != digest:
                    raise ValueError("validator is durably locked to a competing proposal")
                if existing is None:
                    with self._db:
                        self._db.execute(
                            "INSERT INTO vote_locks(height, block_hash) VALUES (?, ?)",
                            (height, digest),
                        )
            else:
                if height != self.height + 1 or block["parent_hash"] != self.head:
                    raise ValueError("proposal is not the next block on this chain")
                next_balances, next_nonces = apply_transactions(
                    self.balances, self.nonces, block["transactions"],
                    self.genesis_hash, self.supply,
                )
                if block["state_root"] != state_root(next_balances, next_nonces):
                    raise ValueError("proposal state root does not match executed state")
                if existing is not None and existing[0] != digest:
                    raise ValueError("validator is durably locked to a competing proposal")
                if existing is None:
                    with self._db:
                        self._db.execute(
                            "INSERT INTO vote_locks(height, block_hash) VALUES (?, ?)",
                            (height, digest),
                        )
            signature = self.private_key.sign(
                vote_signing_bytes(self.genesis_hash, height, digest)
            ).hex()
            return {"validator_id": self.node_id, "signature": signature}

    def commit_certificate(self, raw_certificate: Any) -> None:
        with self._lock:
            balances, nonces, digest = self._validate_and_apply_certificate(
                raw_certificate, self.height, self.head, self.balances, self.nonces
            )
            block = raw_certificate["block"]
            encoded = canonical_bytes(raw_certificate).decode("ascii")
            with self._db:
                self._db.execute(
                    "INSERT INTO certificates(height, certificate) VALUES (?, ?)",
                    (block["height"], encoded),
                )
                self._db.execute(
                    "UPDATE snapshot SET height=?, head=?, state_root=?, balances=?, nonces=? "
                    "WHERE singleton=1",
                    (
                        block["height"], digest, state_root(balances, nonces),
                        canonical_bytes(balances).decode("ascii"),
                        canonical_bytes(nonces).decode("ascii"),
                    ),
                )
            self.height = block["height"]
            self.head = digest
            self.balances = balances
            self.nonces = nonces

    def certificates_from(self, first_height: int) -> List[Dict[str, Any]]:
        _quantity(first_height, "first height", 1)
        with self._lock:
            rows = self._db.execute(
                "SELECT certificate FROM certificates WHERE height>=? ORDER BY height",
                (first_height,),
            ).fetchall()
            return [strict_json_loads(row[0].encode("utf-8")) for row in rows]

    def certificates_page(self, first_height: int) -> Dict[str, Any]:
        _quantity(first_height, "first height", 1)
        with self._lock:
            rows = self._db.execute(
                "SELECT height, certificate FROM certificates WHERE height>=? ORDER BY height",
                (first_height,),
            ).fetchall()
            page: List[Dict[str, Any]] = []
            next_height = first_height
            for index, (height, raw_certificate) in enumerate(rows):
                certificate = strict_json_loads(raw_certificate.encode("utf-8"))
                candidate = {
                    "certificates": page + [certificate],
                    "next_height": height + 1,
                    "more": index + 1 < len(rows),
                }
                if len(canonical_bytes(candidate)) > MAX_CHAIN_PAGE_BYTES:
                    if not page:
                        raise ValueError("chain certificate exceeds page size")
                    break
                page.append(certificate)
                next_height = height + 1
                if len(page) >= MAX_CHAIN_PAGE_CERTIFICATES:
                    break
            return {
                "certificates": page,
                "next_height": next_height,
                "more": len(page) < len(rows),
            }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "node_id": self.node_id,
                "chain_id": self.genesis["chain_id"],
                "genesis_hash": self.genesis_hash,
                "height": self.height,
                "head": self.head,
                "state_root": state_root(self.balances, self.nonces),
                "balances": dict(sorted(self.balances.items())),
                "nonces": dict(sorted(self.nonces.items())),
            }

    def close(self) -> None:
        with self._lock:
            self._db.close()
