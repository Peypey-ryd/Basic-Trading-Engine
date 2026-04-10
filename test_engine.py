"""
Test suite for the trading system.
Run with:  pytest tests/  (from the trading_system/ root)
"""

import struct
import socket
import threading
import time
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.protocol import (
    Message, MessageType, Order, OrderSide, OrderType, OrderStatus, _recv_exactly
)
from server.server import MatchingEngine, get_market_price, TradingServer


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def engine():
    return MatchingEngine()


@pytest.fixture
def fixed_price(monkeypatch):
    """Monkeypatch market price to a stable 100.0 for deterministic tests."""
    monkeypatch.setattr("server.server.get_market_price", lambda s: 100.0)


# ── Protocol Tests ────────────────────────────────────────────────────────────

class TestMessageProtocol:
    def test_encode_produces_length_prefix(self):
        msg = Message(MessageType.HEARTBEAT, {})
        encoded = msg.encode()
        (length,) = struct.unpack("!I", encoded[:4])
        assert length == len(encoded) - 4

    def test_encode_decode_roundtrip_via_socket(self):
        """Send a message through a real socket pair and decode it."""
        msg = Message(MessageType.ORDER_ACK, {"order_id": "ABC123", "status": "PENDING"})

        server_sock, client_sock = socket.socketpair()
        try:
            server_sock.sendall(msg.encode())
            decoded = Message.decode_from_socket(client_sock)
        finally:
            server_sock.close()
            client_sock.close()

        assert decoded is not None
        assert decoded.msg_type == MessageType.ORDER_ACK
        assert decoded.payload["order_id"] == "ABC123"

    def test_decode_returns_none_on_closed_socket(self):
        server_sock, client_sock = socket.socketpair()
        server_sock.close()
        result = Message.decode_from_socket(client_sock)
        client_sock.close()
        assert result is None

    def test_all_message_types_survive_roundtrip(self):
        """Every MessageType should encode and decode without error."""
        server_sock, client_sock = socket.socketpair()
        try:
            for mt in MessageType:
                msg = Message(mt, {"key": "value"})
                server_sock.sendall(msg.encode())
                decoded = Message.decode_from_socket(client_sock)
                assert decoded.msg_type == mt
        finally:
            server_sock.close()
            client_sock.close()


# ── Order Validation Tests ────────────────────────────────────────────────────

class TestOrderValidation:
    def test_valid_market_order(self):
        order = Order(symbol="AAPL", side=OrderSide.BUY,
                      order_type=OrderType.MARKET, quantity=10)
        assert order.order_id != ""
        assert order.client_order_id.startswith("CLT-")

    def test_valid_limit_order(self):
        order = Order(symbol="AAPL", side=OrderSide.BUY,
                      order_type=OrderType.LIMIT, quantity=10, price=150.0)
        assert order.price == 150.0

    def test_negative_quantity_raises(self):
        with pytest.raises(ValueError, match="Quantity must be positive"):
            Order(symbol="AAPL", side=OrderSide.BUY,
                  order_type=OrderType.MARKET, quantity=-5)

    def test_zero_quantity_raises(self):
        with pytest.raises(ValueError, match="Quantity must be positive"):
            Order(symbol="AAPL", side=OrderSide.BUY,
                  order_type=OrderType.MARKET, quantity=0)

    def test_limit_order_without_price_raises(self):
        with pytest.raises(ValueError, match="LIMIT orders require a positive price"):
            Order(symbol="AAPL", side=OrderSide.BUY,
                  order_type=OrderType.LIMIT, quantity=10)

    def test_limit_order_with_zero_price_raises(self):
        with pytest.raises(ValueError, match="LIMIT orders require a positive price"):
            Order(symbol="AAPL", side=OrderSide.BUY,
                  order_type=OrderType.LIMIT, quantity=10, price=0)

    def test_order_serialization_roundtrip(self):
        original = Order(symbol="TSLA", side=OrderSide.SELL,
                         order_type=OrderType.LIMIT, quantity=5, price=250.0)
        restored = Order.from_dict(original.to_dict())
        assert restored.symbol == original.symbol
        assert restored.side == original.side
        assert restored.price == original.price
        assert restored.order_id == original.order_id


# ── Matching Engine Tests ─────────────────────────────────────────────────────

class TestMatchingEngine:
    def test_market_order_fills_immediately(self, engine, fixed_price):
        order = Order(symbol="AAPL", side=OrderSide.BUY,
                      order_type=OrderType.MARKET, quantity=10)
        result = engine.submit(order)
        assert result.status == OrderStatus.FILLED
        assert result.avg_fill_price == 100.0
        assert result.filled_quantity == 10

    def test_market_sell_fills_immediately(self, engine, fixed_price):
        order = Order(symbol="AAPL", side=OrderSide.SELL,
                      order_type=OrderType.MARKET, quantity=5)
        result = engine.submit(order)
        assert result.status == OrderStatus.FILLED

    def test_limit_buy_fills_when_price_favorable(self, engine, fixed_price):
        # Market=100, limit=110 → favorable for a buy
        order = Order(symbol="AAPL", side=OrderSide.BUY,
                      order_type=OrderType.LIMIT, quantity=10, price=110.0)
        result = engine.submit(order)
        assert result.status == OrderStatus.FILLED
        assert len(engine._pending_orders) == 0

    def test_limit_sell_fills_when_price_favorable(self, engine, fixed_price):
        # Market=100, limit=90 → favorable for a sell
        order = Order(symbol="AAPL", side=OrderSide.SELL,
                      order_type=OrderType.LIMIT, quantity=10, price=90.0)
        result = engine.submit(order)
        assert result.status == OrderStatus.FILLED

    def test_limit_buy_queues_when_price_unfavorable(self, engine, fixed_price):
        # Market=100, limit=90 → not favorable for a buy
        order = Order(symbol="AAPL", side=OrderSide.BUY,
                      order_type=OrderType.LIMIT, quantity=10, price=90.0)
        result = engine.submit(order)
        assert result.status == OrderStatus.PENDING
        assert len(engine._pending_orders) == 1

    def test_limit_sell_queues_when_price_unfavorable(self, engine, fixed_price):
        # Market=100, limit=110 → not favorable for a sell
        order = Order(symbol="AAPL", side=OrderSide.SELL,
                      order_type=OrderType.LIMIT, quantity=10, price=110.0)
        result = engine.submit(order)
        assert result.status == OrderStatus.PENDING

    def test_unknown_symbol_is_rejected(self, engine):
        order = Order(symbol="FAKE", side=OrderSide.BUY,
                      order_type=OrderType.MARKET, quantity=1)
        result = engine.submit(order)
        assert result.status == OrderStatus.REJECTED

    def test_cancel_pending_order(self, engine, fixed_price):
        order = Order(symbol="AAPL", side=OrderSide.BUY,
                      order_type=OrderType.LIMIT, quantity=10, price=90.0)
        engine.submit(order)
        cancelled = engine.cancel(order.order_id)
        assert cancelled is not None
        assert cancelled.status == OrderStatus.CANCELLED
        assert len(engine._pending_orders) == 0

    def test_cancel_nonexistent_order_returns_none(self, engine):
        result = engine.cancel("NONEXISTENT")
        assert result is None

    def test_pending_order_fills_when_price_moves(self, engine, monkeypatch):
        """Limit order queued at 90 should fill when market drops to 90."""
        monkeypatch.setattr("server.server.get_market_price", lambda s: 95.0)
        order = Order(symbol="AAPL", side=OrderSide.BUY,
                      order_type=OrderType.LIMIT, quantity=5, price=90.0)
        engine.submit(order)
        assert len(engine._pending_orders) == 1

        # Market moves down to 89 — should now fill
        monkeypatch.setattr("server.server.get_market_price", lambda s: 89.0)
        filled = engine.try_fill_pending()
        assert len(filled) == 1
        assert filled[0].status == OrderStatus.FILLED
        assert len(engine._pending_orders) == 0

    def test_multiple_concurrent_orders(self, engine, fixed_price):
        """Submit several orders from different threads; all should process."""
        results = []
        lock = threading.Lock()

        def submit_order(sym):
            o = Order(symbol=sym, side=OrderSide.BUY,
                      order_type=OrderType.MARKET, quantity=1)
            r = engine.submit(o)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=submit_order, args=("AAPL",))
                   for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 10
        assert all(r.status == OrderStatus.FILLED for r in results)


# ── Integration Test ──────────────────────────────────────────────────────────

class TestClientServerIntegration:
    """Spin up a real server and client socket to test the full message flow."""

    def test_order_request_and_fill(self, monkeypatch):
        monkeypatch.setattr("server.server.get_market_price", lambda s: 100.0)

        server = TradingServer(host="127.0.0.1", port=19999)
        t = threading.Thread(target=server.start, daemon=True)
        t.start()
        time.sleep(0.2)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", 19999))

        try:
            order = Order(symbol="AAPL", side=OrderSide.BUY,
                          order_type=OrderType.MARKET, quantity=10)
            sock.sendall(Message(MessageType.ORDER_REQUEST, order.to_dict()).encode())

            ack = Message.decode_from_socket(sock)
            assert ack.msg_type == MessageType.ORDER_ACK

            update = Message.decode_from_socket(sock)
            assert update.msg_type == MessageType.ORDER_UPDATE
            assert update.payload["status"] == OrderStatus.FILLED.value
            assert update.payload["avg_fill_price"] == 100.0
        finally:
            sock.close()
            server._running = False
