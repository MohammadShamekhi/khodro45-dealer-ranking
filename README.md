# Dealer Intelligence

FastAPI **gateway** between your backend and Redis. The backend sends each request with a **`dealer_id`**; this service reads the live auction snapshot from Redis, **reranks it for that dealer** (via `RANKER_STRATEGY`), and returns the result.

It also exposes **dealer preferences** — merged comment extractions and K45 preference rows per dealer.

It does **not** fetch auctions from Khodro45 and does **not** write the auction Redis snapshot. Another service (e.g. dealer-predict) keeps the auction cache and dealer engagement keys warm.

**Technical design:** [`docs/TECH_DESIGN.md`](docs/TECH_DESIGN.md)

**Backend integration guide:** [`docs/BACKEND_API_GUIDE.md`](docs/BACKEND_API_GUIDE.md)

## How it fits in

```text
External writer  ──►  Redis
                      ├── auctions:live:snapshot + meta
                      ├── dealers_extractions:{dealer_id}
                      └── dealer_model_engagement:{dealer_id}
                              │
Backend  ──GET + X-Dealer-Id──►  Dealer Intelligence  ──read──► Redis
         ◄── ranked ids/full──       (RANKER_STRATEGY)

Backend  ──GET ?dealer_id=────►  /api/v1/dealer_preferences/
                                      ├── Postgres (comment extractions)
                                      └── K45 DB (dealers_preference)
```

1. **Redis** holds the current live auction list (shared snapshot for all dealers), plus optional per-dealer extractions and engagement scores.
2. **Backend** calls this gateway with `X-Dealer-Id: <dealer_id>` on suggested routes.
3. **Gateway** loads the snapshot from Redis, runs the configured ranker, and returns auction ids or full objects in that order.
4. **Dealer preferences** merges AI comment extractions with K45 preference rows for a single dealer.

No `Authorization` header is required on gateway calls — identity comes from `X-Dealer-Id` on suggested routes, or `dealer_id` query param on dealer preferences.

## Endpoints

| Method | Path | `X-Dealer-Id` | Response |
|--------|------|---------------|----------|
| `GET` | `/api/v1/live/` | Not used | Full auction JSON array from Redis (default list order) |
| `GET` | `/api/v1/suggested_ids/` | **Required** | `{ "auction_ids": [...] }` reranked for dealer |
| `GET` | `/api/v1/suggested_full/` | **Required** | Full auction JSON array from Redis, reranked for dealer |
| `GET` | `/api/v1/dealer_preferences/` | — | Comment extraction + merged preferences (`?dealer_id=`) |
| `GET` | `/healthz` | Not used | `{ "ok": true, "ranker_strategy": "..." }` |
| `GET` | `/` | — | Browser playground (manual testing) |

Suggested responses also expose playground headers (CORS-enabled):

- `X-Dealer-Extraction` — dealer comment extractions summary from Redis
- `X-Dealer-Engagement` — top engagement model/brand ranks from Redis

### Base URLs

| Environment | URL |
|-------------|-----|
| **Production** | `https://dealerintelligence.darkube.ir` |
| **Local** | `http://127.0.0.1:8080` (or your `PORT` from `.env`) |

OpenAPI: `{base}/docs`

## Examples

### Suggested ids

```bash
curl -sS 'https://dealerintelligence.darkube.ir/api/v1/suggested_ids/' \
  -H 'X-Dealer-Id: 451301'
```

```json
{
  "auction_ids": [372601, 372597, 372594]
}
```

### Suggested full (ranked auction objects)

```bash
curl -sS 'https://dealerintelligence.darkube.ir/api/v1/suggested_full/' \
  -H 'X-Dealer-Id: 451301'
```

Returns a JSON **array** of auction objects (same shape as `/api/v1/live/`, reranked for the dealer).

### Live list (no reranking)

```bash
curl -sS 'https://dealerintelligence.darkube.ir/api/v1/live/'
```

Returns a JSON **array** of auction objects (same shape as Khodro45 list items).

### Dealer preferences

```bash
curl -sS 'https://dealerintelligence.darkube.ir/api/v1/dealer_preferences/?dealer_id=451301'
```

```json
{
  "dealer_id": 451301,
  "extractions": { "...": "..." },
  "preferences": {
    "dealer_id": 451301,
    "segment": "both",
    "model": [592, 614],
    "year": [],
    "price": [20],
    "enable_notif_by_preference": true,
    "model_titles": ["407"],
    "updated_at": "2026-06-08T12:00:00"
  }
}
```

### Errors

| Code | When |
|------|------|
| **400** | Suggested route without `X-Dealer-Id`, or invalid `dealer_id` |
| **404** | No completed comment extraction for `dealer_id` |
| **503** | Redis snapshot missing; external writer has not populated cache yet |
| **500** | Redis/DB misconfiguration or connection failure |

## Ranker strategy

Set `RANKER_STRATEGY` to pick which in-process model orders suggested results. Register new models in `app/rankers/__init__.py`.

| `RANKER_STRATEGY` | Behavior |
|-------------------|----------|
| `identity` | Redis snapshot order (input order) |
| `ascending_id` | Sort by auction `id` ascending |
| `random` | Shuffled order, stable per `dealer_id` for a given snapshot |
| `preference` | Redis `dealers_extractions:{id}` → tiers: brand+model, model, brand, then rest by ascending id. Missing/empty prefs → ascending id |
| `engagement` | Redis `dealer_model_engagement:{id}` → model match by rank, then brand match by rank, then rest by ascending id. Missing/empty → identity |
| `hybrid` | Preference tiers first; unmatched auctions ranked by engagement. No prefs → full engagement |

On unexpected ranker exception, suggested endpoints fall back to **identity** (snapshot order).

## Environment variables

Copy `.env.example` to `.env`, then:

```bash
set -a && source .env && set +a
```

This project does **not** use `load_dotenv` — you must export env vars before starting uvicorn.

### Auction gateway

| Variable | Description |
|----------|-------------|
| `REDIS_HOST` | Redis host (auction snapshot) |
| `REDIS_PORT` | Redis port |
| `REDIS_DB` | Redis DB index |
| `REDIS_PASSWORD` | Optional Redis password |
| `AUCTION_CACHE_REDIS_KEY` | Snapshot key (default `auctions:live:snapshot`) |
| `AUCTION_CACHE_META_KEY` | Meta hash (default `auctions:live:meta`) |
| `DEALER_EXTRACTIONS_REDIS_KEY_PREFIX` | Preference extractions key prefix (default `dealers_extractions`) |
| `DEALER_ENGAGEMENT_REDIS_KEY_PREFIX` | Engagement scores key prefix (default `dealer_model_engagement`) |
| `RANKER_STRATEGY` | Active ranker (see table above) |
| `HOST` | Bind host (default `0.0.0.0` in Docker) |
| `PORT` | Bind port (default `8080` in Docker) |
| `LOG_LEVEL` | Log level (e.g. `INFO`) |
| `LOG_FILE_PATH` | Optional JSON log file path |

### Dealer preferences (`/api/v1/dealer_preferences/`)

| Variable | Description |
|----------|-------------|
| `POSTGRES_HOST` | Postgres host (comment extractions) |
| `POSTGRES_PORT` | Postgres port |
| `POSTGRES_NAME` | Database name |
| `POSTGRES_USER` | Database user |
| `POSTGRES_PASSWORD` | Database password |
| `K45_HOST` | K45 Postgres host (`dealers_preference` table) |
| `K45_PORT` | K45 Postgres port |
| `K45_NAME` | Database name |
| `K45_USER` | Database user |
| `K45_PASSWORD` | Database password |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — Redis must already contain a snapshot; add POSTGRES_* / K45_* for dealer_preferences
set -a && source .env && set +a
uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8080}" --reload
```

Use **Python 3.12+** (Docker image uses 3.12). On Python 3.14, `psycopg[binary]` is used instead of `psycopg2`.

## Test commands

```bash
export BASE=http://127.0.0.1:${PORT:-8080}
export DEALER_ID=451301

# Health
curl -sS "$BASE/healthz" | python3 -m json.tool

# Auction gateway
curl -sS "$BASE/api/v1/live/" | python3 -c "import sys,json; print(len(json.load(sys.stdin)), 'auctions')"
curl -sS "$BASE/api/v1/suggested_ids/" -H "X-Dealer-Id: $DEALER_ID" | python3 -m json.tool
curl -sS "$BASE/api/v1/suggested_full/" -H "X-Dealer-Id: $DEALER_ID" | python3 -c "import sys,json; print(len(json.load(sys.stdin)), 'auctions')"

# Dealer preferences
curl -sS "$BASE/api/v1/dealer_preferences/?dealer_id=$DEALER_ID" | python3 -m json.tool

# Redis pre-check (if auction routes return 503)
redis-cli HGETALL auctions:live:meta
redis-cli EXISTS auctions:live:snapshot
```

Playground UI: `{base}/` · OpenAPI: `{base}/docs`

## Docker (PaaS / local image)

Build and run the image. **All configuration comes from environment variables** — set them on Darkube (or export before `docker run`). Nothing is baked into the image except defaults for `HOST`, `PORT`, and optional Redis key names.

```bash
docker build -t dealerintelligence .
docker run --rm -p 8080:8080 --env-file .env dealerintelligence
```

`docker run --env-file .env` is optional for local use; on PaaS, set the same variables in the platform UI.

## Deploy (Darkube)

The container starts via Dockerfile `CMD` (`uvicorn` on `HOST` / `PORT`). Set env vars in Darkube — **do not rely on docker-compose**; the app reads only `os.environ`.

| Variable | Required | Notes |
|----------|----------|-------|
| `REDIS_HOST` | **yes** | Shared Redis host (same as your working `redis-cli -h`) |
| `REDIS_PORT` | no (default `6379`) | |
| `REDIS_DB` | no (default `0`) | Must match `-n` in `redis-cli` |
| `REDIS_PASSWORD` | if Redis auth enabled | |
| `AUCTION_CACHE_REDIS_KEY` | no | default `auctions:live:snapshot` |
| `AUCTION_CACHE_META_KEY` | no | default `auctions:live:meta` |
| `DEALER_EXTRACTIONS_REDIS_KEY_PREFIX` | no | default `dealers_extractions` |
| `DEALER_ENGAGEMENT_REDIS_KEY_PREFIX` | no | default `dealer_model_engagement` |
| `RANKER_STRATEGY` | no | `identity`, `ascending_id`, `random`, `preference`, `engagement`, or `hybrid` |
| `HOST` | no | default `0.0.0.0` |
| `PORT` | no | default `8080` |
| `POSTGRES_*`, `K45_*` | for `/dealer_preferences` only | |

Example Darkube env block:

```bash
REDIS_HOST=ai-dealerpredict-redis.khodro45-ai.svc
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=...
AUCTION_CACHE_REDIS_KEY=auctions:live:snapshot
AUCTION_CACHE_META_KEY=auctions:live:meta
DEALER_EXTRACTIONS_REDIS_KEY_PREFIX=dealers_extractions
DEALER_ENGAGEMENT_REDIS_KEY_PREFIX=dealer_model_engagement
RANKER_STRATEGY=hybrid
HOST=0.0.0.0
PORT=8080
```

### Common issues

| Symptom | Fix |
|---------|-----|
| **502 Bad Gateway** | App not listening on `0.0.0.0:8080` |
| **500** on auction routes | Wrong `REDIS_*` or Redis unreachable |
| **503** on auction routes | Snapshot empty — fix external Redis writer |
| **400 on suggested** | Backend must send `X-Dealer-Id` |
| **Suggested order looks like snapshot** | Check `RANKER_STRATEGY` and Redis keys for extractions/engagement |
| **500 on dealer_preferences** | Check `POSTGRES_*` and `K45_*` env vars |
| **404 on dealer_preferences** | No completed comment extraction for that `dealer_id` |
| **pip install fails (psycopg2)** | Use Python 3.12, or keep `psycopg[binary]` from `requirements.txt` |

## Project layout

```text
.
├── README.md
├── requirements.txt
├── .env.example
├── Dockerfile
├── docs/
│   ├── TECH_DESIGN.md
│   └── BACKEND_API_GUIDE.md
├── scripts/
│   └── load_test_auctions.py
├── dealer_preferences/
│   ├── api.py            # GET /api/v1/dealer_preferences/
│   ├── db.py             # Postgres + K45 queries
│   ├── transform.py      # Merge comments + preferences
│   └── config.py         # DB settings, model title mapping
└── app/
    ├── main.py           # HTTP routes + ranker fallback
    ├── auction_cache.py  # Redis read (snapshot / meta)
    ├── settings.py
    ├── ordering.py       # auction id helpers + reorder_by_ids
    ├── rankers/          # pluggable models via RANKER_STRATEGY
    │   ├── identity_ranker.py
    │   ├── ascending_id_ranker.py
    │   ├── random_ranker.py
    │   ├── preference_ranker.py
    │   ├── engagement_ranker.py
    │   └── hybrid_ranker.py
    └── static/index.html # Playground UI
```
