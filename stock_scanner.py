import os
import re
import smtplib
import requests
from collections import Counter
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.generativeai as genai

# ── Keys pulled from GitHub Secrets ──────────────────────────────────────────
GEMINI_API_KEY    = os.environ['GEMINI_API_KEY']
GMAIL_ADDRESS     = os.environ['GMAIL_ADDRESS']
GMAIL_APP_PASSWORD = os.environ['GMAIL_APP_PASSWORD']
EMAIL_RECIPIENT   = os.environ['EMAIL_RECIPIENT']

# ── Subreddits to monitor ─────────────────────────────────────────────────────
SUBREDDITS = [
    'wallstreetbets', 'options', 'stocks',
    'pennystocks', 'Daytrading', 'investing'
]

# ── Words that look like tickers but aren't ───────────────────────────────────
EXCLUDE = {
    'A','I','AM','PM','THE','FOR','ARE','ALL','NEW','NOW','GET','BIG','CAN',
    'CEO','ETF','EPS','IPO','GDP','USA','USD','ATH','IMO','EOD','WSB','DD',
    'OP','US','UK','EU','IT','IS','BE','OR','ON','SO','NO','DO','GO','AI',
    'TO','IF','IN','OF','AT','BY','UP','OH','OK','MY','WE','HE','ME','AN',
    'AS','BUT','NOT','YOU','HIS','HER','HAD','HAS','WAS','TOO','ANY','ITS',
    'FROM','THEY','WHAT','WHEN','WILL','WITH','BEEN','HAVE','WERE','SAID',
    'SHE','HIM','WHO','OWN','OUT','DAY','WAY','MAY','DID','LET','PUT','SET',
    'USE','FED','SEC','TAX','OIL','GAS','CAR','EV','PE','VC','YTD','YOY',
    'TTM','FCF','PO','RH','ML','DL','IMO','TBH','TBF','YOLO','FOMO','TLDR',
    'EDIT','LMAO','HODL','BUY','SELL','HOLD','CALL','PUTS','CALL','LOSS',
    'GAIN','MOON','BEAR','BULL','FUND','FUND','CASH','DEBT','RISK','HIGH',
    'LOW','MID','CAP','BID','ASK','IV','OTM','ITM','ATM','DTE','PNL',
    'FOMC','CPI','PCE','NFP','OPEX','RATE','BOND','NOTE','BILL','REPO'
}

# ── Fetch posts from a subreddit ──────────────────────────────────────────────
def fetch_reddit(subreddit):
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit=100"
    headers = {'User-Agent': 'Mozilla/5.0 StockScanner/1.0'}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            posts = r.json()['data']['children']
            return [f"{p['data'].get('title','')} {p['data'].get('selftext','')}" for p in posts]
    except Exception as e:
        print(f"Reddit error ({subreddit}): {e}")
    return []

# ── Scrape trending tickers from Finviz news ──────────────────────────────────
def fetch_finviz():
    headers = {'User-Agent': 'Mozilla/5.0 StockScanner/1.0'}
    try:
        r = requests.get("https://finviz.com/news.ashx", headers=headers, timeout=10)
        return re.findall(r'\$([A-Z]{1,5})\b', r.text)
    except:
        return []

# ── Pull tickers from text ────────────────────────────────────────────────────
def extract_tickers(texts):
    pattern = re.compile(r'\$([A-Z]{2,5})\b|\b([A-Z]{2,5})\b')
    tickers = []
    for text in texts:
        for match in pattern.findall(text):
            ticker = match[0] or match[1]
            if ticker and ticker not in EXCLUDE and len(ticker) >= 2:
                tickers.append(ticker)
    return tickers

# ── Score by mention frequency ────────────────────────────────────────────────
def score_tickers(tickers):
    return Counter(tickers).most_common(15)

# ── Call Gemini to write the report ──────────────────────────────────────────
def generate_report(top_tickers):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')

    ticker_summary = ', '.join([f"{t} ({c} mentions)" for t, c in top_tickers])

    prompt = f"""You are a sharp options trading analyst. Reddit and Finviz social chatter from this morning shows the following tickers trending by mention volume:

{ticker_summary}

Your job: pick the TOP 3 most interesting candidates for options trading today or this week. Base your selection on mention velocity, any likely catalyst (earnings, short squeeze narrative, news, sector momentum), and options relevance.

For each of the 3 picks write:
- Ticker & why it's trending
- Most likely catalyst
- Options angle: calls or puts, near-term or weekly expiry consideration
- Key risk to watch

Write this as a clean, punchy morning briefing. No fluff. Be direct and actionable."""

    response = model.generate_content(prompt)
    return response.text

# ── Send the report via Gmail ─────────────────────────────────────────────────
def send_email(report_text):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = '📊 Morning Stock Chatter Report'
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = EMAIL_RECIPIENT

    msg.attach(MIMEText(report_text, 'plain'))

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())

    print("Email sent.")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Fetching Reddit posts...")
    all_texts = []
    for sub in SUBREDDITS:
        posts = fetch_reddit(sub)
        all_texts.extend(posts)
        print(f"  r/{sub}: {len(posts)} posts")

    all_tickers = extract_tickers(all_texts)

    print("Fetching Finviz tickers...")
    all_tickers += fetch_finviz()

    print(f"Total ticker mentions: {len(all_tickers)}")
    top = score_tickers(all_tickers)
    print(f"Top tickers: {top}")

    print("Generating report...")
    report = generate_report(top)

    print("Sending email...")
    send_email(report)
    print("Done.")

if __name__ == "__main__":
    main()
