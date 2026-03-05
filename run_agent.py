import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

import requests
import yfinance as yf
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


# -----------------------------
# Secrets / Env
# -----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()     # optional (enhance)
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()           # optional
GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
EMAIL_TO = os.getenv("EMAIL_TO", "ventush88@gmail.com").strip()


def get_market_data():
    """
    HK + China + USD capital markets relevant snapshot.
    """
    tickers = {
        "Hang Seng": "^HSI",
        "HSCEI": "^HSCE",
        "CSI 300": "000300.SS",
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",

        # Rates / risk
        "UST 10Y (yield proxy)": "^TNX",  # TNX is yield * 10

        # FX / commodities / crypto
        "DXY": "DX-Y.NYB",
        "USD/CNH": "CNH=X",
        "USD/JPY": "JPY=X",
        "Brent": "BZ=F",
        "WTI": "CL=F",
        "Gold": "GC=F",
        "BTC": "BTC-USD",
    }

    out = {}
    for name, tk in tickers.items():
        t = yf.Ticker(tk)
        hist = t.history(period="2d")

        if hist is None or hist.empty:
            continue

        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else last

        if "UST 10Y" in name:
            # TNX: e.g. 43.21 -> 4.321%
            last_y = last / 10.0
            prev_y = prev / 10.0
            bp = (last_y - prev_y) * 100  # 1.00% = 100bp
            out[name] = {
                "level": round(last_y, 2),
                "change": round(bp, 1),
                "unit": "%",
                "change_unit": "bp",
            }
        else:
            chg = (last - prev) / prev * 100 if prev != 0 else 0.0
            out[name] = {
                "level": round(last, 2),
                "change": round(chg, 2),
                "unit": "",
                "change_unit": "%",
            }

    return out


def get_headlines():
    """
    Optional: NewsAPI. If no key, return [].
    """
    if not NEWSAPI_KEY:
        return []

    try:
        url = "https://newsapi.org/v2/top-headlines"
        params = {
            "category": "business",
            "language": "en",
            "pageSize": 12,
            "apiKey": NEWSAPI_KEY,
        }
        r = requests.get(url, params=params, timeout=25)
        r.raise_for_status()
        j = r.json()
        articles = j.get("articles", []) or []
        return [a.get("title", "").strip() for a in articles if a.get("title")]
    except Exception:
        return []


def ecm_window_score(mkt):
    """
    Simple desk heuristic (0-10), enough for a daily note.
    """
    score = 5
    if mkt.get("Nasdaq", {}).get("change", 0) > 0:
        score += 1
    if mkt.get("S&P 500", {}).get("change", 0) > 0:
        score += 1
    if mkt.get("Hang Seng", {}).get("change", 0) > 0:
        score += 1
    # rates down supports risk appetite / ECM execution
    if mkt.get("UST 10Y (yield proxy)", {}).get("change", 0) < 0:
        score += 1
    return max(0, min(score, 10))


def dcm_window_state(mkt):
    """
    Use UST10Y bp move as rough funding proxy.
    """
    bp = mkt.get("UST 10Y (yield proxy)", {}).get("change", 0)
    if bp <= -5:
        return "STRONG (rates down)"
    if bp < 0:
        return "OPEN"
    if bp < 5:
        return "MIXED"
    return "WEAK (rates up)"


def template_note(mkt, headlines, ecm_score, dcm_state):
    """
    ALWAYS available. CM desk style, concise, 1-page friendly.
    """
    def g(k):
        v = mkt.get(k)
        if not v:
            return "N/A"
        if v["change_unit"] == "bp":
            return f"{v['level']}{v['unit']} ({v['change']}bp)"
        return f"{v['level']} ({v['change']}%)"

    # risk tone
    risk = "Neutral"
    nas = mkt.get("Nasdaq", {}).get("change", 0)
    hsi = mkt.get("Hang Seng", {}).get("change", 0)
    ybp = mkt.get("UST 10Y (yield proxy)", {}).get("change", 0)
    if (nas > 0 and ybp < 0) or (hsi > 0 and ybp < 0):
        risk = "Mild Risk-On"
    if (nas < 0 and ybp > 0) and (hsi < 0):
        risk = "Risk-Off"

    # headlines
    top_heads = headlines[:8] if headlines else []
    heads_str = "\n".join([f"- {h}" for h in top_heads]) if top_heads else "- N/A"

    # ECM read
    if ecm_score >= 8:
        ecm_read = "OPEN for size (placements / blocks)"
        ipo_read = "IPO tone improving but still selective"
    elif ecm_score >= 6:
        ecm_read = "SELECTIVE (deal-by-deal execution)"
        ipo_read = "IPO window selective"
    else:
        ecm_read = "TIGHT (only high-conviction / pre-sounded)"
        ipo_read = "IPO window challenged"

    # DCM read
    if "STRONG" in dcm_state or "OPEN" in dcm_state:
        ig_read = "Constructive for Asia USD IG; SOE/financials better bid"
        hy_read = "HY still selective / headline-driven"
    else:
        ig_read = "Choppy; funding costs not friendly"
        hy_read = "HY window likely shut"

    note = f"""HK Capital Markets Morning Note (ECM + DCM) — {datetime.utcnow().strftime('%Y-%m-%d')}

Overnight Risk Tone: {risk}

Market Snapshot (HK + China + USD):
- Hang Seng: {g("Hang Seng")} | HSCEI: {g("HSCEI")} | CSI 300: {g("CSI 300")}
- S&P 500: {g("S&P 500")} | Nasdaq: {g("Nasdaq")} | UST 10Y: {g("UST 10Y (yield proxy)")}
- DXY: {g("DXY")} | USD/CNH: {g("USD/CNH")} | USD/JPY: {g("USD/JPY")}
- Brent: {g("Brent")} | Gold: {g("Gold")} | BTC: {g("BTC")}

What Matters for Capital Markets Today:
- Rates / USD tone → DCM funding conditions: {dcm_state} (watch UST moves + DXY + CNH)
- Equity tape → ECM execution: {ecm_read} (liquidity + risk appetite drive placement success)
- China/HK narrative → policy/property/SOE funding headlines can reprice issuance windows quickly

ECM Window Monitor: {ecm_score}/10
- Placements / blocks: {ecm_read}
- IPO: {ipo_read}
- Bias: liquid large caps, catalyst-backed trades; keep sizing disciplined if tape fragile

DCM Window Monitor: {dcm_state}
- Asia USD IG: {ig_read}
- HY: {hy_read}
- Watch: China SOE vs private credit differentiation; CNH volatility as sentiment barometer

Deals / Radar (HK + China + USD):
- ECM: HK large-cap tech/consumer names if tape stable; blocks more feasible than IPOs in mixed tape
- DCM: China SOE / financials more likely to test window when UST stabilizes / bids deepen
- Watchlist: property policy headlines; any sharp USD/CNH move can shut risk quickly

Overnight Headlines:
{heads_str}

Desk Takeaways:
1) ECM: {ecm_read}
2) DCM: {ig_read}
3) Stay nimble around CNH + UST volatility; pre-sound and keep execution optionality
"""
    return note


def llm_enhance_note(base_note, mkt, headlines, ecm_score, dcm_state):
    """
    Optional enhancement. If quota error / no key -> return base_note.
    """
    if not OPENAI_API_KEY:
        return base_note

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        prompt = f"""
You are an investment bank Capital Markets desk (ECM + DCM) strategist.

Rewrite and tighten the following note into a ONE-PAGE PDF-friendly format (English).
Keep the same structure but make it sharper, more desk-like, less generic.

Constraints:
- 350–450 words
- HK focus + China + USD funding
- Keep ECM/DCM 50/50 tone
- Use short bullets where helpful
- Avoid long macro explanations

Inputs:
ECM score: {ecm_score}/10
DCM state: {dcm_state}
Headlines: {headlines[:10] if headlines else "N/A"}

Original note:
{base_note}
""".strip()

        resp = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )
        return resp.output_text

    except Exception:
        # quota / 429 / any error -> fallback
        return base_note


def build_pdf(note_text, filename="morning_note.pdf"):
    """
    Simple clean PDF using ReportLab (very stable in GitHub Actions).
    """
    c = canvas.Canvas(filename, pagesize=A4)
    width, height = A4

    title = "HK Capital Markets Morning Note"
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, height - 50, title)

    c.setFont("Helvetica", 10)
    y = height - 80
    max_width = width - 80

    def draw_wrapped(line, y_pos):
        # naive wrap by words
        words = line.split(" ")
        cur = ""
        for w in words:
            test = (cur + " " + w).strip()
            if c.stringWidth(test, "Helvetica", 10) <= max_width:
                cur = test
            else:
                c.drawString(40, y_pos, cur)
                y_pos -= 14
                cur = w
        if cur:
            c.drawString(40, y_pos, cur)
            y_pos -= 14
        return y_pos

    for para in note_text.split("\n"):
        if not para.strip():
            y -= 10
            continue

        y = draw_wrapped(para, y)
        if y < 60:
            c.showPage()
            c.setFont("Helvetica", 10)
            y = height - 60

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

    base = template_note(mkt, headlines, ecm_score, dcm_state)
    final_note = llm_enhance_note(base, mkt, headlines, ecm_score, dcm_state)

    build_pdf(final_note, "morning_note.pdf")
    send_email_with_attachment("morning_note.pdf")


if __name__ == "__main__":
    main()
