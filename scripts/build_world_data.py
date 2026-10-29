"""Rebuild bundled public assets from pinned GitHub blobs; no runtime download/CDN.

Run npm ci --prefix frontend/world first, then python scripts/build_world_data.py.
Natural Earth public domain; imagery and library attribution: docs/world-widget.md.
"""
import base64
import json
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'nexus/hud/world/assets'
BLOBS = {
    'NotoSansBengali-Regular.ttf': ('notofonts/noto-fonts', '3e66c1a9ea885b999ce9736aa3b509cc51935fff'),
    'OFL-Bengali.txt': ('notofonts/noto-fonts', 'c82d72e422e2d08c5ab439b6bac7c2177ea0c565'),
    'countries-50m.json': ('nvkelso/natural-earth-vector', '9d99f26dc470fcdbd08b062c813005acbdf73ccb'),
    'countries-110m.json': ('nvkelso/natural-earth-vector', '1e6ab74c7042f97013be69ceec798be8e1aff27d'),
    'capitals.json': ('nvkelso/natural-earth-vector', '4da9c6b27b5cd41d7c930ea9cd0c1f84548b65fb'),
    'earth-night.jpg': ('vasturiano/three-globe', '6889180889423b6bcd2c9dfed5e138bcdb5e5e8b'),
}


def simplify_ring(points, tolerance=.045):
    """Douglas–Peucker display simplification, not a political boundary dataset."""
    if len(points) <= 5:
        return points
    def segment_distance(p, a, b):
        x, y = a
        dx, dy = b[0] - x, b[1] - y
        if dx or dy:
            t = max(0., min(1., ((p[0]-x)*dx+(p[1]-y)*dy)/(dx*dx+dy*dy)))
            x, y = x+t*dx, y+t*dy
        return (p[0]-x)**2+(p[1]-y)**2
    keep, stack = {0, len(points)-1}, [(0, len(points)-1)]
    while stack:
        a, b = stack.pop()
        if b-a <= 1:
            continue
        index = max(range(a+1, b), key=lambda i: segment_distance(points[i], points[a], points[b]))
        if segment_distance(points[index], points[a], points[b]) > tolerance*tolerance:
            keep.add(index)
            stack.extend([(a, index), (index, b)])
    result = [points[i] for i in sorted(keep)]
    return result if len(result) >= 4 else points


def main():
    raw = {}
    for name, (repo, sha) in BLOBS.items():
        request = urllib.request.Request(f'https://api.github.com/repos/{repo}/git/blobs/{sha}', headers={'User-Agent': 'Nexus-HUD-asset-build'})
        with urllib.request.urlopen(request, timeout=90) as response:
            raw[name] = base64.b64decode(json.load(response)['content'])
    capitals = {}
    for feature in json.loads(raw['capitals.json'])['features']:
        p = feature['properties']
        if p.get('adm0cap') or p.get('featurecla', '').startswith('Admin-0'):
            capitals.setdefault(p['adm0_a3'], p)
    rows = []
    vertices = {}
    neighbors = {}
    for f in json.loads(raw['countries-50m.json'])['features']:
        ident = f['properties']['ADM0_A3']
        neighbors[ident] = set()
        polys = [f['geometry']['coordinates']] if f['geometry']['type'] == 'Polygon' else f['geometry']['coordinates']
        for poly in polys:
            for ring in poly:
                for x, y in ring:
                    vertices.setdefault((round(x, 3), round(y, 3)), set()).add(ident)
    for group in vertices.values():
        for ident in group:
            neighbors[ident].update(group - {ident})
    colors = {}
    for ident in sorted(neighbors, key=lambda k: (-len(neighbors[k]), k)):
        used = {colors[k] for k in neighbors[ident] if k in colors}
        colors[ident] = next((i for i in range(8) if i not in used), len(colors) % 8)
    for feature in json.loads(raw['countries-50m.json'])['features']:
        p = feature['properties']
        cap = capitals.get(p['ADM0_A3'])
        if not cap:
            cap = next((v for v in capitals.values() if v.get('iso_a2') == p['ISO_A2'] and p['ISO_A2'] != '-99'), None)
        # Never invent a capital/coordinate when absent from the source.
        row = dict(id=p['ADM0_A3'], color=colors[p['ADM0_A3']], name=p['NAME_EN'] or p['ADMIN'], bn=p['NAME_BN'], iso2=p['ISO_A2'],
                   population=p['POP_EST'], population_year=p['POP_YEAR'], rank=p['LABELRANK'],
                   lat=p['LABEL_Y'], lon=p['LABEL_X'], capital=cap['name'] if cap else None,
                   capital_lat=cap['latitude'] if cap else None, capital_lon=cap['longitude'] if cap else None)
        rows.append(row)
    node = "const fs=require('fs'),tz=require('./frontend/world/node_modules/tz-lookup');const a=JSON.parse(fs.readFileSync(0,'utf8'));for(const c of a)c.timezone=tz(c.capital_lat??c.lat,c.capital_lon??c.lon);process.stdout.write(JSON.stringify(a))"
    rows = json.loads(subprocess.check_output(['node', '-e', node], cwd=ROOT, input=json.dumps(rows).encode()))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'countries.json').write_text(json.dumps(rows, ensure_ascii=False, separators=(',', ':')))
    for source, dest in [('countries-50m.json', 'countries-detail.geojson'), ('countries-110m.json', 'countries-low.geojson')]:
        data = json.loads(raw[source])
        for feature in data['features']:
            if dest == 'countries-detail.geojson':
                geom = feature['geometry']
                if geom['type'] == 'Polygon':
                    geom['coordinates'] = [simplify_ring(ring) for ring in geom['coordinates']]
                else:
                    geom['coordinates'] = [[simplify_ring(ring) for ring in poly] for poly in geom['coordinates']]
            feature['properties'] = {'id': feature['properties']['ADM0_A3']}
        (OUT / dest).write_text(json.dumps(data, separators=(',', ':')))
    for name in ('earth-night.jpg', 'NotoSansBengali-Regular.ttf', 'OFL-Bengali.txt'):
        (OUT / name).write_bytes(raw[name])
    (OUT / 'provenance.json').write_text(json.dumps(BLOBS, indent=2))
    print(f'Bundled {len(rows)} admin-0 map units; {sum(bool(r["capital"]) for r in rows)} capitals')


if __name__ == '__main__':
    main()
