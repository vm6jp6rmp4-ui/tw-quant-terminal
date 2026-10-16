#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股選股：MACD 第一根紅柱 + DIF>0 + KD 黃金交叉 + 收盤在布林中線之上 + 外資連續買超

對應使用者原本在 XQ 全球贏家寫的 XScript 選股腳本（不含主力買賣超、收集派發）。

資料來源（皆為免費公開資料）：
  - 股票清單：證交所 / 櫃買中心 OpenAPI
  - 日K：Yahoo Finance chart API
  - 外資買賣超：上市用證交所 T86；上櫃用 FinMind（逐檔查詢，只查技術面通過的候選股）

用法：
  python screen.py                     # 預設參數
  python screen.py --kd-within 3       # KD 在最近 3 天內黃金交叉就算
  python screen.py --days 1            # 外資只要最近 1 天買超
  python screen.py --min-lots 500      # 只掃前一交易日成交量 >= 500 張的股票（較快）
  python screen.py --json              # 輸出 JSON
  python screen.py --intraday          # 盤中模式：用目前成交價當今天的K棒；外資用到前一交易日
進度訊息輸出到 stderr，結果輸出到 stdout。
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

TW_TZ = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
STOCK_CODE = re.compile(r"^[1-9]\d{3}$")  # 4 碼普通股；排除 00 開頭的 ETF、權證等


def log(*msg):
    print(*msg, file=sys.stderr, flush=True)


def get_json(url, tries=4, timeout=30, **kw):
    last = None
    headers = {**UA, **kw.pop("headers", {})}
    for i in range(tries):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, **kw)
            if r.status_code == 429:
                last = "HTTP 429 (rate limited)"
                time.sleep(3 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"讀取失敗 {url}: {last}")


def to_float(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return np.nan


def roc_to_date(s):
    """'1151002' -> date(2026, 10, 2)"""
    s = str(s).strip()
    return date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))


# ---------------------------------------------------------------- 股票清單
# 證交所／櫃買中心最新一天的收盤資料。Yahoo 日K偶爾漏掉前一交易日的K棒，用這份補上
LAST_CLOSE = {}


def remember_last_close(ticker, date, o, h, l, c, v):
    try:
        d = roc_to_date(date)
    except Exception:
        return
    vals = [to_float(x) for x in (o, h, l, c, v)]
    if not any(np.isnan(x) for x in vals[:4]):
        LAST_CLOSE[ticker] = (d, vals)


def load_universe(min_lots):
    stocks = []

    def keep(code, shares):
        if not STOCK_CODE.match(code):
            return False
        if min_lots <= 0:
            return True
        v = to_float(shares)
        return not np.isnan(v) and v / 1000 >= min_lots

    try:
        twse = get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", tries=2, timeout=60)
    except RuntimeError as e:
        # openapi.twse.com.tw 有時被證交所防火牆擋（回傳 HTML）；改用 www.twse.com.tw 的 CSV 版本
        log(f"證交所 OpenAPI 失敗，改用 CSV：{e}")
        import csv, io
        for i in range(4):  # 連線偶爾被重置，重試幾次
            try:
                r = requests.get("https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL",
                                 params={"response": "open_data"}, headers=UA, timeout=60)
                r.raise_for_status()
                break
            except requests.RequestException:
                if i == 3:
                    raise
                time.sleep(3 * (i + 1))
        rd = csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))
        twse = [{"Date": x["日期"], "Code": x["證券代號"], "Name": x["證券名稱"],
                 "TradeVolume": x["成交股數"], "OpeningPrice": x["開盤價"], "HighestPrice": x["最高價"],
                 "LowestPrice": x["最低價"], "ClosingPrice": x["收盤價"]} for x in rd]
    for r in twse:
        code = str(r.get("Code", "")).strip()
        if keep(code, r.get("TradeVolume")):
            stocks.append({"code": code, "name": str(r.get("Name", "")).strip(),
                           "market": "上市", "yahoo": f"{code}.TW"})
            remember_last_close(f"{code}.TW", r.get("Date"), r.get("OpeningPrice"), r.get("HighestPrice"),
                                r.get("LowestPrice"), r.get("ClosingPrice"), r.get("TradeVolume"))

    tpex = get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=90)
    for r in tpex:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        if keep(code, r.get("TradingShares")):
            stocks.append({"code": code, "name": str(r.get("CompanyName", "")).strip(),
                           "market": "上櫃", "yahoo": f"{code}.TWO"})
            remember_last_close(f"{code}.TWO", r.get("Date"), r.get("Open"), r.get("High"),
                                r.get("Low"), r.get("Close"), r.get("TradingShares"))

    list_date = roc_to_date(twse[0]["Date"])
    return stocks, list_date


# ---------------------------------------------------------------- 日K
def tw_now():
    return datetime.now(TW_TZ)


def fetch_history(ticker, intraday=False):
    """回傳日K DataFrame。intraday=True 時回傳 (df, 報價時間)，並用 Yahoo 的即時報價
    （regularMarketPrice / DayHigh / DayLow / Volume）覆寫今天這根還沒收完的K棒。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    data = get_json(url, params={"range": "1y", "interval": "1d"})
    res = (data.get("chart") or {}).get("result")
    if not res:
        return None
    res = res[0]
    ts = res.get("timestamp")
    if not ts:
        return None
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame(
        {"Open": q.get("open"), "High": q.get("high"), "Low": q.get("low"),
         "Close": q.get("close"), "Volume": q.get("volume")},
        index=[datetime.fromtimestamp(t, TW_TZ).date() for t in ts],
    )
    df = df.dropna(subset=["Close", "High", "Low"])
    df = df[~df.index.duplicated(keep="last")]
    lc = LAST_CLOSE.get(ticker)
    if lc and lc[0] not in df.index and len(df) and df.index[0] < lc[0]:
        df.loc[lc[0]] = lc[1]
        df = df.sort_index()
    if not intraday:
        return df

    m = res.get("meta", {})
    price, mt = m.get("regularMarketPrice"), m.get("regularMarketTime")
    if price is None or mt is None:
        return df, None
    qt = datetime.fromtimestamp(mt, TW_TZ)
    d = qt.date()
    old = df.loc[d] if d in df.index else None
    hi = max(x for x in [m.get("regularMarketDayHigh"), price,
                         None if old is None else old["High"]] if x is not None and not pd.isna(x))
    lo = min(x for x in [m.get("regularMarketDayLow"), price,
                         None if old is None else old["Low"]] if x is not None and not pd.isna(x))
    op = old["Open"] if old is not None and not pd.isna(old["Open"]) else price
    vol = m.get("regularMarketVolume")
    if vol is None and old is not None:
        vol = old["Volume"]
    df.loc[d] = [op, hi, lo, float(price), vol]
    df = df.sort_index()
    return df, qt


# ---------------------------------------------------------------- 指標（對齊 XQ 算法）
def indicators(df, a):
    c, h, lo = df["Close"], df["High"], df["Low"]

    def ema(s, n):  # XQ XAverage：第一筆為種子，alpha = 2/(n+1)
        return s.ewm(span=n, adjust=False).mean()

    wc = (h + lo + 2 * c) / 4  # XQ weightedclose()
    dif = ema(wc, a.fast) - ema(wc, a.slow)
    macd = ema(dif, a.signal)
    osc = dif - macd

    hh = h.rolling(a.kd_len, min_periods=1).max()
    ll = lo.rolling(a.kd_len, min_periods=1).min()
    rsv = ((c - ll) / (hh - ll) * 100).where(hh > ll, 50.0).to_numpy()
    k = np.empty(len(rsv))
    d = np.empty(len(rsv))
    pk = pdv = 50.0
    for i, r in enumerate(rsv):
        pk += (r - pk) / a.k_smooth
        pdv += (pk - pdv) / a.d_smooth
        k[i], d[i] = pk, pdv
    k = pd.Series(k, index=c.index)
    d = pd.Series(d, index=c.index)

    bb_mid = c.rolling(a.bb_len).mean()
    return dif, macd, osc, k, d, bb_mid


def fetch_realtime_quotes(symbols, batch=50):
    """Yahoo 股市（台灣版）即時報價，回傳 {yahoo 代號: {price, high, low, open, volume, time}}。
    國際版 query1 的台股報價約延遲 20 分鐘，盤中模式優先用這個；抓不到的再退回 query1。"""
    base = "https://tw.stock.yahoo.com/_td-stock/api/resource/StockServices.stockList;autoRefresh=1;symbols="
    out = {}

    def raw(x):
        v = x.get("raw") if isinstance(x, dict) else x
        return to_float(v)

    for i in range(0, len(symbols), batch):
        chunk = symbols[i:i + batch]
        try:
            j = get_json(base + "%2C".join(chunk), tries=3)
        except RuntimeError as e:
            log(f"即時報價批次失敗（{chunk[0]} 起 {len(chunk)} 檔），改用延遲報價：{e}")
            continue
        for q in j if isinstance(j, list) else []:
            price = raw(q.get("price"))
            t = q.get("regularMarketTime")
            if np.isnan(price) or not t:
                continue
            out[q["symbol"]] = {
                "price": price, "high": raw(q.get("regularMarketDayHigh")),
                "low": raw(q.get("regularMarketDayLow")), "open": raw(q.get("regularMarketOpen")),
                "volume": to_float(q.get("volume")),
                "time": datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(TW_TZ),
            }
    return out


def apply_realtime(df, rt):
    """用即時報價覆寫（或新增）今天的K棒"""
    d = rt["time"].date()
    p = rt["price"]
    hi = rt["high"] if not np.isnan(rt["high"]) else p
    lo = rt["low"] if not np.isnan(rt["low"]) else p
    op = rt["open"] if not np.isnan(rt["open"]) else p
    df.loc[d] = [op, max(hi, p), min(lo, p), p, rt["volume"]]
    return df.sort_index()


REALTIME = {}


def check_technical(stock, a, as_of):
    qt = None
    if a.intraday:
        got = fetch_history(stock["yahoo"], intraday=True)
        if got is None:
            return None
        df, qt = got
        rt = REALTIME.get(stock["yahoo"])
        if rt and rt["time"].date() == as_of:
            df, qt = apply_realtime(df, rt), rt["time"]
    else:
        df = fetch_history(stock["yahoo"])
    if df is None:
        return None
    df = df[df.index <= as_of]
    if len(df) < a.bb_len + 5 or df.index[-1] != as_of:
        return None  # 資料不足或當天沒交易（停牌等）

    dif, macd, osc, k, d, mid = indicators(df, a)
    cross = (k > d) & (k.shift(1) <= d.shift(1))
    close = float(df["Close"].iloc[-1])
    return {
        **stock,
        "close": round(close, 2),
        "quote_time": qt.strftime("%H:%M") if qt else None,
        "day_high": round(float(df["High"].iloc[-1]), 2),
        "day_low": round(float(df["Low"].iloc[-1]), 2),
        "volume_lots": int(np.nan_to_num(df["Volume"].iloc[-1]) / 1000),
        "dif": round(float(dif.iloc[-1]), 3),
        "macd": round(float(macd.iloc[-1]), 3),
        "osc": round(float(osc.iloc[-1]), 3),
        "k": round(float(k.iloc[-1]), 2),
        "d": round(float(d.iloc[-1]), 2),
        "bb_mid": round(float(mid.iloc[-1]), 2),
        "macd_first_red": bool(osc.iloc[-1] > 0 and osc.iloc[-2] <= 0),
        "dif_pos": bool(dif.iloc[-1] > 0),
        "kd_cross": bool(cross.iloc[-a.kd_within:].any()),
        "above_mid": bool(close > mid.iloc[-1]),
    }


# ---------------------------------------------------------------- 外資買賣超
def twse_foreign(d):
    """證交所 T86：回傳 {代號: 外陸資買賣超股數(不含外資自營商)}；尚未公布回傳 None
    已公布的日期存一份在 cache/T86_YYYYMMDD.json，盤中每小時重跑時不用再向證交所要（證交所常擋重複請求）"""
    cache = Path(__file__).resolve().parent / "cache" / f"T86_{d.strftime('%Y%m%d')}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    j = get_json("https://www.twse.com.tw/rwd/zh/fund/T86",
                 params={"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"})
    if j.get("stat") != "OK" or not j.get("data"):
        return None
    fields = j.get("fields", [])
    idx = next((i for i, f in enumerate(fields) if f.startswith("外陸資買賣超股數")), 4)
    out = {row[0].strip(): to_float(row[idx]) for row in j["data"]}
    try:
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(out))
    except OSError:
        pass
    return out


def finmind_foreign(code, start, end):
    """FinMind：回傳 {日期: 外資(不含自營商)買賣超股數}"""
    headers = {}
    if os.environ.get("FINMIND_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['FINMIND_TOKEN']}"
    j = get_json("https://api.finmindtrade.com/api/v4/data", headers=headers, params={
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": code, "start_date": start.isoformat(), "end_date": end.isoformat()})
    if j.get("status") != 200:
        raise RuntimeError(j.get("msg"))
    return {date.fromisoformat(r["date"]): r["buy"] - r["sell"]
            for r in j.get("data", []) if r.get("name") == "Foreign_Investor"}


# ---------------------------------------------------------------- 券商分點
class BrokerAccessError(RuntimeError):
    """FinMind 分點資料需要贊助會員，沒有權限時整個條件改為不套用"""


_BROKER_DAY = {}


def finmind_broker_day(code, d):
    """FinMind TaiwanStockTradingDailyReport 單日分點：回傳 ({分點: 淨買賣股數}, 當天總成交股數)。
    同一檔同一天只查一次（分點家數和集中度共用）"""
    if (code, d) in _BROKER_DAY:
        return _BROKER_DAY[(code, d)]
    headers = {}
    if os.environ.get("FINMIND_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['FINMIND_TOKEN']}"
    # 分點資料表走專屬 endpoint（不走 /api/v4/data），一次只能查一天
    r = requests.get("https://api.finmindtrade.com/api/v4/taiwan_stock_trading_daily_report",
                     headers={**UA, **headers}, timeout=30,
                     params={"data_id": code, "date": d.isoformat()})
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"FinMind HTTP {r.status_code}") from None
    if j.get("status") != 200:
        msg = str(j.get("msg") or j.get("detail") or j)
        if "level" in msg.lower() or "sponsor" in msg.lower() or j.get("status") in (401, 402, 403):
            raise BrokerAccessError(msg)
        raise RuntimeError(msg)
    rows = j.get("data", [])
    if not rows:
        raise RuntimeError(f"{d} 沒有分點資料")
    net, total = {}, 0.0
    for x in rows:
        k = x.get("securities_trader_id") or x.get("securities_trader")
        b = to_float(x.get("buy"))
        net[k] = net.get(k, 0) + b - to_float(x.get("sell"))
        total += b  # 每筆成交都有一個買方，所以全部分點買進股數合計 = 總成交股數
    time.sleep(0.3)
    _BROKER_DAY[(code, d)] = (net, total)
    return net, total


def broker_net(code, dates):
    net, total = {}, 0.0
    for d in dates:
        n, t = finmind_broker_day(code, d)
        total += t
        for k, v in n.items():
            net[k] = net.get(k, 0) + v
    return net, total


def finmind_broker_counts(code, dates):
    """把每家券商分點在這幾天的買賣股數加總，回傳 (淨買超家數, 淨賣超家數)"""
    net, _ = broker_net(code, dates)
    return sum(1 for v in net.values() if v > 0), sum(1 for v in net.values() if v < 0)


def chip_concentration(code, dates, top=15):
    """整體籌碼集中度(%) = (前15大買超分點買超股數合計 − 前15大賣超分點賣超股數合計) ÷ 區間總成交股數 × 100"""
    net, total = broker_net(code, dates)
    if total <= 0:
        raise RuntimeError("區間沒有成交量")
    vals = sorted(net.values())
    buy = sum(v for v in vals[::-1][:top] if v > 0)
    sell = -sum(v for v in vals[:top] if v < 0)
    return round((buy - sell) / total * 100, 1)


# ---------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser(description="台股 MACD/KD/布林/外資/券商分點/籌碼集中度 選股")
    ap.add_argument("--fast", type=int, default=12, help="DIF 短期期數")
    ap.add_argument("--slow", type=int, default=26, help="DIF 長期期數")
    ap.add_argument("--signal", type=int, default=9, help="MACD 期數")
    ap.add_argument("--kd-len", type=int, default=26, help="KD 期數")
    ap.add_argument("--k-smooth", type=int, default=3, help="K 平滑")
    ap.add_argument("--d-smooth", type=int, default=3, help="D 平滑")
    ap.add_argument("--kd-within", type=int, default=1, help="KD 黃金交叉在最近幾天內")
    ap.add_argument("--bb-len", type=int, default=50, help="布林通道期數（中線 = N 日均線）")
    ap.add_argument("--days", type=int, default=2, help="外資連續買超天數（0 = 不看外資）")
    ap.add_argument("--min-lots", type=float, default=0, help="前一交易日成交量至少幾張（預先過濾）")
    ap.add_argument("--workers", type=int, default=8, help="同時下載的連線數")
    ap.add_argument("--broker-days", type=int, default=None,
                    help="券商分點：最近 N 個交易日買超家數 < 賣超家數（0 = 不看；預設收盤模式 3、盤中模式 0）")
    ap.add_argument("--conc-days", type=int, default=None,
                    help="整體籌碼集中度：看最近 N 日（0 = 不看；預設收盤模式 20、盤中模式 0）")
    ap.add_argument("--conc-min", type=float, default=5, help="N 日籌碼集中度至少幾 %%")
    ap.add_argument("--conc-short-days", type=int, default=5,
                    help="另外看最近 M 日籌碼集中度（0 = 不看）")
    ap.add_argument("--conc-short-min", type=float, default=10, help="M 日籌碼集中度要大於幾 %%")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--intraday", action="store_true",
                    help="盤中模式：用目前成交價當今天的K棒算指標；外資買超用到前一交易日")
    a = ap.parse_args()
    if a.broker_days is None:
        a.broker_days = 0 if a.intraday else 3
    if a.conc_days is None:
        a.conc_days = 0 if a.intraday else 20

    t0 = time.time()
    stocks, list_date = load_universe(a.min_lots)
    log(f"股票清單：{len(stocks)} 檔（上市＋上櫃普通股），最新收盤日 {list_date}")
    notes = []
    t86 = {}

    if a.intraday:
        # 盤中：今天的K棒用即時報價；外資資料收盤後才公布，所以用到前一交易日
        got = fetch_history("2330.TW", intraday=True)
        if not got or got[1] is None:
            sys.exit("無法取得台積電即時報價，無法判斷今天是否開盤")
        ref_df, ref_qt = got
        today = tw_now().date()
        log("下載即時報價（Yahoo 股市台灣版）…")
        REALTIME.update(fetch_realtime_quotes([s["yahoo"] for s in stocks] + ["2330.TW"]))
        if "2330.TW" in REALTIME and REALTIME["2330.TW"]["time"].date() == today:
            ref_qt = REALTIME["2330.TW"]["time"]
        n_rt = sum(1 for s in stocks if s["yahoo"] in REALTIME and REALTIME[s["yahoo"]]["time"].date() == today)
        log(f"  即時報價：{n_rt}/{len(stocks)} 檔")
        if ref_qt.date() != today:
            sys.exit(f"今天（{today}）還沒有盤中報價：可能是假日或還沒開盤。"
                     f"最近一筆報價是 {ref_qt:%Y-%m-%d %H:%M}，請改用一般模式（不加 --intraday）。")
        as_of = today
        cal = [d for d in ref_df.index if d < today]
        src = (f"Yahoo 股市即時報價（{n_rt}/{len(stocks)} 檔；其餘用延遲約 20 分鐘的報價）"
               if n_rt else "Yahoo 國際版報價，約延遲 20 分鐘")
        notes.append(f"盤中模式：技術指標用 {ref_qt:%H:%M} 左右的成交價當今天的K棒計算（{src}），"
                     f"收盤前結果仍可能改變")
        if a.days > 0:
            notes.append(f"外資買賣超當天收盤後才公布，盤中改看到前一交易日 {cal[-1]} 為止的連續買超")
        foreign_dates = cal[-a.days:] if a.days > 0 else []
    else:
        # 交易日曆：用台積電的日K
        cal = [d for d in fetch_history("2330.TW").index if d <= list_date]
        as_of = cal[-1]

    # 外資資料要等收盤後才公布；當天還沒公布就整個改用前一交易日
    if a.days > 0 and not a.intraday:
        f = twse_foreign(as_of)
        if f is None:
            notes.append(f"{as_of} 的三大法人資料尚未公布，改用前一交易日 {cal[-2]} 的資料選股")
            cal = cal[:-1]
            as_of = cal[-1]
            time.sleep(3)
            f = twse_foreign(as_of)
            if f is None:
                sys.exit("無法取得證交所三大法人資料（T86）")
        t86[as_of] = f
    if not a.intraday:
        foreign_dates = cal[-a.days:] if a.days > 0 else []

    # ---- 技術面
    log(f"下載日K並計算指標（資料日期 {as_of}）…")
    rows, failed = [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(check_technical, s, a, as_of): s for s in stocks}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
                if r:
                    rows.append(r)
            except Exception:  # noqa: BLE001
                failed += 1
            if n % 200 == 0:
                log(f"  {n}/{len(stocks)}")

    funnel = [("有完整日K資料", len(rows))]
    cand = [r for r in rows if r["macd_first_red"]]
    funnel.append(("MACD 第一根紅柱", len(cand)))
    cand = [r for r in cand if r["dif_pos"]]
    funnel.append(("DIF > 0", len(cand)))
    cand = [r for r in cand if r["kd_cross"]]
    funnel.append((f"KD 黃金交叉（最近 {a.kd_within} 天內）", len(cand)))
    cand = [r for r in cand if r["above_mid"]]
    price_word = "現價" if a.intraday else "收盤"
    funnel.append((f"{price_word}在布林中線（{a.bb_len}MA）之上", len(cand)))

    # ---- 外資（只查技術面通過的候選股）
    unknown = []
    if a.days > 0:
        for d in foreign_dates:
            if d not in t86:
                time.sleep(3)  # 證交所有頻率限制
                try:
                    t86[d] = twse_foreign(d) or {}
                except RuntimeError as e:  # 證交所擋連線時，上市股改用 FinMind 逐檔查
                    log(f"  T86 {d} 讀取失敗，上市股改用 FinMind：{e}")
                    t86[d] = None
        if any(t86[d] is None for d in foreign_dates):
            notes.append("證交所三大法人資料（T86）這次連不上，上市股的外資買賣超改用 FinMind 查詢")
        passed = []
        for r in cand:
            try:
                if r["market"] == "上市" and all(t86[d] is not None for d in foreign_dates):
                    nets = [t86[d].get(r["code"], 0.0) for d in foreign_dates]
                else:
                    fm = finmind_foreign(r["code"], foreign_dates[0] - timedelta(days=7), foreign_dates[-1])
                    nets = [fm.get(d, 0.0) for d in foreign_dates]
                    time.sleep(0.3)
            except Exception as e:  # noqa: BLE001
                r["foreign_error"] = str(e)
                unknown.append(r)
                continue
            r["foreign_lots"] = [int(round(v / 1000)) for v in nets]
            if all(v > 0 for v in nets):
                passed.append(r)
        cand = passed
        funnel.append((f"外資連續 {a.days} 天買超", len(cand)))

    # ---- 券商分點（只查前面全部通過的候選股；盤中用到前一交易日）
    broker_dates = []
    if a.broker_days > 0:
        bcal = [d for d in cal if d < as_of] if a.intraday else [d for d in cal if d <= as_of]
        broker_dates = bcal[-a.broker_days:]
        label = f"券商分點：近 {a.broker_days} 日買超家數 < 賣超家數"
        passed, denied = [], None
        for r in cand:
            if denied:
                break
            try:
                b, s_ = finmind_broker_counts(r["code"], broker_dates)
            except BrokerAccessError as e:
                denied = str(e)
                break
            except Exception as e:  # noqa: BLE001
                r["broker_error"] = str(e)
                unknown.append(r)
                continue
            r["broker_buy"], r["broker_sell"] = b, s_
            if b < s_:
                passed.append(r)
        if denied:
            notes.append(f"券商分點條件未套用：FinMind 分點資料需要贊助會員（{denied}）")
            funnel.append((label + "（未套用：無權限）", len(cand)))
        else:
            cand = passed
            funnel.append((label, len(cand)))

    # ---- 整體籌碼集中度（同一份分點資料）
    conc_dates = []
    if a.conc_days > 0:
        ccal = [d for d in cal if d < as_of] if a.intraday else [d for d in cal if d <= as_of]
        conc_dates = ccal[-a.conc_days:]
        short_dates = ccal[-a.conc_short_days:] if a.conc_short_days > 0 else []
        label = f"籌碼集中度：近 {a.conc_days} 日 ≥ {a.conc_min:g}%"
        if short_dates:
            label += f"，近 {a.conc_short_days} 日 > {a.conc_short_min:g}%"
        passed, denied = [], None
        for r in cand:
            try:
                c = chip_concentration(r["code"], conc_dates)
                cs = chip_concentration(r["code"], short_dates) if short_dates else None
            except BrokerAccessError as e:
                denied = str(e)
                break
            except Exception as e:  # noqa: BLE001
                r["broker_error"] = str(e)
                unknown.append(r)
                continue
            r["conc"], r["conc_short"] = c, cs
            if c >= a.conc_min and (cs is None or cs > a.conc_short_min):
                passed.append(r)
        if denied:
            if not any("分點資料需要贊助會員" in n for n in notes):
                notes.append(f"籌碼集中度條件未套用：FinMind 分點資料需要贊助會員（{denied}）")
            funnel.append((label + "（未套用：無權限）", len(cand)))
        else:
            cand = passed
            funnel.append((label, len(cand)))

    cand.sort(key=lambda r: r["dif"], reverse=True)
    result = {
        "as_of": as_of.isoformat(),
        "intraday": a.intraday,
        "foreign_dates": [d.isoformat() for d in foreign_dates],
        "broker_dates": [d.isoformat() for d in broker_dates],
        "conc_dates": [d.isoformat() for d in conc_dates],
        "params": vars(a),
        "funnel": funnel,
        "scanned": len(stocks),
        "download_failed": failed,
        "notes": notes,
        "matches": cand,
        "foreign_unknown": unknown,
        "elapsed_sec": round(time.time() - t0, 1),
    }

    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    title = "盤中選股結果" if a.intraday else "選股結果"
    print(f"# {title}（資料日期 {as_of}）\n")
    for n in notes:
        print(f"> {n}\n")
    print("## 篩選漏斗\n")
    print(f"- 掃描股票：{len(stocks)} 檔（下載失敗 {failed} 檔）")
    for name, cnt in funnel:
        print(f"- {name}：{cnt} 檔")
    print()
    if cand:
        fd = "／".join(d.strftime("%m/%d") for d in foreign_dates)
        print("## 符合全部條件\n")
        qcol = "| 報價時間 " if a.intraday else ""
        bcol = "| 分點買/賣家數 " if any("broker_buy" in r for r in cand) else ""
        ccol = f"| 集中度% {a.conc_days}日/{a.conc_short_days}日 " if any("conc" in r for r in cand) else ""
        print(f"| 代號 | 名稱 | 市場 | {price_word} {qcol}| 成交量(張) | DIF | OSC | K | D | 布林中線 | 外資買賣超(張) {fd} {bcol}{ccol}|")
        print("|---|---|---|---|" + ("---|" if a.intraday else "") + "---|---|---|---|---|---|---|" + ("---|" if bcol else "") + ("---|" if ccol else ""))
        for r in cand:
            fl = "／".join(f"{v:+,}" for v in r.get("foreign_lots", []))
            qv = f"| {r['quote_time']} " if a.intraday else ""
            bv = f"| {r['broker_buy']}/{r['broker_sell']} " if bcol else ""
            if ccol:
                bv += f"| {r['conc']}/{'' if r['conc_short'] is None else r['conc_short']} "
            print(f"| {r['code']} | {r['name']} | {r['market']} | {r['close']} {qv}| {r['volume_lots']:,} | "
                  f"{r['dif']} | {r['osc']} | {r['k']} | {r['d']} | {r['bb_mid']} | {fl} {bv}|")
    else:
        print("## 沒有股票符合全部條件")
    if unknown:
        print("\n## 前面條件符合，但外資或分點資料查詢失敗\n")
        for r in unknown:
            print(f"- {r['code']} {r['name']}（{r['market']}）：{r.get('foreign_error') or r.get('broker_error')}")
    print(f"\n（耗時 {result['elapsed_sec']} 秒）")


if __name__ == "__main__":
    main()
