# AMVelo — private bike maps

AMVelo estimates bicycle moving time from a user's own cycling history. People can create separate accounts, import a Strava archive, or connect Strava for ongoing sync. Routes and place search are limited to the island of Ireland, including Northern Ireland. A signed-in user can access only their own activities and profile; private API responses are not cached. Passwords are stored as Werkzeug password hashes, and Strava tokens, activity names, and route polylines are encrypted in SQLite using a key derived from the stable `SECRET_KEY`.

The detailed island outline in `static/ireland-boundary.geojson` is derived from [OpenStreetMap relation 7681896](https://www.openstreetmap.org/relation/7681896) and shared by the browser map and server-side route validation. The greenways overlay in `static/greenways.geojson` is an OpenStreetMap snapshot (14 October 2026) of mapped, named Irish greenway paths, including Cork Harbour, Cork Marina to Passage West, Passage Railway, Carrigaline to Crosshaven (mapped in OSM as the connected Old Crosshaven Railway Path), and North Kerry Greenways. It is not a live feed or a guaranteed inventory: coverage depends on routes contributors have mapped and named, and unmapped or proposed connections are not inferred. Both data sets are available under the [Open Database License (ODbL)](https://www.openstreetmap.org/copyright). The map supports user-selectable light or dark appearance and a greenways visibility toggle.

## Run locally

Use Python 3.10 or newer.

```powershell
py -m pip install -r requirements.txt
py .\1.py
```

Open `http://localhost:5000` and choose **Create account**. The development secret key is generated in the ignored `instance` directory on first run. Keep that file and the SQLite database together, private, and backed up; losing the key means encrypted route data and Strava tokens cannot be decrypted.

## Import a Strava archive

1. Sign in or create your account.
2. In Windows Explorer, select the extracted Strava export's `activities.csv` and `activities` folder, then create a ZIP file. You can also zip the whole export folder.
3. In AMVelo, select that ZIP in **Import your Strava archive**.

The importer reads the activity CSV and cycling `.gpx` / `.fit.gz` tracks, and accepts either a ZIP containing the export files at its root or inside one containing folder. It does not extract uploaded files to a public directory or keep the original ZIP. It ignores the separate profile, social, health, privacy-zone, and other export files. Cycling summaries are retained for pace estimates; parsed route polylines and activity names are encrypted in the database and shown only to their account. Accounts can import the same Strava activity IDs without colliding.

Uploads are limited to 100 MB, 5,000 files, and 500 MB uncompressed; individual track files are limited to 20 MB. An archive is a snapshot. Re-import it to add later rides, or connect Strava to synchronize new, edited, and deleted rides.

## Strava live sync

Create an app at [strava.com/settings/api](https://www.strava.com/settings/api). Set its callback domain to `localhost` for local development and use **Connect with Strava** after signing in.

```powershell
$env:STRAVA_CLIENT_ID = "your-client-id"
$env:STRAVA_CLIENT_SECRET = "your-client-secret"
$env:STRAVA_REDIRECT_URI = "http://localhost:5000/auth/strava/callback"
$env:STRAVA_VERIFY_TOKEN = "choose-a-long-random-value"
py .\1.py
```

Use a stable `SECRET_KEY` in production. Strava credentials are encrypted using that key; changing it makes existing credentials unreadable and requires affected users to reconnect. For automatic updates, register one Strava webhook for the app and set `callback_url` to its public HTTPS `/webhooks/strava` endpoint. Strava's webhook owner ID is mapped to the associated account before updating that user's activities.

## Password recovery

Password-reset emails require an SMTP server. Configure these environment variables before enabling account recovery:

```powershell
$env:SMTP_HOST = "smtp.example.com"
$env:SMTP_PORT = "587"
$env:SMTP_FROM = "AMVelo <no-reply@example.com>"
$env:SMTP_USERNAME = "smtp-user"
$env:SMTP_PASSWORD = "smtp-password"
$env:SMTP_USE_TLS = "true"
$env:PUBLIC_BASE_URL = "https://amvelo.example.com"
```

`SMTP_USERNAME` and `SMTP_PASSWORD` are optional when the SMTP server does not require authentication. Keep `SMTP_USE_TLS=true`; public deployments refuse to send reset mail without TLS and an HTTPS `PUBLIC_BASE_URL`. Reset links expire after 30 minutes and work once. Requests are rate-limited, and the response does not reveal whether an email belongs to an account. A successful password reset invalidates that account's existing sessions. The local development URL can be set to `http://localhost:5000`; do not use HTTP for a public site.

## Opening on a phone locally

For testing on the same trusted home network, set `HOST` to `0.0.0.0` and open `http://COMPUTER-IP:5000` on the phone. Allow Windows Firewall access only on a private network. Do not port-forward this development server or expose it publicly. Browser geolocation and Strava OAuth generally require HTTPS; without it, tap the map to set a start point.

## Public deployment checklist

This source is a small application, not a managed production service. Before inviting public users:

- Serve it only behind a correctly configured HTTPS reverse proxy and a production WSGI server. Do not use Flask's development server or debug mode.
- Set `PUBLIC_DEPLOYMENT=1`, a stable, random `SECRET_KEY` of at least 32 characters, and the exact public HTTPS `STRAVA_REDIRECT_URI`. Public-deployment mode enables secure session cookies and refuses to start without the secret or with debug mode.
- Use the WSGI entry point with a production server, for example `waitress-serve --listen=127.0.0.1:5000 wsgi:app` behind the HTTPS proxy. Do not expose the WSGI server port directly to the internet.
- Keep SQLite, its backups, and the signing/encryption key out of the web root and restrict filesystem access to the service account. Back up the key and database together.
- Set up tested backup, restore, monitoring, and incident response. The application operator can access its server and database; account separation protects users from other ordinary website users, not from a compromised host or privileged server operator.
- Configure and test SMTP password recovery, abuse protection behind your chosen reverse proxy, retention, and applicable privacy/legal obligations before advertising the service. Registration is self-service and currently does not verify email addresses.
- Complete Strava's application review/approval and follow the terms and limits of Strava and every map/routing/geocoding/weather provider you use.

Every account's ride data, stats, route API, imports, and Strava sync are scoped to its authenticated account. Anonymous visitors cannot read those endpoints. Start and destination points are checked against the island boundary both in the browser and by the route API. Deleting an account requires its password, revokes its Strava authorization when connected, and removes that account's activities from the app.

## Location and external providers

The application does not publish an activity feed or allow one user to fetch another user's routes. For bicycle routing, it sends the route's start, stops, and destination coordinates to the configured OpenStreetMap routing service. Place search sends the search text to OpenStreetMap's Nominatim service; the duration-based destination finder queries Overpass for nearby named places and sends their coordinates to the bicycle-routing service; current weather for the map centre and route samples, return-at-sunset times, and sampled route elevations are requested from Open-Meteo. The optional aircraft Easter egg sends the map centre and search radius to ADSB.lol and displays its live aircraft reports. OpenStreetMap receives requests for the map tiles displayed in the browser. The dark appearance is applied locally to those tiles. Map data attribution is shown on the map and follows the [OpenStreetMap attribution terms](https://www.openstreetmap.org/copyright). These providers and your hosting provider may process connection or request data under their own policies. Review those policies if the locations are especially sensitive.

`instance/rides.sqlite3` is the default private database path. Override it with `BIKE_MAP_DATABASE`. Access tokens, refresh tokens, route polylines, and activity names are encrypted at rest; other account/activity summary fields are stored in SQLite. The original uploaded archive is not saved. The signed-in **My stats** section summarizes that account's rides and stores its bike type and model; favorite places are private to the same account. The **Explore** section searches nearby named towns and villages and estimates an out-and-back time using that account's typical Strava speed. Route alternatives are supplied by the bicycle-routing provider. AMVelo labels the quickest alternative, the least and most climbing alternatives (when elevation samples are available), and the route with the least distance on signed M/N/R roads as "quieter roads." The elevation profile samples Open-Meteo heights along the selected route and distinguishes uphill from downhill sections. The provider does not support hard road-class exclusions, so a brief main-road connection can remain and road labels depend on provider step references. Ride time is routed distance divided by the account's median speed from up to 30 rides in the last year, falling back to all available rides when fewer than three recent rides are available. When a route has no intermediate stops, AMVelo also checks the signed-in account's complete stored cycling-track history for rides whose recorded departure and arrival points are each within 2 km of the chosen points. It considers rides in either direction, groups nearby GPS traces, and offers up to five most-ridden route variants first. Each suggestion displays an exact stored Strava track, the most recent activity name/date in that group, and the group's median recorded moving time. These tracks are returned only through the authenticated, non-cacheable route response. Select **Start navigation** to follow the highlighted route using live browser GPS; use **Navigate the return leg** to switch to a planned return route. When a return-home deadline is active, navigation updates the required average speed using the remaining route, return leg, and selected comfort margin. Routed options use bicycle-router turn steps; previously ridden tracks use turn cues inferred from the recorded GPS line (not street names). Keep the phone awake and allow location access for live guidance. Departure times can be set directly with the clock-style time picker, or AMVelo can calculate a departure schedule backwards from a chosen return deadline or local sunset; reverse planning assumes no break at the destination. The optional comfort margin is added at each planned arrival (both outbound arrival and home arrival for a return trip), including when scheduling backwards. Estimates exclude stops, traffic lights, and live road conditions. Wind and weather are current conditions at route samples or the map centre, not forecasts for the estimated arrival time.
