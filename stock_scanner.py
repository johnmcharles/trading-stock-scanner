import os
import re
import io
import csv
import time
import json
import smtplib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, date
from collections import Counter, defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import gspread
from google.oauth2.service_account import Credentials

# ── Keys pulled from GitHub Secrets ──────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ['ANTHROPIC_API_KEY']
GMAIL_ADDRESS      = os.environ['GMAIL_ADDRESS']
GMAIL_APP_PASSWORD = os.environ['GMAIL_APP_PASSWORD']
EMAIL_RECIPIENT    = os.environ['EMAIL_RECIPIENT']
SHEETS_ID          = os.environ['SHEETS_ID']
SHEETS_CREDENTIALS = os.environ['SHEETS_CREDENTIALS']

# Optional: when RESEND_API_KEY is set as a secret, mail is sent through
# Resend from a Maineblock address instead of Gmail SMTP, so the briefing
# never touches the Gmail account at all. EMAIL_FROM can override the sender.
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
EMAIL_FROM     = os.environ.get('EMAIL_FROM', 'Maineblock Briefing <hello@maineblock.com>')

# Set SCANNER_DRY_RUN=1 to build the report without emailing or logging it.
DRY_RUN = os.environ.get('SCANNER_DRY_RUN', '') == '1'

# ── Subreddits to monitor via RSS ─────────────────────────────────────────────
SUBREDDITS = [
    'wallstreetbets', 'options', 'stocks',
    'pennystocks', 'Daytrading', 'investing'
]

# ── Indexes, ETFs, and non-stock tickers to exclude ───────────────────────────
EXCLUDE = {
    # Common noise words
    'A','I','AM','PM','THE','FOR','ARE','ALL','NEW','NOW','GET','BIG','CAN',
    'CEO','ETF','EPS','IPO','GDP','USA','USD','ATH','IMO','EOD','WSB','DD',
    'OP','US','UK','EU','IT','IS','BE','OR','ON','SO','NO','DO','GO','AI',
    'TO','IF','IN','OF','AT','BY','UP','OH','OK','MY','WE','HE','ME','AN',
    'AS','BUT','NOT','YOU','HIS','HER','HAD','HAS','WAS','TOO','ANY','ITS',
    'FROM','THEY','WHAT','WHEN','WILL','WITH','BEEN','HAVE','WERE','SAID',
    'SHE','HIM','WHO','OWN','OUT','DAY','WAY','MAY','DID','LET','PUT','SET',
    'USE','FED','SEC','TAX','OIL','GAS','CAR','EV','PE','VC','YTD','YOY',
    'TTM','FCF','PO','RH','ML','DL','TBH','YOLO','FOMO','TLDR','EDIT',
    'LMAO','HODL','BUY','SELL','HOLD','CALL','PUTS','LOSS','GAIN','MOON',
    'BEAR','BULL','FUND','CASH','DEBT','RISK','HIGH','LOW','MID','CAP',
    'BID','ASK','IV','OTM','ITM','ATM','DTE','PNL','FOMC','CPI','PCE',
    'NFP','OPEX','RATE','BOND','NOTE','BILL','REPO','ROI','NET','NEXT',
    'LAST','JUST','LIKE','GOOD','REAL','SOME','MAKE','MUCH','EVEN',
    'ALSO','BACK','INTO','MORE','THAN','THEN','THEM','TIME','VERY','YOUR',
    'LONG','PLAY','WEEK','YEAR','OVER','WANT','NEED','TAKE','OPEN','ONLY',
    'SAYS','SAID','SHOW','LOOK','FEEL','TOLD','WENT','COME','CAME','KEEP',
    'PULL','PUSH','MOVE','RISE','FALL','DROP','PUMP','DUMP','TANK','RIPS',
    'DOWN','HITS','TOPS','ADDS','CUTS','SEES','SETS','WINS',
    # Major indexes and ETFs to explicitly exclude
    'SPY','QQQ','IWM','DIA','VIX','SPX','NDX','RUT','DOW','VOO','VTI',
    'GLD','SLV','TLT','HYG','LQD','XLF','XLE','XLK','XLV','XLU','XLI',
    'XLB','XLP','XLY','XLRE','SMH','ARKK','ARKG','ARKW','ARKF','ARKQ',
    'SQQQ','SPXU','TQQQ','SPXL','UVXY','SVXY','VXX','VIXY','EEM','EFA',
    'AGG','BND','IEMG','IAU','USO','UNG','BITO','BITI','GOVT','TIPS',
    'JEPI','SCHD','SPDW','INDA','FXI','MCHI','KWEB','RSX','GDX','GDXJ'
}

# ── Bullish and bearish signal words ──────────────────────────────────────────
BULLISH_WORDS = {
    'calls','call','moon','bullish','squeeze','breakout','buy','long',
    'rocket','rally','pumping','yolo','tendies','green','up','rip',
    'mooning','undervalued','catalyst','beats','beat','surge','soar'
}
BEARISH_WORDS = {
    'puts','put','bearish','crash','dump','short','overvalued','bubble',
    'sell','red','drop','falling','tank','fraud','miss','misses','down',
    'downgrade','warning','risk','weak','slow','decline','cut'
}

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}
SEC_HEADERS = {
    'User-Agent': 'Maineblock morning scanner (admin@maineblock.com)'
}

# ══════════════════════════════════════════════════════════════════════════════
# Real-symbol universe: the validation gate that keeps fake tickers out.
# Built each run from the official exchange listings (Nasdaq Trader symbol
# directory) with the SEC's listed-companies file as a second source. A
# candidate symbol must resolve against this universe, or, if it arrived as a
# deliberate $cashtag, pass a live Yahoo price check before it can appear.
# ══════════════════════════════════════════════════════════════════════════════

def _symbol_key(symbol):
    """Lookup key: class-share separators removed (BRK/B, BRK.B, BRK-B -> BRKB)."""
    return re.sub(r'[^A-Z0-9]', '', symbol.upper())

def _display_symbol(symbol):
    return symbol.upper().replace('/', '.').replace('-', '.')

def _yahoo_symbol(symbol):
    return symbol.upper().replace('/', '-').replace('.', '-')

def load_universe():
    """Returns {key: {'symbol', 'name', 'is_etf'}} for US-listed stocks/ETFs.
    Test issues are excluded. Sources merge; Nasdaq Trader entries win because
    they carry the ETF and test-issue flags."""
    universe = {}

    def add(symbol, name, is_etf):
        key = _symbol_key(symbol)
        if not key or key in EXCLUDE:
            return
        if key not in universe or universe[key]['is_etf'] is None:
            universe[key] = {
                'symbol': _display_symbol(symbol),
                'name': (name or '').strip(),
                'is_etf': is_etf,
            }

    # Source 1: Nasdaq Trader symbol directory (Nasdaq-listed + other-listed).
    for url, sym_col, etf_col, test_col in (
        ('https://www.nasdaqtrader.com/dynamic/symbols/nasdaqlisted.txt',
         'Symbol', 'ETF', 'Test Issue'),
        ('https://www.nasdaqtrader.com/dynamic/symbols/otherlisted.txt',
         'ACT Symbol', 'ETF', 'Test Issue'),
    ):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            rows = list(csv.DictReader(io.StringIO(r.text), delimiter='|'))
            kept = 0
            for row in rows:
                sym = (row.get(sym_col) or '').strip()
                if not sym or (row.get(test_col) or '').strip() == 'Y':
                    continue
                add(sym, row.get('Security Name', ''),
                    (row.get(etf_col) or '').strip() == 'Y')
                kept += 1
            print(f"  Universe {url.rsplit('/', 1)[-1]}: {kept} symbols")
        except Exception as e:
            print(f"  Universe source failed ({url.rsplit('/', 1)[-1]}): {e}")

    # Source 2: SEC listed companies (equities and many ETFs; no ETF flag).
    try:
        r = requests.get('https://www.sec.gov/files/company_tickers_exchange.json',
                         headers=SEC_HEADERS, timeout=25)
        r.raise_for_status()
        fields = r.json()['fields']
        ti, ni = fields.index('ticker'), fields.index('name')
        kept = 0
        for row in r.json()['data']:
            add(row[ti], row[ni], None)
            kept += 1
        print(f"  Universe SEC exchange file: {kept} rows merged")
    except Exception as e:
        print(f"  Universe SEC source failed: {e}")

    print(f"  Universe total: {len(universe)} real symbols")
    return universe

# Suffixes chatter glues onto tickers ("ASMLEARNINGS" = ASML + EARNINGS).
_GLUE_SUFFIXES = {
    'EARNINGS', 'CALLS', 'PUTS', 'CALL', 'PUT', 'STOCK', 'SHARES', 'OPTIONS',
    'LEARNINGS', 'YOLO', 'MOON', 'GAINS', 'LOSS', 'DD', 'PLAY', 'PLAYS',
    'TOMORROW', 'TODAY', 'WEEK', 'GUYS', 'NEWS', 'RUN', 'DIP', 'TOP', 'CEO',
}

def resolve_token(token, cashtag, universe):
    """Map a raw candidate token to a real symbol.
    Returns (symbol, how) with how in {'listed', 'normalized', 'unverified'},
    or None when the token cannot be tied to a real, tradable symbol.
    - listed:     the token is a real exchange-listed symbol.
    - normalized: a glued token whose prefix is a real symbol and whose tail
                  is known chatter glue (ASMLEARNINGS -> ASML).
    - unverified: not in the listings (possible coverage gap), but written as
                  a deliberate $cashtag; admitted only if Yahoo later returns
                  a live price for it. Bare words never get this path.
    """
    if not token or token in EXCLUDE:
        return None
    key = _symbol_key(token)
    if key in universe:
        return universe[key]['symbol'], 'listed'
    if len(key) >= 6:
        for plen in (5, 4, 3):
            prefix, tail = key[:plen], key[plen:]
            if prefix not in universe or len(tail) < 3 or not tail.isalpha():
                continue
            # A $cashtag is a deliberate ticker reference, so any glued tail
            # is chatter noise. A bare word must end in a known glue suffix,
            # so ordinary long words (CARPOOL -> CAR) are not misread.
            if cashtag or tail in _GLUE_SUFFIXES:
                return universe[prefix]['symbol'], 'normalized'
    if cashtag and 1 <= len(key) <= 6:
        return _display_symbol(token), 'unverified'
    return None

# ── Fetch Reddit via RSS ──────────────────────────────────────────────────────
def fetch_reddit_rss(subreddit):
    url = f"https://www.reddit.com/r/{subreddit}/hot.rss?limit=50"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            ns = {'atom': 'http://www.w3.org/2005/Atom'}
            entries = root.findall('atom:entry', ns)
            texts = []
            for entry in entries:
                title = entry.find('atom:title', ns)
                content = entry.find('atom:content', ns)
                t = title.text if title is not None else ''
                c = content.text if content is not None else ''
                texts.append(f"{t} {c}")
            print(f"  r/{subreddit}: {len(texts)} posts")
            return texts
        else:
            print(f"  r/{subreddit}: HTTP {r.status_code}")
    except Exception as e:
        print(f"  r/{subreddit} error: {e}")
    return []

# ── Fetch Yahoo Finance trending tickers ──────────────────────────────────────
def fetch_yahoo_trending(universe):
    try:
        r = requests.get(
            "https://finance.yahoo.com/trending-tickers/",
            headers=HEADERS, timeout=15
        )
        found = re.findall(r'"symbol":"([A-Z]{1,5})"', r.text)
        unique = []
        for t in dict.fromkeys(found):
            resolved = resolve_token(t, True, universe)
            if resolved:
                unique.append(resolved[0])
        unique = list(dict.fromkeys(unique))[:30]
        print(f"  Yahoo trending: {len(unique)} validated tickers")
        return unique
    except Exception as e:
        print(f"  Yahoo error: {e}")
    return []

# ── Fetch Finviz news tickers ─────────────────────────────────────────────────
def fetch_finviz(universe):
    try:
        r = requests.get("https://finviz.com/news.ashx", headers=HEADERS, timeout=15)
        raw = re.findall(r'\$([A-Z]{1,5})\b', r.text)
        tickers = []
        for t in raw:
            resolved = resolve_token(t, True, universe)
            if resolved:
                tickers.append(resolved[0])
        print(f"  Finviz: {len(tickers)} validated ticker mentions "
              f"({len(raw)} raw)")
        return tickers
    except Exception as e:
        print(f"  Finviz error: {e}")
    return []

# ── Fetch earnings calendar from Nasdaq ───────────────────────────────────────
def fetch_earnings_this_week(universe):
    try:
        today = date.today().strftime('%Y-%m-%d')
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={today}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
        tickers = []
        rows = data.get('data', {}).get('rows', []) or []
        for row in rows[:50]:
            symbol = (row.get('symbol', '') or '').strip().upper()
            resolved = resolve_token(symbol, True, universe) if symbol else None
            if resolved:
                tickers.append(resolved[0])
        print(f"  Earnings today: {tickers}")
        return tickers
    except Exception as e:
        print(f"  Earnings calendar error: {e}")
    return []

# ── Extract tickers and sentiment from text, validated against the universe ──
_TOKEN_RE = re.compile(r'\$([A-Z]{1,12}(?:\.[A-Z])?)\b|\b([A-Z]{2,12}(?:\.[A-Z])?)\b')

def extract_tickers_with_sentiment(texts, universe):
    ticker_mentions = defaultdict(int)
    ticker_sentiment = defaultdict(int)
    stats = {'listed': 0, 'normalized': 0, 'unverified': 0, 'dropped': 0}
    normalized_seen = {}

    for text in texts:
        text_lower = text.lower()
        found_in_text = set()
        for match in _TOKEN_RE.finditer(text):
            cashtag = match.group(1) is not None
            token = match.group(1) or match.group(2)
            resolved = resolve_token(token, cashtag, universe)
            if resolved is None:
                stats['dropped'] += 1
                continue
            symbol, how = resolved
            stats[how] += 1
            if how == 'normalized':
                normalized_seen[token] = symbol
            ticker_mentions[symbol] += 1
            found_in_text.add(symbol)

        bull_score = sum(1 for w in BULLISH_WORDS if w in text_lower)
        bear_score = sum(1 for w in BEARISH_WORDS if w in text_lower)
        sentiment = bull_score - bear_score

        for ticker in found_in_text:
            ticker_sentiment[ticker] += sentiment

    if normalized_seen:
        print(f"  Normalized glued tokens: {normalized_seen}")
    print(f"  Extraction: {stats['listed']} listed, {stats['normalized']} "
          f"normalized, {stats['unverified']} unverified cashtags, "
          f"{stats['dropped']} candidates dropped as not real symbols")
    return ticker_mentions, ticker_sentiment

# ── Score tickers combining frequency and sentiment ───────────────────────────
# Returns (primary, backfill). Primary is the ranked board pool: tickers with
# at least 3 mentions, best first. Backfill holds the tickers below the
# mention line, ranked the same way, so the board can still be completed to
# ten names on a thin day. Both pools carry only validated symbols, and every
# entry still has to pass the live-quote gate before it can appear.
def score_tickers(ticker_mentions, ticker_sentiment, earnings_tickers, yahoo_tickers):
    primary, backfill = {}, {}
    for ticker, count in ticker_mentions.items():
        score = count
        if ticker in earnings_tickers:
            score *= 2.0
        if ticker in yahoo_tickers:
            score *= 1.5
        entry = {
            'count': count,
            'sentiment': ticker_sentiment.get(ticker, 0),
            'score': score,
            'has_earnings': ticker in earnings_tickers
        }
        (primary if count >= 3 else backfill)[ticker] = entry

    rank = lambda pool: sorted(pool.items(), key=lambda x: x[1]['score'], reverse=True)
    return rank(primary)[:15], rank(backfill)[:15]

# ── Verify finalists against live Yahoo quotes and enrich them ────────────────
# A symbol with no live price is not a real, traded instrument: it is dropped
# here even if it slipped through extraction. This is the second gate, and it
# is also what admits 'unverified' cashtags (real names missing from the
# listings files) while still rejecting invented ones.
def fetch_quote(symbol):
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{_yahoo_symbol(symbol)}")
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
        result = (data.get('chart', {}).get('result') or [None])[0]
        if not result:
            return None
        meta = result.get('meta', {})
        price = meta.get('regularMarketPrice')
        if not price:
            return None
        prev = meta.get('chartPreviousClose') or meta.get('previousClose')
        day_pct = ((price - prev) / prev * 100) if prev else None
        return {
            'price': price,
            'day_pct': day_pct,
            'market_cap': 0,  # the chart endpoint has no cap field; see fetch_market_caps
            'instrument': (meta.get('instrumentType') or '').upper(),
            'name': meta.get('longName') or meta.get('shortName') or '',
            'volume': meta.get('regularMarketVolume') or 0,
        }
    except Exception:
        return None

# ── Market caps, fetched in one batch from the quote endpoint ────────────────
# The chart endpoint above returns no marketCap field, which is why the MKT
# CAP column used to render empty. Caps come from the v7 quote endpoint,
# which needs Yahoo's cookie/crumb handshake. Any failure returns an empty
# map; the affected rows simply render a dash, and a cap never gates a ticker.
def fetch_market_caps(symbols):
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        session.get('https://fc.yahoo.com', timeout=10)
        crumb = session.get(
            'https://query2.finance.yahoo.com/v1/test/getcrumb',
            timeout=10).text.strip()
        if not crumb or '<' in crumb:
            return {}
        yahoo_syms = [_yahoo_symbol(s) for s in symbols]
        r = session.get(
            'https://query2.finance.yahoo.com/v7/finance/quote',
            params={'symbols': ','.join(yahoo_syms), 'crumb': crumb},
            timeout=15)
        caps = {}
        for q in r.json().get('quoteResponse', {}).get('result', []):
            cap = q.get('marketCap')
            if cap:
                caps[(q.get('symbol') or '').upper()] = cap
        return caps
    except Exception:
        return {}

def _tidy_name(name):
    if name and name == name.upper():
        name = name.title().replace('Corp', 'Corp.').replace(' Inc', ' Inc.')
    return name

def enrich_and_filter(top_tickers, universe, backfill=()):
    enriched = []

    def consider(ticker, data, from_backfill):
        q = fetch_quote(ticker)
        if q is None:
            print(f"  Dropped {ticker}: no live quote (not a real, traded symbol)")
            return
        entry = universe.get(_symbol_key(ticker), {})
        is_etf = entry.get('is_etf')
        if is_etf is None:
            is_etf = q['instrument'] == 'ETF'
        data = dict(data)
        data.update({
            'name': q['name'] or _tidy_name(entry.get('name', '')),
            'is_etf': bool(is_etf),
            'price': q['price'],
            'day_pct': q['day_pct'],
            'market_cap': 0,
            'volume': q['volume'],
        })
        enriched.append((ticker, data))
        if from_backfill:
            print(f"  Backfilled {ticker} ({data['count']} mentions) "
                  f"to complete the board of ten")
        time.sleep(0.3)

    # There is deliberately no market-cap screen: the board's leaders are
    # routinely mega-caps, and a cap reading says nothing about whether a
    # symbol is real. Reality is enforced by the listings universe and the
    # live-quote gate. (The old $500B drop never fired in practice, because
    # the chart endpoint supplied no cap to test.)
    for ticker, data in top_tickers:
        if len(enriched) >= 10:
            break
        consider(ticker, data, False)
    if len(enriched) < 10:
        for ticker, data in backfill:
            if len(enriched) >= 10:
                break
            consider(ticker, data, True)

    if enriched:
        caps = fetch_market_caps([t for t, _ in enriched])
        for ticker, data in enriched:
            data['market_cap'] = caps.get(_yahoo_symbol(ticker).upper(), 0)
    return enriched

# ── Formatting helpers ────────────────────────────────────────────────────────
def _fmt_cap(cap):
    if not cap:
        return '—'
    if cap >= 1e12:
        return f"${cap/1e12:.2f}T"
    if cap >= 1e9:
        return f"${cap/1e9:.1f}B"
    return f"${cap/1e6:.0f}M"

def _fmt_price(price):
    return f"${price:,.2f}" if price else '—'

def _fmt_pct(pct):
    return f"{pct:+.1f}%" if pct is not None else '—'

def _sentiment_label(score):
    return 'Bullish' if score > 0 else ('Bearish' if score < 0 else 'Neutral')

# ── Call Claude API to generate report ───────────────────────────────────────
def generate_report(top_tickers):
    lines = []
    for ticker, data in top_tickers[:10]:
        kind = 'ETF' if data['is_etf'] else 'Stock'
        name = f" ({data['name']})" if data['name'] else ''
        earnings_flag = ' [EARNINGS TODAY]' if data['has_earnings'] else ''
        lines.append(
            f"{ticker}{name} [{kind}]: {data['count']} mentions, "
            f"sentiment={_sentiment_label(data['sentiment'])}, "
            f"last price {_fmt_price(data['price'])} "
            f"({_fmt_pct(data['day_pct'])} last session), "
            f"market cap={_fmt_cap(data['market_cap'])}{earnings_flag}"
        )

    ticker_summary = '\n'.join(lines)
    today_str = date.today().strftime('%A, %B %d, %Y')
    valid = {t for t, _ in top_tickers[:10]}

    prompt = f"""You are a sharp options trading analyst writing a pre-market briefing for {today_str}.

Social chatter data from Reddit (WSB, options, stocks, pennystocks, Daytrading, investing), Yahoo Finance trending, and Finviz shows these tickers ranked by momentum score. Major index ETFs are excluded by design. Every symbol below has been validated as a real US-listed stock or ETF with a live market price; all are under the $500B market cap screen where a cap is reported.

{ticker_summary}

Select the TOP 3 picks for options trading today or this week. Choose ONLY from the tickers listed above; never invent or substitute a ticker. Weight heavily toward tickers with earnings catalysts, strong bullish sentiment, and high mention counts.

Write the answer in exactly this format, with no introduction and no extra sections:

PICK: TICKER — one line summary of why it's trending
CATALYST: what is most likely driving the move
OPTIONS PLAY: calls or puts, suggested expiry window, and why
RISK: the single biggest risk to the trade

(repeat for each of the 3 picks, separated by a blank line)

MARKET PULSE: one paragraph summarizing the overall mood across all tickers today.

Keep it sharp, punchy, and actionable. No fluff."""

    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    }
    api_headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    def _picks_valid(text):
        picks = re.findall(r'^PICK:\s*\$?([A-Z]{1,6})', text, re.M)
        return len(picks) >= 1 and all(p in valid for p in picks)

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=api_headers, timeout=30)
            print(f"  Claude status: {response.status_code}")
            response.raise_for_status()
            text = response.json()['content'][0]['text']
            if _picks_valid(text):
                return text
            print("  Claude named a ticker outside the validated list; "
                  "retrying with a sterner instruction.")
            payload['messages'] = [{
                "role": "user",
                "content": prompt + "\n\nIMPORTANT: your previous draft named a "
                            "ticker that is not in the list above. Use only "
                            "listed tickers, exactly as written."
            }]
        except Exception as e:
            print(f"  Claude attempt {attempt+1} failed: {e}")
            time.sleep(5)

    print("  Falling back to the data-built report.")
    return template_report(top_tickers)

# ── Deterministic fallback report, built only from validated data ─────────────
def template_report(top_tickers):
    blocks = []
    for ticker, data in top_tickers[:3]:
        label = _sentiment_label(data['sentiment'])
        name = data['name'] or ticker
        kind = 'ETF' if data['is_etf'] else 'stock'
        if data['has_earnings']:
            catalyst = (f"{name} reports earnings today, and chatter is "
                        f"building into the print with {data['count']} mentions.")
            play_dir = 'calls' if data['sentiment'] >= 0 else 'puts'
            play = (f"{play_dir.capitalize()} expiring this week or next, "
                    f"at the money, sized small: the earnings move is the trade.")
            risk = "Earnings are binary; a miss or soft guidance gaps the name either way."
        else:
            catalyst = (f"Sustained social momentum: {data['count']} mentions "
                        f"across Reddit, Yahoo trending, and Finviz, with "
                        f"{label.lower()} sentiment. Last price "
                        f"{_fmt_price(data['price'])} "
                        f"({_fmt_pct(data['day_pct'])} last session).")
            play_dir = 'calls' if data['sentiment'] > 0 else (
                'puts' if data['sentiment'] < 0 else 'calls')
            play = (f"{play_dir.capitalize()}, one to two week expiry, at the "
                    f"money, following the {label.lower()} chatter while it lasts.")
            risk = ("Chatter-driven moves in a " + kind + " reverse quickly "
                    "when attention moves on; no confirmed news catalyst.")
        blocks.append(
            f"PICK: {ticker} — {name} leads the board with {data['count']} "
            f"mentions and {label.lower()} sentiment.\n"
            f"CATALYST: {catalyst}\n"
            f"OPTIONS PLAY: {play}\n"
            f"RISK: {risk}"
        )

    labels = [_sentiment_label(d['sentiment']) for _, d in top_tickers[:10]]
    top_name = top_tickers[0][1]['name'] or top_tickers[0][0] if top_tickers else ''
    pulse = (
        f"MARKET PULSE: Chatter is led by {top_name}. Of the {len(top_tickers)} names on the "
        f"board, {labels.count('Bullish')} read bullish, "
        f"{labels.count('Bearish')} bearish, and {labels.count('Neutral')} "
        f"neutral on social sentiment, with "
        f"{sum(1 for _, d in top_tickers[:10] if d['has_earnings'])} reporting "
        f"earnings today."
    )
    return '\n\n'.join(blocks + [pulse])

# ── Parse a structured report into picks + market pulse ──────────────────────
def parse_report(report_text):
    picks = []
    blocks = re.split(r'^PICK:\s*', report_text, flags=re.M)
    for block in blocks[1:]:
        block = block.strip()
        if not block:
            continue
        head, _, rest = block.partition('\n')
        m = re.match(r'\$?([A-Z]{1,6}(?:\.[A-Z])?)\s*[—\-:]\s*(.*)', head.strip())
        if not m:
            continue
        pick = {'ticker': m.group(1), 'summary': m.group(2).strip(),
                'catalyst': '', 'play': '', 'risk': ''}
        current = None
        for line in rest.splitlines():
            if re.match(r'\s*MARKET PULSE\s*:', line):
                current = None
                continue
            lm = re.match(r'\s*(CATALYST|OPTIONS PLAY|RISK)\s*:\s*(.*)', line)
            if lm:
                current = {'CATALYST': 'catalyst', 'OPTIONS PLAY': 'play',
                           'RISK': 'risk'}[lm.group(1)]
                pick[current] = lm.group(2).strip()
            elif current and line.strip():
                pick[current] += ' ' + line.strip()
        picks.append(pick)
    pulse_m = re.search(r'MARKET PULSE\s*:\s*(.+)$', report_text, re.S | re.M)
    pulse = ''
    if pulse_m:
        pulse = ' '.join(pulse_m.group(1).split('PICK:')[0].split()).strip()
    return (picks, pulse) if picks else None

# ── Build the Maineblock-branded HTML email ──────────────────────────────────
# Brand: Signal Orange #FF8000 on near-black #0B0B0C, warm card surfaces,
# Space Grotesk headings and Inter body (with email-safe fallbacks), orange
# eyebrow labels, table layout for the data grid, inline styles throughout.
import html as _html

def _esc(x):
    return _html.escape(str(x), quote=True)

def build_html_email(report_text, top_tickers):
    today_str = date.today().strftime('%A, %B %d, %Y')
    HF = "'Space Grotesk','Segoe UI',Arial,sans-serif"
    BF = "'Inter','Segoe UI',Arial,sans-serif"
    INK, MUTED, FAINT = '#FAF6EE', '#A39E93', '#6E675C'
    BODY, LINE = '#E9E2D4', '#2A251C'
    UP, DOWN = '#4ADE80', '#F87171'

    # ── Trending table rows ──
    ticker_rows = ''
    for ticker, data in top_tickers[:10]:
        sentiment = data['sentiment']
        if sentiment > 0:
            sent_color, sent_label = UP, '▲ Bullish'
        elif sentiment < 0:
            sent_color, sent_label = DOWN, '▼ Bearish'
        else:
            sent_color, sent_label = MUTED, '— Neutral'

        badges = ''
        if data['has_earnings']:
            badges += ('<span style="background:#FF8000;color:#0B0B0C;'
                       'padding:2px 6px;border-radius:4px;font-size:10px;'
                       'font-weight:700;margin-left:6px;">EARNINGS</span>')
        if data['is_etf']:
            badges += ('<span style="border:1px solid #FF8000;color:#FF8000;'
                       'padding:1px 5px;border-radius:4px;font-size:10px;'
                       'font-weight:700;margin-left:6px;">ETF</span>')
        name_line = (f'<div style="font-family:{BF};font-size:11px;'
                     f'color:{MUTED};margin-top:2px;">{_esc(data["name"])}</div>'
                     if data['name'] else '')
        day = data['day_pct']
        day_color = UP if (day or 0) > 0 else (DOWN if (day or 0) < 0 else MUTED)

        ticker_rows += f"""
        <tr>
            <td style="padding:10px 12px;border-bottom:1px solid #241F16;">
              <span style="font-family:{HF};font-weight:700;font-size:15px;color:{INK};">{_esc(ticker)}</span>{badges}{name_line}
            </td>
            <td style="padding:10px 12px;border-bottom:1px solid #241F16;font-family:{BF};font-size:13px;color:{BODY};">{_fmt_price(data['price'])}</td>
            <td style="padding:10px 12px;border-bottom:1px solid #241F16;font-family:{BF};font-size:13px;font-weight:600;color:{day_color};">{_fmt_pct(day)}</td>
            <td style="padding:10px 12px;border-bottom:1px solid #241F16;font-family:{BF};font-size:13px;color:{MUTED};">{data['count']}</td>
            <td style="padding:10px 12px;border-bottom:1px solid #241F16;font-family:{BF};font-size:13px;color:{sent_color};font-weight:600;">{sent_label}</td>
            <td style="padding:10px 12px;border-bottom:1px solid #241F16;font-family:{BF};font-size:13px;color:{MUTED};">{_fmt_cap(data['market_cap'])}</td>
        </tr>"""
    if not ticker_rows:
        ticker_rows = (f'<tr><td colspan="6" style="padding:14px 12px;'
                       f'font-family:{BF};font-size:13px;color:{MUTED};">'
                       f'No validated tickers today.</td></tr>')

    # ── Analysis section: structured pick cards when the report parses ──
    parsed = parse_report(report_text)
    if parsed:
        picks, pulse = parsed
        cards = ''
        for i, pick in enumerate(picks[:3], 1):
            fields = ''
            for label, key in (('CATALYST', 'catalyst'),
                               ('OPTIONS PLAY', 'play'),
                               ('RISK', 'risk')):
                if pick[key]:
                    fields += f"""
            <div style="margin-top:12px;">
              <div style="font-family:{HF};font-size:11px;font-weight:700;letter-spacing:2px;color:#FF8000;">{label}</div>
              <div style="font-family:{BF};font-size:14px;line-height:1.7;color:{BODY};margin-top:3px;">{_esc(pick[key])}</div>
            </div>"""
            cards += f"""
  <div style="background:#16130E;border:1px solid {LINE};border-radius:12px;padding:20px 24px;margin-bottom:16px;">
    <div style="font-family:{HF};font-size:11px;font-weight:700;letter-spacing:2px;color:#FF8000;">PICK {i}</div>
    <div style="font-family:{HF};font-size:17px;font-weight:700;color:{INK};margin-top:6px;"><span style="color:#FF8000;">{_esc(pick['ticker'])}</span> — {_esc(pick['summary'])}</div>{fields}
  </div>"""
        if pulse:
            cards += f"""
  <div style="background:#16130E;border:1px solid {LINE};border-left:3px solid #FF8000;border-radius:12px;padding:20px 24px;margin-bottom:20px;">
    <div style="font-family:{HF};font-size:11px;font-weight:700;letter-spacing:2px;color:#FF8000;">MARKET PULSE</div>
    <div style="font-family:{BF};font-size:14px;line-height:1.75;color:{BODY};margin-top:8px;">{_esc(pulse)}</div>
  </div>"""
        analysis_html = cards
    else:
        report_html = _esc(report_text).replace('\n', '<br>')
        analysis_html = f"""
  <div style="background:#16130E;border:1px solid {LINE};border-radius:12px;padding:24px 28px;margin-bottom:20px;">
    <div style="font-family:{BF};font-size:14px;line-height:1.8;color:{BODY};">{report_html}</div>
  </div>"""

    top_names = ', '.join(t for t, _ in top_tickers[:3]) or 'No tickers'
    html_doc = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0B0B0C;font-family:{BF};">
<div style="display:none;max-height:0;overflow:hidden;">Top chatter today: {_esc(top_names)} — {today_str}</div>
<div style="max-width:680px;margin:0 auto;padding:24px 16px;">

  <!-- Header -->
  <div style="background:#100D09;border:1px solid {LINE};border-top:3px solid #FF8000;border-radius:12px;padding:28px 32px;margin-bottom:24px;">
    <div style="font-family:{HF};font-size:11px;font-weight:700;color:#FF8000;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">Maineblock · Morning Briefing</div>
    <div style="font-family:{HF};font-size:26px;font-weight:700;color:{INK};">Stock Chatter Report</div>
    <div style="font-family:{BF};font-size:13px;color:{MUTED};margin-top:6px;">{today_str}</div>
  </div>

  <!-- Trending Tickers Table -->
  <div style="font-family:{HF};font-size:12px;font-weight:700;color:#FF8000;letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;">Top Trending Tickers</div>
  <div style="background:#16130E;border:1px solid {LINE};border-radius:12px;padding:12px 12px 16px;margin-bottom:24px;">
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr>
          <th style="padding:8px 12px;text-align:left;font-family:{HF};font-size:11px;color:#8A8378;text-transform:uppercase;letter-spacing:1px;">Ticker</th>
          <th style="padding:8px 12px;text-align:left;font-family:{HF};font-size:11px;color:#8A8378;text-transform:uppercase;letter-spacing:1px;">Price</th>
          <th style="padding:8px 12px;text-align:left;font-family:{HF};font-size:11px;color:#8A8378;text-transform:uppercase;letter-spacing:1px;">Day</th>
          <th style="padding:8px 12px;text-align:left;font-family:{HF};font-size:11px;color:#8A8378;text-transform:uppercase;letter-spacing:1px;">Mentions</th>
          <th style="padding:8px 12px;text-align:left;font-family:{HF};font-size:11px;color:#8A8378;text-transform:uppercase;letter-spacing:1px;">Sentiment</th>
          <th style="padding:8px 12px;text-align:left;font-family:{HF};font-size:11px;color:#8A8378;text-transform:uppercase;letter-spacing:1px;">Mkt Cap</th>
        </tr>
      </thead>
      <tbody>{ticker_rows}
      </tbody>
    </table>
  </div>

  <!-- AI Analysis -->
  <div style="font-family:{HF};font-size:12px;font-weight:700;color:#FF8000;letter-spacing:2px;text-transform:uppercase;margin-bottom:12px;">AI Analysis &amp; Top 3 Picks</div>
{analysis_html}

  <!-- Footer -->
  <div style="text-align:center;font-family:{BF};font-size:11px;color:{FAINT};padding:12px;">
    Maineblock · Numbers done right.<br>
    Generated automatically · For informational purposes only · Not financial advice
  </div>

</div>
</body>
</html>"""
    return html_doc

# ── Build the plain-text email (mirrors the HTML content) ────────────────────
def build_text_email(report_text, top_tickers):
    today_str = date.today().strftime('%A, %B %d, %Y')
    lines = [
        'MAINEBLOCK · MORNING BRIEFING',
        'Stock Chatter Report',
        today_str,
        '',
        'TOP TRENDING TICKERS',
        f"{'TICKER':<8}{'PRICE':>10}{'DAY':>8}{'MENTIONS':>10}  "
        f"{'SENTIMENT':<9}{'MKT CAP':>9}",
    ]
    for ticker, data in top_tickers[:10]:
        lines.append(
            f"{ticker:<8}{_fmt_price(data['price']):>10}"
            f"{_fmt_pct(data['day_pct']):>8}{data['count']:>10}  "
            f"{_sentiment_label(data['sentiment']):<9}"
            f"{_fmt_cap(data['market_cap']):>9}"
            + ('  [EARNINGS TODAY]' if data['has_earnings'] else '')
            + ('  [ETF]' if data['is_etf'] else '')
        )
    if not top_tickers:
        lines.append('No validated tickers today.')
    lines += ['', 'AI ANALYSIS & TOP 3 PICKS', '', report_text, '',
              'Maineblock · Numbers done right.',
              'Generated automatically · For informational purposes only · '
              'Not financial advice']
    return '\n'.join(lines)

# ── Log results to Google Sheets ──────────────────────────────────────────────
def log_to_sheets(top_tickers, report_text):
    try:
        creds_dict = json.loads(SHEETS_CREDENTIALS)
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEETS_ID)

        # ── Daily log tab ─────────────────────────────────────────────────────
        try:
            log_ws = sheet.worksheet('Daily Log')
        except:
            log_ws = sheet.add_worksheet(title='Daily Log', rows=1000, cols=10)
            log_ws.append_row(['Date', 'Pick 1', 'Pick 2', 'Pick 3', 'Top 10 Tickers', 'Report Summary'])

        today_str = date.today().strftime('%Y-%m-%d')
        picks = [t for t, _ in top_tickers[:3]]
        top10 = ', '.join([t for t, _ in top_tickers[:10]])
        summary = report_text[:300].replace('\n', ' ')

        log_ws.append_row([
            today_str,
            picks[0] if len(picks) > 0 else '',
            picks[1] if len(picks) > 1 else '',
            picks[2] if len(picks) > 2 else '',
            top10,
            summary
        ])

        # ── Dashboard data tab (for GitHub Pages) ─────────────────────────────
        try:
            dash_ws = sheet.worksheet('Dashboard')
        except:
            dash_ws = sheet.add_worksheet(title='Dashboard', rows=100, cols=10)

        dash_ws.clear()
        dash_ws.append_row(['last_updated', today_str])
        dash_ws.append_row(['report', report_text])
        for ticker, data in top_tickers[:10]:
            dash_ws.append_row([ticker, data['count'], data['sentiment'], str(data['has_earnings'])])

        print("  Google Sheets updated.")
    except Exception as e:
        print(f"  Sheets error: {e}")

# ── Send the report ───────────────────────────────────────────────────────────
# Preferred path: Resend from a generic Maineblock address (no Gmail involved
# on either side). Fallback: the original Gmail SMTP path, used only while no
# RESEND_API_KEY secret exists, so adding the secret is all it takes to switch.
def send_email(html_content, plain_text):
    subject = f"📊 Stock Chatter Report — {date.today().strftime('%b %d')}"
    domain = EMAIL_RECIPIENT.rsplit('@', 1)[-1] if '@' in EMAIL_RECIPIENT else '?'

    if RESEND_API_KEY:
        r = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'from': EMAIL_FROM,
                'to': [EMAIL_RECIPIENT],
                'subject': subject,
                'html': html_content,
                'text': plain_text,
            },
            timeout=30,
        )
        r.raise_for_status()
        print(f"  Email sent via Resend from {EMAIL_FROM} to ***@{domain}.")
        return

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = EMAIL_RECIPIENT

    msg.attach(MIMEText(plain_text, 'plain'))
    msg.attach(MIMEText(html_content, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())

    print(f"  Email sent via Gmail SMTP to ***@{domain}.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Loading real-symbol universe...")
    universe = load_universe()

    print("Fetching Reddit posts via RSS...")
    all_texts = []
    for sub in SUBREDDITS:
        posts = fetch_reddit_rss(sub)
        all_texts.extend(posts)
        time.sleep(2)

    ticker_mentions, ticker_sentiment = extract_tickers_with_sentiment(all_texts, universe)
    print(f"Unique validated tickers found: {len(ticker_mentions)}")

    print("Fetching Yahoo Finance trending...")
    yahoo_tickers = fetch_yahoo_trending(universe)

    print("Fetching Finviz tickers...")
    finviz_tickers = fetch_finviz(universe)
    for t in finviz_tickers:
        ticker_mentions[t] = ticker_mentions.get(t, 0) + 2

    print("Fetching earnings calendar...")
    earnings_tickers = fetch_earnings_this_week(universe)

    print("Scoring tickers...")
    top_tickers, backfill = score_tickers(ticker_mentions, ticker_sentiment, earnings_tickers, yahoo_tickers)
    print(f"Top before quote verification: {[t for t,_ in top_tickers[:10]]}")
    if backfill:
        print(f"Backfill pool (below the 3-mention line): {[t for t,_ in backfill]}")

    print("Verifying quotes and enriching...")
    top_tickers = enrich_and_filter(top_tickers, universe, backfill)
    print(f"Final validated board: {[t for t,_ in top_tickers[:10]]}")

    if top_tickers:
        print("Generating report with Claude...")
        report = generate_report(top_tickers)
    else:
        report = ("No tickers cleared validation today. Every candidate symbol "
                  "was checked against the exchange listings and live quotes, "
                  "and none carried a verified live price.")
    print(report)

    print("Building emails...")
    html = build_html_email(report, top_tickers)
    text = build_text_email(report, top_tickers)

    if DRY_RUN:
        with open('report_preview.html', 'w') as f:
            f.write(html)
        with open('report_preview.txt', 'w') as f:
            f.write(text)
        print("DRY RUN: wrote report_preview.html and report_preview.txt; "
              "no email sent, nothing logged.")
        return

    print("Logging to Google Sheets...")
    log_to_sheets(top_tickers, report)

    print("Sending email...")
    send_email(html, text)
    print("Done.")

if __name__ == "__main__":
    main()
