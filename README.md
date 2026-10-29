# Vendor Performance Agent - 2024

An AI agent for liquor-vendor analysis (sales, purchases, margin, freight, inventory).
Ask questions in plain English, get answers with tables and charts. All money is in **USD ($)**.

## Build status
| Step | What | Status |
|---|---|---|
| 1 | Setup (`requirements.txt`, `.env`, `README.md`) | Done |
| 2 | `scripts/build_db.py` - CSV -> `inventory.db` | Next |
| 3 | `scripts/build_summary.py` - small `vendor.db` | Pending |
| 4 | `app/db.py`, `app/tools.py` | Pending |
| 5 | `app/agent.py` | Pending |
| 6 | `app.py` (Streamlit UI) | Pending |
| 7 | `test_app.py` | Pending |

## Folder structure
```
vendor-agent/
├── data/              <- put your 6 CSV/XLSX files here (databases are created here too)
├── app/               (db.py, tools.py, agent.py, charts.py)
├── scripts/           (build_db.py, build_summary.py)
├── .env
├── requirements.txt
├── app.py
└── README.md
```

## Setup (Windows)
1. Install Python 3.10 or newer (`python --version` to check).
2. Open Command Prompt in the project folder and create a virtual environment:
   ```
   cd "D:\Vendor Performance Agentic Ai\vendor-agent"
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Create the folders `data`, `app` and `scripts` inside `vendor-agent` (files for them arrive in later steps).
4. Copy these 6 files into `vendor-agent\data\`:
   `sales`, `purchases`, `purchase_prices`, `begin_inventory`, `end_inventory`, `vendor_invoice` (.csv or .xlsx).
   File names can differ (e.g. `SalesFINAL12312016.csv`); the scripts find them by keyword.
   Copy only the files, **not** the `sales`/`Purchases` folders or the `.zip` archives.
5. Free disk space: `sales.csv` is about 1.5 GB, so `inventory.db` can grow to several GB. Keep at least 10 GB free on the drive.

## Configure `.env`
| Key | Meaning |
|---|---|
| `DATA_DIR` | folder with your raw files (`data`) |
| `DB_PATH` | big raw database, built by `build_db.py` (`data/inventory.db`) |
| `SUMMARY_DB_PATH` | small fast database used by the app (`data/vendor.db`) |
| `LLM_PROVIDER` | `gemini`, `groq` or `openai` |
| `LLM_MODEL` | model name for that provider (examples are in `.env`) |
| `GOOGLE_API_KEY` / `GROQ_API_KEY` / `OPENAI_API_KEY` | paste your key for the provider you chose |

Free keys: Gemini at Google AI Studio (aistudio.google.com/apikey), Groq at console.groq.com.
No key? The app still works in rule-based fallback mode (top vendors, low margin, trend).
Never commit `.env` to GitHub.

## Run order
```
python scripts/build_db.py        # 1. load CSVs into data/inventory.db (slow, run once)
python scripts/build_summary.py   # 2. build small data/vendor.db and check KPIs
streamlit run app.py              # 3. open the app
```

## Expected KPIs (2024, used to validate the data)
| KPI | Value |
|---|---|
| Total Purchases | about $321.9M |
| Total Sales | about $343.6M |
| Gross Profit | about $21.65M |
| Margin | about 6.3% |

## Rules this project follows
1. One database path, read from `.env`.
2. Margin % = `SUM(GrossProfit) / SUM(SalesDollars) * 100`. Never `AVG(ProfitMargin)`, never multiply a margin by 100 twice.
3. Freight comes only from `vendor_invoice` (grouped by vendor), never from the summary table.
4. Each table is aggregated separately, then merged in pandas. No join before aggregation.
5. Gross Profit = Sales - Purchases. `NetAfterFreight` = Gross Profit - Freight (separate column).
6. "Last 3 months" counts back from the latest sales date in the data (2024-12-31), not today.
7. Only SELECT queries are allowed. DROP, DELETE, UPDATE and INSERT are blocked.
8. The data has a `Classification` column (1 and 2), not a Category. The filter is labelled "Classification" until you confirm what 1 and 2 mean.

## Troubleshooting
- `python` not found: reinstall Python and tick "Add Python to PATH".
- `pip install` fails on one package: run `pip install -r requirements.txt` again, or install that package alone.
- App says "fallback mode": the API key in `.env` is missing or still the placeholder text.
