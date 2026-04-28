import os
import re
import time
import json
import base64
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
def fetch_yahoo_trending():
    try:
        r = requests.get(
            "https://finance.yahoo.com/trending-tickers/",
            headers=HEADERS, timeout=15
        )
        tickers = re.findall(r'"symbol":"([A-Z]{1,5})"', r.text)
        unique = [t for t in list(dict.fromkeys(tickers))[:30] if t not in EXCLUDE]
        print(f"  Yahoo trending: {len(unique)} tickers")
        return unique
    except Exception as e:
        print(f"  Yahoo error: {e}")
    return []

# ── Fetch Finviz news tickers ─────────────────────────────────────────────────
def fetch_finviz():
    try:
        r = requests.get("https://finviz.com/news.ashx", headers=HEADERS, timeout=15)
        tickers = [t for t in re.findall(r'\$([A-Z]{1,5})\b', r.text) if t not in EXCLUDE]
        print(f"  Finviz: {len(tickers)} ticker mentions")
        return tickers
    except Exception as e:
        print(f"  Finviz error: {e}")
    return []

# ── Fetch earnings calendar from Nasdaq ───────────────────────────────────────
def fetch_earnings_this_week():
    try:
        today = date.today().strftime('%Y-%m-%d')
        url = f"https://api.nasdaq.com/api/calendar/earnings?date={today}"
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
        tickers = []
        rows = data.get('data', {}).get('rows', []) or []
        for row in rows[:50]:
            symbol = row.get('symbol', '').strip().upper()
            if symbol and symbol not in EXCLUDE:
                tickers.append(symbol)
        print(f"  Earnings today: {tickers}")
        return tickers
    except Exception as e:
        print(f"  Earnings calendar error: {e}")
    return []

# ── Get market cap from Yahoo Finance ─────────────────────────────────────────
def get_market_cap(ticker):
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json()
        meta = data.get('chart', {}).get('result', [{}])[0].get('meta', {})
        market_cap = meta.get('marketCap', 0)
        return market_cap
    except:
        return 0

# ── Extract tickers and sentiment from text ───────────────────────────────────
def extract_tickers_with_sentiment(texts):
    pattern = re.compile(r'\$([A-Z]{2,5})\b|\b([A-Z]{2,5})\b')
    ticker_mentions = defaultdict(int)
    ticker_sentiment = defaultdict(int)

    for text in texts:
        text_lower = text.lower()
        found_in_text = set()
        for match in pattern.findall(text):
            ticker = match[0] or match[1]
            if ticker and ticker not in EXCLUDE and len(ticker) >= 2:
                ticker_mentions[ticker] += 1
                found_in_text.add(ticker)

        bull_score = sum(1 for w in BULLISH_WORDS if w in text_lower)
        bear_score = sum(1 for w in BEARISH_WORDS if w in text_lower)
        sentiment = bull_score - bear_score

        for ticker in found_in_text:
            ticker_sentiment[ticker] += sentiment

    return ticker_mentions, ticker_sentiment

# ── Score tickers combining frequency and sentiment ───────────────────────────
def score_tickers(ticker_mentions, ticker_sentiment, earnings_tickers, yahoo_tickers):
    scores = {}
    for ticker, count in ticker_mentions.items():
        if count < 3:
            continue
        score = count
        if ticker in earnings_tickers:
            score *= 2.0
        if ticker in yahoo_tickers:
            score *= 1.5
        sentiment = ticker_sentiment.get(ticker, 0)
        scores[ticker] = {
            'count': count,
            'sentiment': sentiment,
            'score': score,
            'has_earnings': ticker in earnings_tickers
        }

    sorted_tickers = sorted(scores.items(), key=lambda x: x[1]['score'], reverse=True)
    return sorted_tickers[:15]

# ── Filter by market cap ──────────────────────────────────────────────────────
def filter_by_market_cap(top_tickers, max_cap=500_000_000_000):
    filtered = []
    for ticker, data in top_tickers:
        cap = get_market_cap(ticker)
        if cap == 0 or cap <= max_cap:
            data['market_cap'] = cap
            filtered.append((ticker, data))
            time.sleep(0.3)
    return filtered[:10]

# ── Call Claude API to generate report ───────────────────────────────────────
def generate_report(top_tickers):
    lines = []
    for ticker, data in top_tickers[:10]:
        sentiment_label = 'Bullish' if data['sentiment'] > 0 else ('Bearish' if data['sentiment'] < 0 else 'Neutral')
        earnings_flag = ' [EARNINGS TODAY]' if data['has_earnings'] else ''
        cap_str = f"${data['market_cap']/1e9:.1f}B" if data['market_cap'] > 0 else 'Cap unknown'
        lines.append(
            f"{ticker}: {data['count']} mentions, sentiment={sentiment_label}, "
            f"market cap={cap_str}{earnings_flag}"
        )

    ticker_summary = '\n'.join(lines)
    today_str = date.today().strftime('%A, %B %d, %Y')

    prompt = f"""You are a sharp options trading analyst writing a pre-market briefing for {today_str}.

Social chatter data from Reddit (WSB, options, stocks, pennystocks, Daytrading, investing), Yahoo Finance trending, and Finviz shows these tickers ranked by momentum score. Indexes and ETFs have already been filtered out. All tickers are under $500B market cap.

{ticker_summary}

Select the TOP 3 picks for options trading today or this week. Weight heavily toward tickers with earnings catalysts, strong bullish sentiment, and high mention counts.

For each pick write:
TICKER — one line summary of why it's trending
CATALYST: what is most likely driving the move
OPTIONS PLAY: calls or puts, suggested expiry window, and why
RISK: the single biggest risk to the trade

End with a one-paragraph MARKET PULSE summarizing the overall mood across all tickers today.

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

    for attempt in range(3):
        try:
            response = requests.post(url, json=payload, headers=api_headers, timeout=30)
            print(f"  Claude status: {response.status_code}")
            response.raise_for_status()
            return response.json()['content'][0]['text']
        except Exception as e:
            print(f"  Claude attempt {attempt+1} failed: {e}")
            time.sleep(5)

    return "Report generation failed."

# ── Build HTML email ──────────────────────────────────────────────────────────
def build_html_email(report_text, top_tickers):
    today_str = date.today().strftime('%A, %B %d, %Y')

    ticker_rows = ''
    for ticker, data in top_tickers[:10]:
        sentiment = data['sentiment']
        if sentiment > 0:
            sent_color = '#22c55e'
            sent_label = '▲ Bullish'
        elif sentiment < 0:
            sent_color = '#ef4444'
            sent_label = '▼ Bearish'
        else:
            sent_color = '#94a3b8'
            sent_label = '— Neutral'

        earnings_badge = '<span style="background:#f59e0b;color:#000;padding:2px 6px;border-radius:4px;font-size:11px;margin-left:6px;">EARNINGS</span>' if data['has_earnings'] else ''
        cap_str = f"${data['market_cap']/1e9:.1f}B" if data['market_cap'] > 0 else '—'

        ticker_rows += f"""
        <tr>
            <td style="padding:10px 12px;font-weight:700;font-size:15px;color:#f1f5f9;">{ticker}{earnings_badge}</td>
            <td style="padding:10px 12px;color:#94a3b8;">{data['count']}</td>
            <td style="padding:10px 12px;color:{sent_color};font-weight:600;">{sent_label}</td>
            <td style="padding:10px 12px;color:#94a3b8;">{cap_str}</td>
        </tr>"""

    report_html = report_text.replace('\n', '<br>')

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Arial,sans-serif;">
<div style="max-width:680px;margin:0 auto;padding:24px 16px;">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1e3a5f,#0f172a);border-radius:12px;padding:28px 32px;margin-bottom:20px;border:1px solid #1e40af;">
    <div style="font-size:11px;color:#60a5fa;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px;">Morning Briefing</div>
    <div style="font-size:26px;font-weight:800;color:#f1f5f9;">📊 Stock Chatter Report</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:6px;">{today_str}</div>
  </div>

  <!-- Trending Tickers Table -->
  <div style="background:#1e293b;border-radius:12px;padding:20px 24px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:13px;font-weight:700;color:#60a5fa;letter-spacing:1px;text-transform:uppercase;margin-bottom:14px;">Top Trending Tickers</div>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="border-bottom:1px solid #334155;">
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;">Ticker</th>
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;">Mentions</th>
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;">Sentiment</th>
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;">Mkt Cap</th>
        </tr>
      </thead>
      <tbody>{ticker_rows}
      </tbody>
    </table>
  </div>

  <!-- AI Analysis -->
  <div style="background:#1e293b;border-radius:12px;padding:24px 28px;margin-bottom:20px;border:1px solid #334155;">
    <div style="font-size:13px;font-weight:700;color:#60a5fa;letter-spacing:1px;text-transform:uppercase;margin-bottom:16px;">AI Analysis & Top 3 Picks</div>
    <div style="font-size:14px;line-height:1.8;color:#cbd5e1;">{report_html}</div>
  </div>

  <!-- Footer -->
  <div style="text-align:center;font-size:11px;color:#475569;padding:12px;">
    Generated automatically · For informational purposes only · Not financial advice
  </div>

</div>
</body>
</html>"""
    return html

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

# ── Send the report via Gmail ─────────────────────────────────────────────────
def send_email(html_content, plain_text):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"📊 Stock Chatter Report — {date.today().strftime('%b %d')}"
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = EMAIL_RECIPIENT

    msg.attach(MIMEText(plain_text, 'plain'))
    msg.attach(MIMEText(html_content, 'html'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())

    print("  Email sent.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Fetching Reddit posts via RSS...")
    all_texts = []
    for sub in SUBREDDITS:
        posts = fetch_reddit_rss(sub)
        all_texts.extend(posts)
        time.sleep(2)

    ticker_mentions, ticker_sentiment = extract_tickers_with_sentiment(all_texts)
    print(f"Unique tickers found: {len(ticker_mentions)}")

    print("Fetching Yahoo Finance trending...")
    yahoo_tickers = fetch_yahoo_trending()

    print("Fetching Finviz tickers...")
    finviz_tickers = fetch_finviz()
    for t in finviz_tickers:
        ticker_mentions[t] = ticker_mentions.get(t, 0) + 2

    print("Fetching earnings calendar...")
    earnings_tickers = fetch_earnings_this_week()

    print("Scoring tickers...")
    top_tickers = score_tickers(ticker_mentions, ticker_sentiment, earnings_tickers, yahoo_tickers)
    print(f"Top before market cap filter: {[t for t,_ in top_tickers[:10]]}")

    print("Filtering by market cap...")
    top_tickers = filter_by_market_cap(top_tickers)
    print(f"Top after market cap filter: {[t for t,_ in top_tickers[:5]]}")

    print("Generating report with Claude...")
    report = generate_report(top_tickers)
    print(report)

    print("Building HTML email...")
    html = build_html_email(report, top_tickers)

    print("Logging to Google Sheets...")
    log_to_sheets(top_tickers, report)

    print("Sending email...")
    send_email(html, report)
    print("Done.")

if __name__ == "__main__":
    main()
