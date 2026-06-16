import requests
import feedparser
import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
import anthropic
from pyairtable import Api

load_dotenv()

claude          = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
api             = Api(os.getenv("AIRTABLE_API_KEY"))
airtable_client = api.table(os.getenv("AIRTABLE_BASE_ID"), "Leads")
config_table    = api.table(os.getenv("AIRTABLE_BASE_ID"), "Config")
NEWS_API_KEY    = os.getenv("NEWS_API_KEY")
GUARDIAN_KEY    = os.getenv("GUARDIAN_API_KEY")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, application/json, */*",
}

# ── Search queries for NewsAPI and Guardian ───────────────────────────────────
NEWSAPI_QUERIES = [
    "renewable energy utility",
    "solar wind asset management",
    "IPP renewable acquisition",
    "energy utility digital transformation",
    "renewable energy SCADA monitoring",
    "wind solar portfolio expansion",
    "utility renewable operations",
    "energy storage battery grid",
    "renewable energy fund acquisition",
    "power plant asset performance",
]

GUARDIAN_QUERIES = [
    "renewable energy",
    "solar wind utility",
    "energy asset management",
    "independent power producer",
    "utility grid transformation",
]

# ── Renewable energy industry RSS feeds ───────────────────────────────────────
# These are the trade publications where signals appear first
RSS_FEEDS = [
    {"name": "PV Magazine",          "url": "https://www.pv-magazine.com/feed/"},
    {"name": "Energy Storage News",  "url": "https://www.energy-storage.news/feed/"},
    {"name": "Recharge News",        "url": "https://www.rechargenews.com/rss"},
    {"name": "Wind Power Engineering","url": "https://www.windpowerengineering.com/feed/"},
    {"name": "Renewable Energy World","url": "https://www.renewableenergyworld.com/feed/"},
    {"name": "Solar Power World",    "url": "https://www.solarpowerworldonline.com/feed/"},
    {"name": "CleanTechnica Solar",  "url": "https://cleantechnica.com/category/clean-power/solar-power/feed/"},
    {"name": "CleanTechnica Wind",   "url": "https://cleantechnica.com/category/clean-power/wind-power/feed/"},
    {"name": "Reuters Sustainability","url": "https://www.reutersagency.com/feed/?best-topics=sustainable-business"},
]

SIGNAL_KEYWORDS = [
    "renewable energy", "solar", "wind farm", "BESS", "battery storage",
    "asset management", "asset performance", "SCADA", "OEM monitoring",
    "utility", "power producer", "IPP", "generation portfolio",
    "reliability", "O&M", "operations", "grid", "Maximo", "SAP",
    "Powerfactors", "Aveva", "GE APM", "Bazefield", "megawatt", "MW", "GW",
    "digital transformation", "IoT", "analytics", "transmission",
    "interconnection", "power plant", "substation", "energy storage",
    "PPA", "power purchase agreement", "project finance", "commissioning",
    "acquisition", "portfolio expansion", "fund close"
]

# ── ICP scoring prompt — TEMPLATE filled from the Airtable Config table ────────
# The {placeholders} are populated at run time from Config so edits you save in
# the GTM Hub UI flow straight into the agent's scoring. The fixed scaffolding
# (scoring weights, competitor list, JSON schema) stays here so scoring stays
# consistent. Note: literal JSON braces are doubled {{ }} for str.format().
ICP_PROMPT_TEMPLATE = """You are an expert analyst for a renewable energy asset management SaaS company.
Analyze news articles and score companies as potential customers.

IDEAL CUSTOMER PROFILE:
Target organisations: {org_types}

FIRMOGRAPHIC CRITERIA (must meet to score above 40):
- Revenue / budget: {revenue}
- Renewables portfolio: {min_portfolio}
- Geographies: {geographies}
- Tech environment: Multi-vendor SCADA/OEM stack, Maximo/EAM, SAP/Oracle ERP

TARGET PERSONAS (the champion / economic buyer):
{champion}

CORE PAINS:
{core_pain}

HIGH-VALUE SIGNALS (raise score):
- Multiple OEM monitoring platforms in use
- Reliability assessment done manually in Excel
- Competitors mentioned: Greenbytes, Powerfactors, GE APM, Bazefield, Aveva PI
- New renewable assets added to an existing portfolio
- New VP/Head of Asset Management or Reliability Engineering hired
- Portfolio expansion or acquisition
- Digital transformation initiative

DISQUALIFIERS (score <= 15):
{disqualifiers}

SCORING (0-100):
- Firmographic fit: 30pts
- Signal strength and recency: 30pts
- Pain indicators detected: 20pts
- Tech environment match: 10pts
- Competitor displacement opportunity: 10pts

Return ONLY valid JSON, no markdown:
{{
  "company_name": "primary company name or null",
  "company_type": "Utility OR IPP OR Asset Manager OR Corporate Buyer OR Unknown",
  "signal_type": "portfolio_expansion OR executive_hire OR competitor_replacement OR digital_transformation OR new_assets OR regulatory_filing OR funding_round OR operational_challenge",
  "signal_summary": "2-3 sentences on what happened and why this company needs asset management software",
  "portfolio_mw": null,
  "asset_type": "Solar OR Wind OR BESS OR Mixed OR Unknown",
  "geography": "North America OR West Europe OR APAC OR MEA OR Japan OR Unknown",
  "pain_indicators": "specific pain signals detected or none detected",
  "tech_environment": "technology mentions or none detected",
  "competitor_signals": "competitor tools mentioned or none detected",
  "icp_score": 72,
  "score_rationale": "2 sentences explaining score",
  "outreach_tier": "Tier 1 OR Tier 2 OR Tier 3 OR Disqualified",
  "is_relevant": true
}}"""

# Fallbacks — used only if a Config row is missing or the table can't be read,
# so the agent always runs even with an empty/unreachable Config table.
ICP_DEFAULTS = {
    "icp_org_types":     "Large Energy & Utilities companies OR Utility-scale IPPs with renewables in their portfolio",
    "icp_revenue":       "$500M-$10B+ OR asset O&M budget >$100M",
    "icp_min_portfolio": ">100MW (solar, wind, BESS, or mixed)",
    "icp_geographies":   "North America, West Europe, APAC, Middle East & Africa, Japan",
    "icp_champion":      "COO, VP of Generation, Head/VP of Asset Management, Head of Reliability Engineering",
    "icp_core_pain":     ("Disconnected systems: unreliable asset data across multiple OEM platforms; "
                          "no single pane of glass across Solar, Wind, BESS; "
                          "manual reliability assessment in Excel; "
                          "losing generation but cannot identify root cause"),
    "icp_disqualifiers": "Single-site operator only; pure developer; in-house tool builders; revenue clearly below $500M",
}


def build_icp_prompt(cfg):
    """Fill the ICP template with Config values, falling back per-field."""
    def pick(key):
        v = (cfg.get(key) or "").strip()
        return v if v else ICP_DEFAULTS[key]
    return ICP_PROMPT_TEMPLATE.format(
        org_types=pick("icp_org_types"),
        revenue=pick("icp_revenue"),
        min_portfolio=pick("icp_min_portfolio"),
        geographies=pick("icp_geographies"),
        champion=pick("icp_champion"),
        core_pain=pick("icp_core_pain"),
        disqualifiers=pick("icp_disqualifiers"),
    )


def parse_queries(raw):
    """Accept either newline- or comma-separated query lists from the UI."""
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace("\r", "").split("\n") if p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts


def load_config_from_airtable():
    """Read the Config table into a {key: value} dict.
    Assumes the table has fields named 'Key' and 'Value'. If yours differ,
    adjust the field names in the two .get() calls below."""
    try:
        records = config_table.all()
    except Exception as e:
        print(f"  Could not read Config table ({e}) — using built-in defaults")
        return {}
    cfg = {}
    for rec in records:
        f = rec.get("fields", {})
        key = f.get("Key") or f.get("key") or f.get("Name")
        val = f.get("Value") if f.get("Value") is not None else f.get("value")
        if key is not None:
            cfg[str(key)] = "" if val is None else str(val)
    print(f"  Config table: loaded {len(cfg)} keys")
    return cfg


# Built once at import so score_with_icp() always has a usable prompt;
# run_agent() rebuilds it from live Config before each run.
ICP_SYSTEM_PROMPT = build_icp_prompt({})


# ── NewsAPI fetcher ────────────────────────────────────────────────────────────
def fetch_from_newsapi(days_back=30):
    if not NEWS_API_KEY:
        print("  Skipping NewsAPI - key not found")
        return []
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    items, seen = [], set()
    for query in NEWSAPI_QUERIES:
        try:
            r = requests.get("https://newsapi.org/v2/everything", params={
                "q": query, "language": "en", "sortBy": "publishedAt",
                "from": cutoff, "pageSize": 20, "apiKey": NEWS_API_KEY,
            }, headers=HEADERS, timeout=15)
            data = r.json()
            if data.get("status") != "ok":
                continue
            for a in data.get("articles", []):
                title = (a.get("title") or "").strip()
                if not title or title in seen or title == "[Removed]":
                    continue
                seen.add(title)
                items.append({
                    "title": title,
                    "summary": (a.get("description") or "")[:600],
                    "link": a.get("url", ""),
                    "published": (a.get("publishedAt") or "")[:10],
                    "source": a.get("source", {}).get("name", "NewsAPI")
                })
        except Exception as e:
            print(f"  NewsAPI exception: {e}")
    print(f"  NewsAPI:        {len(items)} articles")
    return items


# ── Guardian fetcher ───────────────────────────────────────────────────────────
def fetch_from_guardian(days_back=30):
    if not GUARDIAN_KEY:
        print("  Skipping Guardian - key not found")
        return []
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    items, seen = [], set()
    for query in GUARDIAN_QUERIES:
        try:
            r = requests.get("https://content.guardianapis.com/search", params={
                "q": query, "from-date": cutoff, "page-size": 50,
                "order-by": "newest", "show-fields": "trailText,headline",
                "api-key": GUARDIAN_KEY,
            }, headers=HEADERS, timeout=15)
            data = r.json()
            if data.get("response", {}).get("status") != "ok":
                continue
            for a in data.get("response", {}).get("results", []):
                fields = a.get("fields", {})
                title = (fields.get("headline") or a.get("webTitle") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                items.append({
                    "title": title,
                    "summary": (fields.get("trailText") or "")[:600],
                    "link": a.get("webUrl", ""),
                    "published": a.get("webPublicationDate", "")[:10],
                    "source": "The Guardian"
                })
        except Exception as e:
            print(f"  Guardian exception: {e}")
    print(f"  Guardian:       {len(items)} articles")
    return items


# ── RSS fetcher — industry trade publications ─────────────────────────────────
def fetch_from_rss(days_back=30):
    cutoff_date = datetime.now() - timedelta(days=days_back)
    items, seen = [], set()
    per_source_summary = []

    for feed in RSS_FEEDS:
        added = 0
        try:
            # Fetch with browser headers — many feeds block bare requests
            response = requests.get(feed["url"], headers=HEADERS, timeout=15)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)

            if not parsed.entries:
                per_source_summary.append(f"  {feed['name']:<25} 0 articles")
                continue

            for entry in parsed.entries:
                title = (entry.get("title") or "").strip()
                if not title or title in seen:
                    continue

                # Try to parse the published date
                published_dt = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    try:
                        published_dt = datetime(*entry.published_parsed[:6])
                    except Exception:
                        pass

                # If we have a date and it's too old, skip
                if published_dt and published_dt < cutoff_date:
                    continue

                summary_text = (entry.get("summary") or entry.get("description") or "")[:600]
                # Strip basic HTML
                summary_text = summary_text.replace("<p>", "").replace("</p>", " ")
                summary_text = summary_text.replace("<br>", " ").replace("<br/>", " ")

                seen.add(title)
                items.append({
                    "title": title,
                    "summary": summary_text,
                    "link": entry.get("link", ""),
                    "published": published_dt.strftime("%Y-%m-%d") if published_dt else "Unknown",
                    "source": feed["name"]
                })
                added += 1

            per_source_summary.append(f"  {feed['name']:<25} {added} articles")

        except requests.exceptions.RequestException:
            per_source_summary.append(f"  {feed['name']:<25} feed unavailable")
        except Exception as e:
            per_source_summary.append(f"  {feed['name']:<25} error: {str(e)[:40]}")

    print(f"  RSS feeds:      {len(items)} articles total")
    for line in per_source_summary:
        print(line)
    return items


# ── Combine all sources ────────────────────────────────────────────────────────
def fetch_all_signals(days_back=30):
    print(f"Fetching from all sources (last {days_back} days)...\n")
    all_items = []
    all_items.extend(fetch_from_newsapi(days_back))
    all_items.extend(fetch_from_guardian(days_back))
    all_items.extend(fetch_from_rss(days_back))

    # Final dedup across all sources
    seen = set()
    deduped = []
    for item in all_items:
        key = item["title"].lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    print(f"\nTotal unique articles across sources: {len(deduped)}")
    return deduped


# ── Relevance filter ───────────────────────────────────────────────────────────
def is_relevant(item):
    text = (item["title"] + " " + item["summary"]).lower()
    return any(kw.lower() in text for kw in SIGNAL_KEYWORDS)

def filter_relevant(items):
    relevant = [i for i in items if is_relevant(i)]
    print(f"Relevant after keyword filter: {len(relevant)} of {len(items)}")
    return relevant


# ── ICP scorer ─────────────────────────────────────────────────────────────────
def score_with_icp(item):
    user_prompt = f"""Title: {item['title']}
Published: {item['published']}
Source: {item['source']}
Summary: {item['summary']}
Link: {item['link']}"""

    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=700,
            system=ICP_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}]
        )
        raw = resp.content[0].text.strip()
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    raw = part
                    break
        result = json.loads(raw)
        return result if result.get("is_relevant") is True else None
    except Exception as e:
        print(f"  Claude error: {e}")
        return None


# ── Airtable writer ────────────────────────────────────────────────────────────
def write_to_airtable(data, item):
    company = data.get("company_name")
    if not company or company.lower() in ("null", "none", "unknown", ""):
        print("  Skipping - no company name")
        return
    score = data.get("icp_score")
    tier  = data.get("outreach_tier", "")
    if isinstance(score, int) and score < 25:
        print(f"  Disqualified ({score}/100) - not written")
        return
    try:
        existing = airtable_client.all(formula=f"{{Company Name}}='{company}'")
        if existing:
            print(f"  Duplicate: {company}")
            return
    except Exception:
        pass

    record = {"Status": "New", "Company Name": company}
    if data.get("signal_type"):        record["Signal Type"]     = data["signal_type"]
    if item.get("published") and item["published"] != "Unknown":
        record["Signal Date"] = item["published"]
    if data.get("signal_summary"):     record["Signal Summary"]  = data["signal_summary"]
    if data.get("asset_type"):         record["Asset Type"]      = data["asset_type"]
    if score is not None:
        try:                           record["ICP Score"]       = int(score)
        except (ValueError, TypeError): pass
    if data.get("score_rationale"):    record["Score Rationale"] = data["score_rationale"]
    if data.get("portfolio_mw") is not None:
        try:                           record["Portfolio MW"]    = float(data["portfolio_mw"])
        except (ValueError, TypeError): pass

    notes = []
    if data.get("company_type"):       notes.append(f"Type: {data['company_type']}")
    if data.get("geography"):          notes.append(f"Geo: {data['geography']}")
    if data.get("pain_indicators"):    notes.append(f"Pain: {data['pain_indicators']}")
    if data.get("tech_environment"):   notes.append(f"Tech: {data['tech_environment']}")
    if data.get("competitor_signals"): notes.append(f"Competitors: {data['competitor_signals']}")
    notes.append(f"Tier: {tier}")
    notes.append(f"Source: {item.get('link','')}")
    record["Notes"] = " | ".join(notes)

    try:
        airtable_client.create(record)
        print(f"  Written: {company} - {tier} ({score}/100) [{item['source']}]")
    except Exception as e:
        print(f"  Airtable error: {e}")


# ── Main runner ────────────────────────────────────────────────────────────────
def run_agent(progress=None):
    global NEWSAPI_QUERIES, GUARDIAN_QUERIES, ICP_SYSTEM_PROMPT

    def emit(stage, message, current=0, total=0):
        if progress:
            try:
                progress({"stage": stage, "message": message,
                          "current": current, "total": total})
            except Exception:
                pass

    emit("starting", "Loading configuration")
    print("=" * 60)
    print(f"Agent 01 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # ── Load live configuration from Airtable Config ──────────────────────────
    cfg = load_config_from_airtable()

    na = parse_queries(cfg.get("signals_newsapi"))
    if na:
        NEWSAPI_QUERIES = na
    gu = parse_queries(cfg.get("signals_guardian"))
    if gu:
        GUARDIAN_QUERIES = gu

    ICP_SYSTEM_PROMPT = build_icp_prompt(cfg)

    def as_int(val, default):
        try:
            return int(float(str(val).strip()))
        except (ValueError, TypeError, AttributeError):
            return default

    lookback = as_int(cfg.get("signals_lookback_days"), 30)
    thr1     = as_int(cfg.get("signals_score_tier1"), 75)
    thr2     = as_int(cfg.get("signals_score_tier2"), 50)

    print(f"Sources: NewsAPI ({len(NEWSAPI_QUERIES)} queries) + "
          f"Guardian ({len(GUARDIAN_QUERIES)} queries) + {len(RSS_FEEDS)} RSS feeds")
    print(f"Lookback: {lookback}d | Tier 1 >= {thr1} | Tier 2 >= {thr2}")
    print("ICP: loaded from Config table" if cfg else "ICP: built-in defaults (Config unavailable)")
    print("=" * 60)
    print()

    emit("fetching", "Fetching signals from NewsAPI, Guardian and RSS feeds")
    all_items = fetch_all_signals(days_back=lookback)
    if not all_items:
        print("\nNo articles fetched.")
        emit("done", "No articles fetched", 0, 0)
        return {"fetched": 0, "relevant": 0, "tier1": 0, "tier2": 0,
                "tier3": 0, "disqualified": 0, "new_leads": 0}

    print("\nFiltering for relevant signals...")
    emit("filtering", f"Filtering {len(all_items)} articles", 0, len(all_items))
    relevant = filter_relevant(all_items)
    if not relevant:
        print("No relevant articles found.")
        emit("done", "No relevant articles found", 0, 0)
        return {"fetched": len(all_items), "relevant": 0, "tier1": 0,
                "tier2": 0, "tier3": 0, "disqualified": 0, "new_leads": 0}

    print(f"\nScoring {len(relevant)} articles against ICP...")
    t1, t2, t3, disq = 0, 0, 0, 0

    for i, item in enumerate(relevant):
        preview = item['title'][:60] + "..." if len(item['title']) > 60 else item['title']
        print(f"\n[{i+1}/{len(relevant)}] {preview}")
        emit("scoring", f"Scoring: {preview}", i + 1, len(relevant))
        result = score_with_icp(item)
        if result:
            sc = result.get("icp_score")
            if isinstance(sc, int):
                if   sc >= thr1: result["outreach_tier"] = "Tier 1"
                elif sc >= thr2: result["outreach_tier"] = "Tier 2"
                elif sc >= 25:   result["outreach_tier"] = "Tier 3"
                else:            result["outreach_tier"] = "Disqualified"
            tier = result.get("outreach_tier", "")
            if "Tier 1" in tier:   t1 += 1
            elif "Tier 2" in tier: t2 += 1
            elif "Tier 3" in tier: t3 += 1
            else:                  disq += 1
            write_to_airtable(result, item)
        else:
            print("  Not a relevant company - skipping")
            disq += 1

    new_leads = t1 + t2 + t3
    print("\n" + "=" * 60)
    print(f"Run complete: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Total fetched:   {len(all_items)}")
    print(f"  Keyword match:   {len(relevant)}")
    print(f"  Tier 1:          {t1}  (immediate outreach)")
    print(f"  Tier 2:          {t2}  (queue this week)")
    print(f"  Tier 3:          {t3}  (nurture)")
    print(f"  Disqualified:    {disq}")
    print("=" * 60)

    summary = {"fetched": len(all_items), "relevant": len(relevant),
               "tier1": t1, "tier2": t2, "tier3": t3,
               "disqualified": disq, "new_leads": new_leads}
    emit("done",
         f"Done — {new_leads} qualified lead(s): {t1} Tier 1, {t2} Tier 2, {t3} Tier 3",
         len(relevant), len(relevant))
    return summary


if __name__ == "__main__":
    run_agent()
