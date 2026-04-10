"""
Trading Client
Connects to the trading server and provides an interactive CLI
for submitting and cancelling orders.
"""

import socket
import threading
import logging
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.protocol import (
    Message, MessageType, Order, OrderSide, OrderType
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CLIENT] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("client")

# ── Client ────────────────────────────────────────────────────────────────────

class TradingClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 9999):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._running = False
        self._pending: dict[str, str] = {}   # order_id -> client_order_id

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.connect((self.host, self.port))
            self._running = True
            t = threading.Thread(target=self._receive_loop, daemon=True)
            t.start()
            log.info("Connected to %s:%d", self.host, self.port)
            return True
        except ConnectionRefusedError:
            log.error("Could not connect to server at %s:%d", self.host, self.port)
            return False

    def disconnect(self):
        self._running = False
        if self._sock:
            self._sock.close()
        log.info("Disconnected.")

    # ── Sending Orders ────────────────────────────────────────────────────────

    def send_market_order(self, symbol: str, side: OrderSide, quantity: float) -> str:
        order = Order(
            symbol=symbol.upper(),
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
        )
        self._send(Message(MessageType.ORDER_REQUEST, order.to_dict()))
        log.info("Sent MARKET %s %s x%.0f  [%s]",
                 side.value, symbol.upper(), quantity, order.order_id)
        return order.order_id

    def send_limit_order(self, symbol: str, side: OrderSide,
                         quantity: float, price: float) -> str:
        order = Order(
            symbol=symbol.upper(),
            side=side,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=price,
        )
        self._send(Message(MessageType.ORDER_REQUEST, order.to_dict()))
        log.info("Sent LIMIT  %s %s x%.0f @ %.2f  [%s]",
                 side.value, symbol.upper(), quantity, price, order.order_id)
        return order.order_id

    def cancel_order(self, order_id: str):
        self._send(Message(MessageType.CANCEL_REQUEST, {"order_id": order_id}))
        log.info("Sent CANCEL request for %s", order_id)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _send(self, msg: Message):
        if not self._sock:
            raise RuntimeError("Not connected")
        self._sock.sendall(msg.encode())

    def _receive_loop(self):
        while self._running:
            try:
                msg = Message.decode_from_socket(self._sock)
                if msg is None:
                    log.warning("Server closed the connection.")
                    self._running = False
                    break
                self._handle_message(msg)
            except (ConnectionResetError, BrokenPipeError, OSError):
                if self._running:
                    log.warning("Connection lost.")
                self._running = False
                break
            except Exception as e:
                log.error("Receive error: %s", e)
                break

    def _handle_message(self, msg: Message):

        if msg.msg_type == MessageType.ORDER_ACK:
            p = msg.payload
            log.info("  ✓ ACK    order_id=%s  status=%s",
                     p["order_id"], p["status"])

        elif msg.msg_type == MessageType.ORDER_UPDATE:
            p = msg.payload
            status = p["status"]
            sym    = p["symbol"]
            side   = p["side"]
            qty    = p["quantity"]
            filled = p.get("filled_quantity", 0)
            price  = p.get("avg_fill_price", 0)
            oid    = p["order_id"]

            if status == "FILLED":
                log.info("  ✅ FILLED  %s %s x%.0f @ %.2f  [%s]",
                         side, sym, filled, price, oid)
            elif status == "PARTIALLY_FILLED":
                log.info("  🔶 PARTIAL %s %s %.0f/%.0f @ %.2f  [%s]",
                         side, sym, filled, qty, price, oid)
            elif status == "REJECTED":
                log.warning("  ❌ REJECT  %s %s x%.0f  [%s]",
                            side, sym, qty, oid)
            else:
                log.info("  ℹ ORDER_UPDATE  %s  status=%s  [%s]",
                         sym, status, oid)

        elif msg.msg_type == MessageType.CANCEL_ACK:
            p = msg.payload
            log.info("  🚫 CANCELLED  order_id=%s", p["order_id"])

        elif msg.msg_type == MessageType.ERROR:
            log.error("  ⚠ SERVER ERROR: %s", msg.payload.get("reason"))

        elif msg.msg_type == MessageType.HEARTBEAT:
            pass   # Silently handled


# ── Interactive CLI ───────────────────────────────────────────────────────────

HELP = """
Commands:
  buy  market <SYMBOL> <QTY>                 — Market buy
  sell market <SYMBOL> <QTY>                 — Market sell
  buy  limit  <SYMBOL> <QTY> <PRICE>         — Limit buy
  sell limit  <SYMBOL> <QTY> <PRICE>         — Limit sell
  cancel <ORDER_ID>                          — Cancel an open order
  quit / exit                                — Disconnect and exit

Supported symbols: AAPL, TSLA, NVDA, MSFT, GOOG, AMZN
"""

SYMBOLS = {"AAPL", "TSLA", "NVDA", "MSFT", "GOOG", "AMZN"}


def run_cli(client: TradingClient):
    print(HELP)
    while True:
        try:
            raw = input("order> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not raw:
            continue

        parts = raw.lower().split()
        cmd = parts[0]

        if cmd in ("quit", "exit"):
            break

        if cmd == "cancel":
            if len(parts) != 2:
                print("Usage: cancel <ORDER_ID>")
                continue
            client.cancel_order(parts[1].upper())
            continue

        if cmd in ("buy", "sell") and len(parts) >= 4:
            side = OrderSide.BUY if cmd == "buy" else OrderSide.SELL
            order_type_str = parts[1]
            symbol = parts[2].upper()

            if symbol not in SYMBOLS:
                print(f"Unknown symbol: {symbol}. Supported: {', '.join(sorted(SYMBOLS))}")
                continue

            try:
                qty = float(parts[3])
            except ValueError:
                print("Quantity must be a number.")
                continue

            if order_type_str == "market":
                client.send_market_order(symbol, side, qty)

            elif order_type_str == "limit":
                if len(parts) < 5:
                    print("Limit orders require a price: buy limit <SYM> <QTY> <PRICE>")
                    continue
                try:
                    price = float(parts[4])
                except ValueError:
                    print("Price must be a number.")
                    continue
                client.send_limit_order(symbol, side, qty, price)
            else:
                print(f"Unknown order type: {order_type_str}. Use 'market' or 'limit'.")
            continue

        print("Unrecognised command. Type 'quit' to exit or see usage above.")
        print(HELP)

    client.disconnect()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Trading Client")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--demo", action="store_true",
                   help="Run automated demo orders instead of interactive mode")
    args = p.parse_args()

    client = TradingClient(args.host, args.port)
    if not client.connect():
        sys.exit(1)

    if args.demo:
        print("\n=== DEMO MODE — sending sample orders ===\n")
        time.sleep(0.3)

        # Market orders (should fill immediately)
        client.send_market_order("AAPL", OrderSide.BUY, 100)
        time.sleep(0.3)
        client.send_market_order("TSLA", OrderSide.SELL, 50)
        time.sleep(0.3)

        # Limit order that should fill (price generous)
        oid = client.send_limit_order("NVDA", OrderSide.BUY, 10, 900.00)
        time.sleep(0.3)

        # Limit order that likely won't fill immediately (price tight)
        oid2 = client.send_limit_order("MSFT", OrderSide.BUY, 5, 100.00)
        time.sleep(0.3)

        # Cancel the unfilled limit order
        client.cancel_order(oid2)

        # Bad symbol to demo rejection
        client.send_market_order("FAKE", OrderSide.BUY, 1)

        time.sleep(2)
        client.disconnect()
        print("\n=== Demo complete ===")
    else:
        run_cli(client)
