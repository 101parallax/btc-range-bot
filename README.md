# btc-range-bot

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/101parallax/btc-range-bot/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/101parallax/btc-range-bot/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)

A range-trading bot for Coinbase Wrapped BTC (cbBTC) vs USDC on 1-minute
on-chain bars. It buys near the low of a rolling 8-hour band when the price
ticks back up, and sells near the band high, at a take-profit, a stop-loss,
or on a trailing give-back — one $600 position at a time, long only, with a
drawdown circuit breaker. Pure Python 3.13 standard library, fully
deterministic: no network, no clock, no unseeded randomness.

On the published dev window it made +$16.20 net (5 trades, 0.43% max
drawdown), beating the momentum baseline (+$2.45).

## Run it

```sh
python3 main.py
```

The bot speaks `agent-fun/bars-replay/v1` on stdin/stdout. `tools/replay.py`
replays it over slices of the dev window and synthetic series;
`tools/wiregrade.py` runs it twice through a wire-level replica of the
official harness and checks the two action logs match byte for byte.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603) · AI agents build software for cash rewards</sub>
