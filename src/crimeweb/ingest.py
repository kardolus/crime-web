"""Ingester: NYPD Complaint Data -> Postgres mirror + PostGIS grid clustering.

    python -m crimeweb.ingest backfill   # full history: BOTH resources (historic + current)
    python -m crimeweb.ingest daily       # re-pull a recent window of the CURRENT-YTD resource
    python -m crimeweb.ingest weekly       # full re-pull of both (reconcile late historic edits)

NYPD complaints live in two Socrata resources (historic qgea-i56i + current-YTD
5uac-w243) with identical key columns; we union them, deduped by cmplnt_num (PK).
After upserting we assign each geocoded complaint a stable cluster_id via a fixed
~150 m grid (EPSG:2263) — crime is area-based, so a coarser grid than the crash map's
30 m — and rebuild the `hotspots` table (centroid + label + felony/misd/violation split).
"""

from __future__ import annotations

import datetime
import logging
import os
import sys

import psycopg2
from psycopg2.extras import execute_values

from . import socrata

log = logging.getLogger("crimeweb.ingest")

DSN = os.environ["CRIME_DB_URL"]
PAGE = 50000
DAILY_LOOKBACK_DAYS = 30   # re-pull recent window (complaint corrections lag)
CLUSTER_GRID_FT = 500      # ~150 m grid in EPSG:2263 (NY State Plane, feet)

COLS = ["cmplnt_num", "cmplnt_date", "hour", "borough", "precinct",
        "law_cat", "ofns_desc", "bucket", "lat", "lon"]


def _coord(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _row(r: dict):
    try:
        cid = int(float(r.get("cmplnt_num")))
    except (TypeError, ValueError):
        return None
    date = (r.get("cmplnt_fr_dt") or "")[:10]
    if len(date) != 10 or not date[:4].isdigit() or int(date[:4]) < socrata.MIN_YEAR:
        return None
    t = (r.get("cmplnt_fr_tm") or "").strip()
    hour = None
    try:
        h = int(t.split(":")[0])
        if 0 <= h <= 23:
            hour = h
    except (ValueError, IndexError):
        pass
    try:
        precinct = int(float(r.get("addr_pct_cd")))
    except (TypeError, ValueError):
        precinct = None
    lat, lon = _coord(r.get("latitude")), _coord(r.get("longitude"))
    if lat is None or lon is None or not (40.45 <= lat <= 40.95 and -74.30 <= lon <= -73.65):
        lat = lon = None
    ofns = (r.get("ofns_desc") or "").strip() or None
    return {
        "cmplnt_num": cid, "cmplnt_date": date, "hour": hour,
        "borough": (r.get("boro_nm") or None), "precinct": precinct,
        "law_cat": (r.get("law_cat_cd") or None), "ofns_desc": ofns,
        "bucket": socrata.bucket_for(ofns), "lat": lat, "lon": lon,
    }


_UPSERT = (
    f"INSERT INTO complaints ({','.join(COLS)}) VALUES %s "
    f"ON CONFLICT (cmplnt_num) DO UPDATE SET "
    + ",".join(f"{c}=EXCLUDED.{c}" for c in COLS if c != "cmplnt_num")
)


def _upsert(cur, rows):
    # cmplnt_num has ~0.01% dupes in the source; dedupe within the batch (last wins)
    # so a single INSERT never touches the same PK twice (CardinalityViolation).
    seen = {r["cmplnt_num"]: r for r in rows}
    execute_values(cur, _UPSERT, [[r[c] for c in COLS] for r in seen.values()], page_size=2000)


def _pages(resource, where_extra=None):
    """Keyset pagination by cmplnt_num (a TEXT field — lexical order; avoids deep $offset)."""
    last = None
    while True:
        where = "cmplnt_num IS NOT NULL" if last is None else f"cmplnt_num > '{last}'"
        if where_extra:
            where += f" AND {where_extra}"
        rows = socrata.get(resource, **{"$select": socrata.SELECT, "$where": where,
                                        "$order": "cmplnt_num", "$limit": PAGE})
        if not rows:
            return
        yield rows
        last = rows[-1]["cmplnt_num"]  # lexically-largest in this ASC page
        if len(rows) < PAGE:
            return


def _ingest_resource(conn, resource, where_extra=None, label=""):
    total = 0
    unmapped = {}
    for page in _pages(resource, where_extra):
        rows = [r for r in (_row(x) for x in page) if r]
        for x in page:  # track unmapped offense descs to tune the bucket table
            d = (x.get("ofns_desc") or "").strip()
            if d and d not in socrata.OFNS_BUCKET:
                unmapped[d] = unmapped.get(d, 0) + 1
        with conn.cursor() as cur:
            _upsert(cur, rows)
        conn.commit()
        total += len(rows)
        log.info("%s: upserted %d (running %d)", label, len(rows), total)
    if unmapped:
        top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:10]
        log.info("%s: top unmapped ofns_desc -> 'other': %s", label, top)
    return total


def recluster(conn):
    """Fixed ~150 m grid (EPSG:2263) -> stable cluster_id, then rebuild hotspots."""
    with conn.cursor() as cur:
        log.info("clustering (%d ft grid)…", CLUSTER_GRID_FT)
        cur.execute(f"""
            WITH g AS (
              SELECT cmplnt_num,
                     floor(ST_X(ST_Transform(geom, 2263)) / {CLUSTER_GRID_FT})::bigint * 100000
                     + floor(ST_Y(ST_Transform(geom, 2263)) / {CLUSTER_GRID_FT})::bigint AS cid
              FROM complaints WHERE geom IS NOT NULL
            )
            UPDATE complaints c SET cluster_id = g.cid
            FROM g WHERE c.cmplnt_num = g.cmplnt_num
              AND c.cluster_id IS DISTINCT FROM g.cid
        """)
        log.info("assigned cluster_id to %d changed rows; rebuilding hotspots…", cur.rowcount)
        cur.execute("TRUNCATE hotspots")
        cur.execute("""
            INSERT INTO hotspots (cluster_id, lat, lon, label, n_complaints, n_felony, n_misd, n_violation)
            SELECT cluster_id, avg(lat), avg(lon),
                   'Precinct ' || coalesce(mode() WITHIN GROUP (ORDER BY precinct)::text, '?')
                     || ' · ' || coalesce(initcap(mode() WITHIN GROUP (ORDER BY ofns_desc)), 'Mixed'),
                   count(*),
                   count(*) FILTER (WHERE law_cat='FELONY'),
                   count(*) FILTER (WHERE law_cat='MISDEMEANOR'),
                   count(*) FILTER (WHERE law_cat='VIOLATION')
            FROM complaints WHERE cluster_id IS NOT NULL
            GROUP BY cluster_id
        """)
        cur.execute("SELECT count(*) FROM hotspots")
        log.info("hotspots rebuilt: %d", cur.fetchone()[0])
    conn.commit()


def _update_state(conn):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE ingest_state SET
              last_cmplnt_date = (SELECT max(cmplnt_date) FROM complaints),
              rows_total = (SELECT count(*) FROM complaints),
              updated_at = now()
            WHERE id = 1
        """)
    conn.commit()


def run(mode: str):
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    total = 0
    if mode in ("backfill", "weekly"):
        total += _ingest_resource(conn, socrata.RESOURCE_HISTORIC, label="historic")
        total += _ingest_resource(conn, socrata.RESOURCE_CURRENT, label="current")
    else:  # daily — only the current-YTD resource, recent window
        with conn.cursor() as cur:
            cur.execute("SELECT last_cmplnt_date FROM ingest_state WHERE id=1")
            row = cur.fetchone()
        last = row[0] if row else None
        where = None
        if last:
            since = last - datetime.timedelta(days=DAILY_LOOKBACK_DAYS)
            where = f"cmplnt_fr_dt >= '{since.isoformat()}'"
            log.info("daily: re-pulling current-YTD since %s", since)
        total += _ingest_resource(conn, socrata.RESOURCE_CURRENT, where, label="current")

    recluster(conn)
    _update_state(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT rows_total, last_cmplnt_date FROM ingest_state WHERE id=1")
        rt, lcd = cur.fetchone()
    log.info("done (%s): %d upserted this run; table now %s rows, through %s", mode, total, rt, lcd)
    conn.close()


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode not in ("backfill", "daily", "weekly"):
        sys.exit("usage: python -m crimeweb.ingest [backfill|daily|weekly]")
    run(mode)


if __name__ == "__main__":
    main()
