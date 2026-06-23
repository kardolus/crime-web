"""NYC Crime Map — Starlette app over a local Postgres mirror of NYPD Complaint Data.

Two pages — Hotspots (map + ranked table + KPIs) and Patterns (Chart.js) — driven by
four global, deep-linkable filters: ?year & ?class & ?borough & ?cat. Civic-transparency
framing (where/when/what-offense); no suspect/victim demographics anywhere.
"""

import logging
import os
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import db
from .ui import page, FAVICON_SVG, CHARTJS, LEAFLET_JS, LEAFLET_CSS


def _JSON(env):
    return JSONResponse(env)


def _filters(r) -> dict:
    info = db.available_years()["data"]
    ymax = info.get("max")
    raw_year = r.query_params.get("year")
    year = db.valid_year(raw_year, ymax)
    if raw_year is None:
        year = str(info.get("latest_full"))
    return {
        "year": year,
        "class": db.valid_class(r.query_params.get("class")),
        "borough": db.valid_borough(r.query_params.get("borough")),
        "cat": db.valid_cat(r.query_params.get("cat")),
        "years": info.get("years", []),
        "latest_full": info.get("latest_full"),
    }


def _ycbc(r):
    f = _filters(r)
    return f["year"], f["class"], f["borough"], f["cat"]


# ───────────────────────── JSON API ─────────────────────────
async def api_summary(r):
    return _JSON(db.summary_kpis(*_ycbc(r)))


async def api_hotspots(r):
    return _JSON(db.hotspots(*_ycbc(r)))


async def api_by_year(r):
    _, k, b, c = _ycbc(r)
    return _JSON(db.complaints_by_year(k, b, c))


async def api_by_hour(r):
    return _JSON(db.by_hour(*_ycbc(r)))


async def api_by_weekday(r):
    return _JSON(db.by_weekday(*_ycbc(r)))


async def api_by_month(r):
    return _JSON(db.by_month(*_ycbc(r)))


async def api_class_by_year(r):
    _, _k, b, c = _ycbc(r)
    return _JSON(db.class_by_year(b, c))


async def api_offenses(r):
    return _JSON(db.top_offenses(*_ycbc(r)))


async def api_years(r):
    return _JSON(db.available_years())


async def api_freshness(r):
    return _JSON(db.freshness())


async def favicon(r):
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


_OG = (Path(__file__).parent / "static" / "og.png").read_bytes()


async def og(r):
    return Response(_OG, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


async def healthz(r):
    return PlainTextResponse("ok")


async def ready(r):
    try:
        db.ping()
        return PlainTextResponse("ready")
    except Exception:
        return PlainTextResponse("db unavailable", status_code=503)


async def sourcez(r):
    fresh = db.freshness()
    return JSONResponse({
        "data_through": fresh["data"].get("latest", ""),
        "source_error": fresh["meta"].get("source_error"),
        "cache": db.cache_stats(),
    })


# ───────────────────────── Hotspots page (/) ─────────────────────────
_HOTSPOTS_BODY = """
<div class="kpis">
  <div class="kpi"><div class="kpi-n" id="k-total">–</div><div class="kpi-l">complaints</div></div>
  <div class="kpi"><div class="kpi-n" id="k-felony">–</div><div class="kpi-l">% felony</div></div>
  <div class="kpi"><div class="kpi-n" id="k-misd">–</div><div class="kpi-l">% misdemeanor</div></div>
  <div class="kpi"><div class="kpi-n" id="k-violation">–</div><div class="kpi-l">% violation</div></div>
</div>
<p class="meta" id="meta-line">NYPD complaint hotspots · data through <span id="through">…</span>. <span id="mapped"></span></p>
<div class="card">
  <div class="card-head"><h2>Where complaints concentrate</h2>
    <span class="legend">most common class
      <span class="dot bad"></span>felony
      <span class="dot warn"></span>misdemeanor
      <span class="dot good"></span>violation
      · size = complaints</span></div>
  <div id="map"></div>
  <p class="note">Areas are complaint clusters (~150&nbsp;m grid), labeled by precinct + most common offense. These are <em>reported complaints</em>, subject to reporting bias — not a measure of true crime or neighborhood safety.</p>
</div>
<div class="card">
  <div class="card-head"><h2>Busiest areas</h2>
    <span class="hint legend">hover a row to find it on the map · click a column to sort</span></div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th class="sortable" data-key="label" tabindex="0" aria-sort="none">Area<span class="ind"></span></th>
        <th class="sortable num" data-key="total" tabindex="0" aria-sort="descending">Complaints<span class="ind">▼</span></th>
        <th class="sortable num" data-key="felony" tabindex="0" aria-sort="none">Felony<span class="ind"></span></th>
        <th class="sortable num" data-key="misd" tabindex="0" aria-sort="none">Misd.<span class="ind"></span></th>
        <th class="sortable num" data-key="violation" tabindex="0" aria-sort="none">Violation<span class="ind"></span></th>
      </tr></thead>
      <tbody id="rows"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
    </table>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const isDark = () => document.documentElement.classList.contains('dark');
let map, layer, data = [];
const markers = new Map();
let sort = {key:'total', dir:'desc'};
const NUMERIC = new Set(['total','felony','misd','violation']);

function animate(el, target){
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches){ el.textContent = target.toLocaleString(); return; }
  const dur = 600, t0 = performance.now();
  (function step(t){ const p = Math.min(1,(t-t0)/dur);
    el.textContent = Math.round(target*(1-(1-p)**3)).toLocaleString();
    if (p<1) requestAnimationFrame(step); })(t0);
}
function domClass(x){ return (x.felony>=x.misd && x.felony>=x.violation) ? 'bad'
  : (x.violation>x.misd && x.violation>x.felony) ? 'good' : 'warn'; }
function radius(x){ return Math.max(5, Math.min(24, 4 + 3.2*Math.log10((x.total||0)+1))); }
function keyVal(x,k){ if (k==='label') return x.label.toLowerCase(); return x[k]; }

function renderTable(){
  const m = sort.dir==='asc' ? 1 : -1;
  data.sort((a,b)=>{ const va=keyVal(a,sort.key), vb=keyVal(b,sort.key);
    if (va<vb) return -m; if (va>vb) return m; return b.total-a.total; });
  if (!data.length){ $('rows').innerHTML = '<tr><td colspan="5" class="empty">No complaints match these filters.</td></tr>'; }
  else $('rows').innerHTML = data.map((x,i) =>
    `<tr data-i="${i}">
       <td>${x.label}</td><td class="num">${x.total.toLocaleString()}</td>
       <td class="num">${x.felony.toLocaleString()}</td><td class="num">${x.misd.toLocaleString()}</td>
       <td class="num">${x.violation.toLocaleString()}</td>
     </tr>`).join('');
  document.querySelectorAll('th.sortable').forEach(th => {
    const on = th.dataset.key===sort.key;
    th.querySelector('.ind').textContent = on ? (sort.dir==='asc'?'▲':'▼') : '';
    th.setAttribute('aria-sort', on ? (sort.dir==='asc'?'ascending':'descending') : 'none');
  });
}
function setSort(key){
  if (sort.key===key) sort.dir = sort.dir==='asc'?'desc':'asc';
  else sort = {key, dir: NUMERIC.has(key) ? 'desc' : 'asc'};
  renderTable();
}
const thead = document.querySelector('thead');
thead.addEventListener('click', e => { const th=e.target.closest('th.sortable'); if (th) setSort(th.dataset.key); });
thead.addEventListener('keydown', e => { if (e.key==='Enter'||e.key===' '){ const th=e.target.closest('th.sortable'); if (th){ e.preventDefault(); setSort(th.dataset.key); } } });
$('rows').addEventListener('mouseover', e => { const tr=e.target.closest('tr'); const mk=tr&&markers.get(tr.dataset.i);
  if (mk){ mk.setStyle({weight:4, fillOpacity:1}); mk.bringToFront(); } });
$('rows').addEventListener('mouseout', e => { const tr=e.target.closest('tr'); const mk=tr&&markers.get(tr.dataset.i);
  if (mk){ mk.setStyle({weight:2, fillOpacity:.8}); } });

const tileU = () => 'https://{s}.basemaps.cartocdn.com/' + (isDark()?'dark_all':'light_all') + '/{z}/{x}/{y}{r}.png';
let tileLayer;
async function load(){
  api('/api/summary').then(res => {
    const s = res.data; if (!s || s.total==null) return;
    animate($('k-total'), s.total);
    $('k-felony').textContent = s.pct_felony+'%'; $('k-misd').textContent = s.pct_misd+'%';
    $('k-violation').textContent = s.pct_violation+'%';
    $('mapped').textContent = `${(s.mapped||0).toLocaleString()} of ${(s.total||0).toLocaleString()} complaints are mapped.`;
  });
  api('/api/freshness').then(res => { $('through').textContent = res.data.latest || '—'; });

  const res = await api('/api/hotspots');
  data = res.data || [];
  const pts = data.filter(x => x.lat && x.lon);
  if (!map){
    map = L.map('map');
    tileLayer = L.tileLayer(tileU(), {attribution:'© OpenStreetMap, © CARTO', maxZoom:19}).addTo(map);
    window.addEventListener('themechange', () => tileLayer.setUrl(tileU()));
    if (pts.length) map.fitBounds(pts.map(x => [x.lat, x.lon]), {padding:[30,30]});
    else map.setView([40.7128, -74.0060], 11);
    setTimeout(() => map.invalidateSize(), 0);
  }
  if (layer) layer.remove();
  markers.clear();
  layer = L.layerGroup();
  data.forEach((x,i) => {
    if (!x.lat || !x.lon) return;
    const c = css('--'+domClass(x));
    const mk = L.circleMarker([x.lat, x.lon],
      {radius:radius(x), color:c, fillColor:c, fillOpacity:.8, weight:2});
    mk.bindPopup(`<b>${x.label}</b><br>${x.total.toLocaleString()} complaints`
      + `<br>${x.felony.toLocaleString()} felony · ${x.misd.toLocaleString()} misd. · ${x.violation.toLocaleString()} violation`);
    mk.on('mouseover', () => { const tr=document.querySelector(`tr[data-i="${i}"]`); if (tr) tr.classList.add('row-hl'); });
    mk.on('mouseout',  () => { const tr=document.querySelector(`tr[data-i="${i}"]`); if (tr) tr.classList.remove('row-hl'); });
    markers.set(String(i), mk); mk.addTo(layer);
  });
  layer.addTo(map);
  renderTable();
}
load();
window.addEventListener('resize', () => map && map.invalidateSize());
</script>
"""


def hotspots_page(r):
    f = _filters(r)
    head = (f'<link rel="stylesheet" href="{LEAFLET_CSS[0]}" integrity="{LEAFLET_CSS[1]}" crossorigin="anonymous">'
            f'<script src="{LEAFLET_JS[0]}" integrity="{LEAFLET_JS[1]}" crossorigin="anonymous"></script>')
    return HTMLResponse(page("Hotspots", "/", _HOTSPOTS_BODY, f, head_extra=head))


# ───────────────────────── Patterns page (/patterns) ─────────────────────────
_PATTERNS_BODY = """
<p class="meta">NYPD complaint patterns · data through <span id="through">…</span>. Reported complaints, subject to reporting bias — not a measure of true crime. Times are New York local. Click ⓘ on a chart to learn how to read it.</p>
<div class="grid2">
  <div class="card"><div class="card-head"><h2>Complaints by year</h2><button class="info-btn" data-help="year" aria-label="About this chart">i</button></div><div class="chart-wrap"><canvas id="c-year"></canvas></div></div>
  <div class="card"><div class="card-head"><h2>Complaints by hour of day</h2><button class="info-btn" data-help="hour" aria-label="About this chart">i</button></div><div class="chart-wrap"><canvas id="c-hour"></canvas></div></div>
  <div class="card"><div class="card-head"><h2>Complaints by day of week</h2><button class="info-btn" data-help="dow" aria-label="About this chart">i</button></div><div class="chart-wrap"><canvas id="c-dow"></canvas></div></div>
  <div class="card"><div class="card-head"><h2>Complaints by month</h2><button class="info-btn" data-help="month" aria-label="About this chart">i</button></div><div class="chart-wrap"><canvas id="c-month"></canvas></div></div>
</div>
<h2 class="section">By class & offense</h2>
<div class="grid2">
  <div class="card"><div class="card-head"><h2>Complaints by law class, by year</h2><button class="info-btn" data-help="classyr" aria-label="About this chart">i</button></div><div class="subtitle" style="margin-bottom:8px">Ignores the law-class filter — always shows all three.</div><div class="chart-wrap"><canvas id="c-classyr"></canvas></div></div>
  <div class="card"><div class="card-head"><h2>Top offense types</h2><button class="info-btn" data-help="offenses" aria-label="About this chart">i</button></div><div class="chart-wrap tall"><canvas id="c-offenses"></canvas></div></div>
</div>
<dialog id="help" class="help">
  <div class="help-hd"><h3 id="help-title"></h3><button class="help-close" aria-label="Close">×</button></div>
  <div class="help-bd" id="help-body"></div>
</dialog>
<script>
const v = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const DOW = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const DOW_ORDER = [1,2,3,4,5,6,0];
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
Chart.defaults.font.family = "'DM Sans', sans-serif";
Chart.defaults.color = v('--meta');
Chart.defaults.maintainAspectRatio = false;
const noLegend = {plugins:{legend:{display:false}}};
function empty(id, msg){ const cw = document.getElementById(id).closest('.chart-wrap');
  if (cw) cw.innerHTML = '<p class="empty">'+(msg||'No data for these filters.')+'</p>'; }

(async () => {
  api('/api/freshness').then(res => { document.getElementById('through').textContent = res.data.latest || '—'; });

  const yr = (await api('/api/by_year')).data;
  if (!yr.length) empty('c-year'); else
  new Chart('c-year', {type:'line', data:{labels:yr.map(x=>x.year),
    datasets:[{data:yr.map(x=>x.total), borderColor:v('--accent'), backgroundColor:v('--accent-soft'), tension:.3, fill:true}]},
    options:{...noLegend, scales:{y:{title:{display:true,text:'complaints'}}}}});

  const hr = (await api('/api/by_hour')).data;
  if (!hr.length) empty('c-hour'); else
  new Chart('c-hour', {type:'bar', data:{labels:hr.map(x=>x.hr),
    datasets:[{data:hr.map(x=>x.total), backgroundColor:v('--accent')}]},
    options:{...noLegend, scales:{x:{title:{display:true,text:'hour (NY time)'}}, y:{title:{display:true,text:'complaints'}}}}});

  const dw = (await api('/api/by_weekday')).data;
  if (!dw.length) empty('c-dow'); else {
    const by = {}; dw.forEach(x=>by[x.dow]=x.total);
    new Chart('c-dow', {type:'bar', data:{labels:DOW_ORDER.map(d=>DOW[d]),
      datasets:[{data:DOW_ORDER.map(d=>by[d]??0), backgroundColor:v('--accent')}]},
      options:{...noLegend, scales:{y:{title:{display:true,text:'complaints'}}}}});
  }

  const mo = (await api('/api/by_month')).data;
  if (!mo.length) empty('c-month'); else
  new Chart('c-month', {type:'bar', data:{labels:MONTHS,
    datasets:[{data:mo.map(x=>x.total), backgroundColor:v('--accent')}]},
    options:{...noLegend, scales:{y:{title:{display:true,text:'complaints'}}}}});

  const cy = (await api('/api/class_by_year')).data;
  if (!cy.length) empty('c-classyr'); else
  new Chart('c-classyr', {type:'bar', data:{labels:cy.map(x=>x.year),
    datasets:[{label:'Felony', data:cy.map(x=>x.felony), backgroundColor:v('--bad')},
              {label:'Misdemeanor', data:cy.map(x=>x.misd), backgroundColor:v('--warn')},
              {label:'Violation', data:cy.map(x=>x.violation), backgroundColor:v('--good')}]},
    options:{scales:{x:{stacked:true}, y:{stacked:true, title:{display:true,text:'complaints'}}}}});

  const of = (await api('/api/offenses')).data;
  if (!of.length) empty('c-offenses'); else
  new Chart('c-offenses', {type:'bar', data:{labels:of.map(x=>x.offense),
    datasets:[{data:of.map(x=>x.total), backgroundColor:v('--accent')}]},
    options:{indexAxis:'y', ...noLegend, scales:{x:{title:{display:true,text:'complaints'}}}}});
})();

const HELP = {
  year: ['Complaints by year', 'Total reported NYPD complaints per year for the current law-class, borough, and offense filters (the year filter does not apply here — this chart always spans all years). The partial latest year is excluded so the trend doesn’t show a misleading drop.'],
  hour: ['Complaints by hour of day', 'Complaints bucketed by the hour the incident began (00–23, New York local), for the selected filters.'],
  dow: ['Complaints by day of week', 'Complaints by day of week (Mon–Sun) for the current filters.'],
  month: ['Complaints by month', 'Complaints by calendar month for the current filters — useful for seasonal patterns.'],
  classyr: ['Complaints by law class, by year', 'Reported complaints each year split into felony, misdemeanor, and violation (stacked). This chart deliberately ignores the law-class filter so all three are always comparable.'],
  offenses: ['Top offense types', 'The most common NYPD offense descriptions for the current filters. These are reported complaints, not convictions, and reflect reporting patterns.'],
};
const dlg = document.getElementById('help');
document.querySelectorAll('.info-btn').forEach(b => b.addEventListener('click', () => {
  const [t, body] = HELP[b.dataset.help];
  document.getElementById('help-title').textContent = t;
  document.getElementById('help-body').textContent = body;
  dlg.showModal();
}));
document.querySelector('.help-close').addEventListener('click', () => dlg.close());
dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
</script>
"""


def patterns_page(r):
    f = _filters(r)
    head = f'<script src="{CHARTJS[0]}" integrity="{CHARTJS[1]}" crossorigin="anonymous"></script>'
    return HTMLResponse(page("Patterns", "/patterns", _PATTERNS_BODY, f, head_extra=head))


app = Starlette(routes=[
    Route("/", hotspots_page),
    Route("/patterns", patterns_page),
    Route("/favicon.svg", favicon),
    Route("/favicon.ico", favicon),
    Route("/og.png", og),
    Route("/healthz", healthz),
    Route("/ready", ready),
    Route("/sourcez", sourcez),
    Route("/api/summary", api_summary),
    Route("/api/hotspots", api_hotspots),
    Route("/api/by_year", api_by_year),
    Route("/api/by_hour", api_by_hour),
    Route("/api/by_weekday", api_by_weekday),
    Route("/api/by_month", api_by_month),
    Route("/api/class_by_year", api_class_by_year),
    Route("/api/offenses", api_offenses),
    Route("/api/years", api_years),
    Route("/api/freshness", api_freshness),
])


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
