"""
Trading Server
Accepts client connections, processes orders through a simple matching engine,
and streams back order updates.
"""

import socket
import threading
import logging
import random
import time
import queue
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.protocol import (
    Message, MessageType, Order, OrderStatus, OrderType, OrderSide
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")

# ── Simulated Market Prices ───────────────────────────────────────────────────

MARKET_PRICES: dict[str, float] = {
    "AAPL": 189.50,
    "TSLA": 245.30,
    "NVDA": 875.20,
    "MSFT": 415.80,
    "GOOG": 175.40,
    "AMZN": 192.60,
}


def get_market_price(symbol: str) -> float | None:
    base = MARKET_PRICES.get(symbol)
    if base is None:
        return None
    # Simulate small price movement ±0.5%
    return round(base * (1 + random.uniform(-0.005, 0.005)), 2)


# ── Matching Engine ───────────────────────────────────────────────────────────

class MatchingEngine:
    """
    Simplified matching engine.
    - MARKET orders: always fill at current market price.
    - LIMIT orders:  fill immediately if price is favorable, otherwise queue.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending_orders: dict[str, Order] = {}

    def submit(self, order: Order) -> Order:
        with self._lock:
            market_price = get_market_price(order.symbol)

            if market_price is None:
                order.status = OrderStatus.REJECTED
                log.warning("Rejected order %s — unknown symbol %s",
                            order.order_id, order.symbol)
                return order

            if order.order_type == OrderType.MARKET:
                order = self._fill(order, market_price)

            elif order.order_type == OrderType.LIMIT:
                can_fill = (
                    (order.side == OrderSide.BUY  and market_price <= order.price) or
                    (order.side == OrderSide.SELL and market_price >= order.price)
                )
                if can_fill:
                    order = self._fill(order, market_price)
                else:
                    self._pending_orders[order.order_id] = order
                    log.info("Queued LIMIT order %s — market=%.2f, limit=%.2f",
                             order.order_id, market_price, order.price)

            return order

    def cancel(self, order_id: str) -> Order | None:
        with self._lock:
            order = self._pending_orders.pop(order_id, None)
            if order:
                order.status = OrderStatus.CANCELLED
            return order

    def try_fill_pending(self) -> list[Order]:
        """Called periodically to attempt fills on queued limit orders."""
        filled = []
        with self._lock:
            for oid in list(self._pending_orders):
                order = self._pending_orders[oid]
                market_price = get_market_price(order.symbol)
                if market_price is None:
                    continue
                can_fill = (
                    (order.side == OrderSide.BUY  and market_price <= order.price) or
                    (order.side == OrderSide.SELL and market_price >= order.price)
                )
                if can_fill:
                    order = self._fill(order, market_price)
                    del self._pending_orders[oid]
                    filled.append(order)
        return filled

    @staticmethod
    def _fill(order: Order, price: float) -> Order:
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.avg_fill_price = price
        log.info(
            "FILL  %s %s %s %.0f @ %.2f",
            order.order_id, order.side.value,
            order.symbol, order.quantity, price,
        )
        return order


# ── Client Handler ────────────────────────────────────────────────────────────

class ClientHandler(threading.Thread):
    def __init__(self, conn: socket.socket, addr, engine: MatchingEngine,
                 fill_callbacks: list):
        super().__init__(daemon=True)
        self.conn = conn
        self.addr = addr
        self.engine = engine
        self.fill_callbacks = fill_callbacks
        self._active = True
        self.send_queue: queue.Queue = queue.Queue()

    def run(self):
        log.info("Client connected: %s:%d", *self.addr)
        self.fill_callbacks.append(self._push_update)

        # Dedicated writer thread drains send_queue → socket
        writer = threading.Thread(target=self._writer_loop, daemon=True)
        writer.start()

        try:
            while self._active:
                msg = Message.decode_from_socket(self.conn)
                if msg is None:
                    break
                self._handle(msg)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception as e:
            log.error("Handler error with %s:%d: %s", *self.addr, e)
        finally:
            self._cleanup()
            writer.join(timeout=2)

    def _writer_loop(self):
        """Drains the outbound queue and writes to the socket.
        Decouples the matching engine from slow/blocked clients.
        """
        while self._active:
            try:
                msg = self.send_queue.get(timeout=1.0)
                self.conn.sendall(msg.encode())
            except queue.Empty:
                continue
            except (BrokenPipeError, OSError):
                self._active = False
                break

    def _cleanup(self):
        self._active = False
        if self._push_update in self.fill_callbacks:
            self.fill_callbacks.remove(self._push_update)
        self.conn.close()
        log.info("Client disconnected: %s:%d", *self.addr)

    def _handle(self, msg: Message):
        if msg.msg_type == MessageType.ORDER_REQUEST:
            self._process_order(msg.payload)
        elif msg.msg_type == MessageType.CANCEL_REQUEST:
            self._process_cancel(msg.payload)
        elif msg.msg_type == MessageType.HEARTBEAT:
            self._send(Message(MessageType.HEARTBEAT, {}))

    def _process_order(self, payload: dict):
        try:
            order = Order.from_dict(payload)
        except Exception as e:
            self._send(Message(MessageType.ERROR, {"reason": str(e)}))
            return

        # Acknowledge receipt
        self._send(Message(MessageType.ORDER_ACK, {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "status": OrderStatus.PENDING.value,
        }))

        # Submit to engine and send result
        order = self.engine.submit(order)
        self._send(Message(MessageType.ORDER_UPDATE, order.to_dict()))

    def _process_cancel(self, payload: dict):
        order_id = payload.get("order_id", "")
        order = self.engine.cancel(order_id)
        if order:
            self._send(Message(MessageType.CANCEL_ACK, order.to_dict()))
        else:
            self._send(Message(MessageType.ERROR, {
                "reason": f"Order {order_id} not found or already filled"
            }))

    def _push_update(self, order: Order):
        """Callback from the engine poll thread — enqueues async fill notification."""
        self.send_queue.put(Message(MessageType.ORDER_UPDATE, order.to_dict()))

    def _send(self, msg: Message):
        """Enqueue a message for the writer thread."""
        self.send_queue.put(msg)


# ── Server ────────────────────────────────────────────────────────────────────

class TradingServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 9999):
        self.host = host
        self.port = port
        self.engine = MatchingEngine()
        self._fill_callbacks: list = []
        self._running = False

    def start(self):
        self._running = True
        # Background thread: periodically tries to fill queued limit orders
        t = threading.Thread(target=self._poll_engine, daemon=True)
        t.start()

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(10)
        log.info("Listening on %s:%d", self.host, self.port)

        try:
            while self._running:
                try:
                    srv.settimeout(1.0)
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                handler = ClientHandler(conn, addr, self.engine,
                                        self._fill_callbacks)
                handler.start()
        except KeyboardInterrupt:
            log.info("Server shutting down.")
        finally:
            srv.close()

    def _poll_engine(self):
        while self._running:
            filled = self.engine.try_fill_pending()
            for order in filled:
                for cb in list(self._fill_callbacks):
                    try:
                        cb(order)
                    except Exception:
                        pass
            time.sleep(1)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Trading Server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9999)
    args = p.parse_args()
    TradingServer(args.host, args.port).start()
