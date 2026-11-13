# Insider Tracker

Search any US-listed stock and see when its directors and executives recently
bought or sold shares: who traded, their role, the trade date, when it was
reported, the number of shares and the dollar value.

The data comes straight from SEC Form 4 filings, which insiders must file
within two business days of a trade. It's free and needs no API key.

## Put it online (free, about 10 minutes)

1. Sign in to [render.com](https://render.com) with **Sign in with GitHub**.
2. In Render, click **New → Blueprint**, pick this repository, and click **Connect**.
3. When asked for `SEC_USER_AGENT`, enter your name and email, for example
   `Insider Tracker you@gmail.com`. The SEC requires every program that
   downloads its data to identify itself; this is sent only to sec.gov.
4. Click **Deploy Blueprint**. After a few minutes Render shows your site's
   address (something like `https://insider-tracker-xxxx.onrender.com`).
   Share that link.

The free plan goes to sleep after 15 minutes without visitors, so the first
search after a break can take up to a minute. Later searches are quick.

## Run it on your own computer instead

```
python insider_tracker.py          # opens the search page in your browser
python insider_tracker.py AAPL     # prints a table in the terminal
```

## What you'll see

- **Summary**: a few plain-English lines on who bought or sold, how much was
  pre-planned, and the biggest sale relative to someone's holdings.
- **Bought / Sold on the market**: trades where an insider chose to buy or sell
  at market price with their own money. These say the most.
- **Activity over time**: buys and sales by week or month.
- **Who's trading**: one line per insider with totals and their last trade.
- **All transactions**: use the buttons to show buys, sales, awards & option
  exercises, tax withholding, or gifts. **% of holdings** shows how much of
  that person's shares a trade represents.
- **Board directors / Other insiders**: tick one or both. Other insiders are
  executives and 10% owners who are not on the board.
- **10b5-1**: the trade was scheduled in advance under a trading plan.
- **Copy link** shares the exact search; **Download CSV** gives a spreadsheet.

Not investment advice.
