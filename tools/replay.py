"""Local replay harness for btc-range-bot.

Drives the exact RangeBot class from main.py over bar feeds, applying the
same fill model the harness uses (fee 4 bps, slippage 2 bps, constant-product
impact 1 + a/D against the fill bar's open depth, fills at the next bar's
open, flatten at the last close). Used to check the bot on slices of the dev
window and on synthetic series (flat, trending, choppy) where the official
grade-local cannot run. Numbers here are float approximations; the exact dev
score comes from `node cli/makefun.mjs grade-local`.
"""
import gzip
import importlib.util
import json
import os
import random
import sys

KIT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "..", "kit")
KIT = os.path.normpath(KIT)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FEE_BPS = 4
SLIP_BPS = 2
BPS = 10_000


def load_main():
    spec = importlib.util.spec_from_file_location("bot_main",
                                                  os.path.join(ROOT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_dev_bars():
    bars = []
    with gzip.open(os.path.join(KIT, "dataset", "bars", "CBBTC.ndjson.gz"),
                   "rt") as f:
        for line in f:
            b = json.loads(line)
            # The dataset stores prices/depths as decimal strings; the bot
            # works in ints, so normalise here (synthetic bars are ints).
            bars.append({"o": int(b["o"]), "c": int(b["c"]),
                         "o_depth": int(b["o_depth"][0]),
                         "c_depth": int(b["c_depth"][0])})
    return bars


def synth_bars(kind, n=1440, start=64_500_0000):
    """Synthetic 1-minute bars: dicts with int o/c (price_scale 5) and depth."""
    rng = random.Random(20261117)
    bars = []
    price = float(start)
    for _ in range(n):
        if kind == "flat":
            pass
        elif kind == "down":
            price *= 1 - 0.02 / n  # -2% over the window
        elif kind == "up":
            price *= 1 + 0.02 / n  # +2% over the window
        elif kind == "choppy":
            price *= 1 + rng.gauss(0, 0.0006)
        p = int(round(price))
        bars.append({"o": p, "c": p, "o_depth": 850_000_000_000,
                     "c_depth": 850_000_000_000})
    return bars


def run_feed(mod, bars, label):
    """Replay RangeBot over a list of bar dicts. Returns metrics dict."""
    bot = mod.RangeBot(5, 8, 6)
    cash = 1_000_000_000  # micro-USDC
    qty = 0               # qty_scale 8
    cost = 0              # quote units spent on the position
    trades = 0
    peak = cash
    max_dd = 0.0
    pending = None  # ("buy", notional) or ("sell", qty) decided last bar

    def equity(i):
        return cash + (qty * bars[i]["c"] * 10 ** 6
                       // 10 ** (8 + 5) if qty else 0)

    n = len(bars)
    for k in range(n):
        # settle the pending order at this bar's open (fill of last decision)
        if pending is not None:
            side = pending[0]
            depth = bars[k]["o_depth"]
            op = bars[k]["o"]
            if side == "buy":
                notional = pending[1]
                spend = notional * (BPS - FEE_BPS) // BPS
                # impact factor M = 1 + spend_usd/depth_usd (float is fine here)
                m = 1.0 + (spend / 1e6) / (depth / 1e6)
                price = op * (BPS + SLIP_BPS) / BPS * m
                q = int(spend * 10 ** (8 + 5) // (price * 10 ** 6))
                cash -= notional
                qty += q
                cost += notional
                trades += 1
            else:
                q = pending[1]
                value = q * op * 10 ** 6 // 10 ** (8 + 5)
                m = 1.0 + (value / 1e6) / (depth / 1e6)
                price = max(1.0, op * (BPS - SLIP_BPS) / BPS / m)
                gross = q * price * 10 ** 6 // 10 ** (8 + 5)
                proceeds = gross * (BPS - FEE_BPS) // BPS
                cash += proceeds
                qty = 0
                cost = 0
                trades += 1
            pending = None
        eq = equity(k)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)
        if k == n - 1:
            break
        bot.observe(bars[k]["c"], cash, eq, qty, cost)
        action = bot.decide(cash, qty)
        if action == "buy" and not bot.pending_buy:
            bot.pending_buy = True
            pending = ("buy", min(mod.SIZE_QUOTE_UNITS, cash))
        elif action == "sell" and qty > 0:
            bot.pending_sell = True
            bot.last_sell_bar = bot.scored_bars
            pending = ("sell", qty)
    # flatten at the last close
    if qty > 0:
        depth = bars[-1]["c_depth"]
        cp = bars[-1]["c"]
        value = qty * cp * 10 ** 6 // 10 ** (8 + 5)
        m = 1.0 + (value / 1e6) / (depth / 1e6)
        price = max(1.0, cp * (BPS - SLIP_BPS) / BPS / m)
        gross = qty * price * 10 ** 6 // 10 ** (8 + 5)
        cash += gross * (BPS - FEE_BPS) // BPS
        qty = 0
    pnl_usd = (cash - 1_000_000_000) / 1e6
    return {"label": label, "pnl_usd": round(pnl_usd, 2), "trades": trades,
            "max_dd": round(max_dd, 4)}


def main():
    mod = load_main()
    dev = load_dev_bars()
    tests = [
        ("dev-full", dev),
        ("dev-first-half", dev[:720]),
        ("dev-second-half", dev[720:]),
        ("synth-flat", synth_bars("flat")),
        ("synth-down-2pct", synth_bars("down")),
        ("synth-up-2pct", synth_bars("up")),
        ("synth-choppy", synth_bars("choppy")),
    ]
    for label, bars in tests:
        r = run_feed(mod, bars, label)
        print("%-18s pnl %+.2f USD  trades %2d  max_dd %.4f"
              % (r["label"], r["pnl_usd"], r["trades"], r["max_dd"]))


if __name__ == "__main__":
    main()
