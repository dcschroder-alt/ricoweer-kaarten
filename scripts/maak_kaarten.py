#!/usr/bin/env python3
"""Publiceer twee PNG's vanuit Ricoweer's bestaande dagelijkse modelmix.

Deze exporter opent de bestaande publieke kaartpagina in een headless browser.
Er wordt GEEN nieuwe meteorologische berekening uitgevoerd en uurdata vervangt
NOOIT de dagminimum/-maximumtemperatuur. Bij ontbrekende data/kaarttegels faalt
het script voordat GitHub Pages de twee bestaande PNG's kan vervangen.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

SOURCE = os.environ.get("SOURCE_MAP_URL", "https://dcschroder-alt.github.io/Regioweer/weerkaart.html")
OUT = Path(os.environ.get("OUTPUT_DIR", "public")).resolve()
SCAN_KEY = "rico-national-scan-v75-35punten"
MIN_POINTS = 12
MAX_AGE_MS = 4 * 60 * 60 * 1000
MAX_WAIT_MS = int(os.environ.get("MAX_WAIT_MS", "110000"))
RETRY_WAIT_MS = int(os.environ.get("RETRY_WAIT_MS", "70000"))
SCAN_CACHE_PATH = Path(os.environ.get('SCAN_CACHE_PATH', '.cache/rico_national_scan.json'))

EXPORT_CSS = """
#ricoLiveShell {max-width:1060px!important; padding:12px 10px!important}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherGrid {
 display:grid!important; grid-template-columns:minmax(0,1fr)!important;
 width:100%!important; max-width:1000px!important; margin:0 auto!important; gap:20px!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherCard {
 position:relative!important; background:#e8f2f7!important;
 border:1px solid #cadfe9!important; border-radius:16px!important;
 box-shadow:none!important; overflow:hidden!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherMap {
 height:1030px!important; max-height:none!important; min-height:1030px!important; width:100%!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherTitle {
 display:flex!important; justify-content:space-between!important;
 padding:16px 20px!important; background:#e7f3fb!important;
 color:#064b71!important; font-size:27px!important; line-height:1.3!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherTitle span:last-child {display:none!important}
/* Alleen de gepubliceerde PNG's: kaartlabels leesbaar bij verkleinen in WordPress.
   De live Ricoweer-app, datamodellen en de kaartselectie veranderen niet. */
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherPin.rico82Pin {
 width:98px!important; height:98px!important; margin-left:-8px!important;
 transform:translateY(-8px); overflow:visible!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherPin.rico82Pin .ricoWeatherSymbol {
 display:flex!important; flex:0 0 48px!important; height:48px!important;
 line-height:48px!important; align-items:center!important; justify-content:center!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherPin.rico82Pin .ricoUnifiedWeatherSvg {
 width:49px!important; height:49px!important; max-width:none!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherPin.rico82Pin .ricoWeatherTemp {
 font-size:23px!important; line-height:1.08!important; font-weight:950!important;
 padding:4px 7px!important; border:2px solid white!important;
 border-radius:16px!important; gap:3px!important; flex-shrink:0!important;
 text-shadow:0 0 1px #153e4b22;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherPin.rico82Pin .ricoWeatherTemp .ricoSep {
 font-size:18px!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherPin.rico82Pin .ricoDailyWind {
 font-size:15px!important; line-height:20px!important; padding:3px 7px!important;
 margin-top:3px!important; max-width:none!important; flex-shrink:0!important;
 border-radius:12px!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherPin.rico82Pin .ricoDailyWind .ricoWeatherWindArrow {
 font-size:18px!important;
}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .leaflet-control-zoom {display:none!important}
#ricoLiveShell #ricoWeatherAtlas.ricoLiveAtlas .ricoWeatherMap::after {
 content:'Kaart © OpenStreetMap-bijdragers'; position:absolute; bottom:5px; right:6px;
 z-index:10000; padding:3px 6px; color:#24435d; background:#ffffffdd;
 font:11px/1.2 system-ui,Arial,sans-serif; border-radius:4px; pointer-events:none;
}
"""

VALIDATED_JS = """({cacheKey, dates, maxAge, minPts}) => {
 const raw = localStorage.getItem(cacheKey);
 let pack;
 try { pack = JSON.parse(raw || 'null'); } catch(e) { return false; }
 if (!pack || !Array.isArray(pack.days)) return false;
 const age = Date.now() - Number(pack.savedAt || 0);
 if (age < 0 || age > maxAge || !Array.isArray(pack.models) || pack.models.length < 2) return false;
 const chosen = pack.days.filter(d => dates.includes(d.date));
 if (chosen.length !== 2 || chosen[0].date !== dates[0] || chosen[1].date !== dates[1]) return false;
 if (!chosen.every(d => (d.pts || []).filter(p => Number.isFinite(p.tx) && Number.isFinite(p.tn) &&
           Number.isFinite(p.point?.lat) && Number.isFinite(p.point?.lon)).length >= minPts)) return false;
 const cards = [...document.querySelectorAll('#ricoLiveShell #ricoWeatherAtlas .ricoWeatherGrid > .ricoWeatherCard')];
 return cards.length === 2 && cards.every(c => c.querySelectorAll('.ricoWeatherPin').length >= 8);
}"""

WIDGET_VALIDATED_JS = """({cacheKey, date, maxAge, minPts}) => {
 let pack;
 try {pack=JSON.parse(localStorage.getItem(cacheKey)||'null')} catch(e){return false}
 if(!pack||!Array.isArray(pack.models)||pack.models.length<2||!Array.isArray(pack.days))return false;
 if(Date.now()-Number(pack.savedAt||0)>maxAge)return false;
 const hm=new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Amsterdam',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date());
 const [hour,minute]=hm.split(':').map(Number);
 const start=new Date(Date.parse(date+'T12:00:00Z')+(hour>=17?0:-86400000)).toISOString().slice(0,10);
 const day=pack.days.find(x=>x.date===start);
 if(!day||day.pts.filter(p=>Number.isFinite(p.tx)&&Number.isFinite(p.tn)).length<minPts)return false;
 const card=document.querySelector('#ricoLiveShell #ricoWeatherAtlas .ricoWeatherGrid > .ricoWeatherCard:first-child');
 const rows=[...document.querySelectorAll('#ricoWidgetFacts .rico5DayRow')];
 const dates=Array.from({length:10},(_,i)=>new Date(Date.parse(start+'T12:00:00Z')+i*86400000).toISOString().slice(0,10));
 return !!card&&card.querySelectorAll('.ricoWeatherPin').length>=7 &&
        rows.length===10 && rows.every((r,i)=>r.dataset.date===dates[i]) &&
        !!document.querySelector('#ricoWidgetFacts .rico5DaySummary') &&
        document.querySelector('.ricoWidgetMode')!==null;
}"""

DEBUG_JS = """key => {
 let pack; try {pack=JSON.parse(localStorage.getItem(key)||'null')}catch(e){}
 return {
  scanStatus:document.getElementById('nationalScanStatus')?.textContent?.slice(0,400),
  liveStatus:document.getElementById('ricoLiveStatus')?.textContent?.slice(0,400),
  savedAt:pack?.savedAt, models:pack?.models,
  days:pack?.days?.slice(0,5).map(x=>({date:x.date,count:x.pts?.filter(p=>Number.isFinite(p.tx)).length})),
  visibleCards:document.querySelectorAll('#ricoLiveShell #ricoWeatherAtlas .ricoWeatherCard').length,
  visiblePins:[...document.querySelectorAll('#ricoLiveShell #ricoWeatherAtlas .ricoWeatherCard')].map(x=>x.querySelectorAll('.ricoWeatherPin').length),
  widgetRows:[...document.querySelectorAll('#ricoWidgetFacts .rico5DayRow')].map(x=>x.dataset.date),
  widgetFacts:document.getElementById('ricoWidgetFacts')?.textContent?.slice(0,180)
 }
}"""

QUALITY_JS = """el => {
 const map = el.querySelector('.ricoWeatherMap');
 const loaded = [...(map?.querySelectorAll('img.leaflet-tile-loaded') || [])].filter(x=>x.complete && x.naturalWidth>0);
 const rect = map?.getBoundingClientRect();
 return {tiles: loaded.length, pins:map?.querySelectorAll('.ricoWeatherPin').length||0,
         width:Math.round(rect?.width||0),height:Math.round(rect?.height||0)};
}"""


def prior_ec46_snapshot() -> dict | None:
    """Only prior *published* EC46 source timestamp; never compare two fetches of same run."""
    from urllib.request import Request, urlopen
    from urllib.parse import urljoin
    base=os.environ.get('PUBLISHED_PAGES_URL',
        'https://dcschroder-alt.github.io/ricoweer-kaarten/').rstrip('/')+'/'
    try:
        with urlopen(Request(urljoin(base,'ec46-snapshot.json'),
                   headers={'User-Agent':'Ricoweer-EC46-compare/1.0'}),timeout=8) as r:
            raw=r.read(25000)
        pack=json.loads(raw)
        if not isinstance(pack,dict) or not isinstance(pack.get('weeks'),list):return None
        return pack
    except Exception as exc:
        print('Nog geen vorige EC46-snapshot beschikbaar:',str(exc)[:120],flush=True)
        return None


def read_scan_cache(expected_dates: list[str]) -> dict | None:
    """GitHub Actions cache is private to the repo job, never published as a Pages PNG."""
    try:
        if not SCAN_CACHE_PATH.is_file():return None
        if SCAN_CACHE_PATH.stat().st_size>12*1024*1024:return None
        pack=json.loads(SCAN_CACHE_PATH.read_text(encoding='utf-8'))
        ts=int(pack.get('savedAt') or 0)
        age=datetime.now().timestamp()*1000-ts
        if age<0 or age>MAX_AGE_MS:return None
        if len(pack.get('models',[]))<2:return None
        days={day.get('date'):day for day in pack.get('days',[]) if isinstance(day,dict)}
        for d in expected_dates:
            pts=days.get(d,{}).get('pts',[])
            if sum(1 for pt in pts if isinstance(pt,dict) and
                   isinstance(pt.get('tx'),(int,float)) and
                   isinstance(pt.get('tn'),(int,float)) and
                   isinstance(pt.get('point'),dict) and
                   isinstance(pt['point'].get('lat'),(int,float)) and
                   isinstance(pt['point'].get('lon'),(int,float)))<MIN_POINTS:return None
        print(f'Geldige scan uit workflow-cache gevonden ({age/60000:.0f} minuten oud).',flush=True)
        return pack
    except (OSError,ValueError,TypeError,AttributeError) as exc:
        print('Scan-cache niet bruikbaar:',str(exc)[:180],flush=True)
        return None


def write_scan_cache(pack: dict) -> None:
    if not pack or not isinstance(pack.get('days'),list):return
    try:
        SCAN_CACHE_PATH.parent.mkdir(parents=True,exist_ok=True)
        tmp=SCAN_CACHE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(pack,separators=(',',':')),encoding='utf-8')
        tmp.replace(SCAN_CACHE_PATH)
        print('Gevalideerde scan bewaard voor een volgende GitHub-run.',flush=True)
    except OSError as exc:
        print('Scan-cache niet opgeslagen (export gaat door):',exc,flush=True)


async def make_images() -> None:
    if not SOURCE.startswith("https://") and os.environ.get("ALLOW_HTTP_FOR_TEST") != "1":
        raise RuntimeError("De bronkaart moet via HTTPS beschikbaar zijn.")
    nl_today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    expected = [(nl_today + timedelta(days=i)).isoformat() for i in (1, 2)]
    cached_scan = read_scan_cache(expected)
    print(f"Dagen voor WordPress: morgen {expected[0]}, overmorgen {expected[1]}", flush=True)
    previous_ec46 = prior_ec46_snapshot()
    errors = []
    throttled_urls = set()
    print("API-beleid: geen oude modelscan als nieuw; HTTP 429 wordt afzonderlijk gemeld.", flush=True)

    async with async_playwright() as playwright:
        browser_opts = {"headless": True, "args": ["--no-sandbox"]}
        if os.environ.get("BROWSER_EXECUTABLE"):
            browser_opts["executable_path"] = os.environ["BROWSER_EXECUTABLE"]
        browser = await playwright.chromium.launch(**browser_opts)
        try:
            context = await browser.new_context(
                viewport={"width": 1140, "height": 1400}, device_scale_factor=2,
                locale="nl-NL", timezone_id="Europe/Amsterdam", service_workers="block",
            )
            if cached_scan:
                # Seed the origin's localStorage before Ricoweer's source JS starts.
                # Page logic decides whether to use the stored scan; exporter validates again.
                from urllib.parse import urlparse
                origin = f"{urlparse(SOURCE).scheme}://{urlparse(SOURCE).netloc}"
                payload = json.dumps(cached_scan,separators=(',',':'))
                await context.add_init_script(script=(
                    "(()=>{try{if(location.origin!=="+json.dumps(origin)+")return;"
                    "localStorage.setItem("+json.dumps(SCAN_KEY)+","+json.dumps(payload)+");"
                    "}catch(e){}})();"))
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)) if len(errors) < 15 else None)
            page.on("requestfailed", lambda req: errors.append(f"Aanvraag mislukt: {req.url[:180]} ({req.failure})") if len(errors) < 15 else None)
            def track_response(res):
                if res.status == 429:
                    throttled_urls.add(res.url.split('?')[0])
                if res.status >= 400 and len(errors) < 15:
                    errors.append(f"HTTP {res.status}: {res.url[:180]}")
            page.on("response", track_response)
            page.set_default_timeout(25000)
            from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
            u = urlparse(SOURCE)
            q = dict(parse_qsl(u.query))
            q["publicatie"] = str(int(datetime.now().timestamp() * 1000))
            url = urlunparse(u._replace(query=urlencode(q)))
            response = await page.goto(url, wait_until="domcontentloaded", timeout=55000)
            if not response or not response.ok:
                code = response.status if response else "geen reactie"
                raise RuntimeError(f"De publieke bronkaart is niet bereikbaar: HTTP {code}. Controleer weerkaart.html.")

            await page.add_style_tag(content=EXPORT_CSS)
            # Een scan kan tijdelijk vastlopen terwijl de pagina wel laadt.
            # Probeer dan eenmaal opnieuw in dezelfde browsersessie. De inhoudelijke
            # controles blijven ongewijzigd: nooit oude of incomplete kaarten publiceren.
            for attempt in range(2):
                try:
                    await page.wait_for_function(
                        VALIDATED_JS,
                        arg={"cacheKey": SCAN_KEY, "dates": expected, "maxAge": MAX_AGE_MS, "minPts": MIN_POINTS},
                        timeout=MAX_WAIT_MS if attempt == 0 else RETRY_WAIT_MS, polling=1500,
                    )
                    print(f"Landelijke scan compleet (poging {attempt + 1}).", flush=True)
                    break
                except PlaywrightTimeoutError as exc:
                    debug = await page.evaluate(DEBUG_JS, SCAN_KEY)
                    print(f"Landelijke scan niet compleet na poging {attempt + 1}:", json.dumps(debug, ensure_ascii=False), flush=True)
                    print("Browser- en netwerkfouten:", errors[-15:], flush=True)
                    if throttled_urls:
                        raise RuntimeError("Open-Meteo heeft aanvragen beperkt (HTTP 429). "
                                           "Nieuwe modelscan gestopt om extra verzoeken te vermijden; "
                                           "vorige GitHub Pages-publicatie blijft staan. Bronnen: " +
                                           ", ".join(sorted(throttled_urls))) from exc
                    if attempt == 1:
                        raise RuntimeError("Geen twee complete, actuele dagkaarten na twee pogingen; bestaande publicatie blijft behouden.") from exc
                    print("Herstart bronpagina voor een nieuwe modelscan...", flush=True)
                    await page.reload(wait_until="domcontentloaded", timeout=55000)
                    await page.add_style_tag(content=EXPORT_CSS)

            info = await page.evaluate(
                "key => {const p=JSON.parse(localStorage.getItem(key));return {savedAt:p.savedAt,models:p.models,dates:p.days.map(x=>x.date)};}",
                SCAN_KEY,
            )
            print("Gecontroleerde landelijke modelmix:", json.dumps(info, ensure_ascii=False), flush=True)
            validated_pack = await page.evaluate("key=>JSON.parse(localStorage.getItem(key)||'null')",SCAN_KEY)
            if validated_pack and cached_scan and validated_pack.get('savedAt') == cached_scan.get('savedAt'):
                print('Publicatie gebruikt gevalideerde cachedagscan; oorspronkelijke scantijd blijft zichtbaar.',flush=True)
            write_scan_cache(validated_pack)
            # We vergroten bij de PNG-export ook de labels, omdat WordPress
            # de 1000px brede kaart op mobiele schermen verkleint. CSS geldt
            # alleen op deze headless exportpagina, niet in Ricoweer zelf.
            # De openbare kaartpagina heeft standaard TWEE kaarten naast elkaar.
            # Op een headless browser van 1140 px worden die elk ~511 px breed.
            # Zet uitsluitend voor de PNG-export de kaartpanelen ONDER elkaar;
            # zo blijft de hoge-resolutiekaart breed genoeg zonder controles te verlagen.
            layout = await page.evaluate("""() => {
                const shell = document.querySelector('#ricoLiveShell');
                const atlas = shell?.querySelector('#ricoWeatherAtlas');
                const grid = atlas?.querySelector('.ricoWeatherGrid');
                if (!shell || !atlas || !grid) return {error:'Live kaartlayout ontbreekt'};
                const important = (el, key, value) => el.style.setProperty(key, value, 'important');
                important(shell, 'width', '1060px');
                important(shell, 'max-width', '1060px');
                important(atlas, 'width', '100%');
                important(grid, 'display', 'grid');
                important(grid, 'grid-template-columns', 'minmax(0, 1fr)');
                important(grid, 'width', '100%');
                important(grid, 'max-width', '1000px');
                important(grid, 'margin-left', 'auto');
                important(grid, 'margin-right', 'auto');
                for (const card of grid.querySelectorAll(':scope > .ricoWeatherCard')) {
                    important(card, 'width', '100%');
                    important(card, 'min-width', '0');
                    const map = card.querySelector('.ricoWeatherMap');
                    if (map) {
                        important(map, 'width', '100%');
                        important(map, 'height', '1030px');
                        important(map, 'min-height', '1030px');
                        important(map, 'max-height', 'none');
                    }
                }
                return {
                    gridWidth: Math.round(grid.getBoundingClientRect().width),
                    cardWidths: [...grid.querySelectorAll(':scope > .ricoWeatherCard')].map(c => Math.round(c.getBoundingClientRect().width)),
                    layout: getComputedStyle(grid).gridTemplateColumns
                };
            }""")
            print("Export-layout:", layout, flush=True)
            if layout.get('error') or min(layout.get('cardWidths', [0])) < 700:
                raise RuntimeError('Export-layout kon niet breed genoeg worden gemaakt: ' + str(layout))
            await page.evaluate("""() => {
                window.dispatchEvent(new Event('resize'));
                for(const el of document.querySelectorAll('#ricoLiveShell .ricoWeatherMap'))
                    window.dispatchEvent(new CustomEvent('rico:daily-map-visible',{detail:{element:el}}));
            }""")
            await page.wait_for_timeout(2600)

            cards = page.locator("#ricoLiveShell #ricoWeatherAtlas .ricoWeatherGrid > .ricoWeatherCard")
            if await cards.count() != 2:
                raise RuntimeError("Niet precies twee kaartpanelen gevonden.")
            # Schrijf naar een tijdelijke map. Na alle controles wordt er pas gedeployed.
            OUT.mkdir(parents=True, exist_ok=True)
            for i, filename in enumerate(("morgen.png", "overmorgen.png")):
                card = cards.nth(i)
                await card.scroll_into_view_if_needed()
                await page.wait_for_timeout(1800)
                quality = await card.evaluate(QUALITY_JS)
                print(f"{filename}: {quality}", flush=True)
                if (quality["tiles"] < 8 or quality["pins"] < 8 or
                    quality["width"] < 700 or quality["height"] < 700):
                    raise RuntimeError(f"Kaart {filename} is onvolledig: {quality}; publicatie gestopt.")
                destination = OUT / filename
                await card.screenshot(path=str(destination), type="png", animations="disabled", timeout=50000)
                if destination.stat().st_size < 65000:
                    raise RuntimeError(f"PNG {filename} lijkt leeg.")
                print(f"PNG gemaakt: {destination.name} ({destination.stat().st_size} bytes)", flush=True)
            # Extra WordPress-zijbalkkaart met vijfdaagse Randstadverwachting. Daggegevens komen uit exact dezelfde
            # landelijke modelmix; geen dagmaxima uit uurverwachtingen afleiden.
            # Ook dit bestand pas publiceerbaar maken na inhoudelijke/visuele controle.
            from urllib.parse import urlparse, urlunparse
            source_parts=urlparse(SOURCE)
            if not source_parts.path.endswith('/weerkaart.html'):
                raise RuntimeError('SOURCE_MAP_URL moet eindigen op /weerkaart.html')
            widget_source=urlunparse(source_parts._replace(
                path=source_parts.path[:-len('weerkaart.html')]+'widgetkaart.html',
                query=f'publicatie={int(datetime.now().timestamp()*1000)}'))
            # De widget gebruikt dezelfde gevalideerde, actuele dagscan als de landkaarten.
            # Daarmee voorkom je een tweede API-scan en verschillen rond 17:00 uur.
            if not validated_pack or validated_pack.get('savedAt') is None:
                raise RuntimeError('Geen gevalideerde landelijke dagscan voor de Randstad-widget.')
            await context.add_init_script(script=(
                "(()=>{try{if(location.origin!=="+json.dumps(f"{source_parts.scheme}://{source_parts.netloc}")+ ")return;"
                "localStorage.setItem("+json.dumps(SCAN_KEY)+","+
                json.dumps(json.dumps(validated_pack, separators=(',',':')))+");"
                "}catch(e){}})();"))
            widget_page=await context.new_page()
            widget_page.on('pageerror', lambda e: errors.append('Widget JS: '+str(e)) if len(errors)<20 else None)
            widget_response=await widget_page.goto(widget_source, wait_until='domcontentloaded',timeout=55000)
            if not widget_response or not widget_response.ok:
                raise RuntimeError(f'Compacte bronpagina widgetkaart.html niet bereikbaar ({widget_response.status if widget_response else "geen reactie"}).')
            for attempt in range(2):
                try:
                    await widget_page.wait_for_function(WIDGET_VALIDATED_JS,
                        arg={'cacheKey':SCAN_KEY,'date':expected[0],'maxAge':MAX_AGE_MS,'minPts':MIN_POINTS},
                        timeout=MAX_WAIT_MS if attempt == 0 else RETRY_WAIT_MS,polling=1300)
                    print(f"Randstad-widget compleet (poging {attempt + 1}).", flush=True)
                    break
                except PlaywrightTimeoutError as exc:
                    debug=await widget_page.evaluate(DEBUG_JS,SCAN_KEY)
                    print(f'Widgetdiagnose poging {attempt + 1}:',json.dumps(debug,ensure_ascii=False),flush=True)
                    if attempt == 1:
                        raise RuntimeError('Widgetpagina heeft na twee pogingen onvoldoende actuele dagmodelmix of kaartpunten.') from exc
                    print('Herstart widgetpagina voor nieuwe modelscan...',flush=True)
                    await widget_page.reload(wait_until='domcontentloaded',timeout=55000)
            widget=widget_page.locator('#ricoLiveShell')
            await widget.scroll_into_view_if_needed()
            await widget_page.wait_for_timeout(1600)
            quality=await widget.locator('.ricoWeatherCard:first-child').evaluate(QUALITY_JS)
            facts=await widget_page.locator('#ricoWidgetFacts .rico5DayRow').count()
            size=await widget.evaluate('el=>({w:Math.round(el.getBoundingClientRect().width),h:Math.round(el.getBoundingClientRect().height)})')
            print('WordPress-zijbalkkaart:',quality,'facts:',facts,'grootte:',size,flush=True)
            if quality['tiles']<7 or quality['pins']<7 or quality['width']<430 or quality['height']<560 or facts!=10 or size['w']<450:
                raise RuntimeError('De widget is onvolledig; huidige gepubliceerde afbeeldingen blijven behouden.')
            widget_destination=OUT/'widget.png'
            await widget.screenshot(path=str(widget_destination),type='png',animations='disabled',timeout=50000)
            if widget_destination.stat().st_size<45000:
                raise RuntimeError('Vijfdaagse Randstad-widget PNG lijkt leeg; publicatie gestopt.')
            print(f'Widget gemaakt: {widget_destination.name} ({widget_destination.stat().st_size} bytes)',flush=True)
            # Extra compacte tiendaagse zonder de grote landkaart. Deze versie past
            # in de smalle WordPress-widgetbalk. Exact dezelfde regionale dagmix.
            forecast=widget_page.locator('#ricoWidgetFacts')
            if await forecast.locator('.rico5DayRow').count()!=10:
                raise RuntimeError('Randstad 10-daagse onvolledig: publicatie gestopt.')
            forecast_size=await forecast.evaluate('el=>({w:Math.round(el.getBoundingClientRect().width),h:Math.round(el.getBoundingClientRect().height)})')
            if forecast_size['w']<400 or forecast_size['h']<250:
                raise RuntimeError('Randstad overzicht te klein; publicatie gestopt: '+str(forecast_size))
            # WordPress: zelfstandig Randstad-weerbericht BOVEN de tien dagregels.
            # De tekst wordt uitsluitend uit de reeds gecontroleerde regionale
            # dagrijen samengesteld: geen nieuwe API-calls of uurlijkse maxima.
            # V10.4: periodeanalyse met gevalideerde omslagmomenten en natuurlijkere tekst.
            narrative = await forecast.evaluate(r"""el => {
  const rows=[...el.querySelectorAll('.rico5DayRow')].map(node=>{
    const v=k=>node.dataset[k];
    const num=k=>v(k)==null||v(k)===''?null:Number(v(k));
    const description=(node.innerText||'').split('\n').map(x=>x.trim()).find(x=>/onweer|sneeuw|regenachtig|regen|buien|bui|zon|bewolk|grijs|mist|droog/i.test(x))||'';
    return {date:v('date'),type:(v('type')||description).toLowerCase(),max:num('max'),min:num('min'),rain:num('rain'),sun:num('sun')};
  });
  if(rows.length!==10||rows.some(r=>!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(r.date)||!Number.isFinite(r.max)))throw Error('Onvolledige dagwaarden voor weerbericht');
  const day=r=>new Intl.DateTimeFormat('nl-NL',{weekday:'long',timeZone:'UTC'}).format(new Date(r.date+'T12:00:00Z'));
  const cap=s=>s.charAt(0).toUpperCase()+s.slice(1);
  const round=n=>Math.round(n);
  const hasRain=r=>Number.isFinite(r.rain);
  const hasSun=r=>Number.isFinite(r.sun);
  const kind=r=>{
    const s=r.type;
    if(/onweer/.test(s))return 'onweer';
    if(/sneeuw/.test(s))return 'sneeuw';
    if(/regenachtig|regen/.test(s))return 'regen';
    if(/bui/.test(s))return 'buien';
    if(/zonnig|veel zon/.test(s))return 'zon';
    if(/zon/.test(s))return 'zonwolken';
    if(/bewolk|grijs/.test(s))return 'bewolkt';
    return 'onbekend';
  };
  // Neerslaghoeveelheid en dagsymbool samen beoordelen. Een bui-icoon met
  // 0,6 mm is geen langdurig natte dag; 9 mm regen evenmin een droge dag.
  const wetScore=r=>{
    if(hasRain(r)){
      if(r.rain>=8)return 3;
      if(r.rain>=3)return 2;
      if(r.rain>=1)return 1;
      return 0;
    }
    return kind(r)==='regen'?2:['buien','onweer','sneeuw'].includes(kind(r))?1:0;
  };
  const dry=r=>wetScore(r)===0;
  const veryWet=r=>wetScore(r)>=2;
  const brighter=r=>hasSun(r)?r.sun>=3:['zon','zonwolken'].includes(kind(r));
  const sunny=r=>hasSun(r)?r.sun>=5:kind(r)==='zon';
  const tempRange=arr=>{const v=arr.map(r=>round(r.max));const lo=Math.min(...v),hi=Math.max(...v);return lo===hi?`${lo} graden`:hi-lo===1?`${lo} of ${hi} graden`:`tussen ${lo} en ${hi} graden`;};
  const today=rows[0],tomorrow=rows[1];
  // Geen opgeklopte zekerheid: eerste dagen feitelijk, dag 6–10 voorzichtiger.
  const early=rows.slice(0,5);
  const earlyDry=early.filter(r=>wetScore(r)===0).length;
  const earlyBright=early.filter(brighter).length;
  const earlyMeanMax=early.reduce((s,r)=>s+r.max,0)/early.length;
  const earlyCold=earlyMeanMax<=6;
  const earlySunCold=earlyDry>=3&&earlyBright>=3&&earlyCold;

  const clause=r=>{
    if(veryWet(r))return 'staat geregeld regen op het programma';
    if(kind(r)==='onweer')return 'komen buien voor, mogelijk met onweer';
    if(kind(r)==='sneeuw')return 'is er kans op sneeuw';
    if(wetScore(r)===1)return sunny(r)?'wisselen zonnige perioden en enkele buien elkaar af':'trekken enkele buien over de regio';
    if(sunny(r))return 'krijgt de zon volop ruimte';
    if(brighter(r))return 'is er regelmatig ruimte voor de zon';
    if(kind(r)==='bewolkt')return 'heeft de bewolking de overhand';
    return 'blijft het op de meeste plaatsen droog';
  };
  const maxText=r=>`${round(r.max)} ${round(r.max)===1?'graad':'graden'}`;
  // Verschillende zinsconstructies voor opeenvolgende dagen, zonder
  // afwijkende meteorologische claims of de modelmix te veranderen.
  const firstWeather=r=>{
    if(veryWet(r))return 'valt geregeld regen';
    if(kind(r)==='sneeuw')return 'kan sneeuw vallen';
    if(kind(r)==='onweer')return 'komen buien voor, mogelijk met onweer';
    if(kind(r)==='buien' && Number.isFinite(r.rain) && r.rain>=.2)
      return sunny(r)?'wisselen zon en enkele buien elkaar af':'komen verspreid buien voor';
    if(wetScore(r)===1)return sunny(r)?'is het wisselend zonnig met enkele buien':'trekken enkele buien over de regio';
    if(sunny(r))return 'is er volop ruimte voor de zon';
    if(brighter(r))return 'breekt de zon af en toe door';
    if(kind(r)==='bewolkt')return 'overheerst de bewolking';
    return 'blijft het overwegend droog';
  };
  const secondWeather=(r,previous)=>{
    if(veryWet(r))return 'Regelmatig valt er regen';
    if(kind(r)==='sneeuw')return 'Er is kans op sneeuw';
    if(kind(r)==='onweer')return 'Enkele buien kunnen vergezeld gaan van onweer';
    if(kind(r)==='buien' && Number.isFinite(r.rain) && r.rain>=.2)
      return sunny(r)?'Naast zonnige perioden is een bui mogelijk':'Verspreid trekken er enkele buien over';
    if(wetScore(r)===1)return 'Er komen enkele buien voor';
    if(sunny(r))return sunny(previous)?'De zon blijft een hoofdrol spelen':'De zon krijgt dan ruim baan';
    if(brighter(r))return sunny(previous)?'De zon is wat minder vaak te zien':'Af en toe breekt de zon door';
    if(kind(r)==='bewolkt')return 'Wolkenvelden bepalen grotendeels het weerbeeld';
    return 'Het blijft overwegend droog';
  };
  const todayText=`${cap(day(today))} ${firstWeather(today)}. De temperatuur stijgt tot ${maxText(today)}.`;
  const delta=round(tomorrow.max)-round(today.max);
  const trend=delta<=-3?'duidelijk kouder':delta<=-1?'wat kouder':
              delta>=3?'duidelijk zachter':delta>=1?'iets zachter':'';
  const comparison=trend?`, ${trend} dan ${day(today)}`:'';
  const tomorrowText=sunny(today)&&sunny(tomorrow)&&wetScore(tomorrow)===0?
    `Ook ${day(tomorrow)} is het zonnig. Het wordt ${maxText(tomorrow)}${comparison}.`:
    `${cap(day(tomorrow))}: ${secondWeather(tomorrow,today)}. Het wordt ${maxText(tomorrow)}${comparison}.`;
  // Dag 1-2 worden compact beschreven, met een feitelijke vergelijking.
  const p1=`${todayText} ${tomorrowText}`;
  const mid=rows.slice(2,5),later=rows.slice(5);
  const firstAvg=rows.slice(0,5).reduce((n,r)=>n+r.max,0)/5;
  const laterAvg=later.reduce((n,r)=>n+r.max,0)/5;
  const tempDiff=laterAvg-firstAvg;
  const coldestIndex=rows.slice(0,5).reduce((best,r,i,arr)=>r.max<arr[best].max?i:best,0);
  const warmestIndex=rows.slice(0,5).reduce((best,r,i,arr)=>r.max>arr[best].max?i:best,0);
  const earlyMin=rows.slice(0,5).filter(r=>Number.isFinite(r.min));
  const coldestNight=earlyMin.length?Math.min(...earlyMin.map(r=>r.min)):null;
  const frostEarly=earlyMin.filter(r=>r.min<0).length;
  const dryStart=rows.slice(0,5).filter(dry).length>=3;
  const change=rows.findIndex((r,i)=>i>=2&&i<=6&&wetScore(r)>0&&rows.slice(0,i).filter(dry).length>=Math.min(3,i));
  const rainChange=change>=2?change:-1;
  let p2='';
  // Dag 3-5: eerst de kenmerkende dag en daarna een echte weersomslag.
  if(coldestIndex>=2&&coldestIndex<=4&&rows[coldestIndex].max<=rows[0].max-1){
    const c=rows[coldestIndex];
    p2=`${cap(day(c))} wordt de koudste dag, met een maximum van ${maxText(c)}.`;
    if(c.max<=0)p2+=' Plaatselijk is een ijsdag mogelijk.';
  }else if(mid.filter(sunny).length>=2&&mid.filter(dry).length>=2){
    p2='Ook daarna blijft de zon vaak schijnen en is het overwegend droog.';
  }else if(mid.filter(veryWet).length>=2){
    p2='In de daaropvolgende dagen valt geregeld regen.';
  }else if(mid.filter(brighter).length>=2){
    p2='Ook daarna zijn er geregeld zonnige perioden.';
  }else{
    p2='De dagen daarna wisselen wolkenvelden en drogere perioden elkaar af.';
  }
  // Nachtvorst is een relevant kenmerk, maar tel nooit dag-6–10-minima bij de
  // eerste dagen op en herhaal niet twee keer dezelfde vorstzin.
  if(coldestNight!==null&&coldestNight<0){
    if(coldestNight<=-5)p2+=` In de koudste nacht is matige vorst mogelijk, rond ${Math.round(coldestNight)} graden.`;
    else if(frostEarly>=2)p2+=` Landinwaarts vriest het in meerdere nachten licht, plaatselijk tot ongeveer ${Math.round(coldestNight)} graden.`;
    else p2+=' In een enkele nacht kan het licht vriezen.';
  }
  const transitionIndex=rainChange>=2&&rainChange<=4?rainChange-2:-1;
  if(rainChange>=2&&rainChange<=4){
    const r=rows[rainChange],prev=rows[rainChange-1];
    const wetter=wetScore(r)>=2?'wordt het natter met perioden van regen':'neemt de kans op buien toe';
    if(rainChange===4||!p2.includes(cap(day(r))))p2+=` Vanaf ${day(r)} ${wetter}.`;
    else p2+=` Daarbij ${wetter}.`;
  }else if(rainChange===5||rainChange===6){
    p2+=` Vanaf ${day(rows[rainChange])} neemt de kans op buien toe.`;
  }
  // Dag 6-10: spreek niet over overwegend droog wanneer er meerdere natte
  // dagen zijn. Benoem zowel neerslagontwikkeling als temperatuurtrend.
  const laterWet=later.filter(r=>wetScore(r)>0).length;
  const laterHeavy=later.filter(veryWet).length;
  const laterBright=later.filter(brighter).length;
  let p3=laterHeavy>=3?'Volgende week zijn er regelmatig perioden met regen.':
    laterWet>=3?'Volgende week wisselen droge perioden en buien elkaar af.':
    laterBright>=3?'Verder vooruit zijn er ook zonnige perioden.':
    'Verder vooruit blijft het op veel momenten droog, al kan een bui voorkomen.';
  const laterMax=later.map(r=>Math.round(r.max));
  const laterLo=Math.min(...laterMax),laterHi=Math.max(...laterMax);
  const lateRange=laterHi===laterLo?`${laterLo} graden`:
    laterHi-laterLo===1?`${laterLo} of ${laterHi} graden`:`${laterLo} tot ${laterHi} graden`;
  if(tempDiff>=2){
    p3+=` Het wordt geleidelijk zachter, met maxima van ${lateRange}.`;
  }else if(tempDiff<=-2){
    p3+=` Het wordt geleidelijk kouder, met maxima van ${lateRange}.`;
  }else{
    p3+=` De maxima liggen tussen ${laterLo} en ${laterHi} graden.`;
  }
  p3+=' De verwachting voor deze termijn is nog onzeker.';
  const laterFrost=later.filter(r=>Number.isFinite(r.min)&&r.min<0).length;
  if(laterFrost>=2&&frostEarly===0)p3+=' Ook nachtvorst is mogelijk.';
  const weatherLater=p3;
  // De overbodige zonnigste/natste-dagregels maken plaats voor drie Randstadpluimen.
  el.querySelector('.rico5DaySummary')?.remove();
  el.querySelector('.rico5DayHighlights')?.remove();
  for(const node of el.querySelectorAll('div,p,small,span')){
    if(node.children.length===0&&/Regionale dagmodelmix|Dag- en uurwaarden blijven gescheiden/i.test(node.textContent||'')){
      const scan=el.dataset.scanSavedAt;
      let stamp='';if(scan){const d=new Date(Number(scan));if(!isNaN(d))stamp=new Intl.DateTimeFormat('nl-NL',{timeZone:'Europe/Amsterdam',day:'numeric',month:'long',hour:'2-digit',minute:'2-digit'}).format(d);}
      if(!stamp){const m=(node.textContent||'').match(/scan\s+([^·]+)/i);if(m)stamp=m[1].trim();}
      node.textContent=`Laatste update: ${stamp||'actuele modelscan'} · Kaart © OpenStreetMap-bijdragers`;
    }
  }
  const texts=[p1,p2,p3];
  if(texts.some(x=>/undefined|NaN|null|  |\.\./.test(x)))throw Error('Tekstcontrole: ongeldige waarde');
  // Verifieer de periodeclaims tegen de dagwaarden voordat er gepubliceerd wordt.
  if(transitionIndex>=0){const i=transitionIndex+2;if(wetScore(rows[i])===0)throw Error('Tekstcontrole: onjuiste neerslagomslag');}
  if(mid.filter(veryWet).length>=2&&rainChange<0&&!p2.includes('regen'))throw Error('Tekstcontrole: natte periode ontbreekt');
  const wordCount=texts.join(' ').trim().split(/\s+/).length;
  if(wordCount<35||wordCount>145)throw Error('Tekstcontrole: lengte '+wordCount);
  const box=document.createElement('section');box.id='ricoExportRandstadNarrative';box.setAttribute('aria-label','Automatisch weerbericht Randstad');
  box.style.cssText='box-sizing:border-box;margin:9px 8px 12px;padding:14px 15px;background:#e6f2fa;border:1px solid #bfdbe9;border-radius:12px;color:#17445f;font:15px/1.52 Arial,sans-serif;overflow-wrap:break-word;';
  const headline=earlySunCold&&rainChange>=0&&tempDiff>=2?'Eerst zonnig en koud, later wisselvalliger en zachter':
    earlySunCold&&rainChange>=0?'Eerst zonnig en koud, later meer kans op buien':
    earlySunCold?'Eerst zonnig en koud, later onzekerder':
    veryWet(today)&&veryWet(tomorrow)?(mid.some(brighter)||later.some(brighter)?'Nat begin, later meer ruimte voor de zon':'Regen zet de toon in de Randstad'):
    sunny(today)&&sunny(tomorrow)?'Eerst zonnig, later neemt de onzekerheid toe':
    tempDiff<=-3?'Kouder weer op komst, later meer onzekerheid':
    tempDiff>=3?'Later mogelijk zachter, maar nog onzeker':
    mid.filter(r=>wetScore(r)<=1&&brighter(r)).length>=2?'Geregeld zon, verder vooruit onzekerder':'Wolken, zon en af en toe een bui';
  const heading=document.createElement('div');heading.textContent=headline;heading.style.cssText='font-size:19px;line-height:1.25;font-weight:800;margin-bottom:9px;color:#07527a';box.appendChild(heading);
  for(const str of texts){const p=document.createElement('p');p.textContent=str;p.style.cssText='margin:0 0 9px';box.appendChild(p);}
  box.lastElementChild.style.marginBottom='0';
  const firstRow=el.querySelector('.rico5DayRow');firstRow.parentNode.insertBefore(box,firstRow);
  return {text:texts.join(' '),words:wordCount,firstType:today.type,firstCategory:kind(today)};
}""")
            # EC46: verse GitHub-browser heeft meestal geen localStorage-cache.
            # Gebruik de bestaande Ricoweer-functie; die hergebruikt cache indien aanwezig.
            ec46 = await widget_page.evaluate(r"""async () => {
              const result={weeks:[],error:null,sourceAt:null};
              try {
                let pack=null;
                try {pack=JSON.parse(localStorage.getItem('rw_ec46_6week_v1026')||'null')}catch(e){}
                const valid=p=>p?.data?.weekly?.time?.length && Number.isFinite(Number(p.savedAt)) &&
                  Date.now()-Number(p.savedAt)<36*3600000 && Number(p.savedAt)<=Date.now()+60000;
                if(!valid(pack)) {
                  if(typeof lr6Fetch!=='function')throw Error('EC46-ophaalfunctie ontbreekt');
                  pack=await lr6Fetch(false);
                }
                if(!pack?.data?.weekly?.time?.length)throw Error('Geen EC46-weekgegevens');
                result.sourceAt=Number.isFinite(Number(pack.savedAt))?Number(pack.savedAt):null;
                // Neem exact de EC46-maximaspreiding uit de bestaande app over.
                // De app combineert KNMI TX-klimaat 1996-2025 met EC46-afwijking,
                // of gebruikt dagelijkse EC46-maxima als terugval.
                if(typeof lr6BuildWeeks!=='function' || typeof lr6DisplayWeeks!=='function')
                  throw Error('EC46-spreidingsfunctie ontbreekt in bronpagina');
                let climate=[];
                if(typeof loadClimateDaily==='function') {
                  try { climate=await loadClimateDaily(false) || []; } catch(e) {
                    result.climateError=String(e?.message||e);
                  }
                }
                const all=lr6BuildWeeks(pack.data,climate);
                const future=lr6DisplayWeeks(all).filter(x=>!x.partial);
                const wetValue=x=>{
                  const i=x.i,raw=pack.data.weekly?.precipitation_anomaly_gt0?.[i];
                  if(raw===null||raw===undefined||raw==='')return null;
                  const v=Number(raw);return Number.isFinite(v)?(v<=1?v*100:v):null;
                };
                result.weeks=future.slice(1,6).map((x,i)=>({
                  week:i+2,
                  start:x.start,end:x.end,
                  anomaly:Number.isFinite(x.tA)?x.tA:null,
                  txMin:Number.isFinite(x.txMin)?x.txMin:null,
                  txMax:Number.isFinite(x.txMax)?x.txMax:null,
                  wet:wetValue(x)
                }));
                if(!result.weeks.length)result.error='Geen bruikbare EC46-weken 2–6';
              }catch(e){result.error=String(e?.message||e)}
              return result;
            }""")
            # Presentatie uitsluitend in de WordPress-export: geen aanpassing aan
            # bronwidget, regionale dagmix of modelvalidatie.
            presentation = await forecast.evaluate(r"""el => {
              // Verwijder de lange tussenzin dag 6–10 uit de compacte PNG.
              el.querySelectorAll('.rico5DayDivider').forEach(node => node.remove());
              const css=document.createElement('style');
              css.textContent=`
                #ricoWidgetFacts .rico5DayFeels{display:block;color:#33617b;
                  font-size:11px;font-weight:750;line-height:1.35;margin-top:2px}
                #ricoWidgetFacts #ricoExportEC46{margin:14px 9px 10px!important;
                  padding:14px!important;border-radius:14px!important;
                  background:#f0f6ff!important;border:1px solid #cbdfee!important}
                #ricoWidgetFacts .ricoEC46Week{display:grid;grid-template-columns:72px minmax(0,1fr);
                  align-items:center;gap:9px;padding:9px 1px!important;
                  border-bottom:1px solid #d8e6f4!important;color:#204b68}
                #ricoWidgetFacts .ricoEC46Week:nth-of-type(even){background:#e8f1fc88}
                #ricoWidgetFacts .ricoEC46Badge{display:inline-block;padding:5px 6px;
                  border-radius:8px;background:#d8eafb;color:#075277;font-weight:850;text-align:center}
                #ricoWidgetFacts .ricoEC46Main{display:block;font-size:13px;font-weight:750}
                #ricoWidgetFacts .ricoEC46Signal{display:flex;flex-wrap:wrap;gap:5px;
                  margin-top:5px;font-size:11px;color:#536d80}
                #ricoWidgetFacts .ricoEC46Week{grid-template-columns:74px minmax(0,1fr);
                  border:1px solid #d5e4f0!important;border-radius:11px;
                  padding:9px!important;margin:5px 0;background:#ffffffc7!important}
                #ricoWidgetFacts .ricoEC46Week:nth-of-type(even){background:#edf5ff!important}
                #ricoWidgetFacts .ricoEC46Main{font-size:13px!important}
                #ricoWidgetFacts .ricoEC46Track{height:8px;border-radius:20px;
                  margin-top:8px;background:#dce9f3;position:relative;overflow:hidden}
                #ricoWidgetFacts .ricoEC46Fill{height:100%;border-radius:20px;
                  background:linear-gradient(90deg,#248dc4,#f0b34c)}
                #ricoWidgetFacts .ricoEC46Pill{border-radius:12px;padding:2px 7px;
                  font-weight:700;background:#e5edf6;color:#315a73}
              `;
              el.appendChild(css);
              // Canadese windchillformule: T in graden Celsius, wind in km/u.
              // Regionale dagmaximum- en dagwindwaarden zijn indicatief; geen uurmix.
              const windBft=kmh=>kmh<1?0:kmh<6?1:kmh<12?2:kmh<20?3:
                kmh<29?4:kmh<39?5:kmh<50?6:kmh<62?7:kmh<75?8:kmh<89?9:
                kmh<103?10:kmh<118?11:12;
              // Kleine modelspreiding is bij beperkte modeldekking GEEN hoge zekerheid.
              // Gebruik de echte regionale modelaantallen uit de scan indien beschikbaar.
              const cacheKey='rico-national-scan-v75-35punten';
              let pack=null;try{pack=JSON.parse(localStorage.getItem(cacheKey)||'null')}catch(e){}
              let corrected=0;
              const spread=values=>values.length>=2?Math.max(...values)-Math.min(...values):null;
              const dailyRows=[...el.querySelectorAll('.rico5DayRow')];
              for(const [lead,row] of dailyRows.entries()){
                const status=row.querySelector('.rico5DayConfidence');
                if(!status)continue;
                const date=row.dataset.date;
                // regionalModelData is scoped to the widget IIFE and therefore not
                // callable from page.evaluate. Recalculate the SAME independent
                // regional model medians using the validated cached daily scan.
                let models=[];
                const day=pack?.days?.find(d=>d.date===date);
                const groups=new Map();
                for(const p of (day?.pts||[])){
                  if(!['Randstad-kust','Randstad-west','Randstad-oost'].includes(p?.point?.r))continue;
                  for(const value of (p.modelVals||[])){
                    if(!value?.model)continue;
                    if(!groups.has(value.model))groups.set(value.model,[]);
                    groups.get(value.model).push(value);
                  }
                }
                const median=a=>{
                  const nums=a.filter(Number.isFinite).sort((a,b)=>a-b);
                  return nums.length?nums.length%2?nums[Math.floor(nums.length/2)]:
                    (nums[nums.length/2-1]+nums[nums.length/2])/2:null;
                };
                models=[...groups].map(([model,vals])=>({model,
                    tx:median(vals.map(v=>v.tx)), rain:median(vals.map(v=>v.rain)),
                    wind:median(vals.map(v=>v.wind)),gust:median(vals.map(v=>v.gust))}));
                const measure=field=>models.map(m=>m[field]).filter(Number.isFinite);
                const tx=measure('tx'),rain=measure('rain'),wind=measure('wind'),gust=measure('gust');
                const n=tx.length,ts=spread(tx),rs=spread(rain),ws=spread(wind),gs=spread(gust);
                if(n<2 || rain.length<2 || wind.length<2){
                  status.textContent='🎯 Modelovereenstemming niet te bepalen · '+n+' temperatuurmodellen beschikbaar';
                  status.dataset.level='redelijk';
                  corrected++;continue;
                }
                const reasons=[];
                if(ts>3.5)reasons.push('temperatuur');
                if(rs>4)reasons.push('neerslag');
                if(ws>18)reasons.push('wind');
                if(gs!==null&&gs>25&&!reasons.includes('wind'))reasons.push('windstoten');
                // Overeenstemming is de modelspreiding; beschikbaarheid is een
                // apart gegeven. Een klein aantal modellen leidt niet vanzelf
                // tot 'lage overeenstemming' of 'hoge zekerheid'.
                const agreementLow=ts>5||rs>12||ws>30||(lead>=7&&ts>3.5);
                const agreementGood=ts<=2.5&&rs<=4&&ws<=18;
                const label=agreementLow?'laag':agreementGood?'goed':'redelijk';
                const reason=reasons.length?' · spreiding vooral in '+reasons.slice(0,2).join(' en '):'';
                status.textContent='🎯 Modelovereenstemming '+label+' · '+n+' modellen beschikbaar'+reason;
                status.dataset.level=agreementLow?'laag':agreementGood?'hoog':'redelijk';
                status.title=n+' temperatuurmodellen, '+rain.length+' neerslagmodellen, '+wind.length+
                  ' windmodellen. Ensemblebeschikbaarheid staat los van deze deterministische modelvergelijking.';
                corrected++;
              }
              let count=0;
              for(const row of el.querySelectorAll('.rico5DayRow')){
                const t=Number(row.dataset.max),v=Number(row.dataset.wind);
                if(!Number.isFinite(t)||!Number.isFinite(v)||t>=5||windBft(v)<3)continue;
                const chill=13.12+0.6215*t-11.37*Math.pow(v,.16)+0.3965*t*Math.pow(v,.16);
                if(!Number.isFinite(chill))continue;
                const target=row.querySelector('.rico5DayDescription');
                if(!target)continue;
                const line=document.createElement('small');
                line.className='rico5DayFeels';
                line.textContent='🌬️ Voelt als '+Math.round(chill)+'°';
                target.appendChild(line);count++;
              }
              return {feelsDays:count,confidenceCorrected:corrected,dividerRemoved:true};
            }""")
            print('WordPress dagpresentatie:',presentation,flush=True)
            print('EC46 diagnose:',json.dumps(ec46,ensure_ascii=False),flush=True)
            ec46['previous'] = previous_ec46 if previous_ec46 and (
                previous_ec46.get('sourceAt') != ec46.get('sourceAt')) else None
            ec46_display = await forecast.evaluate(r"""(el,pack) => {
              const box=document.createElement('section');box.id='ricoExportEC46';
              box.style.cssText='box-sizing:border-box;margin:12px 8px;padding:14px;background:#f0f6ff;border:1px solid #c9d8ec;border-radius:14px;color:#17445f;font:14px/1.45 Arial,sans-serif';
              const h=document.createElement('div');h.textContent='📅 EC46 · vooruitblik week 2–6';
              h.style.cssText='font-weight:850;font-size:18px;margin-bottom:5px;color:#075277';box.appendChild(h);
              const sub=document.createElement('p');sub.textContent='Langetermijntrend · temperatuurbanden per week, geen dagverwachting';
              sub.style.cssText='margin:0 0 12px;color:#536d80;font-size:11px';box.appendChild(sub);
              const lines=[];
              const validTrends=(pack.weeks||[]).filter(w=>Number.isFinite(w.anomaly));
              if(validTrends.length>=2){
                const classify=w=>w.anomaly>=.4?'zachter':w.anomaly<=-.4?'kouder':'rond normaal';
                const first=classify(validTrends[0]),last=classify(validTrends[validTrends.length-1]);
                let summary='';
                if(first==='kouder'&&last==='zachter')summary='Eerst kouder dan normaal, later een zachter signaal.';
                else if(first==='zachter'&&last==='kouder')summary='Eerst zachter dan normaal, verder vooruit een kouder signaal.';
                else if(validTrends.every(w=>w.anomaly<=-.4))summary='De komende weken overwegend een kouder signaal.';
                else if(validTrends.every(w=>w.anomaly>=.4))summary='De komende weken overwegend een zachter signaal.';
                else if(first===last&&first!=='rond normaal')summary=`Voorlopig vooral ${first} dan normaal, met wisselende sterkte.`;
                else summary='De temperatuurafwijkingen wisselen per week; de trend is niet eenduidig.';
                const brief=document.createElement('div');brief.className='ricoEC46Brief';brief.textContent=summary;
                brief.style.cssText='margin:0 0 11px;padding:9px 11px;background:#e1f0fb;border-left:4px solid #2587bd;border-radius:8px;font-size:13px;font-weight:750;color:#1a5276';
                box.appendChild(brief);
              }
              const fmt=d=>new Intl.DateTimeFormat('nl-NL',{day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(d+'T12:00:00Z'));
              if(pack.weeks?.length){
                const valid=pack.weeks.filter(w=>Number.isFinite(w.txMin)&&Number.isFinite(w.txMax));
                const axisLo=valid.length?Math.floor(Math.min(...valid.map(w=>Math.min(w.txMin,w.txMax)))-2):0;
                const axisHi=valid.length?Math.ceil(Math.max(...valid.map(w=>Math.max(w.txMin,w.txMax)))+2):15;
                const span=Math.max(4,axisHi-axisLo);
                for(const w of pack.weeks){
                  const delta=Number.isFinite(w.anomaly)?w.anomaly:null;
                  const hasMax=Number.isFinite(w.txMin)&&Number.isFinite(w.txMax);
                  const low=hasMax?Math.floor(Math.min(w.txMin,w.txMax)):null;
                  const high=hasMax?Math.ceil(Math.max(w.txMin,w.txMax)):null;
                  const actual=hasMax?(low===high?`maxima rond ${low} °C`:`maxima ${low}–${high} °C`):'maxima niet beschikbaar';
                  const trend=delta===null?'':delta>=.4?'Warmer dan normaal':delta<=-.4?'Kouder dan normaal':'Rond normaal';
                  const rain=w.wet===null?'':w.wet>=58?'Natter signaal':w.wet<=42?'Droger signaal':'';
                  lines.push(`Week ${w.week} · ${fmt(w.start)}–${fmt(w.end)}: ${actual}${rain?' · '+rain.toLowerCase():''}`);
                  const row=document.createElement('div');row.className='ricoEC46Week';
                  const badge=document.createElement('span');badge.className='ricoEC46Badge';badge.textContent='Week '+w.week;
                  const right=document.createElement('div');
                  const main=document.createElement('div');main.className='ricoEC46Main';main.textContent=`${fmt(w.start)}–${fmt(w.end)} · ${actual}`;
                  const track=document.createElement('div');track.className='ricoEC46Track';
                  track.setAttribute('aria-label',hasMax?`Temperatuurband ${low} tot ${high} graden`:'Geen temperatuurband beschikbaar');
                  if(hasMax){
                    const fill=document.createElement('div');fill.className='ricoEC46Fill';
                    fill.style.marginLeft=Math.max(0,100*(low-axisLo)/span).toFixed(1)+'%';
                    fill.style.width=Math.max(2,Math.min(100,100*(high-low)/span)).toFixed(1)+'%';track.appendChild(fill);
                  }
                  const detail=document.createElement('div');detail.className='ricoEC46Signal';
                  const previous=pack.previous?.weeks?.find(p=>p.week===w.week && p.start===w.start);
                  if(previous && Number.isFinite(previous.anomaly) && Number.isFinite(delta)){
                    const diff=delta-previous.anomaly;
                    if(Math.abs(diff)>=0.5){
                      const badge=document.createElement('span');badge.className='ricoEC46Pill';
                      badge.textContent=(diff>0?'↗ Warmer':'↘ Kouder')+' dan vorige publicatie ('+
                        Math.abs(diff).toFixed(1).replace('.',',')+'° afwijkingsverschil)';
                      detail.appendChild(badge);
                    }
                  }
                  for(const message of [trend,rain].filter(Boolean)){
                    const pill=document.createElement('span');pill.className='ricoEC46Pill';pill.textContent=message;
                    detail.appendChild(pill);
                  }
                  right.append(main,track,detail);row.append(badge,right);box.appendChild(row);
                }
              }else{
                lines.push('EC46-vooruitblik tijdelijk niet beschikbaar.');
                const row=document.createElement('div');row.textContent=lines[0];row.style.cssText='padding:9px 0';box.appendChild(row);
              }
              const note=document.createElement('div');note.textContent='EC46 is een indicatieve, onzekere weektrend; de bandbreedte is geen verwachting voor elke dag.';
              note.style.cssText='font-size:11px;color:#526b83;margin-top:10px';box.appendChild(note);
              el.appendChild(box);return {lines,note:note.textContent};
            }""",ec46)
            quality_text=await forecast.evaluate("""el=>{
              const rows=[...el.querySelectorAll('.rico5DayRow')].map(n=>({
                date:n.dataset.date,type:(n.dataset.type||'').toLowerCase(),
                max:Number(n.dataset.max),min:Number(n.dataset.min),
                rain:Number(n.dataset.rain),sun:Number(n.dataset.sun),
                confidence:(n.querySelector('.rico5DayConfidence')?.textContent||'')}));
              const issues=[];
              if(rows.length!==10)issues.push('Niet precies 10 dagregels');
              for(let i=0;i<rows.length;i++){
                const r=rows[i];
                if(!Number.isFinite(r.max)||!Number.isFinite(r.min)||r.max<r.min-1)
                  issues.push(r.date+': minimum/maximum ongeldig');
                if(!Number.isFinite(r.rain)||r.rain<0||!Number.isFinite(r.sun)||r.sun<0||r.sun>24)
                  issues.push(r.date+': zon/neerslag ongeldig');
                if(/zonnig|veel zon/.test(r.type)&&r.sun<1)
                  issues.push(r.date+': zonnig terwijl minder dan 1 uur zon');
                if(/droog/.test(r.type)&&r.rain>=8)
                  issues.push(r.date+': droog versus veel regen');
                if(/zekerheid hoog/i.test(r.confidence)&&/weinig|beperkt|onvoldoende/i.test(r.confidence))
                  issues.push(r.date+': tegenstrijdige modelzekerheid');
                if(i && Date.parse(r.date+'T12:00:00Z')-Date.parse(rows[i-1].date+'T12:00:00Z')!==86400000)
                  issues.push(r.date+': geen opeenvolgende dag');
              }
              return {issues,checked:rows.length};
            }""")
            print('Redactionele dagcontrole:',quality_text,flush=True)
            if quality_text['issues']:
                raise RuntimeError('Dagwaarden en weersymbolen spreken elkaar tegen: '+
                    '; '.join(quality_text['issues']))
            print('EC46 opmaak:',json.dumps(ec46_display,ensure_ascii=False),flush=True)
            if ec46.get('weeks') and ec46.get('sourceAt'):
                (OUT/'ec46-snapshot.json').write_text(json.dumps({
                    'sourceAt':ec46['sourceAt'],'weeks':ec46['weeks']},ensure_ascii=False),encoding='utf-8')
            # Optionele, werkelijk regionale ECMWF/GEFS-pluimen. Bronstoring mag
            # de dagverwachting of de succesvolle kaartenexport nooit blokkeren.
            plume_image_url = None
            try:
                plume_info = await forecast.evaluate(r"""async (root) => {
  const rows = [...root.querySelectorAll('.rico5DayRow')];
  const dates = rows.map(r => r.dataset.date);
  const winter = [10,11,12,1,2,3].includes(Number(dates[0]?.slice(5,7)));
  // Three representatives for existing Randstad subregions, not a claim of full areal coverage.
  const places = [
    {name:'Randstad-kust',lat:51.98,lon:4.13},
    {name:'Randstad-west',lat:52.02,lon:4.71},
    {name:'Utrecht/Flevoland',lat:52.10,lon:5.18}
  ];
  const models = ['ecmwf_ifs025_ensemble','ncep_gefs025'];
  const fetchedAt=Date.now();
  const vars = ['temperature_2m','precipitation','snowfall'];
  const fetchModel = async model => {
    const q = new URLSearchParams({
      latitude:places.map(p=>p.lat).join(','),longitude:places.map(p=>p.lon).join(','),
      hourly:vars.join(','),models:model,timezone:'Europe/Amsterdam',
      forecast_days:'12',precipitation_unit:'mm'
    });
    const ctl = new AbortController();
    const timer = setTimeout(()=>ctl.abort(),10000);
    try {
      const response=await fetch('https://ensemble-api.open-meteo.com/v1/ensemble?'+q.toString(),
        {signal:ctl.signal,cache:'no-store'});
      if(!response.ok)throw new Error('HTTP '+response.status);
      const body=await response.json();
      const sites=Array.isArray(body)?body:[body];
      if(sites.length!==places.length)throw new Error('onvolledige regionale respons');
      const keys=Object.keys(sites[0]?.hourly||{});
      const memberSuffixes=keys.filter(k=>/^temperature_2m_member\d+$/.test(k))
         .map(k=>k.slice('temperature_2m_'.length));
      if(memberSuffixes.length<5)throw new Error('onvoldoende ensembleleden');
      const members={};
      for(const variable of vars) {
        members[variable]=memberSuffixes.filter(s=>sites.every(site=>
          Array.isArray(site.hourly?.[variable+'_'+s])));
      }
      const data={model,days:{}};
      for(const date of dates){
        data.days[date]={};
        const samples=sites.map(site=>(site.hourly.time||[])
          .map((t,i)=>String(t).slice(0,10)===date?i:-1).filter(i=>i>=0));
        for(const [variable,type] of [['temperature_2m','tx'],['temperature_2m','tn'],
                     ['precipitation','rain'],['snowfall','snow']]){
          const values=[];
          for(const suffix of members[variable]){
            const regional=[];
            for(let j=0;j<sites.length;j++) {
              const src=sites[j].hourly[variable+'_'+suffix];
              const nums=samples[j].map(i=>src[i]).filter(Number.isFinite);
              if(nums.length<(type==='tx'?20:12))continue;
              const v=type==='tx'?Math.max(...nums):type==='tn'?Math.min(...nums):
                 nums.reduce((a,b)=>a+b,0);
              regional.push(v);
            }
            if(regional.length<2)continue;
            regional.sort((a,b)=>a-b);
            values.push(regional[Math.floor(regional.length/2)]);
          }
          data.days[date][type]=values;
        }
      }
      return data;
    }finally{clearTimeout(timer)}
  };
  // Snow depth is a *state* (metres on the ground), not the daily snowfall total.
  // Fetch separately: a model lacking snow_depth must never disable other plumes.
  const fetchSnowDepth=async model=>{
    const q=new URLSearchParams({
      latitude:places.map(p=>p.lat).join(','),longitude:places.map(p=>p.lon).join(','),
      hourly:'snow_depth',models:model,timezone:'Europe/Amsterdam',forecast_days:'12'
    });
    const ctl=new AbortController();const timer=setTimeout(()=>ctl.abort(),10000);
    try{
      const response=await fetch('https://ensemble-api.open-meteo.com/v1/ensemble?'+q.toString(),
        {signal:ctl.signal,cache:'no-store'});
      if(!response.ok)throw new Error('HTTP '+response.status);
      const body=await response.json(),sites=Array.isArray(body)?body:[body];
      if(sites.length!==places.length)throw new Error('onvolledige sneeuwdekrespons');
      const suffixes=Object.keys(sites[0].hourly||{}).filter(k=>/^snow_depth_member\d+$/.test(k))
        .map(k=>k.slice('snow_depth_'.length))
        .filter(s=>sites.every(site=>Array.isArray(site.hourly?.['snow_depth_'+s])));
      if(suffixes.length<5)throw new Error('onvoldoende sneeuwdek-ensembleleden');
      const days={};
      for(const date of dates){
        // Snapshot at 08:00 Europe/Amsterdam: a comparable morning snow cover,
        // not a sum of snowfalls or the largest depth at any arbitrary hour.
        const indices=sites.map(site=>(site.hourly.time||[]).findIndex(t=>
          String(t).startsWith(date+'T08:')));
        const values=[];
        for(const s of suffixes){
          const region=[];
          for(let j=0;j<sites.length;j++){
            if(indices[j]<0)continue;
            const v=sites[j].hourly['snow_depth_'+s][indices[j]];
            if(Number.isFinite(v)&&v>=0)region.push(v*100); // metres -> cm
          }
          if(region.length<2)continue;
          region.sort((a,b)=>a-b);
          values.push(region[Math.floor(region.length/2)]);
        }
        days[date]={snowDepth:values};
      }
      return {model,days};
    }finally{clearTimeout(timer)}
  };
  const results=await Promise.all(models.map(async m=>{
    const [base,depth]=await Promise.all([
      fetchModel(m).catch(err=>{console.warn('Randstad pluimbron niet bereikbaar',m,String(err));return null}),
      fetchSnowDepth(m).catch(err=>{console.warn('Sneeuwdekbron niet beschikbaar',m,String(err));return null})
    ]);
    if(base&&depth){for(const date of dates)base.days[date].snowDepth=depth.days[date]?.snowDepth||[];}
    return base;
  }));
  const available=results.filter(Boolean);
  const percentage=(variable,condition,date)=>{
    const probs=available.map(m=>{
      const arr=m.days[date]?.[variable]||[];
      return arr.length>=5?100*arr.filter(condition).length/arr.length:null;
    }).filter(Number.isFinite);
    return probs.length?probs.reduce((a,b)=>a+b,0)/probs.length:null;
  };
  const quantile=(a,q)=>{if(!a.length)return null;const b=[...a].sort((x,y)=>x-y),i=(b.length-1)*q,l=Math.floor(i);return b[l]+(b[Math.min(l+1,b.length-1)]-b[l])*(i-l)};
  const tempStats=(date,field)=>{
    const stats=available.map(m=>{
      const arr=m.days[date]?.[field]||[];
      return arr.length>=5?[quantile(arr,.2),quantile(arr,.5),quantile(arr,.8)]:null;
    }).filter(Boolean);
    return stats.length?stats[0].map((_,i)=>stats.reduce((s,a)=>s+a[i],0)/stats.length):null;
  };
  // All plots reuse the already downloaded member series; no extra API calls.
  const charts=winter?[
    {title:'❄️ Vorst- en ijsdagkans',detail:'Vorst: minimum < 0 °C · IJsdag: maximum < 0 °C',field:'tn',test:x=>x<0,otherField:'tx',otherTest:x=>x<0,kind:'dual-chance',color:'#238bd1',otherColor:'#123d8f'},
    {title:'🌨️ Sneeuwval en sneeuwdek',detail:'Sneeuwval ≥ 0,1 cm per dag · sneeuwdek ≥ 1 cm om 08:00 uur',
      field:'snow',test:x=>x>=.1,otherField:'snowDepth',otherTest:x=>x>=1,
      kind:'dual-chance',color:'#4a9dd4',otherColor:'#153d83',
      labels:['Sneeuwval (≥ 0,1 cm)','Sneeuwdek (≥ 1 cm, 08:00)']},
    {title:'🌡️ Temperatuurpluim',detail:'Dagmaximum en nachtminimum · P20–P80 · schaal in stappen van 5 °C',kind:'temp',color:'#c95746'},
    {title:'🌧️ Neerslagpluim',detail:'Neerslag per dag in mm · mediaan (P50) en spreiding P20–P80',kind:'rain-plume',color:'#267acb'},
    {title:'🌧️ Neerslagkans',detail:'Kans op minstens 1, 3 of 10 mm per dag · aandeel ensembleleden',kind:'rain-chance'}
  ]:[
    {title:'🌡️ Temperatuurpluim',detail:'Dagmaximum en nachtminimum · P20–P80 · schaal in stappen van 5 °C',kind:'temp',color:'#c95746'},
    {title:'🌧️ Neerslagpluim',detail:'Neerslag per dag in mm · mediaan (P50) en spreiding P20–P80',kind:'rain-plume',color:'#267acb'},
    {title:'🌧️ Neerslagkans',detail:'Kans op minstens 1, 3 of 10 mm per dag · aandeel ensembleleden',kind:'rain-chance'},
    {title:'☀️ Warmtekans',detail:'Dagmaximum ≥ 20 °C',field:'tx',test:x=>x>=20,kind:'chance',color:'#ef943d'}
  ];
  const ns='http://www.w3.org/2000/svg';
  const make=(name,attrs={},txt)=>{const node=document.createElementNS(ns,name);for(const [k,v] of Object.entries(attrs))node.setAttribute(k,String(v));if(txt!==undefined)node.textContent=txt;return node};
  const section=document.createElement('section'); section.id='ricoExportRandstadPlumes';
  section.style.cssText='box-sizing:border-box;margin:12px 8px;padding:12px;background:#eff7fd;border:1px solid #bcd9ed;border-radius:12px;font:13px/1.45 Arial,sans-serif;color:#173f5b';
  const heading=document.createElement('div');heading.textContent='📈 Randstad · pluimen en kansen (10 dagen)';
  heading.style.cssText='font-weight:800;font-size:18px;margin:0 0 4px';section.appendChild(heading);
  const note=document.createElement('div');
  note.textContent=available.length?'ECMWF ENS'+(available.some(m=>m.model.includes('gefs'))?' + GFS GEFS':'')+
    ' · regionale mediaan · ensemble-aanvraag '+new Intl.DateTimeFormat('nl-NL',{timeZone:'Europe/Amsterdam',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}).format(new Date(fetchedAt)):
    'Ensemblegegevens tijdelijk niet beschikbaar; geen kansen ingevuld.';
  note.style.cssText='font-size:11px;color:#536f83;margin-bottom:10px';section.appendChild(note);
  const short=date=>new Intl.DateTimeFormat('nl-NL',{weekday:'short',day:'numeric',timeZone:'UTC'})
    .format(new Date(date+'T12:00:00Z')).replace('.','');
  for(const chart of charts){
    const card=document.createElement('div');card.style.cssText='background:#fff;border:1px solid #d7e7f2;border-radius:10px;padding:13px 8px;margin:0 0 12px';
    const title=document.createElement('div');title.textContent=chart.title;title.style.cssText='font-weight:800;font-size:14px;margin:0 8px 2px';card.appendChild(title);
    const detail=document.createElement('div');detail.textContent=chart.detail;detail.style.cssText='font-size:11px;margin:0 8px 7px;color:#637b8d';card.appendChild(detail);
    const svg=make('svg',{viewBox:'0 0 520 228',width:'100%',height:'auto',role:'img','aria-label':chart.title});
    const x=i=>54+i*49, top=24,bottom=187;
    let samples;
    if(chart.kind==='temp')samples=dates.map(d=>({max:tempStats(d,'tx'),min:tempStats(d,'tn')}));
    else if(chart.kind==='rain-plume')samples=dates.map(d=>tempStats(d,'rain'));
    else if(chart.kind==='rain-chance')samples=dates.map(d=>[1,3,10].map(limit=>percentage('rain',x=>x>=limit,d)));
    else if(chart.kind==='dual-chance')samples=dates.map(d=>({
      frost:percentage(chart.field,chart.test,d),
      ice:percentage(chart.otherField,chart.otherTest,d)
    }));
    else samples=dates.map(d=>percentage(chart.field,chart.test,d));
    // Compact, verified chart takeaway. Missing members never count as zero.
    const summary=document.createElement('div');
    summary.style.cssText='margin:4px 8px 8px;padding:6px 9px;background:#e8f3fa;border-radius:7px;color:#254b68;font-size:11px;font-weight:700';
    if(chart.kind==='dual-chance'){
      const peaks=(key)=>samples.map((v,i)=>({v:v[key],i})).filter(o=>Number.isFinite(o.v)).sort((a,b)=>b.v-a.v)[0];
      const a=peaks('frost'),b=peaks('ice');
      const main=chart.otherField==='snowDepth'?'Sneeuwval':'Vorst';
      const second=chart.otherField==='snowDepth'?'Sneeuwdek':'IJsdag';
      const label=o=>Math.round(o.v)+'% '+short(dates[o.i]);
      summary.textContent=a?(main+' maximaal '+label(a)+(b&&b.v>0?' · '+second+' maximaal '+label(b):'')):
        'Geen bruikbare kansen beschikbaar';
      if(chart.otherField!=='snowDepth'&&a){
        const strong=samples.filter(v=>Number.isFinite(v.frost)&&v.frost>=70).length;
        const lowLater=samples.slice(5).filter(v=>Number.isFinite(v.frost)&&v.frost<30).length;
        if(strong>=2)summary.textContent+=' · meerdere nachten grote vorstkans';
        if(lowLater>=3)summary.textContent+=' · later afnemende vorstkans';
        if(b&&b.v<10)summary.textContent+=' · ijsdagkans klein';
      }
    }else if(chart.kind==='chance'){
      const peak=samples.map((v,i)=>({v,i})).filter(o=>Number.isFinite(o.v)).sort((a,b)=>b.v-a.v)[0];
      summary.textContent=peak?'Hoogste kans: '+Math.round(peak.v)+'% op '+short(dates[peak.i]):
        'Geen bruikbare kansen beschikbaar';
    }else if(chart.kind==='rain-chance'){
      const peak=samples.map((v,i)=>({i,v:v[0]})).filter(o=>Number.isFinite(o.v)).sort((a,b)=>b.v-a.v)[0];
      summary.textContent=peak?'Kans op ≥ 1 mm: maximaal '+Math.round(peak.v)+'% op '+short(dates[peak.i]):'Neerslagkansen niet beschikbaar';
    }else if(chart.kind==='rain-plume'){
      const wet=samples.map((v,i)=>({v,i})).filter(o=>o.v&&Number.isFinite(o.v[1]));
      const peak=[...wet].sort((a,b)=>b.v[1]-a.v[1])[0];
      const formatMm=v=>(v<1?v.toFixed(1):v.toFixed(1)).replace('.',',');
      summary.textContent=peak&&peak.v[1]>=.1?
        'Natste dag volgens de mediaan: '+short(dates[peak.i])+' · '+formatMm(peak.v[1])+' mm (P20–P80: '+formatMm(peak.v[0])+'–'+formatMm(peak.v[2])+' mm)':
        wet.length?'Mediaan blijft onder 0,1 mm per dag; afzonderlijke leden kunnen wel neerslag berekenen':'Neerslaghoeveelheden niet beschikbaar';
    }else{
      const first=samples.find(v=>v.max&&v.min);
      const last=[...samples].reverse().find(v=>v.max&&v.min);
      if(first&&last){
        const diff=last.max[1]-first.max[1];
        summary.textContent=Math.abs(diff)<1.5?'Temperatuurtrend: weinig verandering':
          (diff<0?'Temperatuurtrend: later kouder':'Temperatuurtrend: later zachter');
        summary.textContent+=' · maximum '+Math.round(last.max[1])+'° · minimum '+Math.round(last.min[1])+'° aan het eind';
        const widths=samples.map(v=>v.max&&Number.isFinite(v.max[2])&&Number.isFinite(v.max[0])?v.max[2]-v.max[0]:null);
        const startWidth=widths.slice(0,3).filter(Number.isFinite);
        const endWidth=widths.slice(-3).filter(Number.isFinite);
        if(startWidth.length>=2&&endWidth.length>=2){
          const avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
          if(avg(endWidth)-avg(startWidth)>=1.5)summary.textContent+=' · onzekerheid neemt later toe';
          else if(avg(startWidth)-avg(endWidth)>=1.5)summary.textContent+=' · ensembleleden liggen later dichter bij elkaar';
        }
      }else summary.textContent='Temperatuurtrend niet betrouwbaar beschikbaar';
    }
    card.appendChild(summary);
    // Bij zeer kleine sneeuwkansen is een lege grafiek weinig informatief.
    // Toon de percentages wel, maar bespaar hoogte. Ontbrekende kansen blijven
    // onbeschikbaar en worden nooit als 0% behandeld.
    const snowSamples=chart.kind==='dual-chance'&&chart.otherField==='snowDepth';
    const snowKnown=snowSamples?samples.flatMap(v=>[v.frost,v.ice]).filter(Number.isFinite):[];
    const compactSnow=snowSamples&&snowKnown.length>=dates.length&&
      Math.max(...snowKnown)<15;
    if(compactSnow){
      const note=document.createElement('div');
      note.textContent='Sneeuwkans zeer klein; de volledige grafiek verschijnt vanaf 15% kans op sneeuw of sneeuwdek.';
      note.style.cssText='font-size:11px;color:#446782;padding:5px 9px 9px';
      card.appendChild(note);
      section.appendChild(card);
      continue;
    }
    if(samples.every(v=>chart.kind==='dual-chance'?(v.frost===null&&v.ice===null):chart.kind==='temp'?!v.max&&!v.min:chart.kind==='rain-plume'?!v:chart.kind==='rain-chance'?v.every(x=>!Number.isFinite(x)):v===null)){
      const no=document.createElement('div');no.textContent='Geen bruikbare ensemblegegevens voor deze grafiek';
      no.style.cssText='color:#687f93;font-size:12px;padding:25px 10px';card.appendChild(no);
    }else{
      let low=0,high=100;
      if(chart.kind==='rain-plume'){
        const a=samples.flatMap(v=>v||[]).filter(Number.isFinite);
        low=0;
        const peak=Math.max(0,...a);
        // Stable human-readable rain axis, including very small amounts.
        const steps=[.5,1,2,5,10,20,30,50,75,100,150,200];
        high=steps.find(v=>v>=peak*1.15)||Math.ceil(peak/50)*50+50;
      }
      if(chart.kind==='temp'){
        const a=samples.flatMap(s=>[...(s.max||[]),...(s.min||[])]).filter(Number.isFinite);
        low=5*Math.floor((Math.min(...a,-5)-1)/5);
        high=5*Math.ceil((Math.max(...a,0)+1)/5);
        if(high-low<15)high=low+15;
      }
      const y=v=>bottom-(v-low)/(high-low)*(bottom-top);
      const marks=chart.kind==='temp'?
        Array.from({length:Math.round((high-low)/5)+1},(_,i)=>low+5*i):
        chart.kind==='rain-plume'?Array.from({length:5},(_,i)=>high*i/4):[0,25,50,75,100];
      for(const v of marks){
        const yy=y(v),zero=chart.kind==='temp'&&v===0,cold=chart.kind==='temp'&&v===-5;
        const attrs={x1:54,y1:yy,x2:499,y2:yy,
          stroke:zero?'#245779':cold?'#7798ae':'#cfdee8',
          'stroke-width':zero?2.2:cold?1.55:1};
        if(cold)attrs['stroke-dasharray']='5 4';
        svg.appendChild(make('line',attrs));
        svg.appendChild(make('text',{x:47,y:yy+3.5,'font-size':11,'text-anchor':'end',
          'font-weight':(zero||cold)?800:500,
          fill:(zero||cold)?'#204c70':'#536d82'},
          chart.kind==='temp'?v+'°':chart.kind==='rain-plume'?((v<1?v.toFixed(2):v.toFixed(1)).replace(/\.?0+$/,'').replace('.',',')+' mm'):v+'%'));
      }
      if(chart.kind==='dual-chance'){
        samples.forEach((v,i)=>{
          if(Number.isFinite(v.frost))svg.appendChild(make('rect',{
            x:x(i)-18,y:y(v.frost),width:15,height:Math.max(0,bottom-y(v.frost)),
            rx:2,fill:chart.color,stroke:'#fff','stroke-width':.6}));
          if(Number.isFinite(v.ice))svg.appendChild(make('rect',{
            x:x(i)+2,y:y(v.ice),width:15,height:Math.max(0,bottom-y(v.ice)),
            rx:2,fill:chart.otherColor,stroke:'#fff','stroke-width':.6}));
        });
        // Only a handful of important labels, so the paired bars remain readable.
        for(const [key,dx,color] of [['frost',-10,chart.color],['ice',10,chart.otherColor]]){
          const sorted=samples.map((v,i)=>({value:v[key],i})).filter(o=>Number.isFinite(o.value)&&o.value>=5)
            .sort((a,b)=>b.value-a.value);
          const peak=sorted[0];
          if(peak){
            const yy=Math.max(12,y(peak.value)-5);
            svg.appendChild(make('text',{x:x(peak.i)+dx,y:yy,'text-anchor':'middle',
              'font-size':10,'font-weight':800,fill:color},Math.round(peak.value)+'%'));
          }
        }
      }else if(chart.kind==='rain-chance'){
        // Thresholds are nested probabilities, not additive risks.
        // Stack disjoint probability portions so the bar height equals P(>=1 mm).
        samples.forEach((values,i)=>{
          const [p1,p3,p10]=values;
          if(![p1,p3,p10].every(Number.isFinite))return;
          const stack=[[Math.max(0,p1-p3),'#188bd0'],[Math.max(0,p3-p10),'#174c93'],[p10,'#f4b42f']];
          let base=0;
          for(const [amount,color] of stack){
            const upper=Math.min(100,base+amount);
            svg.appendChild(make('rect',{x:x(i)-12,y:y(upper),width:24,
              height:Math.max(0,y(base)-y(upper)),fill:color,rx:amount===0?0:1}));
            base=upper;
          }
        });
      }else if(chart.kind==='chance'){
        samples.forEach((v,i)=>{if(v===null)return;
          svg.appendChild(make('rect',{x:x(i)-11,y:y(v),width:22,height:Math.max(0,bottom-y(v)),rx:3,fill:chart.color}));
        });
      }else if(chart.kind==='rain-plume'){
        // Daily amounts: P20-P80 range and a separate P50 median line.
        // Do not interpolate missing days and never treat missing values as zero.
        samples.forEach((v,i)=>{
          if(!v||!v.every(Number.isFinite))return;
          const bx=x(i)-12, yTop=y(v[2]), yBottom=y(v[0]);
          svg.appendChild(make('rect',{x:bx,y:yTop,width:24,height:Math.max(0,yBottom-yTop),
            rx:3,fill:'#8fc4ed','fill-opacity':'.65'}));
          svg.appendChild(make('line',{x1:bx,y1:y(v[1]),x2:bx+24,y2:y(v[1]),
            stroke:'#1766a5','stroke-width':3,'stroke-linecap':'round'}));
          if(v[1]>=.5){
            svg.appendChild(make('text',{x:x(i),y:Math.max(12,y(v[2])-5),
              'font-size':10,'text-anchor':'middle','font-weight':700,fill:'#195b8b'},
              'P50 '+v[1].toFixed(1).replace('.',',')));
          }
          // 'Droog' alleen als de gehele weergegeven P20–P80-band nul is.
          // Bij P50=0 met P80>0 kunnen leden nog neerslag voorzien.
          if(v[2]<.05){
            svg.appendChild(make('text',{x:x(i),y:bottom-5,
              'font-size':9,'text-anchor':'middle',fill:'#617e91'},'droog'));
          }
        });
        let group=[];const groups=[];
        samples.forEach((v,i)=>{if(v&&Number.isFinite(v[1]))group.push({i,v});
          else if(group.length){groups.push(group);group=[];}});
        if(group.length)groups.push(group);
        for(const g of groups)svg.appendChild(make('polyline',{
          points:g.map(o=>`${x(o.i)},${y(o.v[1])}`).join(' '),
          stroke:'#1766a5','stroke-width':2.2,fill:'none','stroke-linejoin':'round'}));
      }else{
        // Draw daily maxima and minima independently, without joining gaps.
        for(const [field,color,fill] of [['max','#c95746','#f5c9b9'],['min','#2476b7','#bddcf3']]){
          let segment=[];const segments=[];
          for(const o of samples.map((v,i)=>({i,v:v[field]}))){
            if(o.v)segment.push(o);else if(segment.length){segments.push(segment);segment=[];}
          }
          if(segment.length)segments.push(segment);
          for(const group of segments){
            const poly=group.map(o=>`${x(o.i)},${y(o.v[2])}`).join(' ')+' '+
              [...group].reverse().map(o=>`${x(o.i)},${y(o.v[0])}`).join(' ');
            svg.appendChild(make('polygon',{points:poly,fill,'fill-opacity':'.38'}));
            svg.appendChild(make('polyline',{points:group.map(o=>`${x(o.i)},${y(o.v[1])}`).join(' '),
              fill:'none',stroke:color,'stroke-width':3.25,'stroke-linecap':'round','stroke-linejoin':'round'}));
          }
        }
        const finalIndex=samples.findLastIndex(v=>v.max&&v.min);
        if(finalIndex>=0){
          const end=samples[finalIndex];
          for(const [value,color,offset] of [[end.max[1],'#b43e36',-7],[end.min[1],'#1666ab',12]]){
            if(Number.isFinite(value))svg.appendChild(make('text',{
              x:Math.min(501,x(finalIndex)+4),y:Math.max(12,Math.min(201,y(value)+offset)),
              'font-size':10,'font-weight':800,'text-anchor':'end',fill:color},Math.round(value)+'°'));
          }
        }
      }
      dates.forEach((d,i)=>svg.appendChild(make('text',{x:x(i),y:210,'font-size':10,
        'text-anchor':'middle',fill:'#486479'},short(d))));
      card.appendChild(svg);
      if(chart.kind==='rain-chance'){
        const key=document.createElement('div');
        key.style.cssText='display:flex;gap:13px;flex-wrap:wrap;padding:2px 9px 5px;font-size:11px;color:#34536d';
        for(const [label,color] of [['1–<3 mm','#188bd0'],['3–<10 mm','#174c93'],['≥ 10 mm','#f4b42f']]){
          const item=document.createElement('span');item.textContent='■ '+label;item.style.color=color;key.appendChild(item);
        }
        card.appendChild(key);
        const detail=document.createElement('div');detail.textContent='De kleuren vormen elkaar uitsluitende klassen. De totale staafhoogte is de kans op ≥ 1 mm; geen dubbeltelling.';
        detail.style.cssText='font-size:10px;padding:0 9px 5px;color:#5b7587';card.appendChild(detail);
      }
      if(chart.kind==='rain-plume'){
        const key=document.createElement('div');
        key.style.cssText='display:flex;gap:13px;flex-wrap:wrap;padding:2px 9px 5px;font-size:11px;color:#34536d';
        const band=document.createElement('span');band.textContent='■ P20–P80-band';
        band.style.color='#5c9dce';key.appendChild(band);
        const med=document.createElement('span');med.textContent='━ Mediaan P50';
        med.style.color='#1766a5';key.appendChild(med);card.appendChild(key);
      }
      if(chart.kind==='temp'){
        const key=document.createElement('div');key.style.cssText='display:flex;gap:14px;flex-wrap:wrap;padding:2px 9px 5px;font-size:11px;color:#34536d';
        for(const [label,color] of [['Dagmaximum','#c95746'],['Nachtminimum','#2476b7']]){
          const item=document.createElement('span');item.style.cssText='display:inline-flex;align-items:center;gap:5px';
          const swatch=document.createElement('span');swatch.style.cssText='width:13px;height:3px;background:'+color;
          item.appendChild(swatch);item.appendChild(document.createTextNode(label));key.appendChild(item);
        }
        card.appendChild(key);
      }
      if(chart.kind==='dual-chance'){
        const legend=document.createElement('div');
        legend.style.cssText='display:flex;flex-wrap:wrap;gap:12px;padding:2px 9px 5px;font-size:11px;color:#34536d';
        const labels=chart.labels||['Vorst (TN < 0 °C)','IJsdag (TX < 0 °C)'];
        for(const [label,color] of [[labels[0],chart.color],[labels[1],chart.otherColor]]){
          const item=document.createElement('span');item.style.cssText='display:inline-flex;align-items:center;gap:5px';
          const swatch=document.createElement('span');swatch.style.cssText='display:inline-block;width:11px;height:11px;border-radius:2px;background:'+color;
          item.appendChild(swatch);item.appendChild(document.createTextNode(label));legend.appendChild(item);
        }
        card.appendChild(legend);
        if(chart.otherField==='snowDepth'){
          // Show an unconditional P20-P80 snow-cover range only when the
          // snow-depth ensembles provide real, non-negative observations.
          const dated=dates.map(date=>{
            const modelRanges=available.map(m=>{
              const a=m.days[date]?.snowDepth||[];
              return a.length>=5?[quantile(a,.2),quantile(a,.8)]:null;
            }).filter(Boolean);
            return modelRanges.length?{
              date,lo:modelRanges.reduce((s,a)=>s+a[0],0)/modelRanges.length,
              hi:modelRanges.reduce((s,a)=>s+a[1],0)/modelRanges.length
            }:null;
          }).filter(Boolean);
          const peak=dated.reduce((best,d)=>!best||d.hi>best.hi?d:best,null);
          if(peak&&peak.hi>=.5){
            const detail=document.createElement('div');
            detail.style.cssText='font-size:11px;color:#496c85;padding:2px 9px 5px';
            detail.textContent='Sneeuwdek om 08:00 · grootste P20–P80-bandbreedte '+
              short(peak.date)+': '+peak.lo.toFixed(1).replace('.',',')+'–'+
              peak.hi.toFixed(1).replace('.',',')+' cm (regiosteekproef)';
            card.appendChild(detail);
          }
          if(!dated.length){
            const warning=document.createElement('div');
            warning.style.cssText='font-size:11px;color:#687f93;padding:2px 9px 5px';
            warning.textContent='Sneeuwdekgegevens niet beschikbaar; de blauwe sneeuwvalkans blijft zichtbaar.';
            card.appendChild(warning);
          }
        }
      }
    }
    section.appendChild(card);
  }
  const source=document.createElement('div');
  source.textContent='Neerslagpluim: dagelijkse ensemblehoeveelheden (mm) met P20–P80 en P50; modelkwantielen per bron, daarna gemiddeld over beschikbare bronnen. Neerslagkans: drempels ≥ 1, 3 en 10 mm/dag, gestapeld zonder dubbeltelling. Modelkansen = aandeel ensembleleden; geen gekalibreerde kans. Randstad-steekproef: kust, west en Utrecht/Flevoland. Ensemble-ophaaltijd is niet de modelruntijd. Ontbrekende waarden tellen niet mee.';
  source.style.cssText='font-size:10px;color:#5b7587';section.appendChild(source);
  const risks=root.querySelector('.ricoRiskBox');
  if(risks)risks.insertAdjacentElement('afterend',section);
  else root.appendChild(section);
  return {available:available.map(m=>m.model),plots:charts.map(c=>c.title),dateCount:dates.length,
    fetchedAt:available.length?fetchedAt:null,
    scanAt:Number(root.dataset.scanSavedAt)||null};
}
""")
                print('Randstad-pluimen:', json.dumps(plume_info,ensure_ascii=False),flush=True)
                if plume_info.get('scanAt') and plume_info.get('fetchedAt'):
                    print('Modelscan versus ensemble-ophaalmeting (geen claim over run):',
                          {'dagscan': plume_info['scanAt'], 'ensemble_opgehaald': plume_info['fetchedAt']},flush=True)
                plume_card=forecast.locator('#ricoExportRandstadPlumes')
                plume_png=await plume_card.screenshot(type='png',animations='disabled',timeout=20000)
                if len(plume_png)>10000:
                    plume_image_url='data:image/png;base64,'+base64.b64encode(plume_png).decode('ascii')
            except Exception as exc:
                print('Randstad-pluimen tijdelijk niet beschikbaar, bestaande 10-daagse blijft publiceerbaar:',str(exc),flush=True)

            # De Randstad-ensemblepluimen vervangen het oudere blok met losse
            # modelondersteuningstegels. Dat scheelt duplicatie en nulpercentages.
            # Verwijder uitsluitend het presentatiedeel in de exportbrowser;
            # de modelscan, dagregels en data blijven ongewijzigd.
            removed_risks = await forecast.evaluate("""el => {
              let count=0;
              for (const node of el.querySelectorAll('.ricoRiskBox')) {node.remove();count++}
              return count;
            }""")
            print(f'Overbodig weerkansenblok uit WordPress-PNG verwijderd: {removed_risks}',flush=True)
            print('Randstad-weerbericht:',json.dumps(narrative,ensure_ascii=False),flush=True)
            if narrative['words'] < 25 or narrative['words'] > 145:
                raise RuntimeError('Randstad-weerbericht buiten de afgesproken lengte.')
            compact_file=OUT/'randstad-5dagen.png'
            await forecast.screenshot(path=str(compact_file),type='png',animations='disabled',timeout=50000)
            if compact_file.stat().st_size<12000:
                raise RuntimeError('Randstad 10-daagse PNG lijkt leeg; publicatie gestopt.')
            # Ook een logisch benoemde URL; de bestaande WordPress-afbeelding blijft werken.
            (OUT/'randstad-10dagen-compact.png').write_bytes(compact_file.read_bytes())
            print(f'Compacte Randstad-10-daagse gemaakt: {compact_file.name} ({compact_file.stat().st_size} bytes)',flush=True)
            # V101.0: publiceer ook de volledige Randstad 10-daagse uit DEZELFDE
            # landelijke scan als de widget. Geen tweede ophaalronde, geen andere
            # modelrun en geen terugval op niet-regionale Houten-daggegevens.
            regional = await widget_page.evaluate("""async () => {
              if(typeof drawRandstadCardsPng!=='function')
                throw new Error('De Randstad-PNG-functie ontbreekt');
              const picture=await drawRandstadCardsPng({strict:true,returnDataUrl:true});
              const safeNumber=v=>v==null||v===''||v==='null'||v==='undefined'?null:Number(v);
              const compact=[...document.querySelectorAll('#ricoWidgetFacts .rico5DayRow')].map(el=>({
                date:el.dataset.date,max:safeNumber(el.dataset.max),min:safeNumber(el.dataset.min),
                rain:safeNumber(el.dataset.rain),sun:safeNumber(el.dataset.sun),
                wind:safeNumber(el.dataset.wind),type:el.dataset.type
              }));
              const scanAt=document.getElementById('ricoWidgetFacts')?.dataset.scanSavedAt;
              const latest=JSON.parse(localStorage.getItem('rico-national-scan-v75-35punten')||'null')?.savedAt;
              if(String(latest)!==String(scanAt))throw new Error('De modelscan veranderde tijdens de PNG-export.');
              return {picture,compact,scanAt};
            }""")
            big = regional['picture']
            compact = regional['compact']
            if len(big['rows']) != 10 or len(compact) != 10:
                raise RuntimeError('Randstadkaart en widget hebben niet het juiste aantal dagen.')
            def near(a,b):
                if a is None or b is None: return a is b
                return abs(float(a)-float(b)) < 0.000001
            for a,b in zip(big['rows'],compact):
                for field in ('date','max','min','rain','sun','wind','type'):
                    if not (near(a.get(field),b.get(field)) if field not in ('date','type') else a.get(field)==b.get(field)):
                        raise RuntimeError(f'Randstadkaart en widget verschillen op {a.get("date")} veld {field}: {a.get(field)} vs {b.get(field)}.')
            if not str(big['dataUrl']).startswith('data:image/png;base64,'):
                raise RuntimeError('De Randstad 10-daagse leverde geen geldige PNG.')
            # Plaats hetzelfde weerbericht ook BOVEN de volledige Randstad-PNG.
            # Zo toont de bestaande WordPress-URL randstad-10dagen.png de tekst.
            # Compositie gebeurt lokaal in de browser; geen extra weer-API-calls.
            combined_url = await widget_page.evaluate(r"""async ({imageUrl, text, plumeImageUrl}) => {
              const image = new Image();
              image.src = imageUrl;
              await image.decode();
              const width = image.naturalWidth;
              const scale = width / 1000;
              const canvas = document.createElement('canvas');
              const ctx = canvas.getContext('2d');
              const pad = Math.round(32*scale);
              const inner = width-2*pad;
              const lineHeight = Math.round(30*scale);
              const paragraphs = text.split(/(?<=\.)\s+(?=[A-Z])/); // unused: preserve original text
              const original = document.getElementById('ricoExportRandstadNarrative');
              const ptexts = [...(original?.querySelectorAll('p')||[])].map(p=>p.textContent);
              const ec46=document.getElementById('ricoExportEC46');
              const ec46Lines=ec46?[...ec46.querySelectorAll('.ricoEC46Week')].map(x=>x.querySelector('.ricoEC46Main')?.textContent||''):[];
              if(ptexts.length!==3)throw new Error('Weerbericht ontbreekt bij PNG-compositie');
              ctx.font=`${Math.round(23*scale)}px Arial`;
              function wrap(str){
                const result=[];let line='';
                for(const word of str.split(/\s+/)){
                  const candidate=line?line+' '+word:word;
                  if(ctx.measureText(candidate).width>inner && line){result.push(line);line=word;}
                  else line=candidate;
                }
                if(line)result.push(line);
                return result;
              }
              const lines=ptexts.map(wrap);
               let plume=null;
               if(plumeImageUrl){try{plume=new Image();plume.src=plumeImageUrl;await plume.decode()}catch(e){plume=null}}
              if(ec46Lines.length){lines.push(wrap('EC46 · vooruitblik week 2–6'));for(const t of ec46Lines)lines.push(wrap(t));}
              // Dagelijkse modelzekerheid blijft beschikbaar in de PNG.
              // De oude tekstuele weerkansen zijn bewust verwijderd; de
              // ensemblepluimen zijn nu de visuele kansweergave.
              const confidence=[...document.querySelectorAll('#ricoWidgetFacts .rico5DayRow')].map(node=>{
                 const c=node.querySelector('.rico5DayConfidence');
                 const range=node.querySelector('.rico5DayRange');
                 const windRange=node.querySelector('.rico5DayWindRange');
                 const reason=node.querySelector('.rico5DayReason');
                 const feels=node.querySelector('.rico5DayFeels');
                 return c?`${node.dataset.date.slice(5)}: ${range?.textContent.trim()||''} · ${windRange?.textContent.trim()||''} · ${c.textContent.replace('🎯 ','').trim()}${reason?' · '+reason.textContent.trim():''}${feels?' · '+feels.textContent.trim():''}`:'';
              }).filter(Boolean);
              if(confidence.length){
                lines.push(wrap('Modelrange maxima, windrange en belangrijkste onzekerheid per beschikbare dag:'));
                for(let i=0;i<confidence.length;i+=2)lines.push(wrap(confidence.slice(i,i+2).join('  |  ')));
              }
              if(confidence.length!==10){
                console.warn('PNG: niet alle dagelijkse zekerheidsregels aanwezig; bestaande dagverwachting behouden',
                  {confidenceDays:confidence.length});
              }
              const headingHeight=Math.round(58*scale);
              const textHeight=lines.reduce((n,l)=>n+l.length*lineHeight+Math.round(19*scale),0);
              const chartHeight=plume?Math.round(inner*plume.naturalHeight/plume.naturalWidth):0;
               const topHeight=pad+headingHeight+textHeight+pad+(chartHeight?chartHeight+pad:0);
              canvas.width=width;canvas.height=topHeight+image.naturalHeight;
              ctx.fillStyle='#eaf4fa';ctx.fillRect(0,0,width,topHeight);
              ctx.fillStyle='#07527a';ctx.font=`bold ${Math.round(34*scale)}px Arial`;
              ctx.fillText('Weerbericht Randstad',pad,pad+Math.round(32*scale));
              ctx.fillStyle='#17445f';ctx.font=`${Math.round(23*scale)}px Arial`;
              let y=pad+headingHeight;
              for(const group of lines){
                for(const line of group){ctx.fillText(line,pad,y);y+=lineHeight;}
                y+=Math.round(19*scale);
              }
              if(plume)ctx.drawImage(plume,pad,pad+headingHeight+textHeight,inner,chartHeight);
              ctx.drawImage(image,0,topHeight);
              return canvas.toDataURL('image/png');
            }""", {'imageUrl':big['dataUrl'],'text':narrative['text'],'plumeImageUrl':plume_image_url})
            if not combined_url.startswith('data:image/png;base64,'):
                raise RuntimeError('Volledige Randstad-PNG met weerbericht is ongeldig.')
            big_bytes = base64.b64decode(combined_url.split(',',1)[1], validate=True)
            if len(big_bytes) < 50000:
                raise RuntimeError('De volledige Randstad-PNG lijkt leeg.')
            (OUT/'randstad-10dagen.png').write_bytes(big_bytes)
            print(f'Randstad 10-daagse uit dezelfde scan: {len(big_bytes)} bytes, scan {regional["scanAt"]}',flush=True)
            # Landelijke vijfdaagse als compacte introductie BOVEN de bestaande
            # Randstad-10-daagse. Geen nieuwe API-calls: exact dezelfde, reeds
            # gevalideerde landelijke dagscan uit lokale opslag wordt gebruikt.
            # Nationaal zijn dit *ruimtelijke* verschillen tussen scanpunten;
            # niet te verwarren met ensemble- of modelonzekerheid.
            try:
                national_url = await widget_page.evaluate(r"""({cacheKey,firstDate}) => {
                  let pack;
                  try { pack=JSON.parse(localStorage.getItem(cacheKey)||'null') } catch(e){return null}
                  if(!pack||!Array.isArray(pack.days))return null;
                  const start=new Date(firstDate+'T12:00:00Z');
                  const dates=Array.from({length:5},(_,i)=>new Date(start.getTime()+i*86400000).toISOString().slice(0,10));
                  const validDay=dates.map(date=>pack.days.find(d=>d.date===date));
                  const finite=v=>typeof v==='number'&&Number.isFinite(v);
                  if(validDay.some(d=>!d||!Array.isArray(d.pts)||d.pts.filter(p=>finite(p.tx)&&finite(p.tn)).length<12))return null;
                  const median=a=>{const b=a.filter(finite).sort((x,y)=>x-y);return b.length?b[Math.floor((b.length-1)/2)]+(b.length%2?0:(b[Math.floor(b.length/2)]-b[Math.floor((b.length-1)/2)])/2):null};
                  const avg=a=>{const b=a.filter(finite);return b.length?b.reduce((x,y)=>x+y,0)/b.length:null};
                  const bft=v=>!finite(v)?'—':v<6?1:v<12?2:v<20?3:v<29?4:v<39?5:v<50?6:v<62?7:v<75?8:9;
                  const r=n=>finite(n)?Math.round(n):'—';
                  const coastNames=new Set(['Texel','Terschelling','Den Helder','IJmuiden','Scheveningen','Vlissingen','Schiermonnikoog','Harlingen']);
                  const coast=p=>coastNames.has(p.point?.n);
                  const inEast=p=>finite(p.point?.lon)&&p.point.lon>=5.4&&!coast(p);
                  const region=(pts,fn)=>pts.filter(fn);
                  const range=a=>{const b=a.filter(finite);return b.length?`${Math.round(Math.min(...b))}–${Math.round(Math.max(...b))}°`:'—';};
                  const daylight=date=>{
                    const d=new Date(date+'T12:00:00Z');const start=new Date(Date.UTC(d.getUTCFullYear(),0,0));
                    const n=(d-start)/86400000;const dec=23.44*Math.PI/180*Math.sin(2*Math.PI*(n-81)/365);
                    const lat=52.1*Math.PI/180;
                    const h=Math.acos(Math.max(-1,Math.min(1,-Math.tan(lat)*Math.tan(dec))));
                    return h*24/Math.PI;
                  };
                  const weather=(rain,sun,date)=>{
                    if(finite(rain)&&rain>=8)return 'Regenachtig';
                    if(finite(rain)&&rain>=1)return 'Buien of regen';
                    if(!finite(sun))return finite(rain)?'Overwegend droog':'Weerbeeld beperkt';
                    const frac=sun/Math.max(1,daylight(date));
                    return frac>=.8?'Zonnig':frac>=.6?'Vrij zonnig':frac>=.4?'Zon en wolken':frac>.2?'Veel bewolking':'Geheel bewolkt';
                  };
                  // Per scanpunt opnieuw de gewogen DAG-modelmediaan bepalen.
                  // De modelgewichten volgen de centrale Ricoweer-dagmix; individuele
                  // modellen tellen maar eenmaal mee. Ensemble-P50 is hier NIET per
                  // 35 punten aanwezig en wordt daarom niet stilzwijgend verzonnen.
                  const modelWeight=(key,lead)=>{
                    const short={harmonie:3.2,icon_d2:3.3,arome:3.0,icon:2.5,arpege:2.2,ecmwf:1.65,gfs:1.3,aifs:1.3,gem:1.5};
                    const mid={icon:1.55,arpege:1.35,ecmwf:1.9,gfs:1.4,aifs:1.45,gem:1.55};
                    const long={ecmwf:1.85,gfs:1.45,aifs:1.45,arpege:1.05,icon:1.05,gem:1.55};
                    return (lead<=2?short:lead<=4?mid:long)[key]||1;
                  };
                  const weightedMedian=(a)=>{
                    const b=a.filter(x=>finite(x.v)&&finite(x.w)&&x.w>0).sort((x,y)=>x.v-y.v);
                    if(!b.length)return null;
                    const mid=b.reduce((s,x)=>s+x.w,0)/2;let sum=0;
                    for(const x of b){sum+=x.w;if(sum>=mid)return x.v}return b[b.length-1].v;
                  };
                  const modelPoint=(p,lead)=>{
                    const a=Array.isArray(p.modelVals)?p.modelVals:[];
                    const unique=new Map();
                    for(const x of a)if(x&&x.key&&!unique.has(x.key))unique.set(x.key,x);
                    const vals=[...unique.values()];
                    // Niet ongemerkt terugvallen op een ongewogen puntwaarde.
                    if(vals.length<2)return null;
                    const calc=k=>weightedMedian(vals.map(x=>({v:x[k],w:modelWeight(x.key,lead)})));
                    return {...p,tx:calc('tx'),tn:calc('tn'),rain:median(vals.map(x=>x.rain)),sun:median(vals.map(x=>x.sun)),wind:median(vals.map(x=>x.wind)),snow:median(vals.map(x=>x.snow)),modelCount:vals.length};
                  };
                  // Elk gebied krijgt één stem in de nationale mediaan. De kust,
                  // Randstad en dicht bemonsterde regio's domineren zo niet.
                  const zone=p=>{
                    const n=p.point?.n||'',r=p.point?.r||'',lat=p.point?.lat,lon=p.point?.lon;
                    if(coast(p)||r==='Randstad-kust'||['Hoek van Holland','Middelburg'].includes(n))return 'Kust';
                    if(r==='Wadden'||r==='Noord'||(finite(lat)&&lat>=52.8))return 'Noord';
                    if(r==='Zuid'||(finite(lat)&&lat<=51.65))return 'Zuid';
                    if(finite(lon)&&lon>=5.65)return 'Oost';
                    return 'Midden';
                  };
                  const zones=['Kust','Noord','Midden','Oost','Zuid'];
                  const regionalMedian=(pts,k)=>median(zones.map(z=>median(pts.filter(p=>zone(p)===z).map(p=>p[k]))));
                  const data=validDay.map((d,i)=>{
                    const lead=Number.isFinite(d.lead)?d.lead:i;
                    const pts=d.pts.map(p=>modelPoint(p,lead)).filter(p=>p&&finite(p.tx)&&finite(p.tn));
                    if(pts.length<12||zones.filter(z=>pts.some(p=>zone(p)===z)).length<4)return null;
                    const kust=region(pts,p=>zone(p)==='Kust'),oost=region(pts,p=>zone(p)==='Oost'),binnen=region(pts,p=>zone(p)!=='Kust');
                    const mx=regionalMedian(pts,'tx'),mn=regionalMedian(pts,'tn');
                    const rain=regionalMedian(pts,'rain'),sun=regionalMedian(pts,'sun'),wind=regionalMedian(pts,'wind');
                    const cMax=median(kust.map(p=>p.tx)),eMax=median(oost.map(p=>p.tx));
                    const cMin=median(kust.map(p=>p.tn)),iMin=median(binnen.map(p=>p.tn));
                    const cWind=median(kust.map(p=>p.wind)),iWind=median(binnen.map(p=>p.wind));
                    const wetK=median(kust.map(p=>p.rain)),wetE=median(oost.map(p=>p.rain));
                    const localWet=pts.filter(p=>finite(p.rain)&&p.rain>=1).length;
                    // Toon elke dag de werkelijke verschillen in vijf landsdelen,
                    // zelfs wanneer die kleiner zijn dan de signaaldrempels.
                    // De waarden zijn ruimtelijke regiomedianen, geen onzekerheidsinterval.
                    const regionValues=zones.map(z=>{
                      const regionPts=pts.filter(p=>zone(p)===z);
                      return {name:z, max:median(regionPts.map(p=>p.tx)),
                        min:median(regionPts.map(p=>p.tn)),
                        rain:median(regionPts.map(p=>p.rain)),
                        sun:median(regionPts.map(p=>p.sun)),
                        samples:regionPts.length};
                    });
                    const differences=[];
                    if(finite(cMax)&&finite(eMax)&&Math.abs(cMax-eMax)>=2)differences.push(cMax>eMax?'Kust overdag zachter':'Oosten overdag warmer');
                    if(finite(cMin)&&finite(iMin)&&cMin-iMin>=2)differences.push('Binnenland koudere nachten');
                    if(finite(cWind)&&finite(iWind)&&cWind-iWind>=8)differences.push('Kust duidelijk meer wind');
                    if(finite(wetK)&&finite(wetE)&&Math.abs(wetK-wetE)>=2)differences.push(wetK>wetE?'Meer neerslag aan de kust':'Oosten natter');
                    if(pts.some(p=>finite(p.snow)&&p.snow>=.1))differences.push('Lokaal sneeuwsignaal');
                    if(localWet>=2&&finite(rain)&&rain<1)differences.push('Regionaal kans op buien');
                    // Een korte inhoudelijke weersverwachting in plaats van
                    // terugkerende labels 'Koudste nacht' of 'Weinig verschillen'.
                    // Alleen dagelijkse modelwaarden, geen uurwaarden of verzonnen kans.
                    const coldest=regionValues.filter(z=>finite(z.min)).sort((a,b)=>a.min-b.min);
                    const warmest=regionValues.filter(z=>finite(z.max)).sort((a,b)=>b.max-a.max);
                    const hasFrost=coldest.length&&coldest[0].min<0;
                    const hasModerateFrost=coldest.length&&coldest[0].min<=-5;
                    const frostText=hasModerateFrost?'met lokaal matige vorst':hasFrost?'met lichte vorst':'zonder vorstsignaal';
                    const coastBreezy=finite(cWind)&&finite(iWind)&&cWind-iWind>=8;
                    const regionalShowers=localWet>=Math.max(2,Math.ceil(pts.length*.18));
                    const cloudy=finite(sun)&&sun/Math.max(1,daylight(d.date))<=.2;
                    let forecast;
                    if(finite(rain)&&rain>=8)forecast='Veel bewolking en perioden met regen.';
                    else if(regionalShowers||finite(rain)&&rain>=1)
                      forecast=finite(sun)&&sun/Math.max(1,daylight(d.date))>=.4
                        ?'Zon en wolken, regionaal een bui.':'Wolkenvelden met kans op buien.';
                    else if(cloudy)forecast=hasFrost?'Overwegend grijs; in de nacht kans op vorst.':'Veel bewolking, maar overwegend droog.';
                    else if(finite(sun)&&sun/Math.max(1,daylight(d.date))>=.8)
                      forecast=hasFrost?'Zonnig en droog; in de nacht lichte vorst.':'Veel zon en overwegend droog.';
                    else if(finite(sun)&&sun/Math.max(1,daylight(d.date))>=.6)
                      forecast=coastBreezy?'Vrij zonnig en droog; aan zee meer wind.':'Vrij zonnig met overwegend droog weer.';
                    else if(finite(sun))forecast=hasFrost?'Af en toe zon; in de nacht lichte vorst.':'Wolken en zon, op veel plaatsen droog.';
                    else forecast=finite(rain)?'Overwegend droog; bewolkingsbeeld onzeker.':'Weerbeeld beperkt door ontbrekende modelgegevens.';
                    // Bij kans op matige vorst de kwalificatie in zonnige dagen bijstellen.
                    if(hasModerateFrost&&forecast.includes('lichte vorst'))forecast=forecast.replace('lichte vorst','lokaal matige vorst');
                    const name=new Intl.DateTimeFormat('nl-NL',{weekday:'short',day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(d.date+'T12:00:00Z')).replace('.','');
                    const count=Math.round(median(pts.map(p=>p.modelCount))||0);
                    const feels=finite(mx)&&mx<5&&finite(wind)&&wind>=12?13.12+0.6215*mx-11.37*Math.pow(wind,0.16)+0.3965*mx*Math.pow(wind,0.16):null;
                    let wx=weather(rain,sun,d.date);
                    if(localWet>=Math.max(2,Math.ceil(pts.length*.18))&&finite(rain)&&rain<1)wx='Regionaal buien';
                    return {name,feels,max:mx,min:mn,maxRange:range(pts.map(p=>p.tx)),minRange:range(pts.map(p=>p.tn)),rain,sun,wind,weather:wx,forecast,count,regionValues};
                  });
                  if(data.some(d=>!d))return null;
                  // Dagwaarden sturen de verhaallijn: vergelijk opeenvolgende
                  // dagen, benoem omslag en extreme regionale minima/maxima.
                  // Nooit uurwaarden samenvoegen tot dagwaarden.
                  for(let i=0;i<data.length;i++){
                    const d=data[i],prev=i?data[i-1]:null,next=i+1<data.length?data[i+1]:null;
                    const coldRegion=d.regionValues.filter(z=>finite(z.min)).sort((a,b)=>a.min-b.min)[0];
                    const nearIce=d.regionValues.some(z=>finite(z.max)&&z.max<.5);
                    const dryDay=finite(d.rain)&&d.rain<.5;
                    if(prev&&finite(prev.rain)&&finite(d.rain)&&prev.rain<.5&&d.rain>=1){
                      d.forecast=d.rain>=3?'Omslag: meer bewolking en perioden met regen.':'Omslag: meer bewolking en kans op buien.';
                    }else if(prev&&finite(prev.max)&&finite(d.max)&&d.max-prev.max>=3){
                      d.forecast=dryDay?'Zachter dan gisteren; het blijft overwegend droog.':'Zachter dan gisteren; kans op regen of buien.';
                    }else if(prev&&finite(prev.max)&&finite(d.max)&&prev.max-d.max>=3){
                      d.forecast='Duidelijk kouder dan gisteren. '+(dryDay?'Overwegend droog.':'Kans op buien.');
                    }else if(nearIce&&dryDay&&finite(d.sun)&&d.sun>=3){
                      d.forecast='Zonnige perioden, regionaal rond het vriespunt.';
                    }else if(coldRegion&&coldRegion.min<=-5&&dryDay){
                      d.forecast='Overwegend droog; lokaal matige vorst in de nacht.';
                    }else if(prev&&next&&finite(d.max)&&d.max<=prev.max-1&&d.max<=next.max-1){
                      d.forecast='De koudste dag van deze periode; '+(dryDay?'meestal droog.':'met buienkans.');
                    }else if(prev&&d.weather===prev.weather&&d.forecast===prev.forecast){
                      if(/Zonnig|Vrij zonnig/.test(d.weather))d.forecast='Opnieuw veel zon; '+(coldRegion&&coldRegion.min<0?'landinwaarts nachtvorst.':'overwegend droog.');
                      else if(/bewolkt/i.test(d.weather))d.forecast='Weinig zon; '+(dryDay?'het blijft grotendeels droog.':'regionaal neerslag.');
                    }
                  }
                  const weatherIcon=(w)=>{
                    if(w==='Regenachtig')return '🌧️';
                    if(w==='Buien of regen'||w==='Regionaal buien')return '🌦️';
                    if(w==='Zonnig')return '☀️';
                    if(w==='Vrij zonnig')return '🌤️';
                    if(w==='Zon en wolken')return '⛅';
                    if(w==='Veel bewolking')return '🌥️';
                    if(w==='Geheel bewolkt')return '☁️';
                    return '🌤️';
                  };
                  const width=1000,height=1300;
                  const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
                  const c=canvas.getContext('2d');
                  c.fillStyle='#edf7fd';c.fillRect(0,0,width,height);
                  c.fillStyle='#084b74';c.font='bold 40px Arial';c.fillText('🇳🇱 Nederland · komende 5 dagen',28,51);
                  c.fillStyle='#526e84';c.font='21px Arial';c.fillText('Landelijke modelmix · verschillen tussen 5 landsdelen',28,86);
                  const roundRect=(x,y,w,h,r,fill,stroke)=>{
                    c.beginPath();c.roundRect(x,y,w,h,r);
                    if(fill){c.fillStyle=fill;c.fill()}
                    if(stroke){c.strokeStyle=stroke;c.lineWidth=1.6;c.stroke()}
                  };
                  // Bovenaan de nationale hoofdverwachting; onder elke dag de
                  // regionale maxima/minima naast elkaar, zodat het overzicht
                  // niet een verkorte kopie van de Randstadverwachting is.
                  for(let i=0;i<5;i++){
                    const d=data[i],y=112+i*226;
                    roundRect(13,y,974,216,17,i%2?'#ffffff':'#f7fcff','#c9dfed');
                    c.strokeStyle='#d4e4ed';c.lineWidth=2;
                    c.beginPath();c.moveTo(183,y+15);c.lineTo(183,y+118);c.moveTo(822,y+15);c.lineTo(822,y+118);c.stroke();
                    c.fillStyle='#133e5e';c.font='bold 26px Arial';c.fillText(d.name,30,y+35);
                    c.fillStyle='#687f91';c.font='17px Arial';c.fillText(`max ${d.maxRange}`,30,y+84);
                    c.fillText(`min ${d.minRange}`,30,y+107);
                    c.font='49px "Noto Color Emoji","Segoe UI Emoji",sans-serif';
                    c.fillText(weatherIcon(d.weather),200,y+66);
                    c.fillStyle='#153e5d';c.font='bold 27px Arial';c.fillText(d.weather,288,y+34);
                    const sun=finite(d.sun)?`${r(d.sun)} u zon`:'zon onbekend';
                    const rain=finite(d.rain)?`${d.rain.toFixed(1).replace('.',',')} mm`:'neerslag onbekend';
                    const wind=finite(d.wind)?`${bft(d.wind)} Bft`:'wind onbekend';
                    c.fillStyle='#426377';c.font='20px Arial';
                    c.fillText(`☀ ${sun}   ☂ ${rain}   ≋ ${wind}`,288,y+66);
                    if(finite(d.feels)){
                      c.fillStyle='#315a75';c.font='18px Arial';
                      c.fillText(`Voelt als ${r(d.feels)}°`,689,y+66);
                    }
                    c.fillStyle='#245777';c.font='bold 21px Arial';
                    c.fillText('•  '+d.forecast,288,y+109,514);
                    c.textAlign='right';
                    c.fillStyle='#b13939';c.font='bold 47px Arial';c.fillText(`${r(d.max)}°`,961,y+60);
                    c.fillStyle='#246c9b';c.font='bold 29px Arial';c.fillText(`${r(d.min)}°`,961,y+103);
                    c.textAlign='left';
                    // Vijf vaste regionale tegels, elk met eigen maximum en minimum.
                    // Ontbrekende landsdelen krijgen een streep, nooit een verzonnen waarde.
                    const x0=26,cellW=189,regionY=y+138;
                    for(let j=0;j<5;j++){
                      const z=d.regionValues[j],x=x0+j*cellW;
                      roundRect(x,regionY,179,65,9,j%2?'#e8f2f9':'#e2f0fa','#d0e2ee');
                      c.fillStyle='#315b77';c.font='bold 19px Arial';c.fillText(z.name,x+10,regionY+24);
                      c.fillStyle='#b13939';c.font='bold 29px Arial';c.fillText(finite(z.max)?`${r(z.max)}°`:'—',x+10,regionY+55);
                      c.fillStyle='#246c9b';c.fillText(finite(z.min)?`${r(z.min)}°`:'—',x+94,regionY+55);
                    }
                  }
                  c.fillStyle='#526e84';c.font='17px Arial';
                  c.fillText('Rood: dagmaximum · blauw: nachtminimum · elke regio telt even zwaar mee.',27,1275);
                  return canvas.toDataURL('image/png');
                }""", {'cacheKey':SCAN_KEY,'firstDate':compact[0]['date']})
                if national_url and national_url.startswith('data:image/png;base64,'):
                    (OUT/'nederland-5dagen.png').write_bytes(base64.b64decode(national_url.split(',',1)[1],validate=True))
                    # Voeg de nieuwe kop toe aan BEIDE reeds bestaande WordPress-
                    # afbeeldingsadressen. Geen aanpassing aan de widget of WP nodig.
                    async def prepend_national(image_bytes: bytes) -> bytes:
                        result=await widget_page.evaluate(r"""async ({topUrl,bottomUrl})=>{
                          const images=await Promise.all([topUrl,bottomUrl].map(async src=>{
                            const img=new Image();img.src=src;await img.decode();return img;
                          }));
                          const [top,bottom]=images;
                          const canvas=document.createElement('canvas');
                          canvas.width=bottom.naturalWidth;
                          const h=Math.round(top.naturalHeight*canvas.width/top.naturalWidth);
                          canvas.height=h+bottom.naturalHeight;
                          const ctx=canvas.getContext('2d');
                          ctx.drawImage(top,0,0,canvas.width,h);
                          ctx.drawImage(bottom,0,h);
                          return canvas.toDataURL('image/png');
                        }""",{'topUrl':national_url,'bottomUrl':'data:image/png;base64,'+base64.b64encode(image_bytes).decode('ascii')})
                        if not result.startswith('data:image/png;base64,'):
                            raise RuntimeError('Landelijk voorblad kon niet worden samengesteld.')
                        return base64.b64decode(result.split(',',1)[1],validate=True)
                    # Eerst in het geheugen opbouwen; pas daarna alle drie de
                    # PNG's overschrijven zodat er geen gedeeltelijke update is.
                    compact_with_national=await prepend_national(compact_file.read_bytes())
                    full_with_national=await prepend_national(big_bytes)
                    if min(len(compact_with_national),len(full_with_national))<50000:
                        raise RuntimeError('Landelijke vijfdaagse PNG lijkt onvolledig.')
                    compact_file.write_bytes(compact_with_national)
                    (OUT/'randstad-10dagen-compact.png').write_bytes(compact_with_national)
                    (OUT/'randstad-10dagen.png').write_bytes(full_with_national)
                    print('Landelijke 5-daagse: gewogen dagmodellen en 5 regio-medianen, zonder extra API-verzoeken.',flush=True)
                else:
                    print('Landelijke vijfdaagse niet volledig beschikbaar; bestaande Randstadverwachting ongewijzigd.',flush=True)
            except Exception as exc:
                # Een alleen visuele toevoeging mag geen succesvolle actuele
                # Randstad-publicatie alsnog blokkeren.
                print('Landelijke vijfdaagse overgeslagen; oorspronkelijke Randstad-PNG behouden:',str(exc)[:250],flush=True)
            # Een simpele startpagina is vereist voor GitHub Pages. Deze bevat
            # uitsluitend de twee publiek getoonde afbeeldingen, geen link
            # naar de Ricoweer-app, broncode, caches of model-API's.
            landing = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weerkaarten morgen en overmorgen</title>
<style>body{max-width:920px;margin:20px auto;padding:0 12px;font:16px system-ui,sans-serif;background:#f2f7fb;color:#173b56}h1{font-size:24px}img{max-width:100%;height:auto;display:block;margin:20px auto;border-radius:12px}p{color:#456478}</style>
</head><body><h1>Weerkaarten morgen en overmorgen</h1>
<p>De kaarten worden meerdere keren per dag gecontroleerd. Bij ontbrekende actuele gegevens blijft de vorige publicatie behouden.</p>
<img src="morgen.png" alt="Landelijke weersverwachting voor morgen">
<img src="overmorgen.png" alt="Landelijke weersverwachting voor overmorgen">
<h2>Nederland 5 dagen en Randstad 10 dagen (dezelfde scan)</h2><img src="randstad-10dagen.png" alt="Tien dagen Randstad"><h2>Randstad · tien dagen (compact)</h2><img src="randstad-10dagen-compact.png" alt="Tiendaagse weersverwachting voor de Randstad"><h2>Morgenkaart met tiendaagse</h2><img src="widget.png" alt="Morgenkaart met Randstadverwachting">
</body></html>"""
            (OUT / 'index.html').write_text(landing, encoding='utf-8')
            print("Succes: morgen.png, overmorgen.png, widget.png, randstad-5dagen.png (10 dagen, bestaande URL), randstad-10dagen-compact.png en randstad-10dagen.png zijn gecontroleerd en gereed voor publicatie.", flush=True)
        finally:
            await browser.close()



# Een mislukte bron-API mag niet zorgen dat GitHub Pages een leeg artefact
# publiceert. Bij een mislukte nieuwe scan hergebruik de *ongewijzigde* reeds
# gepubliceerde PNG's, mits alle bestanden volledig kunnen worden gecontroleerd.
# Dit is nadrukkelijk geen nieuwe weersverwachting of EC46-update.
def restore_previous_publication() -> bool:
    from urllib.request import Request, urlopen
    from urllib.parse import urljoin
    import struct

    base = os.environ.get(
        "PUBLISHED_PAGES_URL",
        "https://dcschroder-alt.github.io/ricoweer-kaarten/",
    ).rstrip("/") + "/"
    names = (
        "morgen.png", "overmorgen.png", "widget.png",
        "randstad-5dagen.png", "randstad-10dagen-compact.png",
        "randstad-10dagen.png",
    )
    collected = {}
    for name in names:
        url = urljoin(base, name)
        try:
            req = Request(url, headers={"User-Agent": "Ricoweer-Pages-Fallback/1.0", "Cache-Control": "no-cache"})
            with urlopen(req, timeout=18) as response:
                data = response.read(12 * 1024 * 1024 + 1)
            if len(data) < 20000 or len(data) > 12 * 1024 * 1024:
                raise ValueError("PNG-grootte buiten grenzen")
            if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
                raise ValueError("Geen geldige PNG-header")
            width, height = struct.unpack(">II", data[16:24])
            if width < 350 or height < 250:
                raise ValueError(f"Onverwachte PNG-afmetingen: {width}x{height}")
            collected[name] = data
        except Exception as exc:
            print(f"Terugval niet mogelijk voor {name}: {exc}", flush=True)
            return False
    # Pas na volledige validatie bestanden opslaan. Geen gedeeltelijke set.
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in collected.items():
        (OUT / name).write_bytes(data)
    landing = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ricoweer - laatst beschikbare weerkaarten</title>
<style>body{max-width:920px;margin:20px auto;padding:0 12px;font:16px system-ui,sans-serif;background:#f2f7fb;color:#173b56}img{max-width:100%;height:auto;display:block;margin:16px auto}</style>
</head><body><h1>Ricoweer - laatst beschikbare weerkaarten</h1>
<p>De nieuwste brongegevens waren tijdelijk niet beschikbaar. De bestaande kaarten zijn ongewijzigd behouden; de oorspronkelijke updatevermelding in de PNG's blijft leidend.</p>
<h2>Morgen</h2><img src="morgen.png" alt="Laatst beschikbare morgenkaart">
<h2>Overmorgen</h2><img src="overmorgen.png" alt="Laatst beschikbare overmorgenkaart">
<h2>Randstad - 10 dagen</h2><img src="randstad-10dagen.png" alt="Laatst beschikbare Randstadverwachting">
<h2>Randstad compact</h2><img src="randstad-10dagen-compact.png" alt="Laatst beschikbare compacte Randstadverwachting">
<h2>Widget</h2><img src="widget.png" alt="Laatst beschikbare widget">
</body></html>"""
    (OUT / "index.html").write_text(landing, encoding="utf-8")
    print("TERUGVAL: bron-API tijdelijk niet beschikbaar; zes eerder gepubliceerde PNG's ongewijzigd hergebruikt. Geen nieuwe update of EC46-gegevens.", flush=True)
    return True


if __name__ == "__main__":
    try:
        asyncio.run(make_images())
    except Exception as error:
        print(f"Nieuwe publicatie mislukt: {error}", flush=True)
        # Geen oude PNG's opnieuw als succesvolle update publiceren.
        # GitHub Pages behoudt de bestaande deployment als deze stap faalt.
        print("PUBLICATIE MISLUKT: geen actuele PNG's gemaakt; bestaande GitHub Pages-publicatie blijft staan.", flush=True)
        raise SystemExit(1)