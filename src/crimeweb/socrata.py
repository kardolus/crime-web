"""Socrata access for the NYC Crime Map ingester (web serves from Postgres via db.py).

NYPD Complaint Data spans two Socrata resources with identical key columns:
  * Historic  qgea-i56i  — ~10M rows, 2006 … end of last year (updates ~annually)
  * Current   5uac-w243  — current year-to-date (updates ~daily)

This module is INGESTER-ONLY: the HTTP client + paginated get(), plus the pure
filter constants/validators that db.py and ui.py import. No query/aggregation here.
"""

from __future__ import annotations

import base64
import datetime
import logging
import os

import httpx

log = logging.getLogger("crimeweb.socrata")

DOMAIN = "https://data.cityofnewyork.us/resource"
RESOURCE_HISTORIC = f"{DOMAIN}/qgea-i56i.json"
RESOURCE_CURRENT = f"{DOMAIN}/5uac-w243.json"
MIN_YEAR = 2006
NYC_BBOX = "latitude between 40.45 and 40.95 and longitude between -74.30 and -73.65"

# shared key columns present in BOTH resources
SELECT = ",".join([
    "cmplnt_num", "cmplnt_fr_dt", "cmplnt_fr_tm", "boro_nm", "addr_pct_cd",
    "law_cat_cd", "ofns_desc", "latitude", "longitude",
])

# ───────────────────────── filters (validated/clamped) ─────────────────────────
CLASSES = [
    ("all", "All complaints"),
    ("felony", "Felony"),
    ("misdemeanor", "Misdemeanor"),
    ("violation", "Violation"),
]
BOROUGHS = [
    ("citywide", "Citywide"),
    ("manhattan", "Manhattan"),
    ("brooklyn", "Brooklyn"),
    ("queens", "Queens"),
    ("bronx", "Bronx"),
    ("staten-island", "Staten Island"),
]
CATEGORIES = [
    ("all", "All offenses"),
    ("violent", "Violent"),
    ("property", "Property"),
    ("drug", "Drug"),
    ("weapons", "Weapons"),
    ("fraud", "Fraud"),
    ("vehicle", "Vehicle / traffic"),
    ("public-order", "Public order"),
    ("trespass", "Trespass"),
    ("other", "Other"),
]
_CLASS_SLUGS = {s for s, _ in CLASSES}
_BOROUGH_SLUGS = {s for s, _ in BOROUGHS}
_CAT_SLUGS = {s for s, _ in CATEGORIES}

# law_cat_cd and borough names as they appear in the data (UPPERCASE)
CLASS_DB = {"felony": "FELONY", "misdemeanor": "MISDEMEANOR", "violation": "VIOLATION"}
BOROUGH_DB = {"manhattan": "MANHATTAN", "brooklyn": "BROOKLYN", "queens": "QUEENS",
              "bronx": "BRONX", "staten-island": "STATEN ISLAND"}

# ─── offense bucketing: ~75 ofns_desc strings (incl. source truncations) -> 9 buckets ───
OFNS_BUCKET = {
    # violent
    "ASSAULT 3 & RELATED OFFENSES": "violent", "FELONY ASSAULT": "violent",
    "ROBBERY": "violent", "SEX CRIMES": "violent", "RAPE": "violent",
    "FELONY SEX CRIMES": "violent", "MURDER & NON-NEGL. MANSLAUGHTER": "violent",
    "HOMICIDE-NEGLIGENT,UNCLASSIFIE": "violent", "HOMICIDE-NEGLIGENT-VEHICLE": "violent",
    "KIDNAPPING & RELATED OFFENSES": "violent", "KIDNAPPING": "violent",
    "KIDNAPPING AND RELATED OFFENSES": "violent", "ARSON": "violent",
    "OFFENSES AGAINST THE PERSON": "violent", "OFFENSES RELATED TO CHILDREN": "violent",
    "CHILD ABANDONMENT/NON SUPPORT": "violent", "CHILD ABANDONMENT/NON SUPPORT 1": "violent",
    "ENDAN WELFARE INCOMP": "violent",
    # property
    "PETIT LARCENY": "property", "GRAND LARCENY": "property",
    "CRIMINAL MISCHIEF & RELATED OF": "property", "BURGLARY": "property",
    "GRAND LARCENY OF MOTOR VEHICLE": "property", "POSSESSION OF STOLEN PROPERTY": "property",
    "OTHER OFFENSES RELATED TO THEFT": "property", "OTHER OFFENSES RELATED TO THEF": "property",
    "UNAUTHORIZED USE OF A VEHICLE": "property", "PETIT LARCENY OF MOTOR VEHICLE": "property",
    "THEFT OF SERVICES": "property", "BURGLAR'S TOOLS": "property", "JOSTLING": "property",
    # drug
    "DANGEROUS DRUGS": "drug", "CANNABIS RELATED OFFENSES": "drug",
    "LOITERING FOR DRUG PURPOSES": "drug", "UNDER THE INFLUENCE OF DRUGS": "drug",
    # weapons
    "DANGEROUS WEAPONS": "weapons", "UNLAWFUL POSS. WEAP. ON SCHOOL": "weapons",
    # fraud
    "FORGERY": "fraud", "THEFT-FRAUD": "fraud", "FRAUDS": "fraud",
    "OFFENSES INVOLVING FRAUD": "fraud", "FRAUDULENT ACCOSTING": "fraud",
    # vehicle / traffic
    "VEHICLE AND TRAFFIC LAWS": "vehicle", "INTOXICATED & IMPAIRED DRIVING": "vehicle",
    "INTOXICATED/IMPAIRED DRIVING": "vehicle", "OTHER TRAFFIC INFRACTION": "vehicle",
    # trespass
    "CRIMINAL TRESPASS": "trespass",
    # public order
    "HARRASSMENT 2": "public-order", "OFF. AGNST PUB ORD SENSBLTY &": "public-order",
    "DISORDERLY CONDUCT": "public-order", "PROSTITUTION & RELATED OFFENSES": "public-order",
    "GAMBLING": "public-order", "LOITERING/GAMBLING (CARDS, DIC": "public-order",
    "LOITERING": "public-order", "LOITERING/DEVIATE SEX": "public-order",
    "DISRUPTION OF A RELIGIOUS SERV": "public-order", "FORTUNE TELLING": "public-order",
    "OFFENSES AGAINST PUBLIC SAFETY": "public-order", "ALCOHOLIC BEVERAGE CONTROL LAW": "public-order",
    "OFFENSES AGAINST MARRIAGE UNCL": "public-order", "ABORTION": "public-order",
    # everything else (admin code, misc penal, other state laws, null, …) -> "other"
}


def bucket_for(ofns_desc: str | None) -> str:
    return OFNS_BUCKET.get((ofns_desc or "").strip(), "other")


def _current_year() -> int:
    return datetime.datetime.now(datetime.UTC).year


def valid_class(s: str | None) -> str:
    return s if s in _CLASS_SLUGS else "all"


def valid_borough(s: str | None) -> str:
    return s if s in _BOROUGH_SLUGS else "citywide"


def valid_cat(s: str | None) -> str:
    return s if s in _CAT_SLUGS else "all"


def valid_year(s: str | None, ymax: int | None = None) -> str:
    if s == "all":
        return "all"
    try:
        y = int(s)
    except (TypeError, ValueError):
        return "all"
    hi = ymax or _current_year()
    return str(y) if MIN_YEAR <= y <= hi else "all"


# ───────────────────────── HTTP client + auth ─────────────────────────
_TIMEOUT = float(os.environ.get("SOCRATA_TIMEOUT", "30"))


def _auth_kwargs() -> dict:
    headers = {"User-Agent": "crime.kardol.us (homelab dashboard; kardolus@gmail.com)"}
    key_id = os.environ.get("OPENDATA_API_KEY_ID")
    secret = os.environ.get("OPENDATA_API_KEY_SECRET") or os.environ.get("OPENDATA_API_KEY")
    if key_id and secret:
        token = base64.b64encode(f"{key_id}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    elif os.environ.get("OPENDATA_APP_TOKEN"):
        headers["X-App-Token"] = os.environ["OPENDATA_APP_TOKEN"]
    return {"headers": headers}


_client = httpx.Client(
    timeout=httpx.Timeout(_TIMEOUT),
    transport=httpx.HTTPTransport(retries=3),
    limits=httpx.Limits(max_connections=8),
    **_auth_kwargs(),
)


class SocrataError(Exception):
    pass


def get(resource: str, **params) -> list[dict]:
    """One SoQL GET against a specific resource URL. Numbers come back as strings."""
    params.setdefault("$limit", 50000)
    try:
        r = _client.get(resource, params=params)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise SocrataError(str(e)) from e
