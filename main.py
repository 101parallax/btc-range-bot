"""btc-range-bot: a range trader for cbBTC/USDC on make.fun's bars-replay/v1.

Strategy (original, built for a range-bound 1-minute market):
  * Keep a rolling 8-hour (480-bar) band of closes: its low and its high.
  * BUY a fixed $600 notional when the latest close sits within 50 bps of
    the band low AND ticks up from the previous close (a bounce off the low,
    not a knife catch). One position at a time, long only.
  * SELL the whole position when the close reaches within 10 bps of the
    band high, or at +150 bps take-profit, or at -80 bps stop-loss, or when
    the price gives back 120 bps from its peak since entry (trailing stop).
  * Safety rails: 10-bar cooldown after any sell, no new entries after the
    1380th scored bar (avoids end-of-window flatten friction), and a 3%
    peak-to-trough equity drawdown circuit breaker that halts new entries.
    Warm-up bars (when the hello carries them) seed the band's history but
    never carry orders, so the bot can trade from the first scored bar.
  * Every decision is integer math on the bar's own prices (price_scale 5);
    no network, no clock, no files, no unseeded randomness, so two runs on
    the same bars emit the same orders byte for byte.

Wire notes: the harness sends `hello` once, then one `batch` per minute
(1440 bars), then `done`. Each `batch` carries the bar that just closed, the
fills of the previous batch's orders and the account; the bot answers every
`batch` with exactly one `reply` echoing its `seq`. An order decided on the
`batch` of bar k fills at the open of bar k+1, so every entry/exit signal is
judged on closes, never on the open it cannot know yet.
"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- strategy parameters (bps unless noted) -------------------------------
BAND_BARS = 480        # rolling window for the range band (8 hours)
BUY_NEAR_LOW_BPS = 50  # buy when close is within this of the band low
SELL_NEAR_HIGH_BPS = 10  # sell when close is within this of the band high
TAKE_PROFIT_BPS = 150
STOP_LOSS_BPS = 80
TRAIL_BPS = 120        # give-back from the peak since entry that exits
SIZE_QUOTE_UNITS = 600_000_000  # $600 per trade (USDC micro units)
MIN_TRADE_QUOTE_UNITS = 10_000_000  # $10: below this, do not bother trading
COOLDOWN_BARS = 10    # bars to wait after a sell before re-entering
ENTRY_STOP_BAR = 1380  # stop opening after this many scored bars
HALT_DD_BPS = 300      # 3% equity drawdown from peak halts new entries
MAX_SLIPPAGE_BPS = 100
BPS = 10_000


class RangeBot:
    """Pure strategy state. Fed one bar at a time; returns 'buy', 'sell' or None.

    All prices are ints at the symbol's price_scale (5: /1e5 = USD). Equity,
    cash and costs are ints in quote units (scale 6: /1e6 = USD).
    """

    def __init__(self, price_scale, qty_scale, quote_scale):
        self.price_scale = price_scale
        self.qty_scale = qty_scale
        self.quote_scale = quote_scale
        self.closes = deque(maxlen=BAND_BARS)
        self.scored_bars = 0
        # position tracking (set from the account the harness reports)
        self.entry_price = None  # fill price of the open position, int
        self.peak_close = None   # highest close since entry, int
        self.last_sell_bar = -10**9
        self.peak_equity = None
        self.halted = False
        self.pending_buy = False
        self.pending_sell = False

    def _entry_price_from_cost(self, qty, cost):
        """Recover the position's average fill price from its cost.

        value = qty * price * 10**quote_scale / 10**(qty_scale+price_scale)
        so price = cost * 10**(qty_scale+price_scale) / (qty * 10**quote_scale).
        """
        if qty <= 0 or cost <= 0:
            return None
        return (cost * 10 ** (self.qty_scale + self.price_scale)
                // (qty * 10 ** self.quote_scale))

    def observe(self, close, cash, equity, qty, cost, scored=True):
        """Fold one batch's bar + account in.

        `scored` is False for warm-up bars: they still seed the band's
        history (that is what they are for), but they do not count toward
        the entry-stop bar count, which runs on scored bars only.
        """
        if scored:
            self.scored_bars += 1
        if self.peak_equity is None:
            self.peak_equity = equity
        elif equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity > 0:
            dd_bps = (self.peak_equity - equity) * BPS // self.peak_equity
            if dd_bps >= HALT_DD_BPS:
                self.halted = True
        self.closes.append(close)
        if qty > 0:
            self.pending_buy = False
            if self.entry_price is None:
                self.entry_price = self._entry_price_from_cost(qty, cost)
                self.peak_close = close
            elif self.peak_close is None or close > self.peak_close:
                self.peak_close = close
        else:
            self.entry_price = None
            self.peak_close = None
            self.pending_sell = False

    def decide(self, cash, qty):
        """The order action for this bar: 'buy', 'sell' or None."""
        if len(self.closes) < BAND_BARS:
            return None
        close = self.closes[-1]
        if qty > 0:
            if self.pending_sell:
                return None
            entry = self.entry_price
            if entry is None or entry <= 0:
                return None
            gain_bps = (close - entry) * BPS // entry
            peak = self.peak_close if self.peak_close is not None else close
            peak_bps = (peak - entry) * BPS // entry
            band_high = max(self.closes)
            near_high = close * BPS >= band_high * (BPS - SELL_NEAR_HIGH_BPS)
            if (near_high or gain_bps >= TAKE_PROFIT_BPS
                    or gain_bps <= -STOP_LOSS_BPS
                    or peak_bps - gain_bps >= TRAIL_BPS):
                return "sell"
            return None
        # flat: consider an entry
        if self.pending_buy or self.halted:
            return None
        if self.scored_bars > ENTRY_STOP_BAR:
            return None
        if self.scored_bars - self.last_sell_bar <= COOLDOWN_BARS:
            return None
        band_low = min(self.closes)
        near_low = close * BPS <= band_low * (BPS + BUY_NEAR_LOW_BPS)
        bouncing = len(self.closes) >= 2 and close > self.closes[-2]
        if near_low and bouncing and cash >= MIN_TRADE_QUOTE_UNITS:
            return "buy"
        return None


def make_on_batch(bot):
    def on_batch(state, batch):
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
                break
        if bar is None:
            return []
        close = int(bar["c"])
        holding = state.positions.get(SYMBOL)
        qty = holding.qty if holding else 0
        cost = holding.cost if holding else 0
        # Warm-up bars seed the band's history but never carry orders
        # (the harness rejects them); scored bars do both.
        bot.observe(close, state.cash, state.equity, qty, cost,
                    scored=not state.warming_up)
        if state.warming_up:
            return []
        action = bot.decide(state.cash, qty)
        if action == "buy":
            bot.pending_buy = True
            size = min(SIZE_QUOTE_UNITS, state.cash)
            return [{"side": "buy", "symbol": SYMBOL,
                     "notional_quote_units": size,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        if action == "sell" and qty > 0:
            bot.pending_sell = True
            bot.last_sell_bar = bot.scored_bars
            return [{"side": "sell", "symbol": SYMBOL, "qty": qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []
    return on_batch


def main():
    holder = {}

    def on_hello(state):
        sym = state.symbols[SYMBOL]
        holder["bot"] = RangeBot(sym["price_scale"], sym["qty_scale"],
                                 state.hello["capital"]["quote_scale"])
        holder["on_batch"] = make_on_batch(holder["bot"])

    def dispatch(state, batch):
        # The shim always delivers hello before the first batch.
        return holder["on_batch"](state, batch)

    makefun_bars.run(dispatch, on_hello=on_hello)


if __name__ == "__main__":
    main()
