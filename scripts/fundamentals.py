#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
個股基本面：最近 3 個月營收 YoY、最近 3 季單季 EPS（資料來源 FinMind）

用法：python fundamentals.py 7717 6194 [--json]
"""
import json
import os
import sys
from datetime import date, timedelta

import requests

API = "https://api.finmindtrade.com/api/v4/data"


def fm(dataset, code, start):
    headers = {}
    if os.environ.get("FINMIND_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['FINMIND_TOKEN']}"
    r = requests.get(API, headers=headers, timeout=30,
                     params={"dataset": dataset, "data_id": code, "start_date": start})
    j = r.json()
    if j.get("status") != 200:
        raise RuntimeError(j.get("msg"))
    return j.get("data", [])


_TPEX = None


def tpex_latest(code):
    """櫃買中心公開資料：上櫃公司最新一個月營收（含官方 YoY）。上市公司不在這份資料裡。"""
    global _TPEX
    if _TPEX is None:
        try:
            r = requests.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
            _TPEX = {x["公司代號"]: x for x in r.json()}
        except Exception:  # noqa: BLE001
            _TPEX = {}
    x = _TPEX.get(code)
    if not x:
        return None
    ym = x["資料年月"]  # 民國年月，例如 11508
    y, m = int(ym[:-2]) + 1911, int(ym[-2:])
    return {"month": f"{y}/{m:02d}", "revenue_k": int(float(x["營業收入-當月營收"])),
            "yoy_pct": round(float(x["營業收入-去年同月增減(%)"]), 1), "source": "櫃買中心"}


def revenue_yoy(code, months=3):
    rows = fm("TaiwanStockMonthRevenue", code, (date.today() - timedelta(days=500)).isoformat())
    rev = {(r["revenue_year"], r["revenue_month"]): r["revenue"] for r in rows}
    out = []
    for (y, m) in sorted(rev)[-months:][::-1]:
        prev = rev.get((y - 1, m))
        yoy = round((rev[(y, m)] / prev - 1) * 100, 1) if prev else None
        out.append({"month": f"{y}/{m:02d}", "revenue_k": round(rev[(y, m)] / 1000), "yoy_pct": yoy})
    # FinMind 缺去年資料（例如新上櫃）時，最新一個月改用櫃買中心的官方年增率
    t = tpex_latest(code)
    if t:
        hit = [r for r in out if r["month"] == t["month"]]
        if hit and hit[0]["yoy_pct"] is None:
            hit[0].update(t)
        elif not hit:
            out = [t] + out[:months - 1]
    return out


def quarterly_eps(code, quarters=3):
    rows = fm("TaiwanStockFinancialStatements", code, (date.today() - timedelta(days=500)).isoformat())
    eps = {r["date"]: r["value"] for r in rows if r.get("type") == "EPS"}
    out = []
    for d in sorted(eps)[-quarters:][::-1]:
        y, m = int(d[:4]), int(d[5:7])
        out.append({"quarter": f"{y}Q{(m - 1) // 3 + 1}", "eps": eps[d]})
    return out


def main():
    codes = [a for a in sys.argv[1:] if not a.startswith("--")]
    res = {}
    for c in codes:
        item = {}
        for k, f in (("revenue", revenue_yoy), ("eps", quarterly_eps)):
            try:
                item[k] = f(c)
            except Exception as e:  # noqa: BLE001
                item[k + "_error"] = str(e)
        res[c] = item
    if "--json" in sys.argv:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return
    for c, it in res.items():
        print(f"## {c}")
        for r in it.get("revenue", []):
            yoy = "無去年同期資料" if r["yoy_pct"] is None else f"{r['yoy_pct']:+.1f}%"
            print(f"- {r['month']} 營收 {r['revenue_k']:,} 千元，YoY {yoy}")
        for q in it.get("eps", []):
            print(f"- {q['quarter']} EPS {q['eps']} 元")
        for k in ("revenue_error", "eps_error"):
            if k in it:
                print(f"- {k}: {it[k]}")


if __name__ == "__main__":
    main()
