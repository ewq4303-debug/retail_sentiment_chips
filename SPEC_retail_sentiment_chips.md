# SPEC — 散戶情緒籌碼指標系統（修正版 v1）

> 給 Claude Code 的實作規格。台股零股（Flow）× 集保存量（Stock）雙頻驗證。
> 部署假設：Python pipeline → JSON 寫入 repo → GitHub Actions cron → GitHub Pages（ECharts dark dashboard）。**無本地常駐 process**，故全部資料降頻為日頻 / 週頻 EOD batch。
> 本文修正前版四個硬傷：① 淨流要帶號（非用周轉量）② 雜訊機制是「零股湊整張套利」非當沖 ③ 價差過濾方向待實證、預設關閉 ④ 集保存在 T+2 結算落後、時間軸要對齊。

---

## 0. 設計原則（實作前先讀）

1. **方向（淨流的正負號）不存在於 EOD 公開資料**。零股行情只有成交量與成交價，沒有內外盤／買賣方發起方。本系統用「**集保存量變化錨定日頻零股周轉量**」來推導帶號淨流，而不是對無號的量自己編號。
2. **存量是真值，流量是形狀**。週頻集保的散戶股數變化 = 該週散戶真實淨買賣（真值、帶號）；日頻零股周轉量只負責把這個週淨值「分配」到每一天（決定時間形狀），不負責決定正負。
3. **最新一週（集保未出）只能給暫定方向，集保一出就重述（restate）**。所有日頻指標帶 `provisional` 旗標，跟你 IBKR NAV pipeline 同樣的「先暫估、後校正」模式。
4. **能用比例就不用股數**。集保「佔庫存比例 %」對等比例公司行動（除權配股、分割）天然免疫；只有流量錨定那一步需要還原成調整後股數。

---

## 1. 資料來源與表結構

> 欄位名稱以實際抓取端為準（FinMind / TWSE OpenAPI / TDCC opendata 各有差異），下表標 `⚠️確認` 者請實作時驗證真實欄位。

### 表 A — 日頻・零股流量（盤中＋盤後）
| 欄位 | 來源 | 備註 |
|---|---|---|
| `date` | — | 交易日 |
| `stock_id` | — | |
| `v_intra` 盤中零股成交量(股) | TWSE 盤中零股行情 | ⚠️確認；2024/12/2 起每 5 秒集合競價，cron 抓 EOD 彙總即可 |
| `vwap_intra` 盤中零股均價 | 同上 | 用成交金額/成交量推算 |
| `v_after` 盤後零股成交量(股) | TWSE 盤後零股(零股交易)行情 | 14:30 單一次集合競價，套利雜訊最低 → 權重 1.0 |
| `vwap_after` 盤後零股均價 | 同上 | |

### 表 A' — 日頻・整股（normalizer 用）
| 欄位 | 來源 |
|---|---|
| `v_whole` 整股成交量 / `vwap_whole` / `ret` 日報酬 | TWSE 日成交資訊 / FinMind `TaiwanStockPrice` |
| `v_daytrade` 當沖成交量 | TWSE 當日沖銷交易標的 / FinMind `TaiwanStockDayTrading` ⚠️確認 |

### 表 B — 日頻・法人與信用
| 欄位 | 來源 |
|---|---|
| `inst_net` 三大法人合計淨買超(股) | TWSE T86 / FinMind `TaiwanStockInstitutionalInvestorsBuySell` |
| `margin_bal` 融資餘額 / `short_bal` 融券餘額 | FinMind `TaiwanStockMarginPurchaseShortSale` |
| `sbl_short_bal` 借券賣出餘額 | TWSE SBL / FinMind 借券 dataset ⚠️確認（補空方機構代理） |

### 表 C — 週頻・集保股權分散（TDCC）
| 欄位 | 來源 | 備註 |
|---|---|---|
| `snapshot_date` 公布資料日 | TDCC opendata / FinMind `TaiwanStockHoldingSharesPer` ⚠️確認 | 每週最後營業日（通常週五）收盤後存摺餘額 |
| `tier` 持股分級（15 級距）| 同上 | 1–999 / 1,000–5,000 / … / 1,000,001↑ |
| `holders` 人數 / `shares` 股數 / `pct` 佔集保庫存% | 同上 | |

### 表 D — 月頻・定期定額（baseline 校準，可選但建議）
| 欄位 | 來源 |
|---|---|
| `dca_accounts` 定期定額交易戶數 | TWSE/櫃買「定期定額交易戶數統計排行月報」⚠️確認 |

### Config（全域參數，集中管理）
```yaml
RETAIL_TIER_MAX_SHARES: 5000      # 散戶存量上界（≤5張）；常見替代 10000(≤10張)
BIG_HOLDER_RULE:                  # 大戶門檻隨股價，映射到最近的集保級距邊界
  high_price_threshold: 50        # 元
  big_tier_min_shares_high: 400000   # 股價>50 → ≥400張，近似 400,001 級距以上
  big_tier_min_shares_low: 1000000   # 股價≤50 → ≥1000張，用 1,000,001↑ 頂層
Z_WINDOW: 60                      # 日頻 z-score 視窗（交易日）
DCA_TREND_WINDOW: 60
DCA_SEASONAL_LOOKBACK_MONTHS: 12
NOISE_RECONCILE_TOL: 0.15         # 流量重整對帳容差（相對 ΔStock）
ENABLE_SPREAD_FILTER: false       # ⚠️ 方向待實證，預設關閉，見 §5
ARB_RATIO_Z_THRESHOLD: 2.0
W_ARB_FLOOR: 0.2                  # 高套利嫌疑時盤中零股最低權重
```

---

## 2. 清洗與公司行動調整

1. **除權配股 / 分割 / 減資 / 增資**：維護 `corporate_action_calendar`。
   - **比例型指標（散戶比 `sr`、大戶比 `hb`）用「佔庫存 %」計算 → 對等比例行動免疫，不需調整。**
   - **流量錨定（§4）需要股數差**：先把各週 `shares` 還原到「調整後股數」共同基準（套用調整因子 `adj_factor`），否則 ΔStock 會把配股當成散戶買進。
2. **集保雜訊源 caveat（不剔除、但標記）**：設質專戶、信託、借券專戶各以一戶計、且會在級距間跳動；ID 已歸戶（同一人多券商視為一戶），但設質大量股票會扭曲分級。對 `holders` 的週變化加 `tdcc_noise_flag`。
3. **零股價量缺值**：停牌 / 無成交日 `v_*=0`，不可進入滾動視窗（避免污染 z-score 與中位數），以 NaN 處理並 forward-fill 存量、跳過流量。

---

## 3. 方向分類（Direction Classification）

核心：**用集保存量差錨定，用零股周轉量分配時間形狀。** 分「已結算週（主算法）」與「當週未結算（暫定）」兩條路徑。

### 3.1 零股周轉量（套利調整後）
```
T_d = v_after_d * 1.0  +  v_intra_d * W_arb_d        # 見 §5 求 W_arb_d ∈ [W_ARB_FLOOR, 1]
```

### 3.2 主算法 — 集保錨定分配（已結算週）
令第 w 週對齊窗口 `𝒲_w = (cutoff_{w-1}, cutoff_w]`（cutoff 定義見 §8）。
散戶存量（調整後股數）：
$$S^{retail}_w = \sum_{tier.\,shares \le \text{RETAIL\_TIER\_MAX}} shares_{adj}$$
週真值淨流（帶號）：
$$\Delta S^{retail}_w = S^{retail}_w - S^{retail}_{w-1}$$
分配到日：
$$\text{signed\_flow}_d = \Delta S^{retail}_w \cdot \frac{T_d}{\sum_{k \in \mathcal{W}_w} T_k}, \quad d \in \mathcal{W}_w$$
**性質（必須通過對帳）**：$\sum_{d \in \mathcal{W}_w}\text{signed\_flow}_d = \Delta S^{retail}_w$。正負號完全來自存量，量級也來自存量，零股量只決定每天分多少。

> 注意級距溢出：散戶買零股可能把持股從第1級推進第2級（跨 1,000 股）。因此散戶存量用 **≤ RETAIL_TIER_MAX（含第1、2級）合計**，而非只取第1級，否則 ΔStock 會系統性低估。

### 3.3 暫定路徑 — 當週未結算（集保尚未公布）
無真值可錨。輸出「方向 + 強度」兩個分離欄位，**不偽裝成已對帳股數**：
```
provisional_direction_d = sign( α * (vwap_intra_d - vwap_whole_d)/vwap_whole_d
                              + β * ret_d )              # α,β 預設 0.5/0.5，待校
provisional_intensity_d = zscore(T_d, window=Z_WINDOW)   # 純量級，無號
```
旗標 `provisional=true`。**下次 TDCC 公布後，用 3.2 重算整週並 restate**（把 `provisional` 改 false、寫入 `restated_at`）。

---

## 4. Baseline 剝離（定期定額結構性流入）

定期定額是真散戶、但價格不敏感、集中在月初到月中扣款、且**只買不賣**，會讓淨流長期偏多、污染情緒判讀（ETF 與權值股尤其嚴重）。

分解：`signed_flow = DCA_baseline + sentiment`。
```
# 1) 趨勢項（穩健、抗尖峰）：僅取買方偏壓，故 clip ≥ 0
trend_d = max(0, rolling_median(signed_flow, DCA_TREND_WINDOW))

# 2) 月內季節項：定期定額按日曆日 cluster
seasonal_dom(dom) = max(0, trailing_median_by_day_of_month(signed_flow,
                                lookback=DCA_SEASONAL_LOOKBACK_MONTHS))

# 3) 若有表 D：用實際定期定額強度縮放 baseline
dca_intensity = dca_accounts / total_holders        # 0~1，無資料時設 1
DCA_baseline_d = dca_intensity * (trend_d + seasonal_dom(dom_d))

# 4) 情緒殘差（可正可負）
sentiment_flow_d = signed_flow_d - DCA_baseline_d
```
ETF 給較高 `dca_intensity`（或單獨 multiplier）。**caveat**：個股定期定額金額未公開逐檔拆分，baseline 為統計估計，參數需回測校準（§12）。

---

## 5. 套利湊張過濾（取代「當沖」與原價差過濾）

**制度事實**：盤中零股不可當沖（T 買進須 T+1 才能再交易），故零股爆量**不可能是當沖**，一定留倉。真正雜訊是「分批買零股 → 湊滿 1,000 股 → 到整股市場賣」的跨日套利，會在集保「過水不留」。

求盤中零股權重 `W_arb_d`（盤後恆為 1.0）：
```
# 主訊號：盤中零股量 / 整股量 異常飆高
arb_ratio_d = v_intra_d / max(v_whole_d, 1)
arb_z = zscore(arb_ratio_d, window=Z_WINDOW)

# 輔訊號：同日「零股大買 + 整股法人/自營大賣」並存（湊張賣出特徵）
# 輔訊號：v_intra 接近 1000 整數倍（湊張目標）

W_arb_d = 1.0 if arb_z < ARB_RATIO_Z_THRESHOLD
          else clip(1 - (arb_z - ARB_RATIO_Z_THRESHOLD)/k, W_ARB_FLOOR, 1.0)
```
**原價差過濾器預設關閉**（`ENABLE_SPREAD_FILTER=false`）。理由：套利會*收斂*價差，故「價差大 = 套利多」方向很可能相反——大價差更可能是流動性差、套利沒進來、純散戶亂喊。若要啟用，先做 §12 的方向實證；驗證為正相關才打開，並以 §5 的 `arb_z` 為主、價差為輔。

> 由 §2.2 的對帳保證：套利「過水不留」會讓該週 ΔStock 自動接近 0，因此即使 W_arb 估不準，§10 的存量 gating 仍會把它判為雜訊。雙保險。

---

## 6. 整股側當沖過濾（normalizer 用）

當沖雜訊在整股、不在零股。T86 法人淨買超本身已是結算淨額、近乎免當沖，故當沖過濾**只用在把整股量當分母的地方**：
```
v_whole_settled_d = v_whole_d - v_daytrade_d        # 留倉量
```

---

## 7. 正規化（跨股可比）

```
RetailNetFlow_d = sentiment_flow_d / float_shares_w(d) * 1e4   # 單位：流通股數 bps
# 替代視角（流量可比）：sentiment_flow_d / ADV20(v_whole_settled)
```
`float_shares` 取集保總計或已發行股數（擇一固定）。raw 股數不可跨股比較。

---

## 8. 時間軸對齊（settlement lag）★ 易錯點

**事實**：TDCC 快照為週最後營業日（通常週五）收盤後存摺餘額；因 T+2 交割，**該快照含星期四（含）以前交易、不含星期五當日**。

```
# 對齊截止日 = 快照日的前一個交易日（穩健處理含假日週）
cutoff(snapshot_date) = previous_trading_day(snapshot_date)   # 一般 = 週四

# 第 w 週流量對齊窗口（Thu → Thu，半開區間）
𝒲_w = ( cutoff(snapshot_{w-1}),  cutoff(snapshot_w) ]
```
**§3.2 的流量累加、§10 的存量驗證，都用 `𝒲_w`，不可用週一到週五或週五到週五**，否則訊號糊掉 1~2 天。假日週用 `previous_trading_day` 自動位移。

---

## 9. 核心指標

### 9.1 散戶真實淨流 `RetailNetFlow_d`
§3 帶號 → §4 去定期定額 → §7 正規化。最終日頻情緒流量。

### 9.2 大戶剪刀差 `SmartDumbScissor`（週頻，存量）
大戶比（按股價選級距，見 Config）：
$$hb_w = \frac{S^{big}_w}{Total_w}, \quad sr_w = \frac{S^{retail}_w}{Total_w}$$
```
ScissorLevel_w    = hb_w - sr_w
ScissorMomentum_w = (hb_w - hb_{w-1}) - (sr_w - sr_{w-1})    # = Δhb - Δsr
# Momentum > 0：籌碼向大戶集中（存量偏多）；< 0：散戶化（存量偏空）
```
> 大戶 400 張門檻不落在集保級距邊界上，故映射到最近邊界（≥400,001 股的級距合計）近似；≥1000 張直接用 1,000,001↑ 頂層，乾淨。

### 9.3 主力散戶背離 `DivergenceScore_d`（修正統計）
**不對累積序列做相關係數**（累積序列非定態 → spurious corr；n=5~10 → 估計不穩）。改標準化差值：
```
z_inst_d   = zscore(inst_net_d,        window=Z_WINDOW)
z_retail_d = zscore(RetailNetFlow_d,   window=Z_WINDOW)
DivergenceScore_d = z_inst_d - z_retail_d
# >> 0：法人買 / 散戶賣（偏多背離）；<< 0：法人賣 / 散戶買（偏空背離）
```
可選：對**日頻（非累積）**序列做 Spearman `ρ60(inst_net, RetailNetFlow)` 當輔助；高度負相關 = 大規模換手。

---

## 10. 雙頻驗證矩陣（擴充 + 量級 + 存量 gating）

維度：法人方向 `sign(z_inst)` × 散戶情緒方向 `sign(z_retail_sentiment)` × 存量 `sign(ScissorMomentum)`。

| 法人 | 散戶情緒 | 集保(大戶剪刀差) | 判定 | 意義 |
|---|---|---|---|---|
| 買 | 賣 | 大戶增(散戶減) | **強多** | 散戶下車、籌碼集中，波段發動 |
| 買 | 買 | 大戶增(散戶減) | **強多** | 大戶吃貨、散戶跟風尚未變存量 |
| 賣 | 買 | 散戶大增 | **強空** | 散戶接刀、籌碼凌亂 |
| 賣 | 賣 | 大戶減(散戶減) | **強空** | 一致性出貨 |
| 任一 | 任一 | **≈ 不變（\|ΔStock\|<ε）** | **雜訊／未留倉** | 湊張套利過水，抑制訊號 |
| 買 | 買 | 微增 | 中性偏多 | 共識強、提防洗盤 |
| 衝突組合 | — | — | 待確認 | 等下一份集保 |

**連續分數 + gating（建議實作為主，矩陣當人類可讀對照）**：
```
C_flow_d = w1*z_inst_d + w2*(-z_retail_sentiment_d)          # 流量側技術分（日頻）
G_w      = sign(ScissorMomentum_w)                           # 存量確認（週頻）
dStock   = ΔS_retail_w / Total_w

if abs(dStock) < NOISE_RECONCILE_TOL * typical_weekly_move:
    signal = "NOISE"            # 存量沒動 → 不論流量多大都抑制（套利過水）
elif sign(mean_over_week(C_flow)) == G_w:
    signal = "CONFIRMED", strength = |mean C_flow| * weight(|dStock|)
else:
    signal = "PENDING"         # 流量與存量衝突，等下一份集保
```
權重 `w1,w2` 與 `ε / typical_weekly_move` 由 §12 校準。

---

## 11. 輸出 Schema（JSON，commit 進 repo）

```jsonc
// daily/{stock_id}.json  — 每檔一檔、append 日頻
{
  "stock_id": "2330",
  "as_of": "2026-06-23",
  "daily": [{
    "date": "2026-06-20",
    "signed_flow": -123000,           // 股，§3
    "sentiment_flow": -98000,         // §4 去定期定額後
    "retail_net_flow_bps": -3.7,      // §7 正規化
    "dca_baseline": 25000,
    "w_arb": 1.0,
    "z_inst": 1.4, "z_retail": -0.9,
    "divergence_score": 2.3,          // §9.3
    "provisional": false,             // §3.3；當週未結算為 true
    "restated_at": "2026-06-23T01:10Z"
  }]
}
```
```jsonc
// weekly/{stock_id}.json — 週頻存量
{
  "stock_id": "2330",
  "weekly": [{
    "snapshot_date": "2026-06-20",
    "cutoff": "2026-06-19",           // §8 Thu
    "window": ["2026-06-13","2026-06-19"],
    "sr_pct": 12.4, "hb_pct": 78.1,
    "scissor_level": 65.7,
    "scissor_momentum": 0.42,         // §9.2
    "delta_retail_shares_adj": -540000,
    "reconcile_ok": true,             // §12 對帳
    "tdcc_noise_flag": false
  }]
}
```
```jsonc
// signals/{stock_id}.json
{ "date":"2026-06-20", "signal":"CONFIRMED", "label":"強多",
  "strength":0.82, "matrix_cell":"買/賣/大戶增" }
```

---

## 12. 驗證與回測檢核點（實作須附）

1. **流量↔存量對帳**：每結算週驗 `|Σ signed_flow_d − ΔS_retail_w| / |ΔS_retail_w| ≤ NOISE_RECONCILE_TOL`；超標寫 `reconcile_ok=false` 並告警（多半是公司行動或抓取缺日）。
2. **時間軸單元測試**：跨國定假日、補班日、快照落在週四的週，驗 `cutoff` 與 `𝒲_w` 正確、不重不漏。
3. **價差過濾方向實證**（啟用前必做）：回測 `spread` 與「次日／當週確實留倉（ΔStock）」的相關性符號。為負（價差大→留倉真）則維持關閉或反向；為正才打開。
4. **定期定額 baseline 校準**：用表 D 月戶數對照，檢查殘差 `sentiment_flow` 在已知大盤情緒事件（如重挫日）方向正確、平時無系統性偏多。
5. **散戶級距上界敏感度**：≤5張 vs ≤10張 對訊號穩定度的影響，擇優寫回 Config。
6. **provisional → restate 一致性**：抽查同一週暫定值與重述後值的方向翻轉率，過高代表暫定 α/β 需重校。

---

## 13. 待釐清 / 需實證（Open Items）

- [ ] 各資料源真實欄位名（表 A 零股、表 D 定期定額、借券）
- [ ] 盤中零股逐盤（5 秒）資料若要做 tick-rule 帶號需常駐擷取，超出 GitHub Actions 範圍 → 列 Phase 2 / 改外部排程
- [ ] `BIG_HOLDER_RULE` 400 張映射到集保級距的精確邊界（級距為固定 15 段，非任意門檻）
- [ ] 矩陣 gating 的 `ε / typical_weekly_move` 與 `w1,w2` 權重值
- [ ] 集保 ID 歸戶下「大戶拆單分帳戶」殘餘失真程度評估

---

### 與前版差異摘要（給 reviewer 對照）
| 前版 | 本版修正 |
|---|---|
| `NetFlow = Σ 成交量` | 帶號淨流，集保存量錨定分配（§3） |
| 矩陣「當沖」雜訊 | 改為「零股湊整張套利」，制度上零股不可當沖（§5） |
| 價差過濾打折盤中量 | 預設關閉、方向待實證，主用 arb_ratio z-score（§5） |
| 累積量 rolling correlation | z-score 差值 `z_inst − z_retail`（§9.3） |
| 週五對週五 / 週一到週五 | Thu→Thu 對齊 `𝒲_w`（§8） |
| 只取散戶級距 | 補大戶剪刀差當對手方（§9.2）、定期定額剝離（§4） |
