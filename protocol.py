"""
Shared protocol definitions for the trading system.
Defines order types, statuses, and message serialization.
"""

import json
import uuid
import struct
import socket
from dataclasses import dataclass, asdict
from enum import Enum
from datetime import datetime


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class MessageType(str, Enum):
    ORDER_REQUEST = "ORDER_REQUEST"
    ORDER_ACK = "ORDER_ACK"
    ORDER_UPDATE = "ORDER_UPDATE"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    CANCEL_ACK = "CANCEL_ACK"
    ERROR = "ERROR"
    HEARTBEAT = "HEARTBEAT"


@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: float | None = None       # Required for LIMIT orders
    order_id: str = ""
    client_order_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    timestamp: str = ""

    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError("Quantity must be positive")
        if self.order_type == OrderType.LIMIT and (self.price is None or self.price <= 0):
            raise ValueError("LIMIT orders require a positive price")
        if not self.order_id:
            self.order_id = str(uuid.uuid4())[:8].upper()
        if not self.client_order_id:
            self.client_order_id = f"CLT-{str(uuid.uuid4())[:6].upper()}"
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["order_type"] = self.order_type.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Order":
        d = d.copy()
        d["side"] = OrderSide(d["side"])
        d["order_type"] = OrderType(d["order_type"])
        d["status"] = OrderStatus(d["status"])
        return cls(**d)


@dataclass
class Message:
    msg_type: MessageType
    payload: dict

    def encode(self) -> bytes:
        """Encodes message with a 4-byte length prefix (network byte order)."""
        body = json.dumps({
            "msg_type": self.msg_type.value,
            "payload": self.payload,
        }).encode("utf-8")
        header = struct.pack("!I", len(body))
        return header + body

    @classmethod
    def decode_from_socket(cls, sock: socket.socket) -> "Message | None":
        """Reads exactly one length-prefixed message from a socket.
        Returns None if the connection was closed cleanly.
        """
        raw_header = _recv_exactly(sock, 4)
        if raw_header is None:
            return None
        (length,) = struct.unpack("!I", raw_header)
        raw_body = _recv_exactly(sock, length)
        if raw_body is None:
            return None
        data = json.loads(raw_body.decode("utf-8"))
        return cls(msg_type=MessageType(data["msg_type"]), payload=data["payload"])


def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    """Read exactly n bytes from sock, returning None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
