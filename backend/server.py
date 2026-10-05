"""VASUDHA demo application: stdlib HTTP + SQLite backend."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen
import hashlib, hmac, json, mimetypes, os, secrets, sqlite3, time, uuid, threading
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / 'data' / 'vasudha.sqlite3'
UPLOADS = ROOT / 'data' / 'uploads'
WEB = ROOT / 'frontend'
UPLOADS.mkdir(parents=True, exist_ok=True)
DB.parent.mkdir(parents=True, exist_ok=True)
SECRET = os.environ.get('VASUDHA_SESSION_SECRET', 'local-demo-secret-change-before-deployment')
MAX_UPLOAD = 20 * 1024 * 1024
PLACE_CACHE = {}
PLACE_LOCK = threading.Lock()
LAST_GEOCODE = 0.0

def external_json(url, data=None, headers=None, timeout=35):
    request = Request(url, data=data, headers={'User-Agent': 'VASUDHA-Watershed-Intelligence/1.0 (local GIS viewer)', **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))

def geocode_places(query):
    global LAST_GEOCODE
    key = query.casefold()
    cached = PLACE_CACHE.get(('search', key))
    if cached and time.time() - cached[0] < 86400:
        return cached[1]
    with PLACE_LOCK:
        wait = 1.05 - (time.monotonic() - LAST_GEOCODE)
        if wait > 0:
            time.sleep(wait)
        endpoint = os.environ.get('VASUDHA_GEOCODER_URL', 'https://nominatim.openstreetmap.org/search')
        params = urlencode({'q': query, 'format': 'jsonv2', 'polygon_geojson': 1, 'limit': 6, 'addressdetails': 1})
        result = external_json(f'{endpoint}?{params}')
        LAST_GEOCODE = time.monotonic()
    places = []
    for item in result:
        geometry = item.get('geojson')
        if not geometry or geometry.get('type') not in ('Polygon', 'MultiPolygon') or item.get('osm_type') not in ('relation', 'way'):
            continue
        places.append({'id': item['osm_id'], 'osm_type': item['osm_type'], 'name': item.get('name') or item.get('display_name', '').split(',')[0], 'display_name': item.get('display_name', ''), 'type': item.get('type', ''), 'geometry': geometry, 'bbox': item.get('boundingbox'), 'lat': float(item['lat']) if item.get('lat') else None, 'lon': float(item['lon']) if item.get('lon') else None})
    PLACE_CACHE[('search', key)] = (time.time(), places)
    return places

def _inside_boundary(lon, lat, boundary):
    def ring_contains(ring):
        inside = False
        if not isinstance(ring, list) or len(ring) < 4: return False
        for i, point in enumerate(ring):
            previous = ring[i - 1]
            xi, yi = point[:2]; xj, yj = previous[:2]
            if ((yi > lat) != (yj > lat)) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
                inside = not inside
        return inside
    polygons = boundary.get('coordinates', [])
    if boundary.get('type') == 'Polygon': polygons = [polygons]
    if boundary.get('type') != 'MultiPolygon' and boundary.get('type') != 'Polygon': return True
    for polygon in polygons:
        if polygon and ring_contains(polygon[0]) and not any(ring_contains(hole) for hole in polygon[1:]):
            return True
    return False

def _feature_intersects_boundary(geometry, boundary):
    def points(coords):
        if isinstance(coords, (list, tuple)) and len(coords) >= 2 and all(isinstance(x, (int, float)) for x in coords[:2]):
            return [(coords[0], coords[1])]
        found = []
        for item in coords if isinstance(coords, (list, tuple)) else []: found.extend(points(item))
        return found
    coords = points(geometry.get('coordinates', []))
    return any(_inside_boundary(lon, lat, boundary) for lon, lat in coords)

def osm_features(osm_id, osm_type='relation', bbox=None, boundary=None):
    cache_bbox = ','.join(map(str, bbox)) if isinstance(bbox, (list, tuple)) else str(bbox or '')
    key = ('features', f'{osm_type}/{osm_id}/{cache_bbox}/{"clipped" if boundary else "bbox"}')
    cached = PLACE_CACHE.get(key)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    bbox_filter = ''
    try:
        if isinstance(bbox, str): bbox = bbox.split(',')
        if bbox and len(bbox) == 4:
            south, north, west, east = map(float, bbox)
            if -90 <= south < north <= 90 and -180 <= west < east <= 180:
                bbox_filter = f'({south},{west},{north},{east})'
    except (TypeError, ValueError):
        bbox_filter = ''
    # The area operator times out on some hosted Overpass mirrors for large
    # districts. A boundary bounding-box filter uses their spatial index, and
    # returned features are clipped against the selected polygon below.
    if not bbox_filter: raise ValueError('Selected area must include a valid bounding box.')
    query = f'''[out:json][timeout:25];(nwr["waterway"~"^(river|stream|canal|drain|ditch)$"]{bbox_filter};nwr["natural"~"^(water|wetland|wood|scrub|grassland)$"]{bbox_filter};nwr["water"]{bbox_filter};nwr["landuse"~"^(forest|farmland|meadow|orchard|vineyard)$"]{bbox_filter};);out geom qt 1200;'''
    data = urlencode({'data': query}).encode()
    # A single public Overpass instance is not reliable enough for a hosted app:
    # Render may be unable to route to one host, or that instance may be busy.
    # Keep the existing override first, then fail over to other global instances.
    configured = os.environ.get('VASUDHA_OVERPASS_URL', '').strip()
    fallback_urls = os.environ.get('VASUDHA_OVERPASS_FALLBACK_URLS', '').strip()
    endpoints = [configured or 'https://overpass-api.de/api/interpreter']
    if fallback_urls:
        endpoints.extend(url.strip() for url in fallback_urls.split(',') if url.strip())
    else:
        endpoints.extend([
            'https://overpass.private.coffee/api/interpreter',
            'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
            'https://lz4.overpass-api.de/api/interpreter',
        ])
    endpoints = list(dict.fromkeys(endpoints))
    errors = []
    result = None
    for endpoint in endpoints:
        try:
            result = external_json(endpoint, data=data, headers={'Content-Type': 'application/x-www-form-urlencoded'}, timeout=35)
            break
        except Exception as exc:
            errors.append(f'{endpoint}: {exc}')
    if result is None:
        raise RuntimeError('all OpenStreetMap feature providers failed (' + '; '.join(errors) + ')')
    features = []
    for element in result.get('elements', []):
        tags = element.get('tags', {})
        kind = element.get('type')
        if kind == 'node' and 'lat' in element:
            geometry = {'type': 'Point', 'coordinates': [element['lon'], element['lat']]}
        elif kind == 'way' and element.get('geometry'):
            points = [[p['lon'], p['lat']] for p in element['geometry']]
            if len(points) < 2: continue
            if len(points) >= 4 and points[0] == points[-1]: geometry = {'type': 'Polygon', 'coordinates': [points]}
            else: geometry = {'type': 'LineString', 'coordinates': points}
        elif kind == 'relation':
            lines = [[[p['lon'], p['lat']] for p in member.get('geometry', [])] for member in element.get('members', []) if member.get('role') in ('outer', '') and member.get('geometry')]
            lines = [line for line in lines if len(line) > 1]
            if not lines: continue
            geometry = {'type': 'MultiLineString', 'coordinates': lines}
        else:
            continue
        feature_type = 'drainage' if 'waterway' in tags else 'water-bodies' if 'water' in tags or tags.get('natural') in ('water', 'wetland') else 'vegetation' if tags.get('natural') in ('wood', 'scrub', 'grassland') or tags.get('landuse') in ('forest', 'orchard', 'vineyard') else 'landuse'
        feature = {'type': 'Feature', 'id': f"{kind}/{element['id']}", 'properties': {'name': tags.get('name') or tags.get('waterway') or tags.get('natural') or tags.get('landuse') or 'Mapped feature', 'category': tags.get('natural') or tags.get('landuse') or tags.get('waterway') or tags.get('water') or 'feature', 'source': 'OpenStreetMap', 'layer': feature_type, **{k:v for k,v in tags.items() if k in ('name','waterway','natural','landuse','water','surface')}}, 'geometry': geometry}
        if not boundary or _feature_intersects_boundary(geometry, boundary): features.append(feature)
    collection = {'type': 'FeatureCollection', 'features': features}
    PLACE_CACHE[key] = (time.time(), collection)
    return collection

def connect():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; c.execute('PRAGMA foreign_keys=ON'); return c

def init_db():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','organization')), password_hash TEXT NOT NULL, org_name TEXT, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS organizations(id INTEGER PRIMARY KEY, name TEXT NOT NULL, user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS watersheds(id INTEGER PRIMARY KEY, name TEXT NOT NULL, district TEXT, state TEXT, area_km2 REAL, geometry TEXT);
        CREATE TABLE IF NOT EXISTS interventions(id INTEGER PRIMARY KEY, name TEXT, type TEXT, lat REAL, lng REAL, location TEXT, date TEXT, status TEXT, description TEXT, watershed_id INTEGER REFERENCES watersheds(id), is_demo INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS photos(id INTEGER PRIMARY KEY, filename TEXT NOT NULL, original_name TEXT NOT NULL, location TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, taken_at TEXT NOT NULL, description TEXT, watershed_id INTEGER REFERENCES watersheds(id), intervention_id INTEGER REFERENCES interventions(id), uploader_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS land_use(id INTEGER PRIMARY KEY, category TEXT, area_km2 REAL, share REAL, watershed_id INTEGER REFERENCES watersheds(id));
        CREATE TABLE IF NOT EXISTS drainage(id INTEGER PRIMARY KEY, stream_order INTEGER, length_km REAL, watershed_id INTEGER REFERENCES watersheds(id));
        CREATE TABLE IF NOT EXISTS vegetation(id INTEGER PRIMARY KEY, category TEXT, area_km2 REAL, share REAL, watershed_id INTEGER REFERENCES watersheds(id));
        CREATE TABLE IF NOT EXISTS soil_moisture(id INTEGER PRIMARY KEY, category TEXT, area_km2 REAL, share REAL, watershed_id INTEGER REFERENCES watersheds(id));
        CREATE TABLE IF NOT EXISTS water_bodies(id INTEGER PRIMARY KEY, category TEXT, count INTEGER, area_km2 REAL, watershed_id INTEGER REFERENCES watersheds(id));
        CREATE TABLE IF NOT EXISTS land_degradation(id INTEGER PRIMARY KEY, severity TEXT, area_km2 REAL, share REAL, watershed_id INTEGER REFERENCES watersheds(id));
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY, name TEXT, watershed_id INTEGER REFERENCES watersheds(id), generated_at TEXT, payload TEXT);
        ''')
        photo_columns={r['name'] for r in c.execute('PRAGMA table_info(photos)')}
        if 'time' not in photo_columns:c.execute("ALTER TABLE photos ADD COLUMN time TEXT NOT NULL DEFAULT ''")
        if 'phase' not in photo_columns:c.execute("ALTER TABLE photos ADD COLUMN phase TEXT NOT NULL DEFAULT ''")
        intervention_columns={r['name'] for r in c.execute('PRAGMA table_info(interventions)')}
        if 'is_demo' not in intervention_columns:
            c.execute('ALTER TABLE interventions ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0')
            demo_sites=[(22.731,75.856),(22.742,75.879),(22.711,75.891),(22.759,75.824),(22.776,75.873),(22.698,75.838)]
            c.executemany('UPDATE interventions SET is_demo=1 WHERE lat=? AND lng=?',demo_sites)
        if not c.execute('SELECT 1 FROM watersheds LIMIT 1').fetchone():
            c.execute('INSERT INTO watersheds(name,district,state,area_km2) VALUES(?,?,?,?)',('Bhagir Watershed','Dhar','Madhya Pradesh',246.8))
            wid = c.execute('SELECT id FROM watersheds LIMIT 1').fetchone()[0]
            if os.environ.get('VASUDHA_DISABLE_DEMO_ACCOUNTS','').lower() not in ('1','true','yes'):
                for email,name,role,pw,org in [('organization@vasudha.local','VASUDHA Field Team','organization','ChangeMe123!','VASUDHA Field Organization'),('user@vasudha.local','Demo Analyst','user','ChangeMe123!',None)]:
                    salt=secrets.token_bytes(16); digest=hashlib.scrypt(pw.encode(),salt=salt,n=2**14,r=8,p=1); c.execute('INSERT INTO users(email,name,role,password_hash,org_name,created_at) VALUES(?,?,?,?,?,?)',(email,name,role,salt.hex()+':'+digest.hex(),org,datetime.now(timezone.utc).isoformat()))
                    if role=='organization':c.execute('INSERT INTO organizations(name,user_id,created_at) VALUES(?,?,?)',(org,c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()[0],datetime.now(timezone.utc).isoformat()))
            sample=[('Afforestation','Afforestation',22.731,75.856,'Mhow Block','2025-11-14','Completed','Community plantation across degraded slopes.'),('Check Dam','Check Dam',22.742,75.879,'Jamli Village','2025-08-22','Completed','Masonry check dam with seasonal storage.'),('Contour Trenching','Contour Trenching',22.711,75.891,'Kodariya Ridge','2025-12-03','Ongoing','Contour trenches reduce runoff and improve infiltration.'),('Water Harvesting Structure','Water Harvesting Structure',22.759,75.824,'Pithampur','2026-01-17','Planned','Rainwater harvesting structure planned with village committee.'),('Afforestation','Afforestation',22.776,75.873,'Gawli Palasia','2025-09-02','Completed','Native species planted in community reserve.'),('Check Dam','Check Dam',22.698,75.838,'Berchha','2025-07-19','Ongoing','Small check dam rehabilitation.')]
            for x in sample:c.execute('INSERT INTO interventions(name,type,lat,lng,location,date,status,description,watershed_id,is_demo) VALUES(?,?,?,?,?,?,?,?,?,1)',(*x,wid))
            for table,rows,cols in [('land_use',[('Forest',74.0,30),('Agriculture',91.3,37),('Barren Land',34.6,14),('Built-up Area',22.2,9),('Water Body',24.7,10)],('category','area_km2','share')),('vegetation',[('Dense Vegetation',64.2,26),('Moderate Vegetation',103.7,42),('Sparse Vegetation',78.9,32)],('category','area_km2','share')),('soil_moisture',[('Very High',24.7,10),('High',54.3,22),('Moderate',98.7,40),('Low',49.4,20),('Very Low',19.7,8)],('category','area_km2','share')),('land_degradation',[('Very High',19.7,8),('High',44.4,18),('Moderate',71.6,29),('Low',81.4,33),('Very Low',29.7,12)],('severity','area_km2','share'))]:
                for a,b,d in rows:c.execute(f'INSERT INTO {table}({cols[0]},{cols[1]},{cols[2]},watershed_id) VALUES(?,?,?,?)',(a,b,d,wid))
            for order,length in [(1,38.4),(2,52.7),(3,41.2),(4,26.8),(5,13.5)]:c.execute('INSERT INTO drainage(stream_order,length_km,watershed_id) VALUES(?,?,?)',(order,length,wid))
            for cat,count,area in [('Reservoir',2,1.8),('Check Dam',14,0.9),('Pond',8,1.2),('Seasonal Stream',21,7.6)]:c.execute('INSERT INTO water_bodies(category,count,area_km2,watershed_id) VALUES(?,?,?,?)',(cat,count,area,wid))
            c.execute('INSERT INTO reports(name,watershed_id,generated_at,payload) VALUES(?,?,?,?)',('District Watershed Report',wid,datetime.now(timezone.utc).isoformat(),'{}'))
        org_user=c.execute("SELECT id,org_name,created_at FROM users WHERE role='organization' ORDER BY id LIMIT 1").fetchone()
        if org_user and not c.execute('SELECT 1 FROM organizations WHERE user_id=?',(org_user['id'],)).fetchone():
            c.execute('INSERT INTO organizations(name,user_id,created_at) VALUES(?,?,?)',(org_user['org_name'] or 'VASUDHA Organization',org_user['id'],org_user['created_at']))

def pw_ok(stored, pw):
    try:
        salt,dig=stored.split(':'); return hmac.compare_digest(bytes.fromhex(dig),hashlib.scrypt(pw.encode(),salt=bytes.fromhex(salt),n=2**14,r=8,p=1))
    except Exception:return False

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(WEB),**kw)
    def log_message(self,fmt,*args): print('%s - %s'%(self.address_string(),fmt%args))
    def send_json(self,obj,status=200):
        b=json.dumps(obj,allow_nan=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def body_json(self):
        try:return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        except Exception:return {}
    def viewer(self):
        raw=self.headers.get('Cookie',''); token=None
        for part in raw.split(';'):
            k,_,v=part.strip().partition('=')
            if k=='vasudha_session':token=v
        if not token:return None
        with connect() as c:
            row=c.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?',(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone()
        return dict(row) if row else None
    def do_GET(self):
        path=urlparse(self.path).path
        if path.startswith('/api/'):
            if path == '/api/places/search':
                query = parse_qs(urlparse(self.path).query).get('q', [''])[0].strip()
                if len(query) < 2: return self.send_json({'error':'Enter at least two characters to search for a mapped area.'},400)
                try: return self.send_json({'items':geocode_places(query),'source':'OpenStreetMap Nominatim'})
                except Exception as exc: return self.send_json({'error':f'Place search is temporarily unavailable: {exc}'},502)
            if path == '/api/places/features':
                try:
                    params = parse_qs(urlparse(self.path).query)
                    osm_id = params.get('id',[''])[0]
                    osm_type = params.get('type',['relation'])[0]
                    bbox = params.get('bbox',[''])[0].split(',')
                    if not osm_id.isdigit() or osm_type not in ('relation','way'): return self.send_json({'error':'Select a mapped boundary first.'},400)
                    if len(bbox)!=4 or not all(bbox): return self.send_json({'error':'Selected area must include a valid bounding box.'},400)
                    return self.send_json({'data':osm_features(osm_id,osm_type,bbox),'source':'OpenStreetMap'})
                except Exception as exc: return self.send_json({'error':f'OpenStreetMap features are temporarily unavailable: {exc}'},502)
            if path=='/api/health':return self.send_json({'status':'ok','database':'sqlite','data_sources':['OpenStreetMap','Coordinator field records'],'demo_seed_data_served':False})
            if path=='/api/auth/me':
                u=self.viewer(); return self.send_json({'user':{k:u[k] for k in ('id','email','name','role','org_name')} if u else None})
            if path=='/api/watersheds':
                return self.send_json({'items':[],'source':'No verified watershed boundary is registered. Search for an OpenStreetMap boundary.'})
            if path.startswith('/api/watersheds/'):
                try:wid=int(path.rsplit('/',1)[1])
                except ValueError:return self.send_json({'error':'Invalid watershed id'},400)
                return self.send_json({'error':'No verified watershed boundary is registered.'},404)
            if path=='/api/analytics':
                return self.send_json({'error':'Select a mapped area to load real OpenStreetMap features. Soil moisture and degradation measurement datasets are not connected.'},410)
            if path=='/api/interventions':
                with connect() as c:rows=[dict(x) for x in c.execute('SELECT * FROM interventions WHERE is_demo=0 ORDER BY date DESC,id DESC')]
                return self.send_json({'items':rows,'source':'Coordinator field records'})
            if path.startswith('/api/interventions/'):
                try:iid=int(path.rsplit('/',1)[1])
                except ValueError:return self.send_json({'error':'Invalid intervention id'},400)
                with connect() as c:row=c.execute('SELECT * FROM interventions WHERE id=? AND is_demo=0',(iid,)).fetchone()
                return self.send_json({'intervention':dict(row)} if row else {'error':'Intervention not found'},200 if row else 404)
            if path=='/api/photos':
                with connect() as c:rows=[dict(x) for x in c.execute('SELECT p.*,i.type AS intervention,i.location AS intervention_location,i.status AS intervention_status FROM photos p LEFT JOIN interventions i ON i.id=p.intervention_id AND i.is_demo=0 ORDER BY p.id DESC')]
                return self.send_json({'items':rows})
            if path.startswith('/api/uploads/'):
                file=UPLOADS / Path(path).name
                if file.exists():
                    self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'application/octet-stream'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Content-Length',str(file.stat().st_size)); self.end_headers(); self.wfile.write(file.read_bytes()); return
                return self.send_json({'error':'Photo not found'},404)
            if path.startswith('/api/maps/'):
                topic=path.rsplit('/',1)[-1]
                return self.send_json({'error':f'Bundled sample layer "{topic}" is not served. Search and select an area to load OpenStreetMap features.'},410)
            if path=='/api/reports':
                return self.send_json({'items':[],'note':'Generate an area report after selecting a boundary; no saved sample reports are served.'})
            if path.startswith('/api/reports/'):
                try:rid=int(path.rsplit('/',1)[1])
                except ValueError:return self.send_json({'error':'Invalid report id'},400)
                return self.send_json({'error':'Saved sample reports are not served. Generate a report from a selected area.'},404)
            return self.send_json({'error':'Not found'},404)
        if path.startswith('/api/'):return self.send_json({'error':'Not found'},404)
        if path=='/':self.path='/index.html'
        return super().do_GET()
    def do_POST(self):
        path=urlparse(self.path).path
        if path == '/api/places/features':
            try:
                if int(self.headers.get('Content-Length','0')) > 2 * 1024 * 1024: return self.send_json({'error':'Selected boundary is too large to query.'},413)
                d=self.body_json(); osm_id=str(d.get('id','')); osm_type=d.get('type','relation')
                bbox=d.get('bbox'); boundary=d.get('geometry')
                if not osm_id.isdigit() or osm_type not in ('relation','way'): return self.send_json({'error':'Select a mapped boundary first.'},400)
                if not isinstance(bbox,list) or len(bbox)!=4 or not isinstance(boundary,dict) or boundary.get('type') not in ('Polygon','MultiPolygon'):
                    return self.send_json({'error':'The selected boundary is missing its map geometry.'},400)
                return self.send_json({'data':osm_features(osm_id,osm_type,bbox,boundary),'source':'OpenStreetMap'})
            except Exception as exc: return self.send_json({'error':f'OpenStreetMap features are temporarily unavailable: {exc}'},502)
        if path=='/api/auth/register':
            d=self.body_json(); email=str(d.get('email','')).strip().lower(); pw=str(d.get('password','')); role=d.get('role','user')
            if role not in ('user','organization') or not email or len(pw)<10:return self.send_json({'error':'Enter a valid email and a password of at least 10 characters.'},400)
            salt=secrets.token_bytes(16); digest=hashlib.scrypt(pw.encode(),salt=salt,n=2**14,r=8,p=1)
            try:
                with connect() as c:
                    c.execute('INSERT INTO users(email,name,role,password_hash,org_name,created_at) VALUES(?,?,?,?,?,?)',(email,str(d.get('name') or email.split('@')[0]),role,salt.hex()+':'+digest.hex(),d.get('organization') if role=='organization' else None,datetime.now(timezone.utc).isoformat()))
                    if role=='organization':c.execute('INSERT INTO organizations(name,user_id,created_at) VALUES(?,?,?)',(str(d.get('organization') or 'Organization').strip(),c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()[0],datetime.now(timezone.utc).isoformat()))
                return self.send_json({'created':True},201)
            except sqlite3.IntegrityError:return self.send_json({'error':'An account with that email already exists.'},409)
        if path=='/api/auth/login':
            d=self.body_json(); email=str(d.get('email','')).strip().lower()
            with connect() as c:u=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            if not u or not pw_ok(u['password_hash'],str(d.get('password',''))):return self.send_json({'error':'Email or password was not recognized.'},401)
            token=secrets.token_urlsafe(32); th=hashlib.sha256(token.encode()).hexdigest()
            with connect() as c:c.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(th,u['id'],int(time.time())+60*60*24*7))
            self.send_response(200); b=json.dumps({'user':{k:u[k] for k in ('id','email','name','role','org_name')}}).encode(); self.send_header('Content-Type','application/json'); secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else ''; self.send_header('Set-Cookie',f'vasudha_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800{secure}'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        if path=='/api/auth/logout':
            token=None
            for p in self.headers.get('Cookie','').split(';'):
                k,_,v=p.strip().partition('=')
                if k=='vasudha_session':token=v
            if token:
                with connect() as c:c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
            self.send_response(200); self.send_header('Set-Cookie','vasudha_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'); self.send_header('Content-Length','2'); self.end_headers(); self.wfile.write(b'{}'); return
        if path=='/api/interventions':
            user=self.viewer()
            if not user:return self.send_json({'error':'Sign in with a Coordinator account to record an intervention.'},401)
            if user['role']!='organization':return self.send_json({'error':'Only Coordinator accounts can record intervention sites.'},403)
            d=self.body_json(); kind=str(d.get('type','')).strip(); location=str(d.get('location','')).strip(); status=str(d.get('status','')).strip().title(); date=str(d.get('date') or datetime.now().date().isoformat()).strip()
            allowed_types={'Afforestation','Check Dam','Contour Trenching','Water Harvesting Structure'}
            if kind not in allowed_types or status not in {'Planned','Ongoing','Completed'} or not location:return self.send_json({'error':'Choose an intervention type and status, and enter a location.'},400)
            try:
                lat=float(d.get('lat')); lng=float(d.get('lng'))
                if not (-90<=lat<=90 and -180<=lng<=180):raise ValueError()
                datetime.strptime(date,'%Y-%m-%d')
            except Exception:return self.send_json({'error':'Enter valid coordinates and a date in YYYY-MM-DD format.'},400)
            description=str(d.get('description') or '').strip()
            with connect() as c:
                cur=c.execute('INSERT INTO interventions(name,type,lat,lng,location,date,status,description,watershed_id,is_demo) VALUES(?,?,?,?,?,?,?,?,NULL,0)',(kind,kind,lat,lng,location,date,status,description))
                row=dict(c.execute('SELECT * FROM interventions WHERE id=?',(cur.lastrowid,)).fetchone())
            return self.send_json({'intervention':row,'source':'Coordinator field record'},201)
        if path=='/api/photos':
            user=self.viewer()
            if not user:return self.send_json({'error':'Sign in to upload photos.'},401)
            if user['role']!='organization':return self.send_json({'error':'Photo uploads are available to organization accounts only.'},403)
            length=int(self.headers.get('Content-Length','0'))
            if length>MAX_UPLOAD+1024*1024:return self.send_json({'error':'Image is too large. Maximum size is 20 MB.'},413)
            ctype=self.headers.get('Content-Type','')
            if 'multipart/form-data' not in ctype:return self.send_json({'error':'Use multipart form data.'},400)
            raw=self.rfile.read(length); boundary=ctype.split('boundary=',1)[-1].strip('"').encode(); fields={}; filedata=None; filename=''
            for part in raw.split(b'--'+boundary):
                if b'\r\n\r\n' not in part:continue
                head,content=part.split(b'\r\n\r\n',1); content=content.removesuffix(b'\r\n').removesuffix(b'--\r\n')
                h=head.decode('latin1','ignore'); name=''
                for item in h.split(';'):
                    if 'name=' in item:name=item.split('name=',1)[1].strip(' "')
                    if 'filename=' in item:filename=Path(item.split('filename=',1)[1].strip(' "')).name; filedata=content
                if name and 'filename=' not in h:fields[name]=content.decode('utf8','replace')
            if not filedata:return self.send_json({'error':'Choose an image file to upload.'},400)
            ext=Path(filename).suffix.lower(); allowed={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.webp':'image/webp'}
            if len(filedata)>MAX_UPLOAD:return self.send_json({'error':'Image is too large. Maximum size is 20 MB.'},413)
            if ext not in allowed:return self.send_json({'error':'Choose a .jpg, .jpeg, .png, or .webp image.'},400)
            if not (filedata.startswith(b'\xff\xd8\xff') if ext in ('.jpg','.jpeg') else filedata.startswith(b'\x89PNG\r\n\x1a\n') if ext=='.png' else filedata[:4]==b'RIFF' and filedata[8:12]==b'WEBP'):return self.send_json({'error':'The selected file does not match its image format.'},400)
            try:lat=float(fields['lat']); lng=float(fields['lng']); assert -90<=lat<=90 and -180<=lng<=180 and fields['location'].strip() and fields['taken_at'].strip()
            except Exception:return self.send_json({'error':'Enter a location, valid latitude and longitude, and date.'},400)
            phase=fields.get('phase','').strip().lower()
            if phase not in ('before','after','progress'):return self.send_json({'error':'Choose whether this photo shows before, after, or progress.'},400)
            safe=f'{uuid.uuid4().hex}{ext}'; (UPLOADS/safe).write_bytes(filedata)
            with connect() as c:
                intervention_id=int(fields['intervention_id']) if fields.get('intervention_id') else None
                if intervention_id and not c.execute('SELECT 1 FROM interventions WHERE id=? AND is_demo=0',(intervention_id,)).fetchone():return self.send_json({'error':'Choose a saved Coordinator intervention or leave it unassociated.'},400)
                c.execute('INSERT INTO photos(filename,original_name,location,lat,lng,taken_at,time,phase,description,watershed_id,intervention_id,uploader_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?)',(safe,filename,fields['location'].strip(),lat,lng,fields['taken_at'],fields.get('time',''),phase,fields.get('description',''),intervention_id,user['id'],datetime.now(timezone.utc).isoformat()))
                row=dict(c.execute('SELECT * FROM photos ORDER BY id DESC LIMIT 1').fetchone())
            return self.send_json({'photo':row,'url':'/api/uploads/'+safe},201)
        if path=='/api/reports/export':
            with connect() as c:
                data={'source':'Coordinator field records','interventions':[dict(x) for x in c.execute('SELECT * FROM interventions WHERE is_demo=0 ORDER BY date DESC,id DESC')],'photos':[dict(x) for x in c.execute('SELECT id,location,lat,lng,taken_at,phase,description,intervention_id FROM photos ORDER BY id DESC')]}
            return self.send_json(data)
        return self.send_json({'error':'Not found'},404)

    def do_DELETE(self):
        path=urlparse(self.path).path
        if path.startswith('/api/photos/'):
            user=self.viewer()
            if not user:return self.send_json({'error':'Sign in to manage photos.'},401)
            if user['role']!='organization':return self.send_json({'error':'Photo management is available to organization accounts only.'},403)
            try:pid=int(path.rsplit('/',1)[1])
            except ValueError:return self.send_json({'error':'Invalid photo id'},400)
            with connect() as c:row=c.execute('SELECT filename FROM photos WHERE id=?',(pid,)).fetchone()
            if not row:return self.send_json({'error':'Photo not found'},404)
            with connect() as c:c.execute('DELETE FROM photos WHERE id=?',(pid,))
            try:(UPLOADS/Path(row['filename']).name).unlink(missing_ok=True)
            except OSError:pass
            return self.send_json({'deleted':True})
        return self.send_json({'error':'Not found'},404)

if __name__=='__main__':
    init_db(); host=os.environ.get('VASUDHA_HOST','0.0.0.0'); port=int(os.environ.get('PORT',os.environ.get('VASUDHA_PORT','8000')))
    print(f'VASUDHA available at http://{host}:{port}'); ThreadingHTTPServer((host,port),Handler).serve_forever()
