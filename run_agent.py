import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

import requests
import yfinance as yf
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# -----------------------------
# Config (from env / GitHub Secrets)
# -----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()  # optional
GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
EMAIL_TO = os.getenv("EMAIL_TO", "ventush88@gmail.com").strip()

HK_TZ_LABEL = "HKT"


def get_market_data():
    # HK + China + USD relevant
    tickers = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "Hang Seng": "^HSI",
        "HSCEI": "^HSCE",
        "CSI 300": "000300.SS",
        "UST 10Y (yield proxy)": "^TNX",  # note: TNX is yield*10
        "Brent": "BZ=F",
        "WTI": "CL=F",
        "Gold": "GC=F",
        "BTC": "BTC-USD",
        "DXY": "DX-Y.NYB",
        "USD/JPY": "JPY=X",
        "USD/CNH": "CNH=X",
    }

    out = {}
    for name, tk in tickers.items():
        t = yf.Ticker(tk)
        hist = t.history(period="2d")
        if hist is None or hist.empty:
            continue
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last
        chg = (last - prev) / prev * 100 if prev != 0 else 0.0

        # Format UST 10Y: ^TNX is yield*10
        if "UST 10Y" in name:
            last = last / 10.0
            prev = prev / 10.0
            chg_bp = (last - prev) * 100  # 1.00% = 100bp
            out[name] = {"level": round(last, 2), "change": round(chg_bp, 1), "unit": "%", "change_unit": "bp"}
        else:
            out[name] = {"level": round(last, 2), "change": round(chg, 2), "unit": "", "change_unit": "%"}

    return out


def get_headlines():
    # If no NEWSAPI key, fallback to empty list (still works)
    if not NEWSAPI_KEY:
        return []

    url = "https://newsapi.org/v2/top-headlines"
    params = {
        "category": "business",
        "language": "en",
        "pageSize": 12,
        "apiKey": NEWSAPI_KEY,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    j = r.json()
    articles = j.get("articles", []) or []
    return [a.get("title", "").strip() for a in articles if a.get("title")]


def ecm_window_score(mkt):
    # simple desk-ish heuristic (0-10)
    score = 5
    if mkt.get("Nasdaq", {}).get("change", 0) > 0:
        score += 1
    if mkt.get("S&P 500", {}).get("change", 0) > 0:
        score += 1
    # rates down supports ECM risk appetite
    if mkt.get("UST 10Y (yield proxy)", {}).get("change", 0) < 0:
        score += 1
    if mkt.get("Hang Seng", {}).get("change", 0) > 0:
        score += 1
    return max(0, min(score, 10))


def dcm_window_state(mkt):
    # Use UST10Y bp move as proxy for funding conditions
    bp = mkt.get("UST 10Y (yield proxy)", {}).get("change", 0)
    if bp <= -5:
        return "STRONG (rates down)"
    if bp < 0:
        return "OPEN"
    if bp < 5:
        return "MIXED"
    return "WEAK (rates up)"


def llm_generate_note(mkt, headlines, ecm_score, dcm_state):
    if not OPENAI_API_KEY:
        raise RuntimeError("Missing OPENAI_API_KEY")

    from openai import OpenAI
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Build compact market lines
    def fmt_line(k):
        v = mkt.get(k)
        if not v:
            return None
        if v["change_unit"] == "bp":
            return f"{k}: {v['level']}{v['unit']} ({v['change']}bp)"
        return f"{k}: {v['level']} ({v['change']}%)"

    key_order = [
        "Hang Seng", "HSCEI", "CSI 300",
        "S&P 500", "Nasdaq",
        "UST 10Y (yield proxy)",
        "DXY", "USD/CNH", "USD/JPY",
        "Brent", "WTI", "Gold", "BTC",
    ]
    market_lines = [x for x in (fmt_line(k) for k in key_order) if x]

    headline_lines = headlines[:12]

    prompt = f"""
You are an investment bank Capital Markets desk (ECM + DCM) strategist.

Write a ONE-PAGE "HK Capital Markets Morning Note" (English, concise, punchy).
Focus on Hong Kong market, with China and USD funding conditions.

Must include:
1) Overnight risk tone (1-2 sentences)
2) "What matters for Capital Markets today" (3 bullets, each: move → implication for ECM/DCM)
3) ECM Window Monitor (score {ecm_score}/10 with 2-3 bullets: placements/block/IPO)
4) DCM Window Monitor (state: {dcm_state} with 2-3 bullets: Asia USD IG/HY/SOE tone)
5) Deals/Radar (3 bullets: sectors/issuers likely to tap ECM or USD DCM)
6) Desk Takeaways (3 numbered lines)

Keep it within ~350-450 words.
Avoid generic macro textbook explanations. Sound like a real desk note.

Market snapshot:
- {"; ".join(market_lines)}

Overnight headlines:
- {" | ".join(headline_lines) if headline_lines else "N/A"}
""".strip()

    resp = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
    )
    return resp.output_text


def build_pdf(note_text, filename="morning_note.pdf"):
    # Simple clean one-page PDF
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4

    # Title
    dt = datetime.utcnow().strftime("%Y-%m-%d")
    title = f"HK Capital Markets Morning Note — {dt} ({HK_TZ_LABEL})"
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, height - 50, title)

    c.setFont("Helvetica", 10)
    y = height - 80

    # Wrap text manually
    max_width = width - 80
    for para in note_text.split("\n"):
        if not para.strip():
            y -= 10
            continue

        words = para.split(" ")
        line = ""
        for w in words:
            test = (line + " " + w).strip()
            if c.stringWidth(test, "Helvetica", 10) <= max_width:
                line = test
            else:
                c.drawString(40, y, line)
                y -= 14
                line = w
                if y < 60:
                    # if overflow, start a new page (rare, but safe)
                    c.showPage()
                    c.setFont("Helvetica", 10)
                    y = height - 60
        if line:
            c.drawString(40, y, line)
            y -= 14

    c.showPage()
    c.save()


def send_email_with_attachment(pdf_path):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD):
        raise RuntimeError("Missing GMAIL_USER or GMAIL_APP_PASSWORD")

    msg = EmailMessage()
    msg["Subject"] = "HK Capital Markets Morning Note (PDF)"
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.set_content("Attached: HK Capital Markets Morning Note (one-page PDF).")

    with open(pdf_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=os.path.basename(pdf_path),
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def main():
    mkt = get_market_data()
    headlines = get_headlines()
    ecm_score = ecm_window_score(mkt)
    dcm_state = dcm_window_state(mkt)

    note = llm_generate_note(mkt, headlines, ecm_score, dcm_state)
    build_pdf(note, "morning_note.pdf")
    send_email_with_attachment("morning_note.pdf")


if __name__ == "__main__":
    main()
