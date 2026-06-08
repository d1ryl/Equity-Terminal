"""
Equity Terminal — cloud backend (no gateway, deploy anywhere)
=============================================================
Pure hosted web APIs, so this runs as a Vercel serverless function with no
moomoo OpenD and no always-on computer.

  Prices ......... Stooq daily CSV (keyless) + optional Alpha Vantage fallback
  Fundamentals ... SEC EDGAR (keyless, official)

Run locally:   uvicorn api.index:app --reload --port 8000
Deploy:        push to GitHub -> import on vercel.com (see README.md)
"""

import csv
import io
import os
import datetime as dt

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# SEC requires a descriptive User-Agent with a contact email or it 403s.
# On Vercel, set SEC_EMAIL as an environment variable.
SEC_EMAIL = os.environ.get("SEC_EMAIL", "leedaryl@gmail.com")
SEC_HEADERS = {"User-Agent": f"EquityTerminal/1.0 ({SEC_EMAIL})"}

# Price sources (set whichever keys you have; the app tries them in order).
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_KEY", "")
ALPHAVANTAGE_KEY = os.environ.get("ALPHAVANTAGE_KEY", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")  # for the news endpoint
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")    # optional: analyst price targets (financialmodelingprep.com)

app = FastAPI(title="Equity Terminal — cloud backend", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


def _t(code):
    """'US.NVDA' / 'nvda' -> 'NVDA';  'US.BRK.B' -> 'BRK-B'."""
    parts = code.strip().upper().split(".")
    if parts[0] in ("US", "HK", "SG") and len(parts) > 1:
        parts = parts[1:]
    return "-".join(parts)


# ----------------------------- prices -----------------------------
def _twelvedata(ticker, count, interval="1day"):
    if not TWELVEDATA_KEY:
        return []
    url = ("https://api.twelvedata.com/time_series"
           f"?symbol={ticker}&interval={interval}&outputsize={count}&apikey={TWELVEDATA_KEY}")
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    vals = r.json().get("values")  # error responses have no "values"
    if not vals:
        return []
    bars = []
    for v in reversed(vals):  # API returns newest-first
        try:
            bars.append({
                "date": v["datetime"],
                "open": float(v["open"]), "high": float(v["high"]),
                "low": float(v["low"]), "close": float(v["close"]),
                "volume": int(float(v.get("volume") or 0)),
            })
        except (ValueError, KeyError):
            continue
    return bars[-count:]


def _stooq(ticker, count):
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    text = r.text.strip()
    if not text or text[0] == "<" or "no data" in text.lower():
        return []
    bars = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            bars.append({
                "date": row["Date"],
                "open": float(row["Open"]), "high": float(row["High"]),
                "low": float(row["Low"]), "close": float(row["Close"]),
                "volume": int(float(row.get("Volume") or 0)),
            })
        except (ValueError, KeyError):
            continue
    return bars[-count:]


def _alphavantage(ticker, count):
    if not ALPHAVANTAGE_KEY:
        return []
    url = ("https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
           f"&symbol={ticker}&outputsize=full&apikey={ALPHAVANTAGE_KEY}")
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    series = r.json().get("Time Series (Daily)", {})
    bars = [{
        "date": d,
        "open": float(v["1. open"]), "high": float(v["2. high"]),
        "low": float(v["3. low"]), "close": float(v["4. close"]),
        "volume": int(float(v["5. volume"])),
    } for d, v in sorted(series.items())]
    return bars[-count:]


def _prices(ticker, count, period="1day"):
    """Twelve Data handles any interval; Stooq/Alpha Vantage are daily-only fallbacks."""
    try:
        bars = _twelvedata(ticker, count, period)
        if bars:
            return bars
    except Exception:
        pass
    if period == "1day":
        for fn in (_stooq, _alphavantage):
            try:
                bars = fn(ticker, count)
                if bars:
                    return bars
            except Exception:
                pass
    return []


@app.get("/api/candles")
def candles(code: str = Query(..., description="e.g. US.NVDA or NVDA"),
            count: int = Query(120, ge=10, le=2000),
            period: str = Query("1day", description="1h, 4h, 1day, 1week")):
    t = _t(code)
    bars = _prices(t, count, period)
    if not bars:
        raise HTTPException(404, f"No price data for {t}. US tickers only; "
                                 "set TWELVEDATA_KEY (or ALPHAVANTAGE_KEY) in the backend env.")
    return {"ticker": t, "count": len(bars), "last": bars[-1]["close"], "bars": bars}


@app.get("/api/snapshot")
def snapshot(code: str = Query(..., description="e.g. US.NVDA")):
    """Last price (Stooq) + shares outstanding (SEC EDGAR) — mirrors the
    moomoo backend so the same frontend works against either."""
    t = _t(code)
    last_price = None
    try:
        bars = _prices(t, 1)
        if bars:
            last_price = bars[-1]["close"]
    except Exception:
        pass
    shares = None
    try:
        cik = _cik_map().get(t)
        if cik:
            shares = _latest_shares(cik)
    except Exception:
        pass
    return {"code": t, "last_price": last_price, "issued_shares": shares}


# -------------------------- fundamentals (SEC EDGAR) --------------------------
_CIK_CACHE = {}
OCF_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
]
SBC_TAGS = ["ShareBasedCompensation", "ShareBasedCompensationExpense"]


def _cik_map():
    global _CIK_CACHE
    if _CIK_CACHE:
        return _CIK_CACHE
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers=SEC_HEADERS, timeout=20)
    r.raise_for_status()
    _CIK_CACHE = {row["ticker"].upper(): str(row["cik_str"]).zfill(10)
                  for row in r.json().values()}
    return _CIK_CACHE


def _annual_series(cik, tags, limit=4):
    for tag in tags:
        url = f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
        resp = requests.get(url, headers=SEC_HEADERS, timeout=20)
        if resp.status_code != 200:
            continue
        rows = {}
        for u in resp.json().get("units", {}).get("USD", []):
            if u.get("form") != "10-K":
                continue
            try:
                start = dt.date.fromisoformat(u["start"])
                end = dt.date.fromisoformat(u["end"])
            except Exception:
                continue
            if 350 <= (end - start).days <= 380:
                rows[end] = {"fiscal_year": u.get("fy"),
                             "period_end": end.isoformat(),
                             "value": float(u["val"])}
        if rows:
            return [rows[k] for k in sorted(rows.keys())][-limit:]
    return []


def _latest_shares(cik):
    url = (f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}"
           "/dei/EntityCommonStockSharesOutstanding.json")
    r = requests.get(url, headers=SEC_HEADERS, timeout=20)
    if r.status_code != 200:
        return None
    units = r.json().get("units", {}).get("shares", [])
    if not units:
        return None
    return float(max(units, key=lambda u: u.get("end", ""))["val"])


@app.get("/api/fundamentals")
def fundamentals(code: str = Query(..., description="e.g. US.NVDA"),
                 years: int = Query(3, ge=1, le=6)):
    t = _t(code)
    try:
        cik = _cik_map().get(t)
    except Exception as e:
        raise HTTPException(502, f"SEC ticker map error: {e}")
    if not cik:
        raise HTTPException(404, f"No SEC CIK for {t} — US filers only.")

    ocf = _annual_series(cik, OCF_TAGS, limit=years + 1)
    if not ocf:
        raise HTTPException(404, f"No operating cash flow found for {t}.")
    capex = _annual_series(cik, CAPEX_TAGS, limit=years + 1)
    capex_by_end = {r["period_end"]: r["value"] for r in capex}
    sbc = _annual_series(cik, SBC_TAGS, limit=years + 1)
    sbc_by_end = {r["period_end"]: r["value"] for r in sbc}

    history = []
    for r in ocf:
        cap = capex_by_end.get(r["period_end"])
        sb = sbc_by_end.get(r["period_end"])
        fcf = r["value"] - (cap or 0.0)
        history.append({
            "fiscal_year": r["fiscal_year"],
            "period_end": r["period_end"],
            "operating_cash_flow": r["value"],
            "capex": cap,
            "sbc": sb,
            "free_cash_flow": fcf,
            # SBC is a real economic cost hidden in operating cash flow; subtract it
            # for a more conservative FCF on share-heavy (tech) companies.
            "fcf_ex_sbc": fcf - (sb or 0.0),
        })
    history = history[-years:]
    fcfs = [h["free_cash_flow"] for h in history]
    fcfs_ex = [h["fcf_ex_sbc"] for h in history]

    return {
        "ticker": t,
        "cik": cik,
        "fiscal_year": history[-1]["fiscal_year"],
        "free_cash_flow": history[-1]["free_cash_flow"],
        "fcf_avg": sum(fcfs) / len(fcfs),
        "fcf_ex_sbc_avg": sum(fcfs_ex) / len(fcfs_ex),
        "shares": _latest_shares(cik),
        "history": history,
        "source": "SEC EDGAR (10-K)",
    }


@app.get("/api/news")
def news(code: str = Query(..., description="e.g. US.NVDA"),
         limit: int = Query(12, ge=1, le=50)):
    """Recent company news from Finnhub (needs FINNHUB_KEY)."""
    if not FINNHUB_KEY:
        raise HTTPException(400, "Set FINNHUB_KEY in the backend env to enable news.")
    t = _t(code)
    today = dt.date.today()
    frm = today - dt.timedelta(days=21)
    url = (f"https://finnhub.io/api/v1/company-news?symbol={t}"
           f"&from={frm.isoformat()}&to={today.isoformat()}&token={FINNHUB_KEY}")
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    items = r.json()
    if not isinstance(items, list):
        return {"ticker": t, "news": []}
    out = [{
        "headline": x.get("headline"),
        "summary": x.get("summary"),
        "source": x.get("source"),
        "url": x.get("url"),
        "datetime": x.get("datetime"),  # unix seconds
        "image": x.get("image"),
    } for x in items[:limit]]
    return {"ticker": t, "news": out}


@app.get("/api/analyst")
def analyst(code: str = Query(..., description="e.g. US.NVDA")):
    """Analyst views: recommendation trends (Finnhub, free) + price targets.
    Price targets need FMP_API_KEY (financialmodelingprep.com) or a Finnhub plan
    that includes /stock/price-target. Degrades gracefully when unavailable."""
    t = _t(code)
    recommendation = None
    price_target = None
    notes = []

    # --- recommendation trends (Finnhub free) ---
    if FINNHUB_KEY:
        try:
            url = f"https://finnhub.io/api/v1/stock/recommendation?symbol={t}&token={FINNHUB_KEY}"
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            rows = r.json()
            if isinstance(rows, list) and rows:
                x = rows[0]  # most recent period
                sb = int(x.get("strongBuy", 0) or 0)
                b = int(x.get("buy", 0) or 0)
                h = int(x.get("hold", 0) or 0)
                se = int(x.get("sell", 0) or 0)
                ss = int(x.get("strongSell", 0) or 0)
                total = sb + b + h + se + ss
                score = (2 * sb + b - se - 2 * ss)
                if total == 0:
                    consensus = "No coverage"
                elif score >= total:
                    consensus = "Strong Buy"
                elif score > 0:
                    consensus = "Buy"
                elif score == 0:
                    consensus = "Hold"
                elif score > -total:
                    consensus = "Sell"
                else:
                    consensus = "Strong Sell"
                recommendation = {
                    "period": x.get("period"),
                    "strongBuy": sb, "buy": b, "hold": h, "sell": se, "strongSell": ss,
                    "total": total, "consensus": consensus,
                }
        except Exception as e:
            notes.append("recommendation fetch failed")
    else:
        notes.append("Set FINNHUB_KEY for analyst rating consensus.")

    # --- price target consensus ---
    if FMP_API_KEY:
        try:
            url = (f"https://financialmodelingprep.com/api/v3/price-target-consensus"
                   f"?symbol={t}&apikey={FMP_API_KEY}")
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            j = r.json()
            row = j[0] if isinstance(j, list) and j else (j if isinstance(j, dict) else None)
            if row:
                price_target = {
                    "high": row.get("targetHigh"),
                    "low": row.get("targetLow"),
                    "mean": row.get("targetConsensus"),
                    "median": row.get("targetMedian"),
                    "source": "fmp",
                }
        except Exception:
            notes.append("FMP price-target fetch failed (check FMP_API_KEY / plan).")
    elif FINNHUB_KEY:
        try:
            url = f"https://finnhub.io/api/v1/stock/price-target?symbol={t}&token={FINNHUB_KEY}"
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                j = r.json() or {}
                if j.get("targetMean"):
                    price_target = {
                        "high": j.get("targetHigh"),
                        "low": j.get("targetLow"),
                        "mean": j.get("targetMean"),
                        "median": j.get("targetMedian"),
                        "lastUpdated": j.get("lastUpdated"),
                        "source": "finnhub",
                    }
                else:
                    notes.append("Price targets need FMP_API_KEY or a Finnhub plan that includes price targets.")
            else:
                notes.append("Price targets need FMP_API_KEY or a Finnhub plan that includes price targets.")
        except Exception:
            notes.append("Price target fetch failed.")
    else:
        notes.append("Set FMP_API_KEY for analyst price targets.")

    return {"ticker": t, "recommendation": recommendation,
            "priceTarget": price_target, "notes": notes}


@app.get("/api/health")
def health():
    sources = [s for s, on in [("twelvedata", TWELVEDATA_KEY),
                               ("stooq", True),
                               ("alphavantage", ALPHAVANTAGE_KEY)] if on]
    return {"ok": True, "prices": "+".join(sources),
            "fundamentals": "sec-edgar",
            "news": "finnhub" if FINNHUB_KEY else "off",
            "analyst": ("finnhub-recs" + ("+fmp-targets" if FMP_API_KEY else "")) if FINNHUB_KEY else ("fmp-targets" if FMP_API_KEY else "off")}


@app.get("/")
def root():
    return {"service": "equity-terminal-cloud",
            "endpoints": ["/api/health", "/api/candles", "/api/snapshot",
                          "/api/fundamentals", "/api/news"]}
