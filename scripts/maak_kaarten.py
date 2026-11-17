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
 const hour=Number(new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Amsterdam',hour:'2-digit',hourCycle:'h23'}).format(new Date()));
 const start=new Date(Date.parse(date+'T12:00:00Z')+(hour>=18?0:-86400000)).toISOString().slice(0,10);
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
  visiblePins:[...document.querySelectorAll('#ricoLiveShell #ricoWeatherAtlas .ricoWeatherCard')].map(x=>x.querySelectorAll('.ricoWeatherPin').length)
 }
}"""

QUALITY_JS = """el => {
 const map = el.querySelector('.ricoWeatherMap');
 const loaded = [...(map?.querySelectorAll('img.leaflet-tile-loaded') || [])].filter(x=>x.complete && x.naturalWidth>0);
 const rect = map?.getBoundingClientRect();
 return {tiles: loaded.length, pins:map?.querySelectorAll('.ricoWeatherPin').length||0,
         width:Math.round(rect?.width||0),height:Math.round(rect?.height||0)};
}"""


async def make_images() -> None:
    if not SOURCE.startswith("https://") and os.environ.get("ALLOW_HTTP_FOR_TEST") != "1":
        raise RuntimeError("De bronkaart moet via HTTPS beschikbaar zijn.")
    nl_today = datetime.now(ZoneInfo("Europe/Amsterdam")).date()
    expected = [(nl_today + timedelta(days=i)).isoformat() for i in (1, 2)]
    print(f"Dagen voor WordPress: morgen {expected[0]}, overmorgen {expected[1]}", flush=True)
    errors = []

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
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)) if len(errors) < 15 else None)
            page.on("requestfailed", lambda req: errors.append(f"Aanvraag mislukt: {req.url[:180]} ({req.failure})") if len(errors) < 15 else None)
            page.on("response", lambda res: errors.append(f"HTTP {res.status}: {res.url[:180]}") if res.status >= 400 and len(errors) < 15 else None)
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
            widget_page=await context.new_page()
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
  const tempRange=arr=>{const v=arr.map(r=>round(r.max));const lo=Math.min(...v),hi=Math.max(...v);return lo===hi?`${lo} graden`:hi-lo===1?`${lo} of ${hi} graden`:`${lo} tot ${hi} graden`;};
  const today=rows[0],tomorrow=rows[1];
  const clause=r=>{
    if(veryWet(r))return 'valt geregeld regen';
    if(kind(r)==='onweer')return 'komen buien voor, mogelijk met onweer';
    if(kind(r)==='sneeuw')return 'kan sneeuw vallen';
    if(wetScore(r)===1)return sunny(r)?'zijn er zonnige perioden en enkele buien':'komen enkele buien voor';
    if(sunny(r))return 'schijnt de zon flink';
    if(brighter(r))return 'zijn er perioden met zon';
    if(kind(r)==='bewolkt')return 'overheerst de bewolking';
    return 'is het overwegend droog';
  };
  const delta=round(tomorrow.max)-round(today.max);
  const tempTomorrow=()=>{
    const x=round(tomorrow.max);
    if(delta===0)return `opnieuw ${x} graden`;
    if(delta>0)return delta<=2?`met ${x} graden iets zachter`:`met ${x} graden duidelijk zachter`;
    if(today.max>=10&&tomorrow.max>=10)return delta>=-2?`met ${x} graden iets minder zacht`:`met ${x} graden minder zacht`;
    if(today.max>=9&&tomorrow.max>=7)return `met ${x} graden minder zacht`;
    return delta>=-2?`met ${x} graden iets frisser`:`met ${x} graden duidelijk frisser`;
  };
  const todayText=`Vandaag ${clause(today)} en wordt het ${round(today.max)} graden.`;
  const tomorrowText=(veryWet(today)&&veryWet(tomorrow))?'Morgen blijft het nat':
    (wetScore(today)===wetScore(tomorrow)&&kind(today)===kind(tomorrow))?`Morgen ${clause(tomorrow)}`:
    (wetScore(tomorrow)<wetScore(today))?`Morgen wordt het droger en ${clause(tomorrow)}`:
    `Morgen ${clause(tomorrow)}`;
  const p1=`${todayText} ${tomorrowText} en wordt het ${tempTomorrow()}.`;
  // Perioden vormen op basis van samenhangende dagwaarden, niet losse trefwoorden.
  // Dag 3-5: eerst aanhoudend nat benoemen, daarna het omslagmoment.
  const mid=rows.slice(2,5),later=rows.slice(5);
  const transitionIndex=rows.slice(2,5).findIndex((r,j)=>{
    const i=j+2,prev=rows[i-1];
    const drier=wetScore(r)<=1&&wetScore(prev)>=2;
    const brightChange=brighter(r)&&(!brighter(prev)||(hasSun(r)&&hasSun(prev)&&r.sun>=prev.sun+2));
    const persists=(i===4)||wetScore(rows[i+1])<=1;
    return drier&&brightChange&&persists;
  });
  let p2='';
  if(transitionIndex>=0){
    const i=transitionIndex+2;
    const prior=rows.slice(2,i);
    if(prior.length&&prior.every(veryWet))p2=`Ook ${day(rows[2])} valt geregeld regen. `;
    else if(prior.length&&prior.some(veryWet))p2=`${cap(day(rows[2]))} blijft het nog wisselvallig. `;
    p2+=`Vanaf ${day(rows[i])} wordt het droger en krijgt de zon meer ruimte.`;
    const following=rows[i+1];
    if(i<=3&&following&&wetScore(following)<=1&&brighter(following))p2+=` Ook ${day(following)} zijn er zonnige perioden.`;
  }else if(mid.filter(veryWet).length>=2){
    p2=`Ook ${day(mid[0])} en de dagen daarna valt geregeld regen.`;
  }else if(mid.filter(r=>wetScore(r)<=1&&brighter(r)).length>=2){
    p2=`Vanaf ${day(mid[0])} zijn er vaker droge perioden met zon.`;
  }else if(mid.some(veryWet)){
    p2=`${cap(day(mid.find(veryWet)))} valt geregeld regen; op de andere dagen zijn er ook droge perioden.`;
  }else{
    p2=`Vanaf ${day(mid[0])} zijn er geregeld droge perioden${mid.some(brighter)?' met af en toe zon':''}.`;
  }
  const firstAvg=rows.slice(0,5).reduce((n,r)=>n+r.max,0)/5;
  const laterAvg=later.reduce((n,r)=>n+r.max,0)/5;
  const tempDiff=laterAvg-firstAvg;
  const wetLater=later.filter(veryWet).length;
  const showerLater=later.filter(r=>wetScore(r)===1).length;
  const sunLater=later.filter(brighter).length;
  const weatherLater=wetLater>=3?'Verder vooruit blijft het wisselvallig met regelmatig regen.':
    showerLater+wetLater>=3?'Verder vooruit wisselen droge perioden en enkele buien elkaar af.':
    sunLater>=3?'Verder vooruit zijn er geregeld droge perioden met zon.':'Verder vooruit zijn er overwegend droge perioden.';
  const range=tempRange(later);
  const tempLater=tempDiff<=-3?`Het wordt minder zacht, met maxima van ${range}.`:
    tempDiff>=3?`Het wordt zachter, met maxima van ${range}.`:`De maxima liggen dan rond ${range}.`;
  let p3=`${weatherLater} ${tempLater}`;
  const lows=rows.slice(2).filter(r=>Number.isFinite(r.min));
  const min=lows.length?Math.min(...lows.map(r=>r.min)):null;
  if(min!==null&&min<=0)p3+=' In de koudste nachten kan het plaatselijk rond het vriespunt komen.';
  else if(min!==null&&min<=2)p3+=` In de koudste nachten daalt de temperatuur tot ongeveer ${round(min)} ${round(min)===1?'graad':'graden'}.`;
  // Onderste samenvatting voegt iets toe en herhaalt niet de volledige hoofdtekst.
  const summary=el.querySelector('.rico5DaySummary');
  if(summary){
    const sunniest=rows.reduce((best,r)=>hasSun(r)&&(!best||r.sun>best.sun)?r:best,null);
    const wettest=rows.reduce((best,r)=>hasRain(r)&&(!best||r.rain>best.rain)?r:best,null);
    let note='';
    if(sunniest&&sunniest.sun>=5)note=`De meeste zon wordt ${day(sunniest)} verwacht (${round(sunniest.sun)} uur).`;
    else if(wettest&&wettest.rain>=8)note=`Vooral ${day(wettest)} kan veel regen vallen.`;
    else note='De verwachting voor dag 6–10 is nog onzeker.';
    summary.textContent=note;
  }
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
  if(transitionIndex>=0){const i=transitionIndex+2;if(wetScore(rows[i])>1||!brighter(rows[i]))throw Error('Tekstcontrole: onjuiste omslag');}
  if(mid.filter(veryWet).length>=2&&transitionIndex<0&&!p2.includes('regen'))throw Error('Tekstcontrole: natte periode ontbreekt');
  const wordCount=texts.join(' ').trim().split(/\s+/).length;
  if(wordCount<35||wordCount>145)throw Error('Tekstcontrole: lengte '+wordCount);
  const box=document.createElement('section');box.id='ricoExportRandstadNarrative';box.setAttribute('aria-label','Automatisch weerbericht Randstad');
  box.style.cssText='box-sizing:border-box;margin:9px 8px 12px;padding:14px 15px;background:#e6f2fa;border:1px solid #bfdbe9;border-radius:12px;color:#17445f;font:15px/1.52 Arial,sans-serif;overflow-wrap:break-word;';
  const heading=document.createElement('div');heading.textContent='Weerbericht Randstad';heading.style.cssText='font-size:19px;line-height:1.25;font-weight:800;margin-bottom:9px;color:#07527a';box.appendChild(heading);
  for(const str of texts){const p=document.createElement('p');p.textContent=str;p.style.cssText='margin:0 0 9px';box.appendChild(p);}
  box.lastElementChild.style.marginBottom='0';
  const firstRow=el.querySelector('.rico5DayRow');firstRow.parentNode.insertBefore(box,firstRow);
  return {text:texts.join(' '),words:wordCount,firstType:today.type,firstCategory:kind(today)};
}""")
            # EC46: verse GitHub-browser heeft meestal geen localStorage-cache.
            # Gebruik de bestaande Ricoweer-functie; die hergebruikt cache indien aanwezig.
            ec46 = await widget_page.evaluate(r"""async () => {
              const result={weeks:[],error:null};
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
                result.weeks=future.slice(2,6).map(x=>({
                  start:x.start,end:x.end,
                  anomaly:Number.isFinite(x.tA)?x.tA:null,
                  txMin:Number.isFinite(x.txMin)?x.txMin:null,
                  txMax:Number.isFinite(x.txMax)?x.txMax:null,
                  wet:wetValue(x)
                }));
                if(!result.weeks.length)result.error='Geen bruikbare EC46-weken 3–6';
              }catch(e){result.error=String(e?.message||e)}
              return result;
            }""")
            print('EC46 diagnose:',json.dumps(ec46,ensure_ascii=False),flush=True)
            ec46_display = await forecast.evaluate(r"""(el,pack) => {
              const box=document.createElement('section');box.id='ricoExportEC46';
              box.style.cssText='box-sizing:border-box;margin:12px 8px;padding:12px 15px;background:#f3f7ff;border:1px solid #c9d8ec;border-radius:12px;color:#17445f;font:14px/1.5 Arial,sans-serif';
              const h=document.createElement('div');h.textContent='EC46 · vooruitblik week 3–6';h.style.cssText='font-weight:800;font-size:17px;margin-bottom:7px';box.appendChild(h);
              const lines=[];
              if(pack.weeks?.length){
                for(const w of pack.weeks){
                  const temp=w.anomaly>=.4?'warmer':w.anomaly<=-.4?'kouder':'rond normaal';
                  const delta=(w.anomaly>0?'+':'')+w.anomaly.toFixed(1).replace('.',',')+' °C';
                  const hasMax=Number.isFinite(w.txMin)&&Number.isFinite(w.txMax);
                  const actual=hasMax
                    ?`maxima ${Math.floor(Math.min(w.txMin,w.txMax))} tot ${Math.ceil(Math.max(w.txMin,w.txMax))} °C`
                    :`${temp} (${delta} t.o.v. normaal; maxima niet beschikbaar)`;
                  const rain=w.wet===null?'':w.wet>=58?' · natter signaal':w.wet<=42?' · droger signaal':'';
                  const fmt=d=>new Intl.DateTimeFormat('nl-NL',{day:'numeric',month:'short',timeZone:'UTC'}).format(new Date(d+'T12:00:00Z'));
                  lines.push(`${fmt(w.start)}–${fmt(w.end)}: ${actual}${rain}`);
                }
              }else lines.push('EC46-vooruitblik tijdelijk niet beschikbaar.');
              for(const line of lines){const d=document.createElement('div');d.textContent=line;d.style.cssText='padding:5px 0;border-bottom:1px solid #dbe5f0';box.appendChild(d)}
              const note=document.createElement('div');note.textContent='Indicatieve weektrend, geen dagverwachting.';note.style.cssText='font-size:11px;opacity:.8;margin-top:7px';box.appendChild(note);
              el.appendChild(box);return {lines,note:note.textContent};
            }""",ec46)
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
            combined_url = await widget_page.evaluate(r"""async ({imageUrl, text}) => {
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
              const ec46Lines=ec46?[...ec46.querySelectorAll('div')].map(x=>x.textContent):[];
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
              if(ec46Lines.length){lines.push(wrap('EC46 · vooruitblik week 3–6'));for(const t of ec46Lines.slice(1))lines.push(wrap(t));}
              const headingHeight=Math.round(58*scale);
              const textHeight=lines.reduce((n,l)=>n+l.length*lineHeight+Math.round(19*scale),0);
              const topHeight=pad+headingHeight+textHeight+pad;
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
              ctx.drawImage(image,0,topHeight);
              return canvas.toDataURL('image/png');
            }""", {'imageUrl':big['dataUrl'],'text':narrative['text']})
            if not combined_url.startswith('data:image/png;base64,'):
                raise RuntimeError('Volledige Randstad-PNG met weerbericht is ongeldig.')
            big_bytes = base64.b64decode(combined_url.split(',',1)[1], validate=True)
            if len(big_bytes) < 50000:
                raise RuntimeError('De volledige Randstad-PNG lijkt leeg.')
            (OUT/'randstad-10dagen.png').write_bytes(big_bytes)
            print(f'Randstad 10-daagse uit dezelfde scan: {len(big_bytes)} bytes, scan {regional["scanAt"]}',flush=True)
            # Een simpele startpagina is vereist voor GitHub Pages. Deze bevat
            # uitsluitend de twee publiek getoonde afbeeldingen, geen link
            # naar de Ricoweer-app, broncode, caches of model-API's.
            landing = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weerkaarten morgen en overmorgen</title>
<style>body{max-width:920px;margin:20px auto;padding:0 12px;font:16px system-ui,sans-serif;background:#f2f7fb;color:#173b56}h1{font-size:24px}img{max-width:100%;height:auto;display:block;margin:20px auto;border-radius:12px}p{color:#456478}</style>
</head><body><h1>Weerkaarten morgen en overmorgen</h1>
<p>De kaarten worden meerdere keren per dag automatisch bijgewerkt.</p>
<img src="morgen.png" alt="Landelijke weersverwachting voor morgen">
<img src="overmorgen.png" alt="Landelijke weersverwachting voor overmorgen">
<h2>Randstad · 10 dagen (zelfde scan)</h2><img src="randstad-10dagen.png" alt="Tien dagen Randstad"><h2>Randstad · tien dagen (compact)</h2><img src="randstad-10dagen-compact.png" alt="Tiendaagse weersverwachting voor de Randstad"><h2>Morgenkaart met tiendaagse</h2><img src="widget.png" alt="Morgenkaart met Randstadverwachting">
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