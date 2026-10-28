# Evidence base: factors, gates, regime, construction, validation (researched 2026-10-29)

Coefficients for Beneish/Altman/Ohlson/DSR are standard published values; verify against original papers.

## What survives replication
- McLean & Pontiff (JF 2016, 97 predictors): returns 26% lower out-of-sample, 58% lower post-publication → **plan on ~half the published premium**.
- Hou, Xue & Zhang (RFS 2020, 452 anomalies): 65% fail |t|>1.96 once microcaps are controlled. Value, momentum, investment, profitability hold up best.
- Jensen, Kelly & Pedersen (JF 2023): most factors replicate as **13 themes** across 93 countries → build themes, not single signals.
- Harvey, Liu & Zhu (RFS 2016): demand t > 3 for any new signal.

| Theme | Definition used | Failure mode |
|---|---|---|
| Value | EBIT/EV, FCF/EV, E/P, S/EV (Greenblatt, Gray & Vogel) | long droughts (US 2007-20), value traps → pair with momentum (corr ≈ -0.5, AQR) |
| Quality | GP/A (Novy-Marx 2013), ROIC, -D/E, -earnings variability (AQR QMJ; NSE Quality 30) | expensive; lags junk rallies |
| Momentum | 12-1 and 6-1 returns / 1y daily σ, 50/50 (NSE Momentum 30) | crashes in rebounds after panics (Daniel & Moskowitz 2016) |
| Low vol | -252d σ (Frazzini & Pedersen BAB) | lags sharp bull markets |
| Shareholder yield | (divs + buybacks - issuance)/mcap (Boudoukh et al. 2007); issuance is the robust half | India has few buybacks |
| Revisions/SUE | needs estimate history (not free) | PEAD shrinking in US large caps |

Combine: winsorize, z-score **within sector**, average within theme, re-z, equal-ish fixed weights (DeMiguel et al.: 1/N hard to beat). Never optimize weights.

## India
- IIMA Agarwalla-Jacob-Varma factor library: momentum is the strongest Indian premium. Quantpedia 1995-2021: quality > momentum > value (≈flat). S&P 2005-17: low-vol, quality, momentum beat; value didn't.
- NSE factor indices (Momentum 30, Quality 30, Alpha Low-Vol 30) are validated templates: F&O-only universes, 5% caps, semi-annual rebalance, rank buffers.
- Hazards: promoter pledging (predicts crash risk: Kalia 2024; Chauhan-Mishra-Spahr 2021), ASM/GSM surveillance lists, circuit-limit lock-ins in non-F&O names, related-party transactions, low free float.
- Costs: STT 0.1% each side (delivery) + stamp 0.015% buy + charges → ~0.25-0.35% round trip large caps. STCG 20% (<12m) vs LTCG 12.5% → favour ≥12-month holds.

## Gates (exclusions, not scores)
Altman Z'' < 1.1 (non-financials only) · Beneish M > -1.78 · Piotroski F ≤ 3 · Montier C ≥ 5 · (NI-CFO)/avgTA > 0.10 · negative CFO in 2 of 3 years · net share issuance > 10%/yr · financials: ROA < 0.5% · top-decile 1y volatility (lottery stocks) · liquidity floors.
Campbell-Hilscher-Szilagyi (2008): distressed stocks earn *low* returns, so exclusion adds return as well as safety.

## Regime (keep few and coarse)
- Robust: Faber 10-month SMA as a risk overlay (cuts drawdowns, ~same CAGR; whipsaws). Momentum vol-scaling targets the crash mechanism.
- Fragile: VIX thresholds, yield-curve timing, aggregate vol targeting (Cederburg et al. 2020: real-time vol-managed portfolios underperform in 72/103 cases).
- Rules adopted: below 10m SMA → 50% equity. Crash guard: 24m index return < 0 and (1m vol > 80th pct or VIX > 30 / India VIX > 25) → halve momentum weight, give it to quality.

## Construction
25-40 names; inverse-vol weights; caps 6% (US) / 7% (IN) per name, 25% per sector, min 1.5%; buy top 30, hold until rank > 75 (US) / 60 (IN); quarterly rebalance, trade only on buffer/gate breach or ±30% weight drift; position ≤ 2% of ADV. No leverage, shorts, discretionary stops, or overrides. Novy-Marx & Velikov (2016): high-turnover anomalies die after costs; buffers are essential.

## Validation bars before real money
Point-in-time data, survivorship-free, 90-day fundamental lag, ≥15y history, net of costs. Deflated Sharpe ≥ 0.95 with honest trial count (Bailey & López de Prado 2014). PBO (CSCV, S=16) < 0.2. Positive in 2/3 sub-periods. Survives ±20% parameter perturbation. 5-year holdout opened once. ≥6 months live paper trading. **If it can't beat a 50/50 Nifty200 Momentum 30 + Quality 30 ETF blend (or a US multifactor ETF) after costs, buy the ETFs.**

DSR = Φ((SR̂ − SR₀)·√(T−1) / √(1 − γ₃·SR̂ + (γ₄−1)/4·SR̂²)),  SR₀ = √V[SR̂ₙ]·((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))), γ = 0.5772.

## What top investors / retail errors teach
- Buffett's Alpha (Frazzini-Kabiller-Pedersen 2018): Berkshire ≈ levered cheap + safe + quality + low beta; the "secret" is discipline through 30-50% drawdowns.
- Greenblatt's own retail clients underperformed his formula by overriding it.
- Barber & Odean (2000): highest-turnover households earned 11.4% vs market 17.9%. Disposition effect, lottery stocks, chasing, abandoning mid-drawdown → block mechanically.

Sources: McLean-Pontiff, Hou-Xue-Zhang, Jensen-Kelly-Pedersen (NBER w28432), Daniel-Moskowitz (NBER w20439), Cederburg et al., Bailey & López de Prado (SSRN 2460551), IIMA factor library, NSE index methodology documents, Frazzini-Kabiller-Pedersen (SSRN 3197185), Quantpedia India, Kalia (JAMR 2024).
