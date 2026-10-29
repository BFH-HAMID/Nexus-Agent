import Globe from 'globe.gl';
import * as THREE from 'three';

const $ = id => document.getElementById(id);
const viewVisible = () => !document.hidden && !$('world-widget').hidden && !$('world-widget').closest('[data-gui-view][hidden]');
const NS = 'http://www.w3.org/2000/svg';
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const el = (tag, value, cls) => { const n = document.createElement(tag); if (value != null) n.textContent = value; if (cls) n.className = cls; return n; };
const svg = (tag, attrs = {}) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v)); return n; };
const finite = n => typeof n === 'number' && Number.isFinite(n);
const num = (n, suffix = '') => finite(n) ? Math.round(n).toLocaleString() + suffix : '—';
const safeURL = value => { try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password ? u.href : null; } catch { return null; } };
function anchor(title, url) { const a = el('a', title); const href = safeURL(url); if (href) { a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; } return a; }
function date(value, opts = {}, timezone) { try { return finite(value) ? new Intl.DateTimeFormat('en', {...opts, ...(timezone ? {timeZone: timezone} : {})}).format(value * 1000) : '—'; } catch { return '—'; } }
function ago(at) { if (!finite(at)) return 'time not supplied'; const mins = Math.floor((Date.now() / 1000 - at) / 60); return mins < 0 ? 'publisher date ahead' : mins < 1 ? 'just now' : mins < 60 ? mins + 'm ago' : mins < 1440 ? Math.floor(mins / 60) + 'h ago' : Math.floor(mins / 1440) + 'd ago'; }
function freshness(row) { return (row?.status || 'offline') + ' / ' + (row?.at ? 'last updated ' + date(row.at, {hour: '2-digit', minute: '2-digit'}) : 'no cached data'); }
const palette = ['#13445c','#253f69','#22565e','#344a73','#28626b','#3a4761','#1a506f','#385b68'];
const labelMeasure=document.createElement('canvas').getContext('2d');
let sourceStatus={};
let api = null, session = 0, initialised = false, timer, revision = -1, globe = null, flat = false, labelTimer, idleTimer;
let lowIds=new Set();
let countries = [], byId = new Map(), low = [], detail = [], geo = [], selected = null, hovered = null, config, language = 'en';
let tab = 'World', news = {items: [], hotspots: {},status:'not connected'}, quakes = [], latestWeather = null, location, seen = new Set(), newIds = new Set(), firstNews = true;
let layers = {news: true, quakes: true, home: true, terminator: true, arcs: true}, box = [0, 0, 1000, 500], due = {}, pending = new Set(), favorites = [];
const nameOf = c => language === 'bn' ? c.bn || c.name : c.name;
function home() { return config?.default_location || {name:'Dinajpur, Bangladesh',latitude:25.63,longitude:88.64}; }
function tooltip(country) {
  if (!country) { $('world-tooltip').hidden = true; return; }
  $('world-tooltip').textContent = nameOf(country) + '\nCapital / seat: ' + (country.capital || 'not in bundled source') + '\nPopulation: ' + num(country.population) + ' (' + country.population_year + ' estimate)\nLocal: ' + new Intl.DateTimeFormat(language, {timeZone:country.timezone, hour:'2-digit', minute:'2-digit'}).format(new Date());
  $('world-tooltip').hidden = false;
}
$('world-canvas').parentElement.addEventListener('pointermove', e => { const r = e.currentTarget.getBoundingClientRect(); $('world-tooltip').style.left = Math.min(e.clientX - r.left + 14, r.width - 235) + 'px'; $('world-tooltip').style.top = Math.max(10, Math.min(e.clientY - r.top + 12, r.height - 135)) + 'px'; });
$('world-canvas').parentElement.addEventListener('pointerleave', () => tooltip(null));
function fill(feature) { const c = byId.get(feature.properties.id); return c ? palette[c.color ?? (countries.indexOf(c) % palette.length)] : '#173a51'; }
function sun() { const now = new Date(), day = (now - new Date(Date.UTC(now.getUTCFullYear(), 0, 0))) / 86400000; return {lat:23.44*Math.sin(2*Math.PI*(day-80)/365.2422), lon:180 - (now.getUTCHours()+now.getUTCMinutes()/60)*15}; }
function terminator() {
  const s = sun(), r = Math.PI / 180, normal = new THREE.Vector3(Math.cos(s.lat*r)*Math.cos(s.lon*r),Math.sin(s.lat*r),Math.cos(s.lat*r)*Math.sin(s.lon*r));
  const u = new THREE.Vector3().crossVectors(normal,new THREE.Vector3(0,1,0)).normalize(), v = new THREE.Vector3().crossVectors(normal,u).normalize();
  return Array.from({length:181}, (_,i) => { const t=i*Math.PI/90, p=u.clone().multiplyScalar(Math.cos(t)).addScaledVector(v,Math.sin(t)); return {lat:Math.asin(p.y)/r,lon:Math.atan2(p.z,p.x)/r}; });
}
function paths() {
  const rows = [];
  for (let lat=-60;lat<=60;lat+=30) rows.push({points:Array.from({length:181},(_,i)=>({lat,lon:-180+i*2})),term:false});
  for (let lon=-180;lon<180;lon+=30) rows.push({points:Array.from({length:91},(_,i)=>({lat:-90+i*2,lon})),term:false});
  if (layers.terminator) rows.push({points:terminator(),term:true});
  return rows;
}
function points() {
  const list = countries.filter(c=>c.rank>=5||!lowIds.has(c.id)).map(c=>({lat:c.lat,lng:c.lon,kind:'country',country:c,label:nameOf(c),radius:.11}));
  if(location&&finite(location.latitude)&&finite(location.longitude)&&(location.latitude!==home().latitude||location.longitude!==home().longitude))list.push({lat:location.latitude,lng:location.longitude,kind:'city',label:location.name,radius:.3});
  if (layers.home) list.push({lat:home().latitude,lng:home().longitude,kind:'home',label:home().name,radius:.42});
  if (layers.news) for (const [id,count] of Object.entries(news.hotspots || {})) { const c=byId.get(id); if(c) list.push({lat:c.lat,lng:c.lon,kind:'news',country:c,label:c.name+' / '+count+' recent headlines',radius:Math.min(.8,.16+Math.sqrt(count)*.09)}); }
  if (layers.quakes) for (const q of [...quakes].sort((a,b)=>(b.magnitude||0)-(a.magnitude||0)).slice(0,250)) list.push({...q,lng:q.lon,kind:'quake',label:q.place+' / M'+(q.magnitude??'unknown'),radius:Math.min(.6,.1+Math.max(0,q.magnitude||0)*.045)});
  return list;
}
function updateLayers() {
  if (!initialised) return;
  const pts=points();
  if (globe && !flat) {
    globe.pointsData(pts).ringsData(reduced.matches ? [] : pts.filter(p=>p.kind!=='country'&&p.kind!=='city'&&(p.kind!=='quake'||p.magnitude>=3)).slice(0,60));
    const arcs=layers.arcs && layers.news ? pts.filter(p=>p.kind==='news').slice(0,12).map(p=>({startLat:p.lat,startLng:p.lng,endLat:home().latitude,endLng:home().longitude})) : [];
    globe.arcsData(arcs).arcDashAnimateTime(reduced.matches?0:4500).pathsData(paths());
    updateSun();
  }
  if(flat) drawFlat();
  scheduleLabels();
}
function updateSun() {
  if (!globe) return;
  const s=sun(), p=globe.getCoords(s.lat,s.lon,3), mat=globe.globeMaterial();
  if(mat.userData.sun) mat.userData.sun.value.set(p.x,p.y,p.z).normalize();
}
function buildGlobe() {
  if (new URLSearchParams(window.location.search).get('map') === '2d') return false;
  try {
    const canvas=document.createElement('canvas'); if (!canvas.getContext('webgl2')) return false;
    const stage=$('world-canvas');
    globe=Globe({animateIn:!reduced.matches,rendererConfig:{antialias:true,alpha:true,powerPreference:'low-power'}})(stage)
      .backgroundColor('#00000000').showAtmosphere(true).atmosphereColor(config.theme.atmosphere).atmosphereAltitude(.16)
      .polygonsData(low).polygonGeoJsonGeometry(f=>f.geometry).polygonCapColor(f=>f.properties.id===hovered?.id?'#6de7efcc':fill(f)+'99')
      .polygonSideColor(()=> '#3286ad55').polygonStrokeColor(f=>f.properties.id===hovered?.id?'#d5ffff':config.theme.border)
      .polygonAltitude(f=>f.properties.id===hovered?.id ? .016 : .004).polygonsTransitionDuration(reduced.matches?0:180)
      .polygonLabel(()=>null).onPolygonHover(f=>{ hovered=f?byId.get(f.properties.id):null; tooltip(hovered); globe.polygonCapColor(globe.polygonCapColor()).polygonAltitude(globe.polygonAltitude()); })
      .onPolygonClick(f=>chooseCountry(byId.get(f.properties.id)))
      .pointLat('lat').pointLng('lng').pointColor(p=>p.kind==='home'?'#88ffe0':p.kind==='quake'?'#ffae79':'#60ddff').pointRadius('radius').pointAltitude(.015).pointsMerge(false).pointLabel(()=>null)
      .onPointHover(p=>{ if(!p){tooltip(null);return;} if(p.country) tooltip(p.country);else { $('world-tooltip').textContent=p.label; $('world-tooltip').hidden=false;} })
      .onPointClick(p=>p.country?chooseCountry(p.country):chooseLocation({name:p.label,latitude:p.lat,longitude:p.lng}))
      .ringLat('lat').ringLng('lng').ringColor(p=>p.kind==='quake'?'#ffae79':'#70e7d5').ringMaxRadius(p=>p.kind==='home'?2.5:1.7).ringPropagationSpeed(.8).ringRepeatPeriod(2200)
      .arcColor(()=>['#48ccff66','#69d6ba11']).arcStroke(.3).arcDashLength(.35).arcDashGap(.8)
      .pathPoints('points').pathPointLat('lat').pathPointLng('lon').pathPointAlt(.002).pathColor(p=>p.term?'#ffda8a99':'#79bde31c').pathStroke(p=>p.term?.35:.12).pathTransitionDuration(0)
      .htmlLat('lat').htmlLng('lon').htmlAltitude(.025).htmlElement(c=>{const n=el('span',nameOf(c),'world-label'+(c.id==='HOME'?' home':''));return n;})
      .onZoom(p=>{const next=p.altitude<1.15?detail:low;if(geo!==next){geo=next;globe.polygonsData(next);}scheduleLabels();});
    globe.renderer().setPixelRatio(Math.min(devicePixelRatio || 1,1.5));
    const texture=new THREE.TextureLoader().load('/world/earth-night.jpg'); texture.colorSpace=THREE.SRGBColorSpace;
    const material=new THREE.MeshPhongMaterial({map:texture,color:'#8bb9ce',emissive:'#b4dfff',emissiveMap:texture,emissiveIntensity:.85});
    material.onBeforeCompile=shader=>{ shader.uniforms.worldSun={value:new THREE.Vector3(1,0,0)}; material.userData.sun=shader.uniforms.worldSun;
      shader.vertexShader='varying vec3 worldNormal;\n'+shader.vertexShader.replace('#include <begin_vertex>','#include <begin_vertex>\nworldNormal=normalize(mat3(modelMatrix)*normal);');
      shader.fragmentShader='uniform vec3 worldSun; varying vec3 worldNormal;\n'+shader.fragmentShader.replace('#include <emissivemap_fragment>','#include <emissivemap_fragment>\ntotalEmissiveRadiance*=smoothstep(0.2,-0.2,dot(normalize(worldNormal),worldSun));'); updateSun(); };
    globe.globeMaterial(material);
    const ctrl=globe.controls(); ctrl.enabled=interacting; ctrl.enableZoom=false; ctrl.autoRotate=!reduced.matches; ctrl.autoRotateSpeed=.22; ctrl.enableDamping=true; ctrl.dampingFactor=.08; ctrl.minDistance=115; ctrl.maxDistance=700;
    ctrl.addEventListener('start',stopRotation);ctrl.addEventListener('end',resumeIdle);
    globe.pointOfView({lat:15,lng:65,altitude:1.7},0);geo=low;
    const observer=new ResizeObserver(()=>{if(globe){globe.width(stage.clientWidth).height(stage.clientHeight);scheduleLabels();}});observer.observe(stage);
    stage.addEventListener('webglcontextlost',e=>{e.preventDefault();setFlat(true);},true);
    return true;
  } catch { if(globe) globe._destructor(); globe=null; $('world-canvas').replaceChildren(); return false; }
}
function stopRotation(){clearTimeout(idleTimer);if(globe)globe.controls().autoRotate=false;}
function resumeIdle(){clearTimeout(idleTimer);idleTimer=setTimeout(()=>{if(globe&&!flat&&mapInView&&!reduced.matches&&!document.hidden)globe.controls().autoRotate=true;},20000);}
function fly(lat,lon,altitude=1.3){stopRotation();if(flat){const w=Math.max(55,Math.min(1000,altitude*240));box=[(lon+180)/360*1000-w/2,(90-lat)/180*500-w/4,w,w/2];drawFlat();}else globe?.pointOfView({lat,lng:lon,altitude},reduced.matches?0:1400);resumeIdle();}
function scheduleLabels(){if(!labelTimer)labelTimer=setTimeout(()=>{labelTimer=null;updateLabels();},180);}
function visibleLabels(project,limit){const occupied=[], rows=[];const homeLabel={id:'HOME',rank:-1,name:'Dinajpur · Home',bn:'দিনাজপুর · হোম',lat:home().latitude,lon:home().longitude};for(const c of [...(layers.home?[homeLabel]:[]),...countries].sort((a,b)=>(a.id===selected?.id?-1:b.id===selected?.id?1:a.rank-b.rank))){const p=project(c);if(!p)continue;labelMeasure.font='11px Segoe UI,NexusBangla,system-ui';const width=Math.max(35,labelMeasure.measureText(nameOf(c)).width+8);const rect={x:p.x-width/2,y:p.y-8,w:width,h:18};if(occupied.some(o=>rect.x<o.x+o.w+8&&rect.x+rect.w+8>o.x&&rect.y<o.y+o.h+5&&rect.y+rect.h+5>o.y))continue;occupied.push(rect);rows.push(c);if(rows.length>=limit)break;}return rows;}
function updateLabels(){if(!initialised)return;if(flat)return drawFlatLabels();if(!globe)return;const view=globe.pointOfView(),r=Math.PI/180,stage=$('world-canvas'),limit=view.altitude>2?9:view.altitude>1.1?20:view.altitude>.5?40:65;
 const rows=visibleLabels(c=>{const facing=Math.sin(c.lat*r)*Math.sin(view.lat*r)+Math.cos(c.lat*r)*Math.cos(view.lat*r)*Math.cos((c.lon-view.lng)*r);if(facing<.3)return null;const p=globe.getScreenCoords(c.lat,c.lon,.025);return p&&p.x>20&&p.x<stage.clientWidth-20&&p.y>35&&p.y<stage.clientHeight-35?p:null;},limit);globe.htmlElementsData(rows);}
function countryPath(f){const polys=f.geometry.type==='Polygon'?[f.geometry.coordinates]:f.geometry.coordinates;return polys.flatMap(poly=>poly.map(ring=>ring.map((p,i)=>(i?'L':'M')+((p[0]+180)/360*1000).toFixed(2)+','+((90-p[1])/180*500).toFixed(2)).join('')+'Z')).join('');}
function drawFlat(){if(!initialised||!flat)return;const map=$('world-map');map.replaceChildren();map.setAttribute('viewBox',box.join(' '));const features=box[2]<500?detail:low;
 for(const f of features){const c=byId.get(f.properties.id);if(!c)continue;const path=svg('path',{d:countryPath(f),fill:fill(f),class:'country','fill-rule':'evenodd',tabindex:0,'aria-label':nameOf(c),'data-country':c.id});path.addEventListener('pointerenter',()=>tooltip(c));path.addEventListener('pointerleave',()=>tooltip(null));path.addEventListener('click',()=>chooseCountry(c));path.addEventListener('keydown',e=>{if(e.key==='Enter')chooseCountry(c);});map.append(path);}
 const grid=svg('g',{stroke:'#8db9d21c','stroke-width':.4});for(let x=0;x<=1000;x+=1000/12)grid.append(svg('path',{d:`M${x} 0V500`}));for(let y=0;y<=500;y+=500/6)grid.append(svg('path',{d:`M0 ${y}H1000`}));map.append(grid);
 if(layers.terminator){const line=terminator();let d='';for(let i=0;i<line.length;i++){const p=line[i];d+=(i&&Math.abs(p.lon-line[i-1].lon)<180?'L':'M')+((p.lon+180)/360*1000)+','+((90-p.lat)/180*500);}map.append(svg('path',{d,fill:'none',stroke:'#ffda8a','stroke-width':.8,'vector-effect':'non-scaling-stroke'}));}
 // Tiny units omitted from low-resolution geometry are included in points().
 for(const p of points()){const n=svg('circle',{cx:(p.lng+180)/360*1000,cy:(90-p.lat)/180*500,r:(p.kind==='home'?3:1+p.radius*2)*box[2]/1000,fill:p.kind==='quake'?'#ffae79':p.kind==='home'?'#89ffe1':'#56d2ff',class:'map-marker'});if(p.country){n.setAttribute('data-country',p.country.id);n.addEventListener('pointerenter',()=>tooltip(p.country));n.addEventListener('pointerleave',()=>tooltip(null));}n.addEventListener('click',()=>p.country?chooseCountry(p.country):chooseLocation({name:p.label,latitude:p.lat,longitude:p.lng}));const title=svg('title');title.textContent=p.label;n.append(title);map.append(n);}
 const labels=svg('g',{id:'world-svg-labels'});map.append(labels);drawFlatLabels();}
function drawFlatLabels(){const g=$('world-svg-labels');if(!g)return;g.replaceChildren();const width=$('world-map').clientWidth,height=$('world-map').clientHeight;const rows=visibleLabels(c=>{const x=((c.lon+180)/360*1000-box[0])/box[2]*width,y=((90-c.lat)/180*500-box[1])/box[3]*height;return x>25&&x<width-25&&y>25&&y<height-25?{x,y}:null;},box[2]>700?12:box[2]>350?25:55);for(const c of rows){const n=svg('text',{x:(c.lon+180)/360*1000,y:(90-c.lat)/180*500,'text-anchor':'middle','font-size':box[2]/Math.max(1,$('world-map').clientWidth)*11});n.textContent=nameOf(c);g.append(n);}}
function setFlat(value){flat=value||!globe;$('world-map').toggleAttribute('hidden',!flat);$('world-canvas').hidden=flat;$('world-mode').textContent=flat?'2D SVG fallback · + / − zoom · enable interaction to pan':'3D WebGL · + / − zoom · enable interaction to orbit';if(flat){globe?.pauseAnimation();drawFlat();}else {sceneVisibility();updateLayers();resumeIdle();}}
function zoom(factor){stopRotation();if(flat){const w=Math.min(1000,Math.max(40,box[2]*factor)),cx=box[0]+box[2]/2,cy=box[1]+box[3]/2;box=[cx-w/2,cy-w/4,w,w/2];drawFlat();}else if(globe)globe.pointOfView({altitude:Math.max(.15,Math.min(5,globe.pointOfView().altitude*factor))},reduced.matches?0:350);resumeIdle();}
let interacting=false, mapInView=false;
function sceneVisibility(){if(!globe)return;if(viewVisible()&&mapInView&&!flat&&api)globe.resumeAnimation();else globe.pauseAnimation();}
new IntersectionObserver(entries=>{mapInView=entries[0].isIntersecting;sceneVisibility();},{rootMargin:'100px'}).observe($('world-canvas').parentElement);
$('world-interact').onclick=()=>{interacting=!interacting;$('world-interact').setAttribute('aria-pressed',String(interacting));$('world-interact').textContent=interacting?'Disable map interaction':'Enable map interaction';$('world-canvas').parentElement.classList.toggle('interacting',interacting);if(globe)globe.controls().enabled=interacting;};
let drag=null;$('world-map').addEventListener('pointerdown',e=>{if(!interacting)return;drag={x:e.clientX,y:e.clientY,box:[...box]};});window.addEventListener('pointerup',()=>drag=null);$('world-map').addEventListener('pointermove',e=>{if(!drag)return;const r=$('world-map').getBoundingClientRect();box=[drag.box[0]-(e.clientX-drag.x)/r.width*box[2],drag.box[1]-(e.clientY-drag.y)/r.height*box[3],box[2],box[3]];$('world-map').setAttribute('viewBox',box.join(' '));scheduleLabels();});$('world-map').addEventListener('wheel',e=>{if(!e.ctrlKey&&!e.metaKey)return;e.preventDefault();zoom(e.deltaY>0?1.2:.8);},{passive:false});
function chooseCountry(c){if(!c)return;selected=c;fly(c.lat,c.lon,.9);$('world-country-name').textContent=nameOf(c);$('world-country-info').textContent='Capital / seat: '+(c.capital||'not provided')+' · Population '+num(c.population)+' ('+c.population_year+' source estimate) · '+c.timezone;paintNews();if(finite(c.capital_lat)&&finite(c.capital_lon))chooseLocation({name:c.capital+', '+nameOf(c),latitude:c.capital_lat,longitude:c.capital_lon},false);else{$('world-weather-status').textContent='Capital coordinates absent from bundled source; use city search. Previous forecast remains labelled.';}}
function chooseLocation(value,move=true){location=value;$('world-weather-name').textContent=value.name;if(move)fly(value.latitude,value.longitude,.75);refreshOne('weather',true);updateLayers();paintFavorites();}
function paintNews(){const filtered=(news.items||[]).filter(n=>(!selected||n.countries.includes(selected.id))&&n.category===tab);const list=$('world-news-list');list.replaceChildren();if(!filtered.length)list.append(el('p',selected?'No fetched '+tab+' headlines matched '+nameOf(selected)+'.':'No fetched headlines in this category.','muted'));
 for(const n of filtered.slice(0,60)){const card=el('article',null,'world-news-card'),meta=el('div',null,'world-news-meta');meta.append(el('span',n.source),el('span',ago(n.published)));if(newIds.has(n.id))meta.append(el('span','NEW','world-new'));const heading=el('h4');heading.append(anchor(n.title,n.url));card.append(meta,heading,el('p',n.summary));list.append(card);}
 const focus=$('world-focus-news');focus.replaceChildren();const matches=(news.items||[]).filter(n=>!selected||n.countries.includes(selected.id)).slice(0,4);for(const n of matches)focus.append(anchor(n.title,n.url));if(!matches.length)focus.append(el('p','No matching fetched headlines.','muted'));
 $('world-ticker-text').textContent=(news.items||[]).slice(0,8).map(n=>n.source+' / '+n.title).join('     •     ')||'No live headlines fetched yet.';$('world-ticker-text').classList.toggle('running',!reduced.matches&&tickerRunning&&(news.items||[]).length>1);
 $('world-news-status').textContent=news.status+(selected?' / '+nameOf(selected):' / all countries')+' · '+(news.items||[]).length+' fetched';
 const sources=$('world-sources');sources.replaceChildren();for(const s of news.sources||[]){const p=el('p');p.append(anchor(s.name,s.terms),document.createTextNode(' — '+freshness(s)));sources.append(p);}}
function spark(values,color,width=70,height=24){const valid=(values||[]).filter(finite).slice(-168),n=svg('svg',{viewBox:`0 0 ${width} ${height}`,role:'img','aria-label':valid.length>1?'Observed price/temperature trend':'Not enough samples for trend'});if(valid.length<2)return n;const min=Math.min(...valid),range=Math.max(...valid)-min||1;const d=valid.map((v,i)=>(i?'L':'M')+(2+i/(valid.length-1)*(width-4)).toFixed(1)+','+(height-3-(v-min)/range*(height-6)).toFixed(1)).join('');n.append(svg('path',{d,fill:'none',stroke:color,'stroke-width':1.3}));return n;}
function paintCrypto(row){const strip=$('world-crypto');strip.replaceChildren();if(!row.data){strip.append(el('p',freshness(row)+' · quotes unavailable','muted'));return;}for(const c of row.data.items){const cls=finite(c.change)&&c.change<0?'world-down':'world-up',card=el('div',null,'world-coin');card.title=row.data.provider+' · '+freshness(row)+' · '+c.spark_period;card.append(el('strong',c.symbol),spark(c.spark,cls==='world-up'?'#69d6ba':'#fb91a0'),el('div',finite(c.price)?new Intl.NumberFormat('en',{style:'currency',currency:c.quote==='USD'?'USD':'USD',maximumFractionDigits:c.price<1?4:2}).format(c.price)+(c.quote==='USDT'?' USDT':'' ):'—','price'),el('span',(finite(c.change)?(c.change>=0?'+':'')+c.change.toFixed(2)+'% / 24h':'change unknown')+' · '+row.status,'change '+cls));strip.append(card);}const credit=el('p',null,'muted');credit.append(anchor(row.data.provider,row.data.provider==='CoinGecko'?'https://www.coingecko.com/en/api':'https://www.binance.com/en'),document.createTextNode(' · '+freshness(row)));strip.append(credit);}
function weatherIcon(code,night=false){if(!finite(code))return '◌';if(code===0)return night?'☾':'☀';if([71,73,75,77,85,86].includes(code))return '❄';return code<4?'☁':code<50?'≋':code<95?'☂':'ϟ';}
function weatherName(code){if(!finite(code))return 'Unknown';if([71,73,75,77,85,86].includes(code))return 'Snow';return code===0?'Clear':code<4?'Cloudy':code<50?'Fog':code<70?'Rain':code<95?'Showers':'Thunderstorm';}
function paintWeather(row){latestWeather=row;$('world-weather-status').textContent=freshness(row);const data=row.data;if(!data){$('world-current').replaceChildren(el('strong','—'),el('span','Forecast unavailable; no cached data'));$('world-weather-details').replaceChildren();$('world-hourly').replaceChildren();$('world-forecast').replaceChildren();$('world-weather-alert').hidden=true;return;}
 const c=data.current,d=data.daily,h=data.hourly,tz=data.timezone;$('world-current').replaceChildren(el('span',weatherIcon(c.weather_code,c.is_day===0),'world-weather-icon'),el('strong',num(c.temperature_2m,'°')),el('span',weatherName(c.weather_code)+' · '+(c.is_day?'day':'night')));
 const next=(h.time||[]).map((t,i)=>({t,i})).filter(v=>v.t>=Date.now()/1000).slice(0,24),chance=next.map(v=>h.precipitation_probability?.[v.i]).filter(finite),dailyIndex=Math.max(0,(d.time||[]).findIndex(t=>t+86400>Date.now()/1000));
 const fields=[['Feels like',num(c.apparent_temperature,'°C')],['Humidity',num(c.relative_humidity_2m,'%')],['Wind',num(c.wind_speed_10m,' km/h')],['Rain chance / next 24h',chance.length?num(Math.max(...chance),'%'):'—'],['UV / daily max',num(d.uv_index_max?.[dailyIndex])],['Rain now',num(c.precipitation,' mm')],['Sunrise',date(d.sunrise?.[dailyIndex],{hour:'2-digit',minute:'2-digit'},tz)],['Sunset',date(d.sunset?.[dailyIndex],{hour:'2-digit',minute:'2-digit'},tz)],['Local timezone',tz||'unknown']];
 $('world-weather-details').replaceChildren(...fields.map(([k,v])=>{const n=el('div',v);n.prepend(el('small',k));return n;}));
 const hourly=$('world-hourly');hourly.replaceChildren();const chart=spark(next.map(v=>h.temperature_2m?.[v.i]),'#73cde9',380,70);chart.setAttribute('aria-label','Next 24 hourly temperatures: '+next.map(v=>date(v.t,{hour:'2-digit'},tz)+' '+num(h.temperature_2m?.[v.i],'°')).join(', '));hourly.append(chart,el('p',next.length?date(next[0].t,{hour:'2-digit',minute:'2-digit'},tz)+' → '+date(next.at(-1).t,{hour:'2-digit',minute:'2-digit'},tz)+' · '+next.map(v=>num(h.temperature_2m?.[v.i])).join(' / ')+' °C':'No future hourly samples','muted'));
 const forecast=$('world-forecast');forecast.replaceChildren();for(let i=0;i<Math.min(7,d.time?.length||0);i++){const day=el('div',date(d.time[i],{weekday:'short'},tz));day.append(el('span',weatherIcon(d.weather_code?.[i])),el('div',num(d.temperature_2m_max?.[i])+'° / '+num(d.temperature_2m_min?.[i])+'°'));forecast.append(day);}
 $('world-weather-alert').hidden=!data.alerts?.length;$('world-weather-alert').textContent=(data.alerts||[]).join(' · ')+(data.alerts?.length?' — '+data.alert_notice:'');}
function paintFavorites(){const parent=$('world-favorites');parent.replaceChildren();for(const f of favorites){const b=el('button',f.name,'secondary');b.onclick=()=>chooseLocation(f);const remove=el('button','×','secondary');remove.setAttribute('aria-label','Remove '+f.name);remove.onclick=()=>{favorites=favorites.filter(v=>v!==f);saveFavorites();paintFavorites();};parent.append(b,remove);}}
function saveFavorites(){try{localStorage.setItem('nexus-world-favorites',JSON.stringify(favorites));}catch{$('world-status').textContent='Favorites storage unavailable';}}
function loadFavorites(){try{const x=JSON.parse(localStorage.getItem('nexus-world-favorites')||'[]');favorites=Array.isArray(x)?x.filter(v=>typeof v.name==='string'&&v.name.length<=120&&finite(v.latitude)&&finite(v.longitude)&&Math.abs(v.latitude)<=90&&Math.abs(v.longitude)<=180).slice(0,8):[];}catch{favorites=[];}paintFavorites();}
async function refreshOne(kind,force=false){if(!api||!viewVisible()||pending.has(kind)||(!force&&Date.now()<(due[kind]||0)))return;const gen=session,request=api,loc=location;pending.add(kind);const path=kind==='weather'?'weather?latitude='+loc.latitude+'&longitude='+loc.longitude:kind;try{const row=await request(path);if(gen!==session)return;if(kind==='weather'&&loc!==location)return;due[kind]=Date.now()+config.refresh[kind]*1000;if(kind==='news'){news=row;newIds=new Set(firstNews?[]:row.items.filter(n=>!seen.has(n.id)).map(n=>n.id));seen=new Set(row.items.map(n=>n.id));firstNews=false;paintNews();updateLayers();}if(kind==='crypto')paintCrypto(row);if(kind==='weather')paintWeather(row);if(kind==='quakes'){quakes=(row.data||[]).filter(q=>finite(q.time)&&Date.now()/1000-q.time>=0&&Date.now()/1000-q.time<=86400);$('world-mode').title='USGS / '+freshness(row);$('world-quakes-status').textContent='USGS '+freshness(row)+' · '+quakes.length+' quakes / strongest 250 shown';updateLayers();}sourceStatus[kind]=row.status;const states=Object.values(sourceStatus);$('world-status').textContent=(states.every(s=>s==='offline')?'OFFLINE · bundled map / last cache':states.some(s=>s==='offline'||s==='partial')?'PARTIAL / OFFLINE SOURCES':'Public caches updated')+' · '+new Date().toLocaleTimeString();}catch{$('world-status').textContent='Public data unavailable · local map remains available';}finally{pending.delete(kind);if(kind==='weather'&&loc!==location&&api)refreshOne('weather',true);}}
async function connect(request){api=request;const gen=++session;clearInterval(timer);pending.clear();try{const metadata=await api('countries');if(gen!==session)return;config=metadata.config;if(!config.enabled){globe?.pauseAnimation();$('world-widget').hidden=true;return;}$('world-widget').hidden=false;countries=metadata.items;byId=new Map(countries.map(c=>[c.id,c]));language=config.language;layers={...config.layers};for(const n of document.querySelectorAll('[data-layer]'))n.checked=layers[n.dataset.layer];$('world-widget').style.setProperty('--world-accent',config.theme.accent);$('world-widget').style.setProperty('--world-border',config.theme.border);location={...home()};$('world-weather-name').textContent=location.name;
 if(!initialised){const responses=await Promise.all([fetch('/world/countries-low.geojson'),fetch('/world/countries-detail.geojson')]);[low,detail]=await Promise.all(responses.map(r=>{if(!r.ok)throw Error('Bundled polygons missing');return r.json().then(x=>x.features);}));if(gen!==session)return;lowIds=new Set(low.map(f=>f.properties.id));initialised=true;setFlat(!buildGlobe());loadFavorites();}else{setFlat(flat);}
 const list=$('world-countries');list.replaceChildren();for(const c of countries){const option=el('option');option.value=nameOf(c);option.label=c.name+' / '+(c.bn||'');list.append(option);}due={};updateLayers();for(const k of ['news','crypto','weather','quakes'])refreshOne(k,true);timer=setInterval(()=>{for(const k of ['news','crypto','weather','quakes'])refreshOne(k);},30000);
 }catch{$('world-status').textContent='Unable to load widget configuration/assets. Inspect local installation.';}}
window.addEventListener('hud:connected',e=>connect(e.detail.api));window.addEventListener('hud:disconnected',()=>{session++;api=null;clearInterval(timer);$('world-status').textContent='Disconnected · cached public view only';stopRotation();sceneVisibility();});window.addEventListener('hud:world-update',e=>{if(e.detail&&revision!==e.detail.revision){const before=revision;revision=e.detail.revision;if(before>=0&&config)for(const k of ['news','crypto','weather','quakes'])refreshOne(k,true);}});
window.addEventListener('nexus:view',e=>{$('world-widget').classList.toggle('world-hidden',e.detail!=='world');if(e.detail!=='world'){globe?.pauseAnimation();stopRotation();}else{sceneVisibility();resumeIdle();for(const k of ['news','crypto','weather','quakes'])refreshOne(k);}});
document.addEventListener('visibilitychange',()=>{$('world-widget').classList.toggle('world-hidden',document.hidden);if(document.hidden){globe?.pauseAnimation();stopRotation();}else{sceneVisibility();resumeIdle();for(const k of ['news','crypto','weather','quakes'])refreshOne(k);}});reduced.addEventListener('change',()=>{stopRotation();updateLayers();resumeIdle();});
$('world-home').onclick=()=>{selected=byId.get('BGD')||null;$('world-country-name').textContent=home().name;$('world-country-info').textContent='Fixed configured Home · not device tracking';chooseLocation({...home()});paintNews();};$('world-reset').onclick=()=>{selected=null;box=[0,0,1000,500];if(flat)drawFlat();else globe?.pointOfView({lat:15,lng:65,altitude:1.7},reduced.matches?0:1200);paintNews();resumeIdle();};$('world-zoom-in').onclick=()=>zoom(.75);$('world-zoom-out').onclick=()=>zoom(1.3);$('world-flat').onclick=()=>setFlat(!flat);
$('world-fullscreen').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await $('world-widget').requestFullscreen();}catch{$('world-status').textContent='Browser refused full-screen; map still works';}};
$('world-jump').onclick=()=>{const q=$('world-search').value.trim().toLocaleLowerCase();const c=countries.find(c=>[c.name,c.bn,c.id,c.iso2].some(n=>n?.toLocaleLowerCase()===q));if(c)chooseCountry(c);else $('world-status').textContent='Choose an exact country name from search suggestions';};$('world-search').addEventListener('keydown',e=>{if(e.key==='Enter')$('world-jump').click();});
$('world-language').onclick=()=>{language=language==='en'?'bn':'en';const options=$('world-countries').children;countries.forEach((c,i)=>{if(options[i])options[i].value=nameOf(c);});if(selected)$('world-country-name').textContent=nameOf(selected);if(globe)globe.htmlElement(globe.htmlElement());scheduleLabels();if(flat)drawFlat();};$('world-clear-country').onclick=()=>{selected=null;$('world-country-name').textContent='All countries';$('world-country-info').textContent='Country focus cleared. Weather remains labelled by location.';paintNews();};for(const n of document.querySelectorAll('[data-layer]'))n.onchange=()=>{layers[n.dataset.layer]=n.checked;updateLayers();};for(const n of document.querySelectorAll('[data-tab]'))n.onclick=()=>{tab=n.dataset.tab;for(const b of document.querySelectorAll('[data-tab]')){b.setAttribute('aria-selected',String(b===n));b.classList.toggle('secondary',b!==n);}paintNews();};
let tickerRunning=true;$('world-ticker-pause').onclick=()=>{tickerRunning=!tickerRunning;$('world-ticker-pause').textContent=tickerRunning?'Pause ticker':'Resume ticker';paintNews();};$('world-favorite').onclick=()=>{if(!location)return;if(!favorites.some(f=>f.latitude===location.latitude&&f.longitude===location.longitude)){favorites.push({...location,name:location.name.slice(0,120)});favorites=favorites.slice(-8);saveFavorites();paintFavorites();}};
$('world-city-form').onsubmit=async e=>{e.preventDefault();if(!api)return;const q=$('world-city').value.trim(),gen=session;try{const result=await api('cities?q='+encodeURIComponent(q));if(gen!==session)return;$('world-city-results').replaceChildren();for(const c of result.data||[]){const b=el('button',c.name,'secondary');b.onclick=()=>chooseLocation(c);$('world-city-results').append(b);}if(!result.data?.length)$('world-city-results').textContent=freshness(result)+' · no city results';}catch{$('world-city-results').textContent='City lookup unavailable';}};
const clock=()=>{$('world-clock').textContent=new Intl.DateTimeFormat('en',{timeZone:'Asia/Dhaka',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date());};clock();setInterval(()=>{if(!document.hidden)clock();},1000);
setInterval(()=>{if(initialised&&!document.hidden){if(!flat&&globe)globe.pathsData(paths());updateSun();}},60000);
window.dispatchEvent(new Event('hud:world-ready'));
