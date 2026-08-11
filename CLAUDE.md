# Argo — Content Ops Genie App

## What This Is

Argo is a Databricks App (Streamlit) that provides natural-language access to Content Operations metrics via Databricks Genie spaces. Users ask questions in English; an LLM routes them to the correct Genie space, which generates and executes SQL.

## Security — PUBLIC REPO

This repo is **public**. Never commit:
- API tokens, secrets, passwords, or credentials
- Databricks workspace URLs or host values
- Internal email addresses or PII
- Airtable API keys or base IDs
- Service principal secrets

All secrets are injected via **environment variables** set in the Databricks App deployment config (`app.yaml` references them, but values live in Databricks). If you need a new secret, add it as an env var reference in `app.yaml` and configure the actual value in the Databricks App settings.

## Architecture Overview

```
User Question
    │
    ▼
query_decomposer.py  (Haiku — classifies single vs multi-space)
    │
    ▼
genie_client.py  (sends question to Genie space, polls for result)
    │
    ▼
result_synthesizer.py  (Sonnet — combines multi-space results into prose)
    │
    ▼
app.py  (Streamlit UI — displays answer + dataframe + SQL)
```

### Key Files

| File | Purpose |
|------|---------|
| `app.py` | Main Streamlit app. Contains `GENIE_SPACES` dict, routing integration, UI. |
| `query_decomposer.py` | LLM-based query router. System prompt describes all spaces. |
| `result_synthesizer.py` | Combines results from multiple Genie spaces into one answer. |
| `llm_client.py` | Thin wrapper around Databricks Foundation Model serving endpoints. |
| `genie_client.py` | Handles Genie API: start-conversation, poll for result, parse response. |
| `conversation_store.py` | Persists chat history to a Delta table. |
| `tools/` | Sidebar tools (each is a standalone Streamlit component). |
| `notebooks/` | Table-building notebooks that materialize the backing data. |
| `benchmarks/config.json` | Benchmark test suite — questions + expected answers for all spaces. |

## Genie Spaces (Current)

| Space | ID | Backing Table(s) |
|-------|-----|------------------|
| Content Metrics | `01f18b97de0117d8af61d23c1eaa8d7e` | 11+ ops metrics tables |
| Redeliveries | `01f18b97de2f122eb27a1b307d68e50e` | redelivery_weekly |
| Dupe Checker V2 | `01f122f4e2921b7a9c28ef03d0812ee6` | v_dupe_checker_titles, v_dupe_checker_policy_windows |
| Partner Detail | `01f1912d11a91341b59b2836ca3777c6` | partner_health, partner_issues |
| Content Library | `01f1951ff07e199aa6d9c0718f342e35` | content_library |

## How to Add a New Genie Space

### Step 1: Create the Backing Table

Write a notebook under `notebooks/` that materializes a denormalized table in `core_dev.techops`. Genie works best with flat, pre-joined tables. Include column comments (Genie uses them for discoverability):

```python
comments = {"col_name": "Description of what this column means"}
for col, comment in comments.items():
    spark.sql(f"ALTER TABLE core_dev.techops.my_table ALTER COLUMN {col} COMMENT '{comment}'")
```

### Step 2: Create the Genie Space via API

```bash
# Create
databricks api post /api/2.0/genie/spaces --json '{
  "title": "My Space",
  "description": "Short UI label",
  "warehouse_id": "a6b9541289d75c6e",
  "parent_path": "/Users/swhitney@tubi.tv",
  "serialized_space": "{\"version\": 2}"
}'

# Bind table
databricks api patch /api/2.0/genie/spaces/{SPACE_ID} --json '{
  "serialized_space": "{\"version\": 2, \"data_sources\": {\"tables\": [{\"identifier\": \"core_dev.techops.my_table\"}]}}"
}'
```

### Step 3: Push Instructions to the Space

**CRITICAL**: Genie reads instructions from per-table `description` arrays inside `serialized_space`, NOT the top-level `description` field. The top-level description is just a UI label.

```bash
# Read current state (GET does NOT return serialized_space — use no-op PATCH)
databricks api patch /api/2.0/genie/spaces/{SPACE_ID} --json '{}'

# Update: serialized_space must be a JSON STRING in the body
databricks api patch /api/2.0/genie/spaces/{SPACE_ID} --json @payload.json
```

Payload structure:
```json
{
  "serialized_space": "{\"version\":2,\"data_sources\":{\"tables\":[{\"identifier\":\"core_dev.techops.my_table\",\"description\":[\"Instructions for Genie about this table. Include SQL rules, column explanations, and example queries.\"]}]}}"
}
```

Rules:
- `description` must be an **array of strings**, not a plain string
- `serialized_space` in the PATCH body must be a **stringified JSON string**, not a nested object
- Fields like `general_instructions`, `instructions`, `system_prompt` do NOT exist in this API — they are silently ignored
- Always preserve `"version": 2`
- The table field name is `identifier` (not `name` or `table_name`)

### Step 4: Wire into the LLM Router (4 locations)

1. **`app.py` → `GENIE_SPACES` dict** — add entry with id, description, keywords, color
2. **`query_decomposer.py` → `SYSTEM_PROMPT`** — add numbered entry describing the space
3. **`query_decomposer.py` → `valid_spaces` set** — add exact space name string
4. **`benchmarks/config.json`** — add space to `spaces` dict and add test cases

### Step 5: Deploy

```bash
git push origin main
databricks apps deploy techopstest
```

### Step 6: Add to Daily Refresh Job

Job ID `386450264768500` ("Content Metric Snapshots") runs all table notebooks daily.

## How the LLM Router Works

1. User question goes to `QueryDecomposer` which uses **Haiku** (`databricks-claude-haiku-4-5`) to classify: single-space or multi-space?
2. The system prompt lists all spaces with descriptions and routing rules
3. For multi-space queries, sub-questions are sent to Genie spaces in parallel via `ThreadPoolExecutor`
4. Results are combined by `ResultSynthesizer` using **Sonnet** (`databricks-claude-sonnet-4-5`)
5. If the LLM fails, falls back to keyword-based routing (`route_query()` in `app.py`)

To add routing awareness for a new space, update the system prompt in `query_decomposer.py` with:
- What data the space has
- What question types it answers
- When to prefer it over similar spaces

## Benchmarks

Run the benchmark suite to validate all Genie spaces:

```bash
cd benchmarks && python run.py
```

`config.json` contains test cases with:
- `question` — what to ask
- `expected_space` — which space should answer it
- `validation_type` — "percentage" (within 20%), "exact", or "table_used"
- `validation_sql` — ground-truth SQL to compare against

## Development Patterns

- **Warehouse**: All SQL executes on warehouse `a6b9541289d75c6e` (Tableau Extracts)
- **SQL execution API**: `POST /api/2.0/sql/statements` with `warehouse_id` and `statement`
- **Snapshot tables**: Always filter with `WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM table)`
- **date_trunc**: Always quote the first argument: `date_trunc("month", ...)` not `date_trunc(month, ...)`
- **Service principal**: `5c07f0be-ca04-40f9-966d-211f52725e08` needs CAN_RUN on any new Genie space

## Deployment

This app deploys as a Databricks App named `techopstest`:

```bash
databricks apps deploy techopstest
```

The app reads its config from `app.yaml`. Environment variables are configured in the Databricks App settings UI (not committed to the repo).
