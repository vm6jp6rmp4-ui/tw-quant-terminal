#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股開盤每小時自動選股與基本面補充資料整合腳本 (run_hourly.py)
- 盤中時段 (台灣時間 09:00 ~ 13:30)：自動以 `--intraday --json` 執行 `screen.py`
- 收盤與盤後時段 (13:31 以後)：自動以收盤模式 `--json` 執行 `screen.py`
- 對篩選出的每檔股票自動呼叫 `fundamentals.py` 補充「近 3 個月營收 YoY」與「近 3 季單季 EPS」
- 將結果寫入：
  1. data/latest_scan.json (供前端網頁即時讀取)
  2. data/history/scan_YYYYMMDD_HHMM.json (保留當日每小時歷史快照)
  3. data/hourly_index.json (紀錄當日所有時段快照清單與漏斗摘要)
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TW_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"


def is_intraday_time(now_tw: datetime) -> bool:
    """判斷目前是否為台股盤中交易時段 (09:00 ~ 13:30)"""
    hm = now_tw.hour * 100 + now_tw.minute
    return 900 <= hm <= 1330


def run_screener(intraday: bool, extra_args: list[str]) -> dict:
    cmd = [sys.executable, str(SCRIPTS_DIR / "screen.py"), "--json"]
    if intraday:
        cmd.append("--intraday")
    cmd.extend(extra_args)
    print(f"[run_hourly] 執行選股腳本: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0:
        # 若為假日或尚未開盤導致 --intraday 退出，自動退回一般收盤模式重跑
        if intraday:
            print("[run_hourly] 盤中模式未取得今日即時報價，自動切換為最近收盤日模式...", file=sys.stderr)
            fallback_cmd = [c for c in cmd if c != "--intraday"]
            proc = subprocess.run(fallback_cmd, capture_output=True, text=True)
            if proc.stderr:
                print(proc.stderr, file=sys.stderr)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"screen.py 執行失敗 (exit={proc.returncode})")
    return json.loads(proc.stdout)


def enrich_fundamentals(matches: list[dict]) -> None:
    """呼叫 fundamentals.py 為符合條件的個股加上近 3 月營收 YoY 與近 3 季 EPS"""
    codes = [m["code"] for m in matches if "code" in m]
    if not codes:
        return
    cmd = [sys.executable, str(SCRIPTS_DIR / "fundamentals.py"), *codes, "--json"]
    print(f"[run_hourly] 查詢候選股基本面資料 ({len(codes)} 檔)...", file=sys.stderr)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        print(f"[run_hourly] 基本面查詢略過: {proc.stderr}", file=sys.stderr)
        return
    try:
        fund_map = json.loads(proc.stdout)
        for m in matches:
            c = m.get("code")
            if c in fund_map:
                m["fundamentals"] = fund_map[c]
    except Exception as exc:
        print(f"[run_hourly] 解析基本面 JSON 失敗: {exc}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="台股每小時選股排程執行器")
    ap.add_argument("--force-intraday", action="store_true", help="強制使用盤中模式")
    ap.add_argument("--force-close", action="store_true", help="強制使用收盤模式")
    args, unknown = ap.parse_known_args()

    now_tw = datetime.now(TW_TZ)
    if args.force_intraday:
        intraday = True
    elif args.force_close:
        intraday = False
    else:
        intraday = is_intraday_time(now_tw)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    result = run_screener(intraday=intraday, extra_args=unknown)
    enrich_fundamentals(result.get("matches", []))

    slot_id = now_tw.strftime("%Y%m%d_%H%M")
    slot_label = now_tw.strftime("%H:%M") + (" 盤中快篩" if result.get("intraday") else " 收盤定稿")
    result["generated_at_tw"] = now_tw.strftime("%Y-%m-%d %H:%M:%S")
    result["slot_id"] = slot_id
    result["slot_label"] = slot_label

    # 1. 儲存歷史快照
    snap_path = HISTORY_DIR / f"scan_{slot_id}.json"
    snap_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2. 更新最新結果
    latest_path = DATA_DIR / "latest_scan.json"
    latest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 3. 更新 hourly_index.json (保留最近 12 次執行紀錄供前端切換時段)
    index_path = DATA_DIR / "hourly_index.json"
    history_list = []
    if index_path.exists():
        try:
            history_list = json.loads(index_path.read_text(encoding="utf-8")).get("runs", [])
        except Exception:
            history_list = []

    summary_entry = {
        "slot_id": slot_id,
        "slot_label": slot_label,
        "generated_at_tw": result["generated_at_tw"],
        "as_of": result.get("as_of"),
        "intraday": result.get("intraday", False),
        "scanned": result.get("scanned", 0),
        "match_count": len(result.get("matches", [])),
        "elapsed_sec": result.get("elapsed_sec", 0),
        "file": f"data/history/scan_{slot_id}.json",
    }
    history_list = [h for h in history_list if h.get("slot_id") != slot_id]
    history_list.insert(0, summary_entry)
    history_list = history_list[:12]

    index_path.write_text(
        json.dumps({"updated_at_tw": result["generated_at_tw"], "runs": history_list}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[run_hourly] 完成！已更新 {latest_path} 與 {snap_path} (符合: {len(result.get('matches', []))} 檔)", file=sys.stderr)


if __name__ == "__main__":
    main()
