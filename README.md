# VASUDHA

Geospatial intelligence for smarter watershed development. This repository contains a responsive 19-screen watershed application built from `SIH_PS2_PDF.pdf`, a Python HTTP API, SQLite storage, GeoJSON demo layers and an interactive Leaflet map.

## Run locally

Requires Python 3.10 or later. The API and database use Python's standard library; Leaflet and map tiles load from their public providers when a network connection is available.

```powershell
python backend/server.py
```

Open <http://127.0.0.1:8000>. SQLite initializes itself in `data/vasudha.sqlite3` and creates the two local demo accounts on first run.

## Local development accounts

These accounts are for local development only. Change the passwords and session secret before deployment.

| Role | Email | Password |
|---|---|---|
| Coordinator (Organization) | organization@vasudha.local | ChangeMe123! |
| User | user@vasudha.local | ChangeMe123! |

New accounts can also be registered from the login screen. Passwords are stored as salted scrypt hashes. Login creates a random server-side session with an HttpOnly, SameSite=Lax cookie. `POST /api/photos` checks authentication and role on the server: unauthenticated requests receive 401; signed-in User requests receive 403; Organization uploads are checked for required coordinates, extension, image signature and an 8 MB limit. Uploaded files receive random names in `data/uploads/`.

## PDF page-to-screen map

See [screen-map.md](screen-map.md) for the full Page 1–19 checklist. The bundled GIS layers, watershed metrics and intervention records remain DEMO data. Analytics now includes an area search that loads the selected OpenStreetMap boundary and mapped features at runtime. Land use, vegetation, water and drainage summaries count mapped OSM objects; drainage length is calculated from returned line geometry. OSM coverage can be incomplete, and the current feature query is capped at 1,200 objects. These summaries are not satellite-derived measurements.

Soil moisture, land degradation severity, satellite vegetation indices, time-series change and intervention outcomes are not available from the connected OSM query. Analytics reports these as unavailable rather than filling them with demo values. Connect verified Earth observation and field datasets to provide those measurements.


Organization (Coordinator) photo uploads are stored with their submitted GPS coordinates and metadata. The Watershed Intervention Map reads those saved photos and displays a distinct photo marker at each coordinate; selecting a marker opens the uploaded image and record details. Intervention markers seeded in the local database are illustrative demo records.

## Map data and tile attribution

The Leaflet map switches between OpenStreetMap street tiles and Esri World Imagery without removing the watershed, thematic, intervention or photo overlays. GeoJSON lives in `data/geojson/`. OpenStreetMap and Esri attributions are shown on the map. Provider terms and usage limits apply to the public tile services.

## Main routes

- `GET /api/health`, `/api/auth/me`, `/api/watersheds`, `/api/watersheds/{id}`
- `POST /api/auth/register`, `/api/auth/login`, `/api/auth/logout`
- `GET /api/maps/{layer}`, `/api/analytics`, `/api/interventions`, `/api/interventions/{id}`
- `GET /api/photos`, `POST /api/photos`, `DELETE /api/photos/{id}`
- `GET /api/reports`, `/api/reports/{id}`, `POST /api/reports/export`

`POST /api/reports/export` returns the locally stored watershed, intervention and photo records as JSON. The Area Report screen instead searches for a boundary, reports the selected area's returned OpenStreetMap features, and exports that area-specific snapshot as JSON or browser Print / Save as PDF.

## Configuration

Set `VASUDHA_HOST`, `PORT` (or `VASUDHA_PORT` locally) or `VASUDHA_SESSION_SECRET` before starting the server. The server binds to all interfaces by default; use a high-entropy session secret and serve public traffic behind HTTPS.

## Deploy to Render

The included `render.yaml` defines a Python web service with a persistent disk mounted at `data/` for the SQLite database and uploaded photos. The service uses a generated session secret and disables the local development accounts. The Render disk requires a paid web service plan.

1. Push this project to a GitHub repository.
2. In Render, choose **New > Blueprint**, connect the repository, and deploy the `render.yaml` Blueprint.
3. When the service is live, open its `onrender.com` URL and create an account.

Render deploys from the connected Git repository, so local files must be committed and pushed before the Blueprint can use them. To use a custom domain, add it in the Render service settings after deployment.

## Live map search

Map pages start with Esri World Imagery satellite tiles. Searching is user-triggered: choose a returned OpenStreetMap administrative boundary to draw its outline and request mapped land use, waterways, vegetation and water features within that boundary. Results are OpenStreetMap data (not satellite-derived classifications), may be incomplete, and require an internet connection. OSM attribution is displayed on the map. The geocoder is cached, limited to one request per second, and can be switched with `VASUDHA_GEOCODER_URL`; the Overpass endpoint can be changed with `VASUDHA_OVERPASS_URL`. Use an appropriately provisioned provider for production traffic. Soil moisture, degradation measurements, and intervention records still require verified source datasets; the bundled indicators remain illustrative demo values.
