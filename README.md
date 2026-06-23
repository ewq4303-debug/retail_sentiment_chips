# 散戶情緒籌碼指標系統 · Retail Sentiment Chips

台股 **零股（Flow）× 集保存量（Stock）雙頻驗證** 的 EOD batch 系統。
依 [`SPEC`](SPEC_retail_sentiment_chips.md) 實作：Python pipeline → JSON 寫入 repo →
GitHub Actions cron → GitHub Pages（ECharts dark dashboard）。**無本地常駐 process**。

## 設計核心（SPEC §0–§1）

1. **方向不存在於 EOD 公開資料**。零股行情只有量價、無買賣方。系統用
   **集保存量變化錨定零股周轉量** 推導帶號淨流，而非對無號的量自己編號。
2. **存量是真值，流量是形狀**。週頻集保散戶股數變化 = 該週真實淨買賣（帶號）；
   日頻零股周轉量只把週淨值「分配」到每一天（決定時間形狀），不決定正負。
3. **最新一週（集保未出）只給暫定方向，集保一出就 restate**（`provisional` 旗標）。
4. **能用比例就不用股數**：比例型指標對等比例公司行動天然免疫。

## 架構

```
config/
  config.yaml              全域參數（§1 Config）
  universe.txt             追蹤標的
  corporate_actions.yaml   公司行動行事曆（§2）
src/retail_sentiment/
  config.py                設定載入
  calendar_utils.py        交易日曆 + settlement lag 對齊（§8，Thu→Thu 半開窗口）
  sources.py               資料源：finmind / synthetic 兩 backend（§1 表 A–D）
  corporate_actions.py     adj_factor 還原（§2）
  arbitrage.py             套利湊張過濾 → W_arb、零股周轉量 T（§5, §3.1）
  direction.py             帶號淨流：存量錨定分配 + 暫定路徑（§3）
  baseline.py              定期定額 baseline 剝離（§4）
  indicators.py            正規化 + 背離 + 大戶剪刀差（§7, §9）
  matrix.py                雙頻驗證矩陣 + 量級 + 存量 gating（§10）
  reconcile.py             流量↔存量對帳（§12.1）
  output.py                JSON Schema 輸出（§11）
  pipeline.py              編排
run.py                     進入點
tests/                     §12.2 時間軸 / §12.1 對帳單元測試
docs/                      GitHub Pages（index.html + data/）
.github/workflows/         cron pipeline + Pages 部署
```

## 本地執行

```bash
pip install -r requirements.txt
python run.py --backend synthetic       # 合成資料，無需金鑰，產出 demo
python run.py                           # 有 FINMIND_TOKEN 時抓真實資料
python -m pytest -q                     # 跑單元測試
# 預覽 dashboard：
python -m http.server -d docs 8000      # 開 http://localhost:8000
```

## 資料來源

| backend | 說明 |
|---|---|
| `synthetic` | 確定性假資料，自洽含可偵測籌碼集中事件；保證 CI 無金鑰也能跑通並產 demo |
| `finmind` | FinMind v4 抓整股/法人/融資券/當沖；需 `FINMIND_TOKEN`。任何 dataset 失敗皆降級不中斷 |

選擇邏輯：環境變數 `DATA_BACKEND`；未設且無 `FINMIND_TOKEN` → 自動 `synthetic`。

### 集保（股權分散，週頻存量）
FinMind 的 `TaiwanStockHoldingSharesPer` 為**贊助會員專屬**（免費 token 回 HTTP 400），
故集保改用 **TDCC 官方 opendata**（`getOD.ashx?id=1-5`，免金鑰），每週累積寫入
`cache/tdcc/{id}.csv`（隨 repo commit）。FinMind 若有權限（含歷史）則優先。

opendata 只有最新一週，需累積 ≥2 週才有 ΔStock 強弱訊號。已用 importer 從外部累積
的歷史**一次性 seed**（本 repo `cache/tdcc/` 已含多週），之後 cron 繼續往後累積：

```bash
# 從公開 repo 目錄匯入每週 CSV（原始 opendata 格式）
python scripts/import_tdcc_history.py --from-github <owner>/<repo>:<path>
# 或本機目錄
python scripts/import_tdcc_history.py --local /path/to/tdcc_history
```

> **零股資料**（SPEC §1 表 A / §13）：FinMind 無乾淨日頻零股 dataset，改接 **TWSE 歷史行情單**
> （`src/retail_sentiment/twse.py`）。盤中零股 `TWTC7U`、盤後零股 `TWT53U` 皆支援
> **帶 `date` 參數的歷史單日查詢**（回全市場），故 `backfill_universe` 可一次回補整個 lookback
> 視窗（每交易日只抓一次、服務全 universe），結果累積寫入 **`cache/oddlot/{id}.csv`**（隨 repo
> commit），並用 `cache/oddlot/_fetched_{REPORT}.json` markers 避免重抓（含假日空資料）。
> cache 仍未涵蓋的日以整股量比例合成代理並標 `oddlot_is_proxy`。盤後零股權重 1.0（§1 表 A）。
> 欄位採防禦式偵測（中文 fields 關鍵字比對）。每次回補受 `oddlot_backfill_max_days_per_run`
> 與 `oddlot_request_delay_sec` 限流；首次跑滿視窗後，之後 cron 只補新交易日。

## GitHub Actions 部署

`.github/workflows/pipeline.yml`：

1. cron（台北早上，抓前一交易日 EOD）或手動 `workflow_dispatch` 觸發
2. 跑測試 → 跑 pipeline → 把 `docs/data/*.json` commit 回 repo
3. 部署 `docs/` 到 GitHub Pages

啟用步驟：
1. Repo **Settings → Pages → Source: GitHub Actions**
2. （可選）**Settings → Secrets → Actions** 新增 `FINMIND_TOKEN`；不加則用 synthetic
3. Actions 分頁手動 **Run workflow** 跑第一次，或等 cron

## 輸出 Schema（§11）

`docs/data/index.json`（總表）、`daily/{id}.json`、`weekly/{id}.json`、`signals/{id}.json`。

## 核心指標

- **RetailNetFlow**（§9.1）：帶號淨流 → 去定期定額 → 正規化（流通股數 bps）
- **SmartDumbScissor**（§9.2）：大戶剪刀差 `hb−sr` 與 momentum `Δhb−Δsr`（週頻存量）
- **DivergenceScore**（§9.3）：`zscore(inst) − zscore(retail)`（z 差值，避免累積序列 spurious corr）
- **雙頻訊號**（§10）：CONFIRMED / NOISE（湊張過水存量未動）/ PENDING / PROVISIONAL

## 驗證（§12）

- `tests/test_calendar.py`：cutoff=週四、假日週位移、Thu→Thu 半開窗口不重不漏
- `tests/test_reconcile.py`：分配後 `Σ signed_flow == ΔS_retail`、方向來自存量

## 待釐清（SPEC §13）

- 各資料源真實欄位名（零股、定期定額、借券）
- 盤中零股逐盤帶號需常駐擷取 → Phase 2
- `BIG_HOLDER_RULE` 400 張到集保級距的精確邊界
- 矩陣 gating 的 `ε / typical_weekly_move` 與 `w1,w2` 權重校準
