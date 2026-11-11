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
# A failed backfill/weekly pod resumes from its checkpoint if the checkpoint was touched
# this recently; older ones belong to an abandoned run and are discarded (start over).
RESUME_WINDOW_HOURS = 20

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


def _pages(resource, where_extra=None, start_after=None):
    """Keyset pagination by cmplnt_num (a TEXT field — lexical order; avoids deep $offset).
    `start_after` resumes after that cmplnt_num (from a checkpoint)."""
    last = start_after
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


# ───────────────────────── resume checkpoints ─────────────────────────
# The weekly re-pull reads ~10M rows over ~1h. A Socrata outage mid-pull used to kill the pod,
# and the Job's retry pod started again from the first page (2026-11-09: two pods, 19 + 32 min,
# both lost to the same 503 outage). Now every page commits its keyset cursor in the same
# transaction as its rows, so a retry pod picks up exactly where the last one stopped.

def _ensure_checkpoints(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ingest_checkpoint (
              key         text PRIMARY KEY,          -- '<mode>:<label>', e.g. 'weekly:historic'
              last_cmplnt text,                      -- keyset cursor: last source cmplnt_num done
              rows_done   bigint NOT NULL DEFAULT 0,
              done_at     timestamptz,               -- resource finished (skip it on resume)
              updated_at  timestamptz NOT NULL DEFAULT now()
            )""")
    conn.commit()


def _load_checkpoint(conn, key):
    """(last_cmplnt, rows_done, done) for a fresh checkpoint, else None (and the stale one is reset)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT last_cmplnt, rows_done, done_at IS NOT NULL FROM ingest_checkpoint
                       WHERE key=%s AND updated_at > now() - make_interval(hours => %s)""",
                    (key, RESUME_WINDOW_HOURS))
        row = cur.fetchone()
        if row is None:
            cur.execute("""INSERT INTO ingest_checkpoint (key) VALUES (%s)
                           ON CONFLICT (key) DO UPDATE SET last_cmplnt=NULL, rows_done=0,
                             done_at=NULL, updated_at=now()""", (key,))
    conn.commit()
    return row


def _save_checkpoint(cur, key, last_cmplnt, rows_done):
    cur.execute("""UPDATE ingest_checkpoint SET last_cmplnt=%s, rows_done=%s, updated_at=now()
                   WHERE key=%s""", (last_cmplnt, rows_done, key))


def _finish_checkpoint(conn, key):
    with conn.cursor() as cur:
        cur.execute("UPDATE ingest_checkpoint SET done_at=now(), updated_at=now() WHERE key=%s", (key,))
    conn.commit()


def _clear_checkpoints(conn, mode):
    """A run that completed leaves nothing to resume: the next run starts from the top."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ingest_checkpoint WHERE key LIKE %s", (f"{mode}:%",))
    conn.commit()


def _ingest_resource(conn, resource, where_extra=None, label="", checkpoint=None):
    """Upsert every page of `resource`. With `checkpoint` (a key), progress is saved per page
    and a recent unfinished checkpoint is resumed from; a finished one skips the resource."""
    total = 0
    unmapped = {}
    start_after = None
    if checkpoint:
        cp = _load_checkpoint(conn, checkpoint)
        if cp:
            last_cmplnt, rows_done, done = cp
            if done:
                log.info("%s: already finished in this run (%d rows) — skipping", label, rows_done)
                return 0
            if last_cmplnt:
                start_after, total = last_cmplnt, rows_done
                log.info("%s: resuming after cmplnt_num %s (%d rows already done)",
                         label, last_cmplnt, rows_done)
    for page in _pages(resource, where_extra, start_after):
        rows = [r for r in (_row(x) for x in page) if r]
        for x in page:  # track unmapped offense descs to tune the bucket table
            d = (x.get("ofns_desc") or "").strip()
            if d and d not in socrata.OFNS_BUCKET:
                unmapped[d] = unmapped.get(d, 0) + 1
        total += len(rows)
        with conn.cursor() as cur:
            _upsert(cur, rows)
            if checkpoint:  # same transaction as the rows: never ahead of, or behind, the data
                _save_checkpoint(cur, checkpoint, page[-1]["cmplnt_num"], total)
        conn.commit()
        log.info("%s: upserted %d (running %d)", label, len(rows), total)
    if checkpoint:
        _finish_checkpoint(conn, checkpoint)
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
    full = mode in ("backfill", "weekly")
    if full:
        _ensure_checkpoints(conn)
        total += _ingest_resource(conn, socrata.RESOURCE_HISTORIC, label="historic",
                                  checkpoint=f"{mode}:historic")
        total += _ingest_resource(conn, socrata.RESOURCE_CURRENT, label="current",
                                  checkpoint=f"{mode}:current")
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
    if full:
        _clear_checkpoints(conn, mode)
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
