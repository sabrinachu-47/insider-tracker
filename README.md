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

## What the columns mean

- **Open-market buy / sale**: the insider bought or sold on the stock market
  with their own money. These are the trades that say most about their view.
- **10b5-1**: the trade was scheduled in advance under a trading plan.
- Untick **Real buys/sales only** to also see stock awards, option exercises
  and shares withheld for taxes.

Not investment advice.
