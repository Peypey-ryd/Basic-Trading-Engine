# Simple Trading System

A lightweight client-server trading system written in pure Python (no dependencies).
It demonstrates a real-world order-flow architecture: a client sends orders over TCP,
and a server runs a matching engine that fills, queues, or rejects them.

---

## Features

| Feature | Detail |
|---|---|
| **Market orders** | Fill immediately at simulated market price |
| **Limit orders** | Queue until market price crosses the limit |
| **Cancel orders** | Cancel any open (unfilled) order |
| **Multi-client** | Each client gets its own thread; fills are pushed asynchronously |
| **Live price simulation** | Market prices fluctuate ±0.5% each evaluation cycle |
| **Rejection handling** | Unknown symbols are rejected with an error message |
| **Heartbeat** | Server pings idle clients; client handles silently |

---

## Key Architectural Decisions

* Used length-prefixed binary headers to ensure message integrity over TCP.

* Implemented a thread-safe producer-consumer model for outbound messages to prevent slow-client blocking.

* Built a modular MatchingEngine verified by a comprehensive pytest suite.

---

## Project Structure

```
trading_system/
├── shared/
│   └── protocol.py       # Shared message types, Order dataclass, serialization
├── server/
│   └── server.py         # TCP server + matching engine
├── client/
│   └── client.py         # TCP client + interactive CLI
├── tests/
│   └── test_engine.py    # pytest test suite (23 tests)
└── README.md
```

---

## Requirements

- Python **3.10+** (uses `float | None` union syntax)
- `pytest` for running the test suite (`pip install pytest`)
- No other third-party packages — stdlib only (`socket`, `threading`, `json`, `struct`, `dataclasses`)

---

## Quick Start

### 1. Clone

```bash
git clone https://github.com/Peypey-ryd/Basic-Trading-Engine.git
cd main
```

### 2. Start the server

```bash
python server/server.py
# Listening on 127.0.0.1:9999
```

### 3a. Interactive client

```bash
python client/client.py
```

You'll get a prompt:

```
order> buy market AAPL 100
order> sell limit TSLA 50 240.00
order> cancel <ORDER_ID>
order> quit
```

### 3b. Automated demo

```bash
python client/client.py --demo
```

Runs a scripted sequence: market buys/sells, a filling limit order,
a cancelled limit order, and a bad-symbol rejection — all in ~3 seconds.

---

## Command Reference

```
buy  market <SYMBOL> <QTY>              Market buy
sell market <SYMBOL> <QTY>              Market sell
buy  limit  <SYMBOL> <QTY> <PRICE>      Limit buy (queues if price not met)
sell limit  <SYMBOL> <QTY> <PRICE>      Limit sell
cancel <ORDER_ID>                        Cancel an open order
quit / exit                              Disconnect and exit
```

**Supported symbols:** `AAPL`, `TSLA`, `NVDA`, `MSFT`, `GOOG`, `AMZN`

---

## Running the Tests

```bash
pip install pytest
pytest tests/ -v
```

The suite covers 23 cases across three layers:

- **Protocol** — length-prefix encoding, socket roundtrip, EOF handling, all message types
- **Order validation** — positive quantity, limit price requirements, serialization roundtrip
- **Matching engine** — market/limit fills, queuing, cancellation, concurrent submissions, price-movement fills
- **Integration** — full client→server→engine→client flow over a real TCP socket

---



Both programs accept `--host` and `--port` flags:

```bash
python server/server.py --host 0.0.0.0 --port 8888
python client/client.py --host 192.168.1.10 --port 8888
```

---

## Architecture

```
Client                              Server
  |                                   |
  |-- ORDER_REQUEST (len-prefix) ---> |
  |                                   |-- MatchingEngine.submit()
  |<-- ORDER_ACK -------------------- |      |
  |<-- ORDER_UPDATE (FILLED/...) ---- |      |-- MARKET  → fill immediately
  |                                   |      |-- LIMIT   → queue or fill
  |-- CANCEL_REQUEST ---------------> |
  |<-- CANCEL_ACK ------------------- |
  |                                   |
  |           [1-second poll loop]    |
  |<-- ORDER_UPDATE (FILLED) -------- |-- pending limit orders re-evaluated
```

### Length-Prefixed Framing

Every message is prefixed with a 4-byte unsigned integer (network byte order) indicating
the length of the following JSON body. This solves TCP's stream fragmentation problem —
a single `recv()` call may return partial data, and `_recv_exactly()` loops until the
full message arrives before parsing.

### Producer-Consumer Write Queue

Each `ClientHandler` runs two threads: a **reader** that blocks on `decode_from_socket`,
and a **writer** that drains a `queue.Queue` and calls `sendall`. This decouples the
matching engine from slow clients — the engine enqueues fill notifications and moves on
immediately without waiting for a slow socket write to complete.

### Message Types

| Message | Direction | Purpose |
|---|---|---|
| `ORDER_REQUEST` | C → S | Submit a new order |
| `ORDER_ACK` | S → C | Server acknowledged receipt |
| `ORDER_UPDATE` | S → C | Status change (filled, rejected, …) |
| `CANCEL_REQUEST` | C → S | Request cancellation |
| `CANCEL_ACK` | S → C | Cancellation confirmed |
| `ERROR` | S → C | Server-side error |
| `HEARTBEAT` | both | Keep-alive ping |

All messages are newline-delimited JSON over a plain TCP socket.

---

## Sample Output

**Server:**
```
02:21:26 [SERVER] INFO Listening on 127.0.0.1:9999
02:21:27 [SERVER] INFO Client connected: 127.0.0.1:16383
02:21:28 [SERVER] INFO FILL  06341451 BUY AAPL 100 @ 189.18
02:21:28 [SERVER] INFO FILL  317AC891 SELL TSLA 50 @ 244.16
02:21:28 [SERVER] INFO Queued LIMIT order 789C866B — market=415.23, limit=100.00
02:21:29 [SERVER] WARNING Rejected order 6335C040 — unknown symbol FAKE
```

**Client:**
```
02:21:28 [CLIENT] INFO Sent MARKET BUY AAPL x100  [06341451]
02:21:28 [CLIENT] INFO   ✓ ACK    order_id=06341451  status=PENDING
02:21:28 [CLIENT] INFO   ✅ FILLED  BUY AAPL x100 @ 189.18  [06341451]
02:21:29 [CLIENT] INFO   🚫 CANCELLED  order_id=789C866B
02:21:29 [CLIENT] WARNING   ❌ REJECT  BUY FAKE x1  [6335C040]
```
