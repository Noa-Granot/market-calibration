## Data acquisition notes

Polymarket's API is public but thinly documented. Three constraints found
by testing rather than from the docs:

- **`fidelity` is effectively mandatory.** `prices-history` returns an empty
  list rather than an error when it is omitted, which initially looked like
  the data being unavailable. `fidelity` is bucket width in minutes;
  1440 gives one point per day.
- **History starts April 2023.** Sampling by quarter, 2023 Q1 returned 0/6
  markets with history and Q2 returned 6/6. The break is clean, and matches
  Polymarket's move from an AMM to a CLOB order book.
- **Gamma caps `offset` at 2000.** Deeper pagination requires the
  `/markets/keyset` endpoint. The current dataset works within that ceiling.

Of 2,100 markets in the feed, 1,301 remained after a $20k volume floor and a
30-day minimum duration (which excludes auto-generated sports micro-markets).
1,300 of those returned price history.