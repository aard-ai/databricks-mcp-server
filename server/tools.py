"""
MCP tools for external economic intelligence.

Tools exposed to the Supervisor Agent:
  - get_inflation            : Latest CPI inflation for any country (World Bank API)
  - get_macro_indicators     : CPI, GDP growth, interest rate, unemployment (World Bank API)
  - analyze_economic_webpage : Fetch a webpage + analyse it using Foundation Model API
  - list_tnso_dataflows      : List curated TNSO (Thailand NSO) SDMX dataflows
  - get_tnso_data            : Fetch time-series data from the TNSO SDMX REST API
  - web_search               : Search the web for economic data and news (DuckDuckGo)
"""

import httpx

from server import utils

WORLD_BANK_BASE = "https://api.worldbank.org/v2"
# Thailand National Statistical Office (TNSO) — SIS-CC .Stat Suite, SDMX-REST v1.
# Serves SDMX-JSON 1.0 (singular `data.structure`) on
# `Accept: application/vnd.sdmx.data+json;version=1.0`. No auth required.
TNSO_BASE = "https://ns1-stathub.nso.go.th/rest"
TNSO_AGENCY = "TNSO"

# Curated TNSO dataflows with live-verified SDMX dimension orders and example keys.
# Harvested from GET {TNSO_BASE}/dataflow/TNSO on 2026-07-19 (909 flows total);
# dimension orders and observations confirmed live against the data endpoint.
# Key format: dimension values joined by "." (positional, DSD order) — an empty
# segment means "all values for that dimension".
# NOTE: TNSO TIME_PERIOD values are Buddhist-Era (BE) years — subtract 543 for the
# Gregorian year (e.g. 2564 = 2021). Dimension labels are returned in English.
TNSO_DATAFLOWS: dict[str, dict] = {
    "DF_01DI_IND_AGING": {
        "description": "Aging Index — ratio of elderly to child population, by province (every 3 years)",
        "version": "1.0",
        "dimensions": "POP_IND . SEX . AREA . CWT . UNIT_MUL . UNIT . FREQ",
        "common_keys": {
            "Aging index, total, Mae Hong Son province (CWT=58)": "DEM_IND101._T._T.58.0.INX.A3",
            "All provinces / all measures (large)":               "all",
        },
    },
    "DF_01LET": {
        "description": "Life expectancy of the population, by area, age and sex (annual)",
        "version": "1.1",
        "dimensions": "FREQ . AREA . AGE . SEX . POP_IND . UNIT",
        "common_keys": {
            "All series (small flow, ~8 series)": "all",
        },
    },
    "DF_01POP_DENSITY": {
        "description": "Density of population, by province (persons per sq. km)",
        "version": "1.1",
        "dimensions": "POP_SUBJ . POP_IND . AREA . CWT . FREQ . UNIT",
        "common_keys": {
            "All provinces / all measures": "all",
        },
    },
    "DF_01DI_POP_CENS": {
        "description": "Census population, by age group, area and municipality",
        "version": "1.0",
        "dimensions": "POP_IND . AGE_GP . AREA . MUNICIPAL . UNIT . FREQ",
        "common_keys": {
            "All series": "all",
        },
    },
    "DF_01HH_TOILET": {
        "description": "Households using sanitary toilets, by facility type and province (%)",
        "version": "1.2",
        "dimensions": "STAT_LIST . LIST_STATUS . TOILET_FACILITY . AREA . CWT . UNIT . FREQ",
        "common_keys": {
            "All series": "all",
        },
    },
    "DF_01HH_ENERGY": {
        "description": "Household energy consumption, by cooking-fuel type and area",
        "version": "1.0",
        "dimensions": "STAT_LIST . LIST_STATUS . COOKING_FUEL . AREA . UNIT . FREQ",
        "common_keys": {
            "All series (small flow, ~48 series)": "all",
        },
    },
    "DF_01POP_MID": {
        "description": "Mid-year population, by age group, sex and province",
        "version": "2.0",
        "dimensions": "STAT_LIST . LIST_STATUS . AGE_GP . SEX . AREA . CWT . UNIT . FREQ",
        "common_keys": {
            "All series (use start_period to limit)": "all",
        },
    },
    "DF_02AWE": {
        "description": "Average wages of employees, by sex, education, industry and region (large)",
        "version": "1.0",
        "dimensions": (
            "STAT_LIST . LIST_STATUS . SEX . AGE_GROUP . LEV_OF_EDU . INDUSTRY . "
            "REGION . AREA_MUNICIPAL . UNIT_MUL . UNIT . FREQ"
        ),
        "common_keys": {
            "All series — 1600+ series, scope with key/start_period": "all",
        },
    },
}


def _parse_sdmx_json(response_data: dict, max_series: int = 50, max_obs: int = 8) -> list[dict]:
    """
    Parse SDMX-JSON 1.0 (.Stat Suite) compact format into a flat list of records.

    Each record contains all series dimension label values, the time period,
    and the observation value. TIME_PERIOD is emitted verbatim; for TNSO this is
    a Buddhist-Era year (subtract 543 for Gregorian).
    """
    data_block = response_data.get("data", {})
    structure = data_block.get("structure", {})
    datasets = data_block.get("dataSets", [])
    if not datasets:
        return []

    dataset = datasets[0]
    dims = structure.get("dimensions", {})
    series_dims = dims.get("series", [])
    obs_dims = dims.get("observation", [])

    # Build TIME_PERIOD index → period string
    time_dim = next((d for d in obs_dims if d["id"] == "TIME_PERIOD"), obs_dims[0] if obs_dims else None)
    time_index: dict[str, str] = {}
    if time_dim:
        time_index = {str(i): v["id"] for i, v in enumerate(time_dim["values"])}

    records: list[dict] = []
    series_items = list(dataset.get("series", {}).items())[:max_series]

    for series_key, series_val in series_items:
        # Decode positional key ("0:1:2:0:1") to human-readable dimension labels
        key_parts = series_key.split(":")
        labels: dict[str, str] = {}
        for i, dim in enumerate(series_dims):
            if i < len(key_parts):
                idx = int(key_parts[i])
                vals = dim.get("values", [])
                if idx < len(vals):
                    labels[dim["id"]] = vals[idx].get("name") or vals[idx].get("id", "")

        # Most-recent observations first
        observations = series_val.get("observations", {})
        sorted_obs = sorted(observations.items(), key=lambda x: int(x[0]), reverse=True)

        for obs_idx, obs_val in sorted_obs[:max_obs]:
            value = obs_val[0] if obs_val else None
            if value is not None:
                records.append({
                    **labels,
                    "period": time_index.get(obs_idx, obs_idx),
                    "value": value,
                })

    return records

DEFAULT_INDICATORS: dict[str, str] = {
    "FP.CPI.TOTL.ZG":    "Inflation, consumer prices (annual %)",
    "NY.GDP.MKTP.KD.ZG": "GDP growth (annual %)",
    "FR.INR.RINR":       "Real interest rate (%)",
    "SL.UEM.TOTL.ZS":   "Unemployment, total (% of labour force)",
}

# Databricks Foundation Model API endpoint
CLAUDE_ENDPOINT = "databricks-claude-sonnet-4-6"


def load_tools(mcp_server) -> None:
    """Register all economic-intelligence tools with the MCP server."""

    # ── 1. Health check ────────────────────────────────────────────────────────

    @mcp_server.tool
    def health() -> dict:
        """
        Check the health of the MCP server and Databricks connection.

        Returns:
            dict: status and message confirming the server is running.
        """
        return {
            "status": "healthy",
            "message": "Economic Intelligence MCP Server is running.",
            "tools": [
                "get_inflation",
                "get_macro_indicators",
                "analyze_economic_webpage",
                "list_tnso_dataflows",
                "get_tnso_data",
            ],
        }

    # ── 2. Inflation rate (World Bank) ─────────────────────────────────────────

    @mcp_server.tool
    def get_inflation(country_code: str = "AU", year: int | None = None) -> dict:
        """
        Get the latest CPI inflation rate for a country from the World Bank API.

        Use this tool when asked about inflation rates, cost-of-living trends,
        or CPI figures for any country.

        Args:
            country_code: ISO country code — e.g. 'AU' (Australia), 'US', 'JP',
                          'GB', 'CN', 'IN', 'SG', 'NZ'. Default is 'AU'.
            year:         Specific year (optional). Defaults to most recent available.

        Returns:
            dict with keys: country, year, inflation_rate_pct, indicator, source, source_url
        """
        params: dict = {"format": "json", "per_page": 5}
        if year:
            params["date"] = str(year)
        else:
            params["mrv"] = 1

        url = f"{WORLD_BANK_BASE}/country/{country_code}/indicator/FP.CPI.TOTL.ZG"
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()

            records = data[1] if len(data) > 1 else []
            for rec in records:
                if rec.get("value") is not None:
                    return {
                        "country": rec.get("country", {}).get("value", country_code),
                        "country_code": country_code.upper(),
                        "year": rec.get("date"),
                        "inflation_rate_pct": round(rec["value"], 2),
                        "indicator": "CPI Inflation (annual %)",
                        "source": "World Bank Open Data",
                        "source_url": (
                            f"https://data.worldbank.org/indicator/"
                            f"FP.CPI.TOTL.ZG?locations={country_code}"
                        ),
                    }
            return {
                "country_code": country_code.upper(),
                "error": "No data found for this country/year combination.",
                "source_url": (
                    f"https://data.worldbank.org/indicator/"
                    f"FP.CPI.TOTL.ZG?locations={country_code}"
                ),
            }
        except Exception as e:
            return {"country_code": country_code.upper(), "error": str(e)}

    # ── 3. Multiple macro indicators (World Bank) ──────────────────────────────

    @mcp_server.tool
    def get_macro_indicators(
        country_code: str = "AU",
        indicators: list[str] | None = None,
    ) -> list[dict]:
        """
        Fetch multiple macroeconomic indicators for a country from the World Bank API.

        Use this tool when asked for a broad economic overview, or when multiple
        indicators (inflation, GDP, interest rates, unemployment) are needed at once.

        Default indicators fetched:
          - FP.CPI.TOTL.ZG   — Inflation, CPI (annual %)
          - NY.GDP.MKTP.KD.ZG — GDP growth (annual %)
          - FR.INR.RINR        — Real interest rate (%)
          - SL.UEM.TOTL.ZS   — Unemployment (% of labour force)

        Args:
            country_code: ISO country code e.g. 'AU', 'US', 'CN', 'IN', 'JP'.
            indicators:   Optional list of World Bank indicator codes to override defaults.

        Returns:
            List of dicts: [{indicator_code, indicator_name, value, year, country, source_url}]
        """
        codes = indicators or list(DEFAULT_INDICATORS.keys())
        results = []

        with httpx.Client(timeout=15) as client:
            for code in codes:
                url = f"{WORLD_BANK_BASE}/country/{country_code}/indicator/{code}"
                try:
                    resp = client.get(
                        url, params={"format": "json", "mrv": 1, "per_page": 5}
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    records = data[1] if len(data) > 1 else []
                    for rec in records:
                        if rec.get("value") is not None:
                            results.append({
                                "indicator_code": code,
                                "indicator_name": DEFAULT_INDICATORS.get(code, code),
                                "country": rec.get("country", {}).get("value", country_code),
                                "country_code": country_code.upper(),
                                "year": rec.get("date"),
                                "value": round(rec["value"], 3),
                                "source": "World Bank Open Data",
                                "source_url": (
                                    f"https://data.worldbank.org/indicator/"
                                    f"{code}?locations={country_code}"
                                ),
                            })
                            break
                except Exception as e:
                    results.append({
                        "indicator_code": code,
                        "country_code": country_code.upper(),
                        "error": str(e),
                    })

        return results

    # ── 4. Webpage analysis via Claude_for_Analysis ────────────────────────────

    @mcp_server.tool
    def analyze_economic_webpage(url: str, question: str) -> str:
        """
        Fetch a public webpage and use the Claude_for_Analysis Databricks model
        serving endpoint to extract and answer a question about economic data on it.

        Use this tool for sources that don't have a structured API, such as:
          - Reserve Bank of Australia: https://www.rba.gov.au/statistics/
          - ABS CPI release: https://www.abs.gov.au/statistics/economy/prices/consumer-price-index-australia/latest-release
          - US Bureau of Labor Statistics: https://www.bls.gov/cpi/
          - OECD data explorer: https://data.oecd.org/
          - IMF World Economic Outlook: https://www.imf.org/en/Publications/WEO

        Args:
            url:      Full URL of the webpage to fetch and analyse.
            question: Specific question about the data on the page —
                      e.g. 'What is the latest quarterly CPI figure and which
                      category drove the increase?'

        Returns:
            Claude's answer with specific figures, units, and dates from the page.
        """
        # 1. Fetch the page
        try:
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                resp = client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; DatabricksMCPBot/1.0)"},
                )
                resp.raise_for_status()
                page_text = resp.text[:12000]  # cap to avoid token overflow
        except Exception as e:
            return f"Failed to fetch {url}: {e}"

        # 2. Call Foundation Model API via REST
        try:
            w = utils.get_user_authenticated_workspace_client()
            payload = {
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are an economic data analyst. Extract specific data points "
                            "and answer questions accurately using only the provided webpage "
                            "content. Always include exact figures, units, and dates."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Webpage URL: {url}\n\n"
                            f"Page content:\n{page_text}\n\n"
                            f"Question: {question}"
                        ),
                    },
                ],
                "max_tokens": 500,
            }
            resp = w.api_client.do(
                "POST",
                f"/serving-endpoints/{CLAUDE_ENDPOINT}/invocations",
                body=payload,
            )
            return resp["choices"][0]["message"]["content"]
        except Exception as e:
            return f"Model call failed: {e}"

    # ── 5. List ABS dataflows ──────────────────────────────────────────────────

    @mcp_server.tool
    def list_tnso_dataflows() -> dict:
        """
        List curated Thailand National Statistical Office (TNSO) SDMX dataflows
        that can be queried with get_tnso_data.

        Use this tool first to discover what TNSO data is available before
        calling get_tnso_data. Each entry gives the dataflow's version, the
        dimension order (for building SDMX keys), and example keys.

        Returns:
            dict mapping dataflow_id → {description, version, dimensions, common_keys}
        """
        return TNSO_DATAFLOWS

    # ── 6. Fetch ABS SDMX data ─────────────────────────────────────────────────

    @mcp_server.tool
    def get_tnso_data(
        dataflow: str,
        key: str = "all",
        start_period: str | None = None,
        end_period: str | None = None,
        version: str | None = None,
        agency: str = TNSO_AGENCY,
        max_series: int = 20,
        max_obs_per_series: int = 8,
    ) -> dict:
        """
        Fetch time-series data from the Thailand National Statistical Office (TNSO)
        SDMX REST API (https://ns1-stathub.nso.go.th/rest). No API key required.

        Use list_tnso_dataflows first to see available dataflows, their dimension
        orders, and common key patterns.

        Curated dataflows (see list_tnso_dataflows for full detail):
          - DF_01DI_IND_AGING  Aging Index by province (every 3 years)
          - DF_01LET           Life expectancy (annual)
          - DF_01POP_DENSITY   Population density by province
          - DF_01DI_POP_CENS   Census population by age group
          - DF_01HH_TOILET     Households using sanitary toilets (%)
          - DF_01HH_ENERGY     Household energy consumption by fuel type
          - DF_01POP_MID       Mid-year population by age group and sex
          - DF_02AWE           Average wages of employees (large)

        SDMX key syntax (positional, dot-separated):
          Each position corresponds to a dimension in DSD order (see `dimensions`
          in list_tnso_dataflows). Leave a position blank to mean "all values".
          Example for DF_01DI_IND_AGING (POP_IND.SEX.AREA.CWT.UNIT_MUL.UNIT.FREQ):
            "DEM_IND101._T._T.58.0.INX.A3" → aging index, total, Mae Hong Son, index, 3-yearly

        IMPORTANT: TNSO TIME_PERIOD values are Buddhist-Era years — subtract 543 for
        the Gregorian year (e.g. period "2564" = 2021). start_period / end_period, if
        given, must also be Buddhist-Era.

        Args:
            dataflow:             TNSO dataflow ID (e.g. "DF_01DI_IND_AGING").
            key:                  SDMX key filter (default "all" returns everything).
                                  Use common_keys from list_tnso_dataflows for presets.
            start_period:         Start of date range in BE years — e.g. "2560".
            end_period:           End of date range (optional, defaults to latest available).
            version:              Optional dataflow version (e.g. "1.0"). Omit for latest.
            agency:               SDMX agency id (default "TNSO"; this node also hosts
                                  "IAEG-SDGs" SDG-indicator flows).
            max_series:           Maximum number of data series to return (default 20).
            max_obs_per_series:   Most-recent observations per series (default 8).

        Returns:
            dict with keys:
              - dataflow:   the dataflow ID queried
              - source:     "Thailand National Statistical Office"
              - source_url: link to the TNSO stat-hub
              - records:    list of flat dicts [{dimension_labels..., period, value}]
              - series_count: total series returned
              - error:      present only if the request failed
        """
        params: dict[str, str] = {"detail": "dataOnly"}
        if start_period:
            params["startPeriod"] = start_period
        if end_period:
            params["endPeriod"] = end_period

        flow_ref = f"{agency},{dataflow.upper()}"
        if version:
            flow_ref += f",{version}"
        url = f"{TNSO_BASE}/data/{flow_ref}/{key}"

        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.get(
                    url,
                    params=params,
                    headers={
                        "Accept": "application/vnd.sdmx.data+json;version=1.0",
                        "Accept-Language": "en",
                    },
                )
                resp.raise_for_status()
                raw = resp.json()

            records = _parse_sdmx_json(raw, max_series=max_series, max_obs=max_obs_per_series)

            return {
                "dataflow": dataflow.upper(),
                "source": "Thailand National Statistical Office",
                "source_url": "https://ns1-stathub.nso.go.th",
                "key_used": key,
                "start_period": start_period,
                "end_period": end_period,
                "series_count": len({
                    tuple(
                        (k, v) for k, v in r.items() if k not in ("period", "value")
                    )
                    for r in records
                }),
                "records": records,
            }

        except httpx.HTTPStatusError as e:
            return {
                "dataflow": dataflow.upper(),
                "error": f"HTTP {e.response.status_code}: {e.response.text[:300]}",
                "hint": (
                    "Check the dataflow ID and key syntax. "
                    "Use list_tnso_dataflows to see available dataflows and key examples."
                ),
            }
        except Exception as e:
            return {"dataflow": dataflow.upper(), "error": str(e)}

    # ── 7. Web search (DuckDuckGo HTML) ─────────────────────────────────────

    @mcp_server.tool
    def web_search(
        query: str,
        max_results: int = 5,
        region: str = "wt-wt",
        time_range: str | None = None,
    ) -> list[dict]:
        """
        Search the web for economic data, news, reports, or any topic using DuckDuckGo.

        Use this tool when you need to find recent information, news articles,
        or data sources that are not available through the other specialised tools.

        Args:
            query:       Search query — e.g. 'Australia inflation rate 2026',
                         'RBA interest rate decision March 2026'.
            max_results: Number of results to return (default 5, max 20).
            region:      Region code for results — e.g. 'au-en' (Australia),
                         'us-en' (US), 'uk-en' (UK), 'wt-wt' (global, default).
            time_range:  Filter by recency — 'd' (past day), 'w' (past week),
                         'm' (past month), 'y' (past year), or None (all time).

        Returns:
            List of dicts with keys: title, href, body (snippet).
        """
        import re

        try:
            params = {"q": query, "kl": region}
            if time_range:
                params["df"] = time_range

            with httpx.Client(timeout=15, follow_redirects=True) as client:
                resp = client.get(
                    "https://html.duckduckgo.com/html/",
                    params=params,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/120.0.0.0 Safari/537.36",
                    },
                )
                resp.raise_for_status()
                html = resp.text

            results = []
            # Parse result blocks from DuckDuckGo HTML
            blocks = re.findall(
                r'<a rel="nofollow" class="result__a" href="([^"]*)"[^>]*>(.*?)</a>'
                r'.*?<a class="result__snippet"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )
            for href, title, body in blocks[:min(max_results, 20)]:
                # Clean HTML tags from title and body
                clean = lambda s: re.sub(r"<[^>]+>", "", s).strip()
                # Decode DuckDuckGo redirect URL
                from urllib.parse import unquote, parse_qs, urlparse
                parsed = urlparse(href)
                actual_url = parse_qs(parsed.query).get("uddg", [href])[0]
                results.append({
                    "title": clean(title),
                    "href": unquote(actual_url),
                    "body": clean(body),
                })

            return results if results else [{"message": "No results found", "query": query}]
        except Exception as e:
            return [{"error": str(e), "query": query}]
