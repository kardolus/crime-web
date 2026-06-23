# crime-web — NYC Crime Map

A city-wide dashboard of NYPD complaint data: a map of where complaints concentrate,
a ranked table, and pattern charts — filterable by **year**, **law class** (felony /
misdemeanor / violation), **borough**, and **offense category**. Lives at
**[crime.kardol.us](https://crime.kardol.us)**.

It's a sibling of [`crashes-web`](../crashes-web) (NYC Crash Map) — same architecture,
different open dataset.

> **These are reported complaints, subject to reporting bias — not a measure of true
> crime or neighborhood safety.** The dashboard is civic transparency about *where,
> when, and what offense* is reported. It deliberately **excludes all suspect/victim
> demographics** (race, age, sex are never pulled, stored, or queryable).

## Architecture — local Postgres+PostGIS mirror

Serves from a local **Postgres+PostGIS** mirror of NYC Open Data's **NYPD Complaint
Data**, refreshed by an ingester. Every page is a fast, indexed SQL aggregation.

```
NYPD Complaint Data (two Socrata resources)
  historic qgea-i56i (~10M, 2006…last year)  +  current-YTD 5uac-w243
        │  backfill + daily/weekly ingest + PostGIS grid clustering
        ▼
crime-postgres  ──read-only SQL──>  crime-web (Starlette)  ──>  Browser
```

- **`ingest.py`** — `python -m crimeweb.ingest {backfill|daily|weekly}`.
  - `backfill` pulls BOTH resources; `daily` pulls only the current-YTD resource for a
    recent window; `weekly` re-pulls both to reconcile late edits that land in the
    historic dataset. Keyset-paginated by `cmplnt_num` (a **text** field → lexical
    order), deduped by `cmplnt_num` PK (~0.01% source dupes).
  - Assigns each geocoded complaint a stable `cluster_id` via a **fixed ~150 m grid**
    (EPSG:2263) — crime is area-based, so coarser than the crash map's 30 m — and
    rebuilds `hotspots` (centroid + "Precinct N · top offense" label + felony/misd/
    violation split). Uses `socrata.py`.
- **`db.py`** — read-only (`crime_ro`) psycopg2 layer, `{data, meta}` envelopes.
  Hotspots = `GROUP BY cluster_id` over `complaints JOIN hotspots`, ranked by count.
- **`socrata.py`** — Socrata client + validators + the `OFNS_BUCKET` map (75 offense
  descriptions → 9 categories). Ingester-only.
- **`server.py`** / **`ui.py`** — Starlette routes + two page bodies; flightdeck shell,
  4-filter nav, OG card.

**Single replica only** (in-process cache). DB is host-docker `crime-postgres`
(`:5436`, postgis/postgis:16) via a no-selector Service+Endpoints. The mirror is a
**regenerable public-data mirror → not backed up**; rebuild via the backfill Job.

## Filters

`?year=<YYYY|all>&class=<all|felony|misdemeanor|violation>&borough=<citywide|…>&cat=<all|violent|property|drug|weapons|fraud|vehicle|public-order|trespass|other>`

## Develop

```bash
uv run python -m crimeweb.server   # http://localhost:8000  (needs CRIME_DB_URL)
```

## Deploy (forge k8s)

```bash
docker build -t ghcr.io/kardolus/crime-web:v1 . && docker push ghcr.io/kardolus/crime-web:v1
kubectl create namespace crime
# secrets: crime-db (ro), crime-db-rw, opendata (Socrata key pair), ghcr-pull
kubectl apply -f deploy/k8s/      # web + Svc/Endpoints + daily & weekly CronJobs
kubectl -n crime create job crime-backfill --from=cronjob/crime-ingest -- python -m crimeweb.ingest backfill
```

Then add `crime.kardol.us` to the platform Cloudflare tunnel + a DNS record. Host DB
compose is codified in `kardolus/forge-infra`.
