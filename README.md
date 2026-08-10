> [!WARNING]
> **Research fork — not for data access.**
>
> `aard-ai/databricks-mcp-server` is a research clone of [`VNSHANPR/databricks-mcp-server`](https://github.com/VNSHANPR/databricks-mcp-server), kept for studying how
> MCP servers behave when retargeted at other statistical agencies' SDMX endpoints. It is
> not maintained, is not published to any package registry, and **must not be used as an
> MCP server for data access**. For a working server, use the upstream project.

# Economic Intelligence MCP Server

A production-ready **Model Context Protocol (MCP) server** deployed as a **Databricks App**, designed to give AI Supervisor Agents real-time access to external macroeconomic data — no API keys required for most sources.

Built as a companion to the Databricks Supervisor Agent pattern, this server exposes live economic indicators from the World Bank, the Australian Bureau of Statistics (ABS), and any public economic webpage — all via the standardised MCP protocol over Streamable HTTP.

---

## Architecture

```
Databricks Supervisor Agent
        │
        │  MCP over HTTPS (Streamable HTTP / JSON-RPC 2.0)
        ▼
Unity Catalog HTTP Connection  ──────────────────────────────────────┐
(OAuth M2M, auto-refreshing token)                                   │
        │                                                            │
        ▼                                                            │
Economic Intelligence MCP Server (Databricks App)                    │
├── FastMCP  (MCP protocol layer)                                     │
├── FastAPI  (HTTP server)                                            │
└── Tools                                                            │
    ├── get_inflation            ──▶  World Bank Open Data API        │
    ├── get_macro_indicators     ──▶  World Bank Open Data API        │
    ├── analyze_economic_webpage ──▶  Claude (Databricks Model Serving)
    ├── list_abs_dataflows       ──▶  ABS SDMX REST API               │
    └── get_abs_data             ──▶  ABS SDMX REST API               │
```

---

## Tools

### 🌍 `get_inflation`
Returns the latest CPI inflation rate for any country using the World Bank Open Data API.

```json
{ "country_code": "AU" }
→ { "country": "Australia", "year": "2023", "inflation_rate_pct": 5.6, "source": "World Bank" }
```

### 📊 `get_macro_indicators`
Fetches a bundle of key macroeconomic indicators in one call — CPI inflation, GDP growth, real interest rate, and unemployment rate.

```json
{ "country_code": "US" }
→ [
    { "indicator_name": "Inflation, consumer prices (annual %)", "value": 4.12, "year": "2023" },
    { "indicator_name": "GDP growth (annual %)", "value": 2.54, "year": "2023" },
    ...
  ]
```

### 🔍 `analyze_economic_webpage`
Fetches any public URL and uses **Claude** (via Databricks Model Serving) to extract and answer a specific economic question from the page content.

```json
{
  "url": "https://www.rba.gov.au/statistics/",
  "question": "What is the current cash rate target?"
}
→ "The RBA cash rate target is 4.35% as of November 2023."
```

Works with:
- Reserve Bank of Australia (RBA)
- Australian Bureau of Statistics (ABS)
- US Bureau of Labor Statistics (BLS)
- OECD, IMF, and any other public economic data page

### 🇦🇺 `list_abs_dataflows`
Returns a curated catalogue of Australian Bureau of Statistics SDMX dataflows with dimension descriptions and ready-to-use key examples.

```json
→ {
    "CPI":     { "description": "Consumer Price Index — all groups (quarterly)", ... },
    "LF":      { "description": "Labour Force — unemployment, participation (monthly)", ... },
    "WPI":     { "description": "Wage Price Index — hourly rates (quarterly)", ... },
    "ANA_AGG": { "description": "National Accounts — GDP aggregates (quarterly)", ... },
    ...
  }
```

### 📈 `get_abs_data`
Fetches time-series data directly from the ABS SDMX REST API. Returns fully labelled records — no cryptic codes.

```json
{
  "dataflow": "LF",
  "key": "M13.3.1599.20.AUS.M",
  "start_period": "2025-01"
}
→ [
    { "MEASURE": "Unemployment rate", "REGION": "Australia",
      "TSEST": "Seasonally Adjusted", "period": "2026-01", "value": 4.07 },
    ...
  ]
```

**Supported dataflows:**

| ID | Dataset | Frequency |
|----|---------|-----------|
| `CPI` | Consumer Price Index | Quarterly |
| `LF` | Labour Force Survey | Monthly |
| `WPI` | Wage Price Index | Quarterly |
| `ANA_AGG` | National Accounts (GDP) | Quarterly |
| `RPPI` | Residential Property Price Indexes | Quarterly |
| `MERCH_EXP` | Merchandise Exports | Monthly |
| `MERCH_IMP` | Merchandise Imports | Monthly |
| `BOP` | Balance of Payments | Quarterly |

---

## Project Structure

```
databricks-mcp-server/
├── server/
│   ├── app.py       # FastMCP + FastAPI combined ASGI app, header middleware
│   ├── main.py      # Uvicorn entry point
│   ├── tools.py     # All MCP tool definitions
│   └── utils.py     # Databricks OAuth / on-behalf-of auth utilities
├── static/
│   └── index.html   # Status page served at /
├── app.yaml         # Databricks App command config
├── pyproject.toml   # Project metadata + dependencies (uv / hatchling)
└── requirements.txt # Bootstrap: just "uv"
```

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| MCP framework | [FastMCP](https://github.com/jlowin/fastmcp) ≥ 2.12.5 |
| HTTP server | FastAPI + Uvicorn |
| Package manager | [uv](https://github.com/astral-sh/uv) |
| Deployment | Databricks Apps |
| Auth | OAuth M2M via Unity Catalog HTTP Connection |
| LLM | Claude via Databricks Model Serving (`Claude_for_Analysis`) |
| External data | World Bank Open Data API, ABS SDMX REST API |

---

## Deployment

### Prerequisites
- Databricks CLI installed and authenticated
- A Databricks workspace with Apps enabled
- A Claude model serving endpoint deployed (named `Claude_for_Analysis`)

### Deploy

```bash
# 1. Create the app
databricks apps create --json '{"name": "economic-intelligence-mcp"}'

# 2. Sync source code to workspace
databricks sync . /Workspace/Users/<your-email>/databricks-mcp-server

# 3. Deploy
databricks apps deploy economic-intelligence-mcp \
  --source-code-path /Workspace/Users/<your-email>/databricks-mcp-server
```

### Register in Unity Catalog

Run this SQL in a Databricks notebook or SQL editor to make the MCP server discoverable by Supervisor Agents:

```sql
CREATE CONNECTION economic_intelligence_mcp TYPE HTTP OPTIONS (
  host              = 'https://<your-app-url>.databricksapps.com',
  port              = '443',
  base_path         = '/mcp',
  is_mcp_connection = 'true',
  token_endpoint    = 'https://<your-workspace>.cloud.databricks.com/oidc/v1/token',
  client_id         = '<app-service-principal-client-id>',
  client_secret     = '<client-secret-from-account-console>',
  oauth_scope       = 'all-apis'
);

GRANT USE CONNECTION ON CONNECTION economic_intelligence_mcp TO `account users`;
```

> **Note:** Get the `client_id` from the app details (`service_principal_client_id`) and generate the `client_secret` from the Account Console → Service Principals → Generate Secret.

### Add to Supervisor Agent

In Databricks Agent Bricks, add `economic_intelligence_mcp` as an **MCP Connection** tool in the agent's Tools configuration.

---

## Authentication

This server uses **OAuth M2M (Machine-to-Machine)** for authentication:

- The Unity Catalog connection holds `client_id` + `client_secret`
- Unity Catalog automatically fetches a fresh OAuth JWT before each call
- The Databricks Apps proxy validates the JWT and forwards the request
- Tools that call Databricks (e.g. `analyze_economic_webpage`) use on-behalf-of auth via the forwarded `x-forwarded-access-token` header

> ⚠️ PAT tokens (Personal Access Tokens) do **not** work with the Databricks Apps proxy — always use OAuth M2M for the UC connection.

---

## Local Development

```bash
# Install dependencies
pip install uv
uv sync

# Run locally
uv run economic-intelligence-mcp --port 8000
```

The server will be available at `http://localhost:8000` with the MCP endpoint at `http://localhost:8000/mcp`.

---

## References

- [MCP Specification (2025-03-26)](https://spec.modelcontextprotocol.io)
- [FastMCP Documentation](https://gofastmcp.com)
- [Databricks Apps Documentation](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html)
- [Databricks MCP Server Template](https://github.com/databricks/app-templates/tree/main/mcp-server-hello-world)
- [World Bank Open Data API](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392)
- [ABS SDMX REST API](https://www.abs.gov.au/about/data-services/application-programming-interfaces-apis/data-api-user-guide)
