"""Wire-level local grader for btc-range-bot.

Replicates the kit harness (sim/bars/engine.ts) exactly, in integer math:
hello once, one batch per bar, fills of the previous batch's orders at the
next bar's open (fee 4 bps, slippage 2 bps, constant-product impact 1+a/D
against the fill bar's open depth, walking pool per bar), flatten at the last
close. Runs the bot as `python3 -P -s main.py` with PYTHONHASHSEED=0, twice,
and compares the two reply streams byte for byte (the determinism check the
official grade-local performs inside Docker, which is unavailable here).
Prints a JSON summary: net PnL, trades, max drawdown, fees, and the reply
stream sha256 of each run.
"""
import gzip
import hashlib
import json
import os
import subprocess
import sys

KIT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "kit"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BPS = 10_000


# ------------------------------------------------------------------ dataset
def load_bars():
    bars = []
    with gzip.open(os.path.join(KIT, "dataset", "bars", "CBBTC.ndjson.gz"),
                   "rt") as f:
        for line in f:
            b = json.loads(line)
            bars.append({
                "symbol": "CBBTC", "t": int(b["t"]),
                "o": int(b["o"]), "h": int(b["h"]), "l": int(b["l"]),
                "c": int(b["c"]), "v": None,
                "o_depth": [int(d) for d in b["o_depth"]],
                "c_depth": [int(d) for d in b["c_depth"]],
            })
    return bars


# ------------------------------------------------------------- fill models
def buy_fill(notional, open_px, depth, qty_scale, price_scale, quote_scale):
    """(price, qty, fee) of a buy; single-hop pool, integer math (engine.ts)."""
    spend = notional * (BPS - 4) // BPS
    # M = 1 + spend/depth as a reduced fraction: num/den = (D+spend)/D.
    m_num, m_den = depth + spend, depth
    price = open_px * (BPS + 2) * m_num // (BPS * m_den)
    qty = spend * 10 ** (qty_scale + price_scale) // (price * 10 ** quote_scale)
    return price, qty, notional - spend


def sell_fill(qty, open_px, depth, qty_scale, price_scale, quote_scale):
    """(price, proceeds, fee) of a sell; single-hop pool, integer math."""
    value = qty * open_px * 10 ** quote_scale // 10 ** (qty_scale + price_scale)
    m_num, m_den = depth + value, depth
    slipped = open_px * (BPS - 2) * m_den // (BPS * m_num)
    price = slipped if slipped >= 1 else 1
    gross = qty * price * 10 ** quote_scale // 10 ** (qty_scale + price_scale)
    proceeds = gross * (BPS - 4) // BPS
    return price, proceeds, gross - proceeds


def value_of(qty, price, qty_scale, price_scale, quote_scale):
    return qty * price * 10 ** quote_scale // 10 ** (qty_scale + price_scale)


# ------------------------------------------------------------------- engine
class Engine:
    def __init__(self, bars):
        self.bars = {b["t"]: b for b in bars}
        self.first_close = bars[0]["t"]
        self.step = 60_000
        self.n = len(bars)
        self.cash = 1_000_000_000
        self.qty = 0
        self.cost = 0
        self.close = None
        self.close_depth = []
        self.trades = 0
        self.fees = 0
        self.pending = []       # decided orders awaiting their fill bar
        self.undelivered = []   # fills to deliver in the next batch
        self.used_ids = set()
        self.equity_series = [self.cash]
        self.seq = 0
        self.qs, self.ps, self.qty_s = 6, 5, 8

    def equity_now(self):
        e = self.cash
        if self.qty > 0:
            e += value_of(self.qty, self.close, self.qty_s, self.ps, self.qs)
        return e

    def account(self):
        pos = []
        if self.qty > 0:
            pos.append({"symbol": "CBBTC", "qty": str(self.qty),
                        "cost_quote_units": str(self.cost)})
        return {"cash_quote_units": str(self.cash), "positions": pos,
                "equity_quote_units": str(self.equity_now())}

    def decide(self, reply):
        """Decision checks of one reply, in the engine's order (engine.ts)."""
        equity = self.equity_now()
        is_last = self.seq == self.n - 1
        buys = 0
        open_syms = {"CBBTC"} if self.qty > 0 else set()
        for index, order in enumerate(reply["orders"]):
            side = order["side"]
            amount = int(order["notional_quote_units"]) if side == "buy" \
                else int(order["qty"])
            reject = None
            if index >= 16:
                reject = "max_orders_per_bar"
            elif order["client_id"] in self.used_ids:
                reject = "duplicate_client_id"
            elif amount == 0:
                reject = "zero_amount"
            elif is_last:
                reject = "after_end"
            elif self.close is None:
                reject = "no_bar"
            elif side == "buy":
                cash_left = self.cash - buys
                held_value = value_of(self.qty, self.close, self.qty_s,
                                      self.ps, self.qs)
                if amount < 1_000_000:
                    reject = "below_min_notional"
                elif amount > cash_left:
                    reject = "insufficient_cash"
                elif ("CBBTC" not in open_syms and len(open_syms) >= 1):
                    reject = "max_open_positions"
                elif (held_value + amount) * BPS > 10_000 * equity:
                    reject = "max_weight"
                if reject is None:
                    buys += amount
                    open_syms.add("CBBTC")
            else:
                available = self.qty
                if amount == 0 or amount > available:
                    reject = "insufficient_position"
                elif (amount != available
                      and value_of(amount, self.close, self.qty_s, self.ps,
                                   self.qs) < 1_000_000):
                    reject = "below_min_notional"
            self.used_ids.add(order["client_id"])
            self.pending.append({"order": order, "seq": self.seq,
                                 "index": index,
                                 "decided_bar": self.first_close + self.seq * self.step,
                                 "fill_bar": self.first_close + (self.seq + 1) * self.step,
                                 "reject": reject, "amount": amount,
                                 "close": self.close})

    def _fill_record(self, p, status, reason, price, qty, notional, fee):
        rec = {"client_id": p["order"]["client_id"], "symbol": "CBBTC",
               "side": p["order"]["side"], "status": status, "reason": reason,
               "decided_bar": p["decided_bar"], "fill_bar": p["fill_bar"],
               "price": str(price), "qty": str(qty),
               "notional_quote_units": str(notional),
               "fee_quote_units": str(fee)}
        self.undelivered.append(rec)

    def resolve(self, bar):
        """Resolve pending orders at this bar's open (walking pool per bar)."""
        pool_used = False
        for p in self.pending:
            if p["reject"] is not None:
                self._fill_record(p, "rejected", p["reject"], 0, 0, 0, 0)
                continue
            if bar is None:
                self._fill_record(p, "rejected", "no_bar", 0, 0, 0, 0)
                continue
            depth = bar["o_depth"][0]
            if p["order"]["side"] == "buy":
                price, q, fee = buy_fill(p["amount"], bar["o"], depth,
                                         self.qty_s, self.ps, self.qs)
                if price * BPS > p["close"] * (BPS + int(p["order"]["max_slippage_bps"])):
                    self._fill_record(p, "rejected", "max_slippage", 0, 0, 0, 0)
                    continue
                if q == 0:
                    self._fill_record(p, "rejected", "zero_amount", 0, 0, 0, 0)
                    continue
                self.cash -= p["amount"]
                self.qty += q
                self.cost += p["amount"]
                self.trades += 1
                self.fees += fee
                self._fill_record(p, "filled", None, price, q, p["amount"], fee)
            else:
                price, proceeds, fee = sell_fill(p["amount"], bar["o"], depth,
                                                 self.qty_s, self.ps, self.qs)
                if price * BPS < p["close"] * (BPS - int(p["order"]["max_slippage_bps"])):
                    self._fill_record(p, "rejected", "max_slippage", 0, 0, 0, 0)
                    continue
                self.cash += proceeds
                self.cost = self.cost * (self.qty - p["amount"]) // self.qty
                self.qty -= p["amount"]
                self.trades += 1
                self.fees += fee
                self._fill_record(p, "filled", None, price, p["amount"],
                                  proceeds, fee)
            pool_used = True
        self.pending = []

    def step_to(self, k):
        t = self.first_close + k * self.step
        bar = self.bars.get(t)
        self.resolve(bar)
        if bar is not None:
            self.close = bar["c"]
            self.close_depth = bar["c_depth"]
        fills = self.undelivered
        self.undelivered = []
        account = self.account()
        self.equity_series.append(account["equity_quote_units"])
        return {"type": "batch", "seq": k, "t": t,
                "bars": [bar] if bar is not None else [],
                "fills": fills, "account": account}

    def finish(self):
        for p in self.pending:
            assert p["reject"] is not None, "last-batch order passed checks"
        self.pending = []
        if self.qty > 0:
            price, proceeds, fee = sell_fill(self.qty, self.close,
                                             self.close_depth[0],
                                             self.qty_s, self.ps, self.qs)
            self.cash += proceeds
            self.fees += fee
            self.qty = 0
            self.cost = 0
        self.equity_series.append(self.cash)
        return {"type": "done", "seq": self.n, "reason": "end_of_data",
                "account": self.account()}


def run_bot(bars):
    """Run main.py once over the bars; return (replies, metrics)."""
    hello = {"type": "hello", "protocol": "agent-fun/bars-replay/v1",
             "seed": "0",
             "symbols": [{"symbol": "CBBTC",
                          "mint": "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij",
                          "price_scale": 5, "qty_scale": 8}],
             "capital": {"quote": "USDC", "quote_scale": 6,
                         "quote_units": "1000000000"},
             "bar_interval": "1m", "first_close_ms": bars[0]["t"],
             "caps": {"max_open_positions": 1, "max_orders_per_bar": 16,
                      "max_weight_bps": 10000, "shorts": False,
                      "leverage": False},
             "fees": {"fee_bps": 4, "slippage_bps": 2,
                      "min_notional_quote_units": "1000000"},
             "latency_bars": 1}
    eng = Engine(bars)
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    proc = subprocess.Popen(
        [sys.executable, "-P", "-s", os.path.join(ROOT, "main.py")],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT)
    replies = []
    proc.stdin.write(json.dumps(hello) + "\n")
    proc.stdin.flush()
    for k in range(eng.n):
        batch = eng.step_to(k)
        proc.stdin.write(json.dumps(batch) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"bot exited early at batch {k}: "
                               + proc.stderr.read()[-2000:])
        replies.append(line)
        reply = json.loads(line)
        assert reply["type"] == "reply" and reply["seq"] == k, \
            f"bad reply at batch {k}: {line[:200]}"
        assert len(reply["orders"]) <= 16, "over 16 orders"
        eng.decide(reply)
        eng.seq += 1
    done = eng.finish()
    proc.stdin.write(json.dumps(done) + "\n")
    proc.stdin.flush()
    proc.stdin.close()
    rc = proc.wait(timeout=60)
    err = proc.stderr.read()
    if rc != 0:
        raise RuntimeError(f"bot exited {rc}: {err[-2000:]}")
    if err.strip():
        print("bot stderr:", err[-1000:], file=sys.stderr)
    peak = 0
    max_dd = 0.0
    for e in eng.equity_series:
        e = int(e)
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak)
    metrics = {"net_pnl_quote_units": eng.cash - 1_000_000_000,
               "net_pnl_usd": (eng.cash - 1_000_000_000) / 1e6,
               "trades": eng.trades, "fees_quote_units": eng.fees,
               "max_drawdown": max_dd}
    return replies, metrics


def main():
    bars = load_bars()
    r1, m1 = run_bot(bars)
    r2, m2 = run_bot(bars)
    h1 = hashlib.sha256("".join(r1).encode()).hexdigest()
    h2 = hashlib.sha256("".join(r2).encode()).hexdigest()
    print(json.dumps({
        "runs": 2, "reproduced": r1 == r2,
        "action_log_sha256_run1": h1, "action_log_sha256_run2": h2,
        "metrics_run1": m1, "metrics_run2": m2,
        "guardrails": {"min_trades_1": m1["trades"] >= 1,
                       "max_drawdown_le_25pct": m1["max_drawdown"] <= 0.25},
    }, indent=2))


if __name__ == "__main__":
    main()
