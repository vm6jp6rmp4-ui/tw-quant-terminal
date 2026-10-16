# Calvin Hsu｜台股全市場量化選股與每日投研日報終端

這是一個結合 **全市場上市櫃台股每小時自動掃描（MACD + KD + 布林中線 + 外資連買 + 券商分點 + 籌碼集中度）** 與 **Calvin Hsu 研究投資日報（台股盤前早報 / 盤後解析）** 的全自動量化網站專案。

## 核心架構與特色

1. **開盤每小時全自動執行（GitHub Actions 免費排程）**
   - 內建 `.github/workflows/hourly_screener.yml`，於**每週一至週五台股開盤期間（09:05、10:05、11:05、12:05、13:05）**每隔一小時自動執行 `scripts/run_hourly.py`。
   - **盤中時段（09:00–13:30）**：自動啟用 `--intraday` 盤中模式，透過 Yahoo 股市台灣版即時報價合成當日 K 棒，即時計算 XQ 加權 MACD、KD(26,3,3) 與 50MA 布林中線，並搭配截至前一交易日之外資連續買超篩選。
   - **收盤與盤後時段（13:35 & 16:35）**：自動切換為收盤完整模式，待證交所 T86 與 FinMind 分點籌碼更新後，自動套用「近 3 日買超家數 < 賣超家數」與「20日/5日整體籌碼集中度」條件，並呼叫 `fundamentals.py` 寫入每檔候選股的 **近 3 個月營收 YoY** 與 **近 3 季單季 EPS**。
2. **Cyber-Quant 深色金融終端介面（`index.html`）**
   - 採用台股習慣之 **紅漲綠跌** 視覺設計，包含 **7 階篩選漏斗動態視覺化**、**開盤每小時時段快照切換器（09:05～16:35）**、**互動式條件模擬器**、**個股技術面與基本面深度抽屜**，以及 **Calvin Hsu 每日精選投研日報雙頁數位版**。

## 3 分鐘免費上線取得專屬網址（GitHub Pages 方案）

1. **建立 GitHub 儲存庫**
   - 登入 GitHub，點擊右上角 `+` → **New repository**，命名為 `tw-quant-terminal`（設為 Public 即可免費使用無限分鐘數的 GitHub Actions 與 GitHub Pages）。
2. **上傳本專案所有檔案**
   - 將本壓縮包解壓縮後的所有檔案（包含 `.github/`、`scripts/`、`data/`、`index.html`、`requirements.txt`）上傳並 Commit 到 `main` 分支。
3. **開啟 GitHub Pages 網址**
   - 進入儲存庫的 **Settings** → **Pages**，在 **Build and deployment** 下方：
     - **Source** 選擇 `Deploy from a branch`
     - **Branch** 選擇 `main` / `/ (root)`，點擊 **Save**。
   - 等待約 1 分鐘，上方就會顯示你的專屬公開網址：`https://<你的帳號>.github.io/tw-quant-terminal/`。
4. **（選填）設定 FinMind 贊助會員 Token**
   - 若要啟用第 6、7 項「券商分點家數」與「整體籌碼集中度」條件，請至儲存庫的 **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，新增名稱為 `FINMIND_TOKEN` 的金鑰。
5. **手動立即測試排程**
   - 點擊儲存庫上方的 **Actions** 頁籤 → 選擇左側 **Hourly Taiwan Stock Screener & Pages Deploy** → 點擊右側 **Run workflow**，即可隨時手動觸發全市場掃描並更新網頁。
