#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Calvin Hsu｜台股全市場量化選股與每日投研日報 (Streamlit 雲端版入口)
直接内嵌完整互動式量子終端網頁，並支援在側邊欄即時觸發 `scripts/run_hourly.py` 重新掃描。
"""
import json
import subprocess
import sys
from pathlib import Path
import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="Calvin Hsu｜台股量化選股與投研日報終端", layout="wide", initial_sidebar_state="collapsed")

with st.sidebar:
    st.markdown("### ⚡ 盤中掃描控制台")
    kd_within = st.slider("KD 黃金交叉天數 (--kd-within)", 1, 5, 1)
    days = st.slider("外資連續買超天數 (--days)", 0, 5, 2)
    min_lots = st.number_input("最低成交量張數 (--min-lots)", min_value=0, value=0, step=100)
    intraday = st.checkbox("盤中即時模式 (--intraday)", value=True)
    if st.button("🚀 立即掃描全市場台股", use_container_width=True):
        with st.spinner("正在掃描上市櫃 ~1,800 檔普通股，約需 3~5 分鐘..."):
            cmd = [sys.executable, str(ROOT / "scripts" / "run_hourly.py")]
            cmd.append("--force-intraday" if intraday else "--force-close")
            cmd.extend(["--kd-within", str(kd_within), "--days", str(days), "--min-lots", str(min_lots)])
            subprocess.run(cmd)
        st.success("掃描完成！已更新最新選股結果。")

html_content = (ROOT / "index.html").read_text(encoding="utf-8")
latest_json = ROOT / "data" / "latest_scan.json"
if latest_json.exists():
    injected = f"<script>window.__LIVE_SCAN_DATA__ = {latest_json.read_text(encoding='utf-8')};</script>"
    html_content = html_content.replace("</head>", injected + "</head>", 1)

components.html(html_content, height=1400, scrolling=True)
