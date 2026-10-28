# Data sources & MCP servers (researched 2026-10-29)

Verify pricing before paying; vendor pages change often.

## Free stack (what the framework uses today)
| Need | US | India |
|---|---|---|
| Constituents | `raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv` (cols: Symbol, Security, GICS Sector, …, CIK) | `archives.nseindia.com/content/indices/ind_nifty{50,200,500}list.csv` (cols: Company Name, Industry, Symbol, Series, ISIN Code); needs browser User-Agent |
| Prices | yfinance (v1.7.0) | yfinance `.NS` (avoid `.BO`, unreliable) |
| Fundamentals | yfinance (4-5y, **not point-in-time**); SEC EDGAR `data.sec.gov/api/xbrl/companyfacts/CIK##########.json` has `filed` dates → true point-in-time | yfinance (~5y, not PIT) |
| Estimates | yfinance snapshot only (store daily to build revision history) | same |
| Macro | FRED | FRED mirrors some RBI series |
| Paper execution | Alpaca paper API | none free; use built-in paper broker |

yfinance gotchas: HTTP 429 after ~100 rapid calls → cache + throttle; restated data; ToS forbids commercial use.

## Paid upgrades (~$40-100/mo)
- **EODHD** All-In-One (~$90-100/mo): US + NSE/BSE prices, fundamentals, index components. Best single India vendor.
- **FMP** Starter (~$22-29/mo): strong US fundamentals, estimates, insider trades. NSE coverage unconfirmed.
- **Zerodha Kite Connect** (₹500/mo, historical included since Feb 2025): clean NSE prices + execution. Candles are unadjusted.
- **Screener.in** export (~₹4,999/yr): best India fundamentals, promoter pledges. No official API.
- Massive (ex-Polygon), Tiingo: excellent US prices, no India.

## MCP servers
| Server | Status |
|---|---|
| Zerodha Kite MCP | Official, `github.com/zerodha/kite-mcp-server`, hosted `https://mcp.kite.trade/mcp` |
| Alpaca MCP | Official, `uvx alpaca-mcp-server`, paper + live |
| EODHD MCP | Official, `github.com/EodHistoricalData/EODHD-MCP-Server` |
| Massive/Polygon MCP | Official (experimental), `github.com/polygon-io/mcp_polygon` |
| Financial Datasets MCP | Official, paid key |
| Groww / Upstox / Dhan | Broker MCPs, account-linked |
| Yahoo/FMP/Finnhub/Screener | Community only; check GitHub owner before installing |

## Provider-abstraction notes
- Key everything by ISIN (India) / CIK (US); map vendor symbols.
- Store `filed_date` + `fetched_at`; for non-PIT sources assume a conservative lag (period_end + 60d India, +45d US) in backtests.
- Snapshot constituent lists on a schedule: none of the free lists has history (survivorship bias).
