"""Postgres data layer for the NYC Crime Map (read-only crime_ro role).

Fast indexed SQL aggregations over the local mirror (complaints + hotspots tables,
filled daily by ingest.py). Same {data, meta} envelopes + cached() TTL as the crash
map. Hotspots come from a precomputed, filter-independent cluster_id (fixed ~150 m
grid), ranked by complaint count — framed as "where complaints concentrate", not a
danger ranking. NO suspect/victim demographics are stored or queryable.
"""

from __future__ import annotations

import datetime
import logging
import os
import threading
import time

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

from .socrata import (CLASSES, BOROUGHS, CATEGORIES, CLASS_DB, BOROUGH_DB,  # noqa: F401
                      valid_year, valid_class, valid_borough, valid_cat, MIN_YEAR)

log = logging.getLogger("crimeweb.db")


def _where(year, klass, borough, cat, prefix=""):
    """Return (sql_conditions, params). `prefix` aliases columns (e.g. 'c.')."""
    p = prefix
    cond, params = [], []
    if year and year != "all":
        y = int(year)
        cond.append(f"{p}cmplnt_date >= %s AND {p}cmplnt_date < %s")
        params += [f"{y}-01-01", f"{y + 1}-01-01"]
    if klass in CLASS_DB:
        cond.append(f"{p}law_cat = %s")
        params.append(CLASS_DB[klass])
    if borough in BOROUGH_DB:
        cond.append(f"{p}borough = %s")
        params.append(BOROUGH_DB[borough])
    if cat and cat != "all":
        cond.append(f"{p}bucket = %s")
        params.append(cat)
    return (" AND ".join(cond) if cond else "TRUE", params)


# ───────────────────────── pool + cache ─────────────────────────
_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(1, 5, os.environ["CRIME_DB_URL"])
    return _pool


def _query(sql, params=()):
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        pool.putconn(conn, close=True)
        conn = None
        raise
    finally:
        if conn is not None:
            pool.putconn(conn)


_cache: dict[str, tuple[float, object]] = {}
_last_good: dict[str, object] = {}
_lock = threading.Lock()


def _q(key, ttl, producer, empty):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return {"data": hit[1], "meta": {"stale": False}}
    try:
        data = producer()
    except Exception as e:
        log.warning("db query failed (%s): %s", key, e)
        with _lock:
            lg = _last_good.get(key)
        if lg is not None:
            return {"data": lg, "meta": {"stale": True, "source_error": "db_unavailable"}}
        return {"data": empty, "meta": {"stale": False, "source_error": "db_unavailable"}}
    with _lock:
        _cache[key] = (time.time(), data)
        _last_good[key] = data
    return {"data": data, "meta": {"stale": False}}


def cache_stats() -> dict:
    return {"keys": len(_cache)}


def ping() -> bool:
    _query("SELECT 1")
    return True


def _i(v):
    return int(v) if v is not None else 0


# ───────────────────────── queries ─────────────────────────
def available_years():
    def produce():
        r = _query("SELECT min(cmplnt_date) lo, max(cmplnt_date) hi FROM complaints")[0]
        if not r["hi"]:
            raise RuntimeError("empty complaints table")
        ymax, hi = r["hi"].year, r["hi"]
        latest_full = ymax if hi.month == 12 else ymax - 1
        return {"years": list(range(MIN_YEAR, ymax + 1)), "max": ymax,
                "latest_full": max(latest_full, MIN_YEAR), "data_through": hi.isoformat()}
    return _q("years", 6 * 3600, produce,
              {"years": list(range(MIN_YEAR, datetime.date.today().year + 1)),
               "max": datetime.date.today().year, "latest_full": datetime.date.today().year - 1,
               "data_through": ""})


def freshness():
    def produce():
        r = _query("SELECT max(cmplnt_date) hi FROM complaints")[0]
        return {"latest": r["hi"].isoformat() if r["hi"] else ""}
    return _q("freshness", 3600, produce, {"latest": ""})


def summary_kpis(year, klass, borough, cat):
    key = f"sum:{year}:{klass}:{borough}:{cat}"

    def produce():
        w, p = _where(year, klass, borough, cat)
        r = _query(f"""
            SELECT count(*) total,
                   count(*) FILTER (WHERE law_cat='FELONY') felony,
                   count(*) FILTER (WHERE law_cat='MISDEMEANOR') misd,
                   count(*) FILTER (WHERE law_cat='VIOLATION') violation,
                   count(*) FILTER (WHERE geom IS NOT NULL) mapped
            FROM complaints WHERE {w}""", p)[0]
        tot = _i(r["total"])
        pct = lambda n: round(100 * n / tot) if tot else 0
        f, m, v = _i(r["felony"]), _i(r["misd"]), _i(r["violation"])
        return {"total": tot, "felony": f, "misd": m, "violation": v,
                "pct_felony": pct(f), "pct_misd": pct(m), "pct_violation": pct(v),
                "mapped": _i(r["mapped"])}
    return _q(key, 3600, produce, {})


def hotspots(year, klass, borough, cat, limit=500):
    key = f"hot:{year}:{klass}:{borough}:{cat}:{limit}"

    def produce():
        w, p = _where(year, klass, borough, cat, prefix="c.")
        rows = _query(f"""
            SELECT h.lat, h.lon, h.label,
                   count(*) total,
                   count(*) FILTER (WHERE c.law_cat='FELONY') felony,
                   count(*) FILTER (WHERE c.law_cat='MISDEMEANOR') misd,
                   count(*) FILTER (WHERE c.law_cat='VIOLATION') violation
            FROM complaints c JOIN hotspots h USING (cluster_id)
            WHERE c.cluster_id IS NOT NULL AND {w}
            GROUP BY h.cluster_id, h.lat, h.lon, h.label
            ORDER BY count(*) DESC
            LIMIT %s""", p + [int(limit)])
        return [{"lat": r["lat"], "lon": r["lon"], "label": r["label"] or "",
                 "total": _i(r["total"]), "felony": _i(r["felony"]),
                 "misd": _i(r["misd"]), "violation": _i(r["violation"])} for r in rows]
    return _q(key, 3600, produce, [])


def complaints_by_year(klass, borough, cat):
    key = f"byyear:{klass}:{borough}:{cat}"

    def produce():
        w, p = _where("all", klass, borough, cat)
        rows = _query(f"""
            SELECT extract(year from cmplnt_date)::int yr, count(*) total,
                   count(*) FILTER (WHERE law_cat='FELONY') felony,
                   count(*) FILTER (WHERE law_cat='MISDEMEANOR') misd,
                   count(*) FILTER (WHERE law_cat='VIOLATION') violation
            FROM complaints WHERE {w} GROUP BY 1 ORDER BY 1""", p)
        return [{"year": _i(r["yr"]), "total": _i(r["total"]), "felony": _i(r["felony"]),
                 "misd": _i(r["misd"]), "violation": _i(r["violation"])}
                for r in rows if _i(r["yr"]) >= MIN_YEAR]
    return _q(key, 6 * 3600, produce, [])


def by_hour(year, klass, borough, cat):
    key = f"hour:{year}:{klass}:{borough}:{cat}"

    def produce():
        w, p = _where(year, klass, borough, cat)
        rows = _query(f"SELECT hour, count(*) n FROM complaints WHERE {w} AND hour IS NOT NULL GROUP BY hour", p)
        by = {_i(r["hour"]): _i(r["n"]) for r in rows}
        return [{"hr": h, "total": by.get(h, 0)} for h in range(24)]
    return _q(key, 3600, produce, [])


def by_weekday(year, klass, borough, cat):
    key = f"dow:{year}:{klass}:{borough}:{cat}"

    def produce():
        w, p = _where(year, klass, borough, cat)
        rows = _query(f"SELECT extract(dow from cmplnt_date)::int dow, count(*) n FROM complaints WHERE {w} GROUP BY 1", p)
        by = {_i(r["dow"]): _i(r["n"]) for r in rows}  # 0=Sunday
        return [{"dow": d, "total": by.get(d, 0)} for d in range(7)]
    return _q(key, 3600, produce, [])


def by_month(year, klass, borough, cat):
    key = f"month:{year}:{klass}:{borough}:{cat}"

    def produce():
        w, p = _where(year, klass, borough, cat)
        rows = _query(f"SELECT extract(month from cmplnt_date)::int m, count(*) n FROM complaints WHERE {w} GROUP BY 1", p)
        by = {_i(r["m"]): _i(r["n"]) for r in rows}
        return [{"month": m, "total": by.get(m, 0)} for m in range(1, 13)]
    return _q(key, 3600, produce, [])


def class_by_year(borough, cat):
    """Felony/misd/violation by year — ignores the class filter (always shows all three)."""
    key = f"classyr:{borough}:{cat}"

    def produce():
        w, p = _where("all", "all", borough, cat)
        rows = _query(f"""
            SELECT extract(year from cmplnt_date)::int yr,
                   count(*) FILTER (WHERE law_cat='FELONY') felony,
                   count(*) FILTER (WHERE law_cat='MISDEMEANOR') misd,
                   count(*) FILTER (WHERE law_cat='VIOLATION') violation
            FROM complaints WHERE {w} GROUP BY 1 ORDER BY 1""", p)
        return [{"year": _i(r["yr"]), "felony": _i(r["felony"]),
                 "misd": _i(r["misd"]), "violation": _i(r["violation"])}
                for r in rows if _i(r["yr"]) >= MIN_YEAR]
    return _q(key, 6 * 3600, produce, [])


def top_offenses(year, klass, borough, cat, limit=12):
    key = f"offenses:{year}:{klass}:{borough}:{cat}:{limit}"

    def produce():
        w, p = _where(year, klass, borough, cat)
        rows = _query(f"""
            SELECT initcap(ofns_desc) offense, count(*) n FROM complaints
            WHERE {w} AND ofns_desc IS NOT NULL
            GROUP BY ofns_desc ORDER BY 2 DESC LIMIT %s""", p + [int(limit)])
        return [{"offense": r["offense"], "total": _i(r["n"])} for r in rows]
    return _q(key, 6 * 3600, produce, [])
