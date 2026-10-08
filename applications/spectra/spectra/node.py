"""Loopback-only HTTP nodes routed over the fixed genesis graph."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
    urlopen,
)

from .ledger import Ledger, strict_json_loads

MAX_REQUEST_BYTES = 256 * 1024
MAX_HANDLERS = 24
RPC_TIMEOUT = 3.0


def _shortest_path(
    n: int, edges: List[List[int]], start: int, destination: int
) -> List[int]:
    neighbors: Dict[int, Set[int]] = {vertex: set() for vertex in range(n)}
    for left, right in edges:
        neighbors[left].add(right)
        neighbors[right].add(left)
    queue: List[Tuple[int, List[int]]] = [(start, [start])]
    visited = {start}
    while queue:
        vertex, path = queue.pop(0)
        if vertex == destination:
            return path
        for neighbor in sorted(neighbors[vertex]):
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))
    raise ValueError("genesis graph has no route between validators")


class NodeService:
    def __init__(self, directory: str, node_id: int, db_path: Optional[str] = None):
        self.directory = Path(directory)
        genesis_path = self.directory / "genesis.json"
        raw = genesis_path.read_bytes()
        self.genesis = strict_json_loads(raw)
        self.validators = self.genesis["validators"]
        self.edges = self.genesis["topology"]["edges"]
        self.relayed_messages = 0
        self._relay_lock = threading.Lock()
        private_key = (self.directory / "keys" / ("node-%d.key" % node_id)).read_text(
            encoding="ascii"
        ).strip()
        path = db_path or str(self.directory / "data" / ("node-%d.sqlite3" % node_id))
        self.ledger = Ledger(path, self.genesis, node_id, private_key)

    def _post_overlay(
        self, path: List[int], message: Mapping[str, Any], received: bool = False
    ) -> Dict[str, Any]:
        if not path or len(path) > len(self.validators) or len(set(path)) != len(path):
            raise ValueError("invalid overlay path")
        if path[0] != self.ledger.node_id:
            raise ValueError("overlay path must start at this node")
        for left, right in zip(path, path[1:]):
            if [min(left, right), max(left, right)] not in self.edges:
                raise ValueError("overlay route contains a non-genesis edge")
        if len(path) == 1:
            return self._dispatch(message)
        if received:
            with self._relay_lock:
                self.relayed_messages += 1
        next_id = path[1]
        body = json.dumps(
            {"path": path[1:], "message": message},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return _request_json(
            "POST",
            self.validators[next_id]["host"],
            self.validators[next_id]["port"],
            "/route",
            body,
        )

    def _dispatch(self, message: Mapping[str, Any]) -> Dict[str, Any]:
        if type(message) is not dict or type(message.get("kind")) is not str:
            raise ValueError("invalid overlay message")
        if message["kind"] == "vote":
            if set(message) != {"kind", "block"}:
                raise ValueError("invalid vote request schema")
            return {"vote": self.ledger.vote(message["block"])}
        if message["kind"] == "certificate":
            if set(message) != {"kind", "certificate"}:
                raise ValueError("invalid certificate message schema")
            self.ledger.commit_certificate(message["certificate"])
            return {"committed": self.ledger.height}
        raise ValueError("unknown overlay message")

    def submit(self, value: Any) -> Dict[str, Any]:
        if self.ledger.node_id != 0:
            raise ValueError("transactions must be submitted to leader zero")
        if type(value) is not dict or set(value) != {"transactions"}:
            raise ValueError("invalid submit request schema")
        block = self.ledger.propose(value["transactions"])
        votes = []
        errors = []
        for validator_id in range(len(self.validators)):
            try:
                if validator_id == 0:
                    result = self._dispatch({"kind": "vote", "block": block})
                else:
                    path = _shortest_path(
                        len(self.validators), self.edges, 0, validator_id
                    )
                    result = self._post_overlay(path, {"kind": "vote", "block": block})
                vote = result["vote"]
                if vote["validator_id"] != validator_id:
                    raise ValueError("vote identity does not match route destination")
                votes.append(vote)
            except Exception as exc:
                errors.append({"validator_id": validator_id, "error": str(exc)})
        from .ledger import make_certificate, quorum_size

        if len(votes) < quorum_size(len(self.validators)):
            raise QuorumUnavailable(len(votes), errors)
        certificate = make_certificate(block, votes)
        self.ledger.commit_certificate(certificate)
        delivered = [0]
        for validator_id in range(1, len(self.validators)):
            try:
                path = _shortest_path(
                    len(self.validators), self.edges, 0, validator_id
                )
                self._post_overlay(
                    path, {"kind": "certificate", "certificate": certificate}
                )
                delivered.append(validator_id)
            except Exception:
                continue
        return {
            "height": self.ledger.height,
            "head": self.ledger.head,
            "votes": len(votes),
            "delivered": delivered,
        }

    def sync_from_neighbor(self, neighbor: int) -> Dict[str, Any]:
        if type(neighbor) is not int or not 0 <= neighbor < len(self.validators):
            raise ValueError("invalid neighbor id")
        node_id = self.ledger.node_id
        edge = [min(node_id, neighbor), max(node_id, neighbor)]
        if neighbor == node_id or edge not in self.edges:
            raise ValueError("catch-up source must be a direct genesis neighbor")
        endpoint = self.validators[neighbor]
        result = _request_json(
            "GET",
            endpoint["host"],
            endpoint["port"],
            "/chain?from=%d" % (self.ledger.height + 1),
        )
        if type(result) is not dict or set(result) != {"certificates"}:
            raise ValueError("invalid neighbor chain response")
        certificates = result["certificates"]
        if type(certificates) is not list:
            raise ValueError("invalid neighbor certificate list")
        for certificate in certificates:
            self.ledger.commit_certificate(certificate)
        return {"height": self.ledger.height, "replayed": len(certificates)}


class QuorumUnavailable(Exception):
    def __init__(self, received: int, errors: List[Dict[str, Any]]):
        super().__init__("quorum unavailable (%d votes)" % received)
        self.received = received
        self.errors = errors


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Tuple[str, int], handler: Any, service: NodeService):
        self.service = service
        self._handler_slots = threading.BoundedSemaphore(MAX_HANDLERS)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._handler_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "SpectraDevnet/1"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> NodeService:
        return cast(NodeService, self.server.service)  # type: ignore[attr-defined]

    def log_message(self, format_string: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body exceeds size limit")
        payload = self.rfile.read(length)
        if len(payload) != length:
            raise ValueError("truncated request body")
        return strict_json_loads(payload)

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/status":
            status = self.service.ledger.status()
            status["relayed_messages"] = self.service.relayed_messages
            self._send_json(200, status)
            return
        if parsed.path == "/chain":
            query = parse_qs(parsed.query, strict_parsing=True)
            if set(query) != {"from"} or len(query["from"]) != 1:
                self._send_json(400, {"error": "from query parameter is required"})
                return
            try:
                first_height = int(query["from"][0])
                certificates = self.service.ledger.certificates_from(first_height)
                encoded = json.dumps(
                    {"certificates": certificates},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(encoded) > MAX_REQUEST_BYTES:
                    raise ValueError("chain response exceeds size limit")
                self._send_json(200, {"certificates": certificates})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        try:
            value = self._read_json()
            if self.path == "/route":
                if type(value) is not dict or set(value) != {"path", "message"}:
                    raise ValueError("invalid route envelope")
                path = value["path"]
                if type(path) is not list or not path or len(path) > 8:
                    raise ValueError("invalid route path")
                if any(type(vertex) is not int for vertex in path):
                    raise ValueError("invalid route vertex")
                result = self.service._post_overlay(
                    path, value["message"], received=True
                )
                self._send_json(200, result)
                return
            if self.path == "/submit":
                result = self.service.submit(value)
                self._send_json(200, result)
                return
            if self.path == "/sync":
                if type(value) is not dict or set(value) != {"neighbor"}:
                    raise ValueError("invalid sync request schema")
                result = self.service.sync_from_neighbor(value["neighbor"])
                self._send_json(200, result)
                return
            self._send_json(404, {"error": "not found"})
        except QuorumUnavailable as exc:
            self._send_json(
                503,
                {"error": str(exc), "votes": exc.received, "failures": exc.errors},
            )
        except (HTTPError, URLError, OSError) as exc:
            self._send_json(503, {"error": str(exc)})
        except Exception as exc:
            self._send_json(400, {"error": str(exc)})


def _request_json(
    method: str, host: str, port: int, path: str, payload: Optional[bytes] = None
) -> Dict[str, Any]:
    url = "http://%s:%d%s" % (host, port, path)
    request = Request(url, data=payload, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("Content-Length", str(len(payload)))
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(
            self,
            req: Request,
            fp: Any,
            code: int,
            msg: str,
            headers: Any,
            new_url: str,
        ) -> Optional[Request]:
            return None

    opener = build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=RPC_TIMEOUT) as response:
            response_body = response.read(MAX_REQUEST_BYTES + 1)
        if len(response_body) > MAX_REQUEST_BYTES:
            raise ValueError("RPC response exceeds size limit")
        result = strict_json_loads(response_body)
    except HTTPError as exc:
        body = exc.read(MAX_REQUEST_BYTES + 1)
        try:
            detail = strict_json_loads(body)
        except ValueError:
            detail = {"error": str(exc)}
        raise RuntimeError(str(detail)) from exc
    if type(result) is not dict:
        raise ValueError("RPC response must be a JSON object")
    return result


def serve_node(directory: str, node_id: int, db_path: Optional[str] = None) -> None:
    service = NodeService(directory, node_id, db_path)
    validator = service.validators[node_id]
    server = BoundedThreadingHTTPServer(
        (validator["host"], validator["port"]), RequestHandler, service
    )
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        service.ledger.close()


def wait_for_node(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen("http://%s:%d/status" % (host, port), timeout=0.3) as response:
                return int(response.status) == 200
        except (OSError, URLError):
            time.sleep(0.05)
    return False
