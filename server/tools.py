"""
MCP tools for external economic intelligence.

Tools exposed to the Supervisor Agent:
  - get_inflation            : Latest CPI inflation for any country (World Bank API)
  - get_macro_indicators     : CPI, GDP growth, interest rate, unemployment (World Bank API)
  - analyze_economic_webpage : Fetch a webpage + analyse it using Foundation Model API
  - list_abs_dataflows       : List available OECD SDMX dataflows (OECD.SDD.TPS)
  - get_abs_data             : Fetch time-series data from the OECD SDMX REST API
  - web_search               : Search the web for economic data and news (DuckDuckGo)
"""

import httpx

from server import utils

WORLD_BANK_BASE = "https://api.worldbank.org/v2"
OECD_BASE = "https://sdmx.oecd.org/public/rest"

# Curated major OECD dataflows (agency OECD.SDD.TPS) with live-verified SDMX keys.
# OECD data queries need the full flow reference {agency},{flow},{version}; ids follow the
# DSD_<x>@DF_<y> convention and live under sub-agencies (bare agency "OECD" has zero flows).
# Key format: dimension values joined by "." — an empty segment means "all values" for that
# dimension. Every example key below was decoded from a live series that returned an observation.
OECD_DATAFLOWS: dict[str, dict] = {
    "DSD_CPI_COU_WEIGHTS@DF_CPI_CTRY_WEIGHTS": {
        "agency": "OECD.SDD.TPS",
        "version": "1.0",
        "description": "CPI country weights — OECD composition (annual)",
        "dimensions": "REF_AREA . FREQ . MEASURE . UNIT_MEASURE",
        "common_keys": {
            "Australia, CPI weight (all measures)":       "AUS...",
            "Australia, annual, fully specified":         "AUS.A.CPI_W.PT_CPI_OECD",
        },
    },
    "DSD_G20_PRICES@DF_G20_PRICES": {
        "agency": "OECD.SDD.TPS",
        "version": "1.0",
        "description": "G20 — Consumer price indices, all items",
        "dimensions": "REF_AREA . FREQ . METHODOLOGY . MEASURE . UNIT_MEASURE . EXPENDITURE . ADJUSTMENT . TRANSFORMATION",
        "common_keys": {
            "China, quarterly CPI, period-on-period growth": "CHN.Q.N.CPI.PC._T.N.G1",
        },
    },
    "DSD_PRICES@DF_PRICES_ALL": {
        "agency": "OECD.SDD.TPS",
        "version": "1.0",
        "description": "Consumer price indices (CPIs, HICPs), COICOP 1999 — very large flow; always pass a scoped key + start_period",
        "dimensions": "REF_AREA . FREQ . METHODOLOGY . MEASURE . UNIT_MEASURE . EXPENDITURE . ADJUSTMENT . TRANSFORMATION",
        "common_keys": {
            "Belgium, quarterly CPI index": "BEL.Q.N.CPI.IX._T.N._Z",
        },
    },
    "DSD_LFS@DF_IALFS_UNE_M": {
        "agency": "OECD.SDD.TPS",
        "version": "1.0",
        "description": "Monthly unemployment rate (infra-annual labour statistics)",
        "dimensions": "REF_AREA . MEASURE . UNIT_MEASURE . TRANSFORMATION . ADJUSTMENT . SEX . AGE . ACTIVITY . FREQ",
        "common_keys": {
            "Greece, male, 15-24, unemployment rate": "GRC.UNE_LF_M.PT_LF_SUB._Z.N.M.Y15T24._Z.A",
        },
    },
    "DSD_LFS@DF_IALFS_EMP_WAP_Q": {
        "agency": "OECD.SDD.TPS",
        "version": "1.0",
        "description": "Employment rate — share of working-age population",
        "dimensions": "REF_AREA . MEASURE . UNIT_MEASURE . TRANSFORMATION . ADJUSTMENT . SEX . AGE . ACTIVITY . FREQ",
        "common_keys": {
            "Ireland, male, 25-54, employment rate": "IRL.EMP_WAP.PT_WAP_SUB._Z.Y.M.Y25T54._Z.A",
        },
    },
    "DSD_PDB@DF_PDB_ULC_Q": {
        "agency": "OECD.SDD.TPS",
        "version": "1.0",
        "description": "Productivity and unit labour costs (quarterly)",
        "dimensions": "REF_AREA . FREQ . MEASURE . ACTIVITY . UNIT_MEASURE . PRICE_BASE . TRANSFORMATION . ADJUSTMENT . CONVERSION_TYPE",
        "common_keys": {
            "Denmark, GDP per person employed, YoY growth": "DNK.Q.GDPEMP._T.PA.Q.GY.S.NC",
        },
    },
}


def _parse_sdmx_json(response_data: dict, max_series: int = 50, max_obs: int = 8) -> list[dict]:
    """
    Parse SDMX-JSON 1.0 (data.structure singular + series/observation split) into
    a flat list of records. OECD serves this shape when Accept carries version=1.0.

    Each record contains all series dimension label values, the time period,
    and the observation value.
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

        # Most-recent observations first.
        # NOTE: OECD does NOT return TIME_PERIOD values in chronological order, so the
        # observation *index* is not a proxy for recency (index 53 → "1993", 2024 → index 3).
        # Sort by the resolved period string, which orders correctly for plain years and for
        # ISO-style quarter/month keys, and is also chronological for ABS/DotStat.
        observations = series_val.get("observations", {})
        sorted_obs = sorted(
            observations.items(),
            key=lambda kv: time_index.get(kv[0], kv[0]),
            reverse=True,
        )

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
                "list_abs_dataflows",
                "get_abs_data",
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
    def list_abs_dataflows() -> dict:
        """
        List the available OECD (agency OECD.SDD.TPS) SDMX dataflows that can be
        queried with get_abs_data.

        Use this tool first to discover what OECD data is available before calling
        get_abs_data. Each entry carries the sub-agency id and version needed to
        build the OECD flow reference, plus live-verified example keys.

        Returns:
            dict mapping dataflow_id → {agency, version, description, dimensions, common_keys}
        """
        return OECD_DATAFLOWS

    # ── 6. Fetch ABS SDMX data ─────────────────────────────────────────────────

    @mcp_server.tool
    def get_abs_data(
        dataflow: str,
        key: str = "all",
        agency: str | None = None,
        version: str | None = None,
        start_period: str | None = None,
        end_period: str | None = None,
        max_series: int = 20,
        max_obs_per_series: int = 8,
    ) -> dict:
        """
        Fetch time-series data from the OECD SDMX REST API
        (https://sdmx.oecd.org/public/rest). No API key required.

        Use list_abs_dataflows first to see available dataflows, their sub-agency,
        version, dimension order, and common key patterns.

        OECD flow references are {agency},{dataflow},{version}. Content lives under
        sub-agency ids like OECD.SDD.TPS (the bare agency "OECD" has zero flows), and
        dataflow ids follow the DSD_<x>@DF_<y> convention. When the dataflow is one of
        the curated OECD_DATAFLOWS entries, its agency and version are looked up
        automatically; otherwise pass them explicitly (agency defaults to OECD.SDD.TPS,
        version to 1.0).

        Common dataflows (agency OECD.SDD.TPS):
          - DSD_CPI_COU_WEIGHTS@DF_CPI_CTRY_WEIGHTS  CPI country weights (annual)
          - DSD_G20_PRICES@DF_G20_PRICES             G20 consumer price indices
          - DSD_PRICES@DF_PRICES_ALL                 CPIs/HICPs COICOP 1999 (large)
          - DSD_LFS@DF_IALFS_UNE_M                    Monthly unemployment rate
          - DSD_LFS@DF_IALFS_EMP_WAP_Q                Employment rate
          - DSD_PDB@DF_PDB_ULC_Q                      Productivity & unit labour costs

        SDMX key syntax (positional, dot-separated):
          Each position corresponds to a dimension in order (see list_abs_dataflows).
          Leave a position blank to mean "all values" for that dimension.
          Examples for DSD_CPI_COU_WEIGHTS@DF_CPI_CTRY_WEIGHTS
            (REF_AREA.FREQ.MEASURE.UNIT_MEASURE):
            "AUS..."                  → Australia, all frequencies/measures/units
            "AUS.A.CPI_W.PT_CPI_OECD" → Australia, annual, fully specified

        Args:
            dataflow:             OECD dataflow ID (e.g. "DSD_CPI_COU_WEIGHTS@DF_CPI_CTRY_WEIGHTS").
            key:                  SDMX key filter (default "all" returns everything).
                                  Use common_keys from list_abs_dataflows for useful presets.
            agency:               OECD sub-agency id (e.g. "OECD.SDD.TPS"). Optional — resolved
                                  from the curated catalogue, else defaults to OECD.SDD.TPS.
            version:              Dataflow version (e.g. "1.0"). Optional — resolved from the
                                  catalogue, else defaults to "1.0".
            start_period:         Start of date range — e.g. "2020-Q1", "2020-01", "2015".
            end_period:           End of date range (optional, defaults to latest available).
            max_series:           Maximum number of data series to return (default 20).
            max_obs_per_series:   Most-recent observations per series (default 8).

        Returns:
            dict with keys:
              - dataflow:   the dataflow ID queried
              - agency:     the resolved sub-agency id
              - version:    the resolved dataflow version
              - source:     "OECD"
              - source_url: link to the OECD Data Explorer
              - records:    list of flat dicts [{dimension_labels..., period, value}]
              - series_count: total series returned
              - error:      present only if the request failed
        """
        params: dict[str, str] = {"detail": "dataOnly"}
        if start_period:
            params["startPeriod"] = start_period
        if end_period:
            params["endPeriod"] = end_period

        flow_id = dataflow.upper()
        entry = OECD_DATAFLOWS.get(flow_id, {})
        resolved_agency = agency or entry.get("agency") or "OECD.SDD.TPS"
        resolved_version = version or entry.get("version") or "1.0"

        # OECD requires the full flow reference {agency},{flow},{version} (short form 404s)
        url = f"{OECD_BASE}/data/{resolved_agency},{flow_id},{resolved_version}/{key}"

        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.get(
                    url,
                    params=params,
                    headers={"Accept": "application/vnd.sdmx.data+json;version=1.0"},
                )
                resp.raise_for_status()
                raw = resp.json()

            records = _parse_sdmx_json(raw, max_series=max_series, max_obs=max_obs_per_series)

            return {
                "dataflow": flow_id,
                "agency": resolved_agency,
                "version": resolved_version,
                "source": "OECD",
                "source_url": "https://data-explorer.oecd.org/",
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
                    "Use list_abs_dataflows to see available dataflows and key examples."
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
