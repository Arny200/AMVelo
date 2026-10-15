const initialTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
const boundaryResponse = await fetch("/static/ireland-boundary.geojson");
if (!boundaryResponse.ok) {
  throw new Error(`Could not load the Ireland map outline (${boundaryResponse.status}).`);
}
const islandBoundary = (await boundaryResponse.json()).geometry.coordinates[0]
  .map(([longitude, latitude]) => [latitude, longitude]);
const irelandBounds = L.latLngBounds(islandBoundary);
const islandLatitudeGridSize = 0.05;
const islandEdgesByLatitude = new Map();
for (let index = 0; index < islandBoundary.length; index += 1) {
  const start = islandBoundary[index];
  const end = islandBoundary[(index + 1) % islandBoundary.length];
  const edge = [start, end];
  const firstGridRow = Math.floor(Math.min(start[0], end[0]) / islandLatitudeGridSize);
  const lastGridRow = Math.floor(Math.max(start[0], end[0]) / islandLatitudeGridSize);
  for (let row = firstGridRow; row <= lastGridRow; row += 1) {
    if (!islandEdgesByLatitude.has(row)) islandEdgesByLatitude.set(row, []);
    islandEdgesByLatitude.get(row).push(edge);
  }
}
let mapWeatherTimer = null;
let mapWeatherRequestId = 0;
let lastMapWeatherKey = "";
const mapWeatherCache = new Map();
function greenwayLineStyle() {
  return {
    color: getComputedStyle(document.documentElement).getPropertyValue("--greenway").trim(),
    weight: Math.max(1, Math.min(3, 0.8 + (map.getZoom() - 6) * 0.35)),
    opacity: 0.9,
    lineCap: "round",
    lineJoin: "round",
  };
}
const map = L.map("map", {
  zoomControl: false,
  scrollWheelZoom: true,
  zoomSnap: 1,
  zoomDelta: 1,
  minZoom: 6,
  maxBounds: irelandBounds.pad(0.2),
  maxBoundsViscosity: 1,
});
map.fitBounds(irelandBounds.pad(0.08), { padding: [24, 24], maxZoom: 7 });
L.control.zoom({ position: "bottomright" }).addTo(map);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  updateWhenZooming: false,
  updateWhenIdle: true,
  keepBuffer: 4,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
const coastlineMask = L.polygon(
  [
    [[45, -20], [65, -20], [65, 5], [45, 5]],
    islandBoundary,
  ],
  {
    stroke: false,
    fillColor: "#b8dce8",
    fillOpacity: 1,
    fillRule: "evenodd",
    interactive: false,
  },
).addTo(map);
const greenwaysLayer = L.geoJSON(null, {
  style: greenwayLineStyle,
  onEachFeature: (feature, layer) => {
    if (feature.properties?.name) {
      layer.bindTooltip(feature.properties.name, { sticky: true, direction: "top" });
    }
  },
});
map.on("zoomend", () => greenwaysLayer.setStyle(greenwayLineStyle()));

function setTheme(theme, savePreference = true) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  document.documentElement.style.colorScheme = dark ? "dark" : "light";
  document.querySelector('meta[name="theme-color"]').content = dark ? "#111916" : "#fbfcfa";
  coastlineMask.setStyle({
    fillColor: getComputedStyle(document.documentElement).getPropertyValue("--ocean").trim(),
  });
  greenwaysLayer.setStyle({
    ...greenwayLineStyle(),
  });
  const toggle = $("theme-toggle");
  if (toggle) {
    toggle.setAttribute("aria-pressed", String(dark));
    toggle.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
    toggle.title = dark ? "Light mode" : "Dark mode";
  }
  const dialogToggle = $("dialog-theme-toggle");
  if (dialogToggle) {
    dialogToggle.setAttribute("aria-pressed", String(dark));
    dialogToggle.textContent = dark ? "Light mode" : "Dark mode";
  }
  if (savePreference) {
    try {
      localStorage.setItem("cadence-theme", dark ? "dark" : "light");
    } catch (error) {
      console.warn("Could not save the map appearance preference.", error);
    }
  }
}

const ridesLayer = L.layerGroup();
let ridesVisible = true;
let routeLayer = null;
let originMarker = null;
let destinationMarker = null;
let origin = null;
let destination = null;
let stops = [];
let activePlaceField = null;
let pendingMapField = null;
let routeOptions = [];
let returnRoute = null;
let selectedRouteId = null;
let routeRequestId = 0;
let routeRequestPending = false;
let scheduleRequestId = 0;
const sunsetCache = new Map();
let statusData = null;
let searchTimer = null;
let lastSearchAt = 0;
let searchAbort = null;
let lastRoutedDestination = null;
let csrfToken = null;
let authMode = "login";
let navigationWatchId = null;
let navigationMarker = null;
let navigationAccuracy = null;
let activeNavigationRoute = null;
let navigationCoordinates = [];
let navigationCumulativeMeters = [];
let navigationProgressMeters = 0;
let navigationLastPosition = null;
let navigationWakeLock = null;

const $ = (id) => document.getElementById(id);
const originInput = $("origin-input");
const destinationInput = $("destination-input");
const routeButton = $("route-button");
const placeFields = new Map();
const originField = {
  role: "origin",
  input: originInput,
  suggestions: $("origin-suggestions"),
  point: null,
  marker: null,
};
const destinationField = {
  role: "destination",
  input: destinationInput,
  suggestions: $("suggestions"),
  point: null,
  marker: null,
};
placeFields.set(originInput.id, originField);
placeFields.set(destinationInput.id, destinationField);
setTheme(initialTheme, false);
const greenwaysToggle = $("greenways-toggle");
greenwaysToggle.addEventListener("click", () => {
  const visible = !map.hasLayer(greenwaysLayer);
  if (visible) greenwaysLayer.addTo(map);
  else map.removeLayer(greenwaysLayer);
  greenwaysToggle.setAttribute("aria-pressed", String(visible));
  greenwaysToggle.setAttribute(
    "aria-label",
    visible ? "Hide mapped Irish greenways" : "Show mapped Irish greenways",
  );
  greenwaysToggle.title = visible ? "Hide mapped Irish greenways" : "Show mapped Irish greenways";
});
async function loadGreenways() {
  try {
    const greenwaysResponse = await fetch("/static/greenways.geojson");
    if (!greenwaysResponse.ok) {
      throw new Error(`Could not load the greenways map data (${greenwaysResponse.status}).`);
    }
    const greenways = await greenwaysResponse.json();
    if (!Array.isArray(greenways.features) || !greenways.features.length) {
      throw new Error("The greenways map data contains no routes.");
    }
    greenwaysLayer.addData(greenways);
    greenwaysLayer.addTo(map);
    greenwaysToggle.disabled = false;
  } catch (error) {
    greenwaysToggle.disabled = true;
    greenwaysToggle.title = "Greenways are temporarily unavailable";
    setMapStatus("Greenways could not be loaded. Please refresh the page.");
    console.error("Could not load the mapped Irish greenways.", error);
  }
}
void loadGreenways();

function isOnIrelandIsland(latitude, longitude) {
  if (!irelandBounds.contains([latitude, longitude])) {
    return false;
  }
  let inside = false;
  const edges = islandEdgesByLatitude.get(Math.floor(latitude / islandLatitudeGridSize)) || [];
  for (const [[previousLatitude, previousLongitude], [currentLatitude, currentLongitude]] of edges) {
    const cross = (longitude - previousLongitude) * (currentLatitude - previousLatitude) -
      (latitude - previousLatitude) * (currentLongitude - previousLongitude);
    if (
      Math.abs(cross) < 1e-9 &&
      latitude >= Math.min(previousLatitude, currentLatitude) - 1e-9 &&
      latitude <= Math.max(previousLatitude, currentLatitude) + 1e-9 &&
      longitude >= Math.min(previousLongitude, currentLongitude) - 1e-9 &&
      longitude <= Math.max(previousLongitude, currentLongitude) + 1e-9
    ) return true;
    if ((currentLatitude > latitude) !== (previousLatitude > latitude)) {
      const crossingLongitude = previousLongitude +
        (latitude - previousLatitude) * (currentLongitude - previousLongitude) /
        (currentLatitude - previousLatitude);
      if (longitude < crossingLongitude) inside = !inside;
    }
  }
  return inside;
}

function decodePolyline(encoded) {
  let index = 0;
  let latitude = 0;
  let longitude = 0;
  const points = [];
  while (index < encoded.length) {
    let result = 0;
    let shift = 0;
    let byte;
    do {
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    latitude += (result & 1) ? ~(result >> 1) : result >> 1;
    result = 0;
    shift = 0;
    do {
      byte = encoded.charCodeAt(index++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    longitude += (result & 1) ? ~(result >> 1) : result >> 1;
    points.push([latitude / 1e5, longitude / 1e5]);
  }
  return points;
}

async function fetchJson(url, options) {
  const requestOptions = { ...options, headers: { ...(options?.headers || {}) } };
  if (requestOptions.method && requestOptions.method.toUpperCase() !== "GET") {
    if (!csrfToken) await loadCsrfToken();
    requestOptions.headers["X-CSRF-Token"] = csrfToken;
  }
  const response = await fetch(url, requestOptions);
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`The server returned an unexpected response (${response.status}).`);
  }
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status}).`);
    error.status = response.status;
    throw error;
  }
  return data;
}

async function loadCsrfToken() {
  const response = await fetch("/api/csrf", { cache: "no-store" });
  const result = await response.json();
  if (!response.ok || !result.csrf_token) {
    throw new Error(result.error || "Could not start a secure session. Refresh the page.");
  }
  csrfToken = result.csrf_token;
}

function setMapStatus(text) {
  $("map-status").textContent = text;
}

function setRouteHint(text, isError = false) {
  $("route-hint").textContent = text;
  $("route-hint").style.color = isError ? "#bd6257" : "";
}

function updateRouteButton() {
  routeButton.disabled =
    routeRequestPending ||
    !origin ||
    !destination ||
    stops.some((stop) => !stop.point) ||
    !statusData?.has_activities;
  $("clear-origin").hidden = !originInput.value;
  $("clear-destination").hidden = !destinationInput.value;
}

function formatDuration(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  const hours = Math.floor(minutes / 60);
  const remaining = minutes % 60;
  if (hours === 0) return `${minutes} min`;
  if (!remaining) return `${hours} hr`;
  return `${hours} hr ${remaining} min`;
}

function formatRelativeDate(dateText) {
  if (!dateText) return "Not synced yet";
  const time = new Date(dateText).getTime();
  if (!Number.isFinite(time)) return "Recently";
  const minutes = Math.max(0, Math.floor((Date.now() - time) / 60000));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function showSyncState(data) {
  const isSyncing = data.sync_state === "syncing";
  const isError = data.sync_state === "error";
  $("sync-label").textContent = isSyncing
    ? "Syncing ride history"
    : isError
      ? (data.sync_error || "Sync needs attention")
      : `Updated ${formatRelativeDate(data.last_sync_at)}`;
  const dot = document.querySelector(".sync-dot");
  dot.classList.toggle("syncing", isSyncing);
  dot.classList.toggle("error", isError);
  $("sync-button").disabled = isSyncing;
  $("sync-button").textContent = isSyncing ? "Working…" : "Sync now";
  if (isSyncing) setMapStatus("Bringing your ride history onto the map");
}

function renderStatus(data) {
  statusData = data;
  $("auth-card").hidden = true;
  $("account-bar").hidden = false;
  $("private-content").hidden = false;
  $("account-email").textContent = data.email;
  $("ride-stats").hidden = data.activity_count === 0;
  $("stat-rides").textContent = data.activity_count.toLocaleString();
  $("stat-distance").textContent = data.distance_km.toLocaleString();
  $("stat-speed").textContent = data.average_speed_kmh ?? "—";
  $("sync-date").textContent = formatRelativeDate(data.last_sync_at || data.archive_imported_at);
  if (data.connected) {
    $("connection-title").textContent = data.athlete_name
      ? `Riding with ${data.athlete_name}`
      : "Strava connected";
    $("connection-copy").textContent = "Your history is shaping every estimate.";
    $("connect-button").hidden = true;
    $("sync-details").hidden = false;
    showSyncState(data);
  } else {
    $("connection-title").textContent = data.activity_count
      ? "Your Strava archive is ready."
      : "Your rides, your rhythm.";
    $("connection-copy").textContent = data.connected
      ? "Your history is shaping every estimate."
      : "Import your archive or connect Strava to get started.";
    $("connect-button").hidden = false;
    $("sync-details").hidden = true;
  }
  $("archive-status").classList.toggle("error", Boolean(data.import_error));
  $("archive-status").textContent = data.import_state === "syncing"
    ? "Importing your private ride history…"
    : data.import_error
      ? data.import_error
      : data.archive_imported_at
        ? `Archive imported ${formatRelativeDate(data.archive_imported_at)}. Re-import to include newer rides.`
        : "ZIP files up to 100 MB. Your original export is not kept.";
  if (data.activity_count) {
    setMapStatus(`${data.activity_count.toLocaleString()} rides in your private map`);
    if (!destination) setRouteHint("Choose an arrival point and your rides will do the rest.");
  } else {
    setMapStatus(data.connected ? "Your Strava ride history is syncing" : "Your private map is ready");
    setRouteHint("Import your Strava archive or connect Strava to get started.");
  }
  updateRouteButton();
}

function renderSignedOut() {
  statusData = null;
  $("auth-card").hidden = false;
  $("account-bar").hidden = true;
  $("private-content").hidden = true;
  ridesLayer.clearLayers();
  map.removeLayer(ridesLayer);
  $("rides-toggle").disabled = true;
  setRidesVisible(false);
  if (routeLayer) routeLayer.remove();
  if (originMarker) originMarker.remove();
  if (destinationMarker) destinationMarker.remove();
  routeLayer = null;
  originMarker = null;
  destinationMarker = null;
  origin = null;
  destination = null;
  originField.point = null;
  destinationField.point = null;
  for (const stop of stops) {
    if (stop.marker) stop.marker.remove();
    placeFields.delete(`${stop.id}-input`);
    stop.row.remove();
  }
  stops = [];
  activePlaceField = null;
  pendingMapField = null;
  map.getContainer().style.cursor = "";
  originInput.value = "";
  destinationInput.value = "";
  clearRouteDisplay();
  lastRoutedDestination = null;
  $("route-result").hidden = true;
  $("map-status").textContent = "Your private ride map";
  updateRouteButton();
}

async function loadStatus() {
  try {
    const data = await fetchJson("/api/status");
    renderStatus(data);
    if (data.activity_count) await loadRides();
  } catch (error) {
    if (error.status === 401) {
      renderSignedOut();
    } else {
      setRouteHint(error.message, true);
    }
  }
}

async function loadRides() {
  try {
    const activities = await fetchJson("/api/activities");
    ridesLayer.clearLayers();
    const allBounds = L.latLngBounds([]);
    for (const activity of activities) {
      if (!activity.polyline) continue;
      const points = decodePolyline(activity.polyline);
      if (points.length < 2 || points.some(([lat, lon]) => !isOnIrelandIsland(lat, lon))) continue;
      L.polyline(points, {
        color: "#769883",
        weight: 2,
        opacity: 0.25,
        lineCap: "round",
        lineJoin: "round",
      }).addTo(ridesLayer);
      for (const point of points) allBounds.extend(point);
    }
    $("rides-toggle").disabled = ridesLayer.getLayers().length === 0;
    setRidesVisible(ridesVisible && !destination);
    if (allBounds.isValid() && !lastRoutedDestination && !origin) {
      map.fitBounds(allBounds, { padding: [45, 45], maxZoom: 12 });
    }
  } catch (error) {
    setMapStatus(error.message);
  }
}

function markerIcon(kind) {
  const className = kind === "origin" ? "map-pin map-pin-start" : "map-pin map-pin-end";
  return L.divIcon({
    className: "",
    html: `<span class="${className}"></span>`,
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
}

function setRidesVisible(visible) {
  const toggle = $("rides-toggle");
  ridesVisible = visible;
  if (visible && ridesLayer.getLayers().length) ridesLayer.addTo(map);
  else map.removeLayer(ridesLayer);
  const isVisible = visible && ridesLayer.getLayers().length > 0;
  toggle.setAttribute("aria-pressed", String(isVisible));
  toggle.setAttribute("aria-label", isVisible ? "Hide my ride history" : "Show my ride history");
  toggle.title = isVisible ? "Hide my ride history" : "Show my ride history";
}

function clearRouteDisplay() {
  routeRequestId += 1;
  scheduleRequestId += 1;
  if (routeLayer) routeLayer.remove();
  routeLayer = null;
  routeOptions = [];
  returnRoute = null;
  selectedRouteId = null;
  $("departure-date").disabled = false;
  $("departure-clock").disabled = false;
  $("departure-label").textContent = "DEPARTURE TIME";
  $("route-options").replaceChildren();
  $("route-result").hidden = true;
  lastRoutedDestination = null;
}

function updatePlaceValue(field, point, label) {
  if (!isOnIrelandIsland(point.lat, point.lng)) {
    setRouteHint(
      field.role === "origin"
        ? "Choose a departure point on the island of Ireland."
        : "Choose an arrival point or stop on the island of Ireland.",
      true,
    );
    return false;
  }
  field.point = { lat: point.lat, lon: point.lng };
  field.input.value = label || `${point.lat.toFixed(5)}, ${point.lng.toFixed(5)}`;
  field.input.dataset.selectedLabel = field.input.value;
  field.suggestions.hidden = true;
  if (field.marker) field.marker.remove();
  const markerKind = field.role === "origin" ? "origin" : "destination";
  field.marker = L.marker(point, {
    icon: markerIcon(markerKind),
    zIndexOffset: field.role === "origin" ? 700 : 800,
  }).addTo(map);
  if (field.role === "origin") {
    origin = field.point;
    originMarker = field.marker;
  } else if (field.role === "destination") {
    destination = field.point;
    destinationMarker = field.marker;
    setRidesVisible(false);
  } else {
    const stop = stops.find((item) => item.id === field.stopId);
    if (stop) {
      stop.point = field.point;
      stop.marker = field.marker;
    }
  }
  activePlaceField = null;
  pendingMapField = null;
  map.getContainer().style.cursor = "";
  clearRouteDisplay();
  updateRouteButton();
  setMapStatus(
    field.role === "origin"
      ? "Departure point selected"
      : field.role === "destination"
        ? "Arrival ready for your ride"
        : "Stop added to your ride",
  );
  setRouteHint(
    origin && destination
      ? "Ready when you are. Choose a route."
      : "Choose another point, or use the map to set it.",
  );
  return true;
}

function createStopField() {
  if (stops.length >= 5) {
    setRouteHint("You can add up to five stops to one ride.", true);
    return;
  }
  const id = `stop-${crypto.randomUUID()}`;
  const row = document.createElement("div");
  row.className = "route-stop";
  const label = document.createElement("label");
  label.className = "field-label";
  label.htmlFor = `${id}-input`;
  label.textContent = `STOP ${stops.length + 1}`;
  const wrap = document.createElement("div");
  wrap.className = "destination-wrap";
  wrap.innerHTML =
    '<span class="destination-dot stop-dot" aria-hidden="true"></span>';
  const input = document.createElement("input");
  input.id = `${id}-input`;
  input.type = "search";
  input.placeholder = "Add a place to pass through";
  input.autocomplete = "off";
  const pick = document.createElement("button");
  pick.className = "field-action";
  pick.type = "button";
  pick.dataset.pickMap = id;
  pick.setAttribute("aria-label", "Pick stop on map");
  pick.title = "Pick stop on map";
  pick.textContent = "⌖";
  const remove = document.createElement("button");
  remove.className = "clear-button remove-stop";
  remove.type = "button";
  remove.setAttribute("aria-label", "Remove stop");
  remove.textContent = "×";
  const suggestions = document.createElement("ul");
  suggestions.className = "suggestions";
  suggestions.id = `${id}-suggestions`;
  suggestions.setAttribute("role", "listbox");
  suggestions.hidden = true;
  wrap.append(input, pick, remove);
  row.append(label, wrap, suggestions);
  $("stop-list").append(row);
  const stop = { id, row, point: null, marker: null };
  const field = {
    id,
    stopId: id,
    role: "stop",
    input,
    suggestions,
    point: null,
    marker: null,
  };
  stops.push(stop);
  placeFields.set(input.id, field);
  registerPlaceField(field);
  remove.addEventListener("click", () => {
    if (field.marker) field.marker.remove();
    placeFields.delete(input.id);
    row.remove();
    stops = stops.filter((item) => item.id !== id);
    stops.forEach((item, index) => {
      item.row.querySelector(".field-label").textContent = `STOP ${index + 1}`;
    });
    clearRouteDisplay();
    updateRouteButton();
  });
  updateRouteButton();
  input.focus();
}

function registerPlaceField(field) {
  field.input.addEventListener("focus", () => {
    activePlaceField = field;
    if (field.input.value.length >= 3 && field.suggestions.children.length) {
      field.suggestions.hidden = false;
    } else if (field.role === "origin" && !field.point) {
      addCurrentLocationChoice(field);
      field.suggestions.hidden = false;
    }
    setRouteHint("Type a place to search, or tap the map to set this point.");
  });
  field.input.addEventListener("input", () => {
    const query = field.input.value.trim();
    if (query !== field.input.dataset.selectedLabel && field.point) {
      if (field.marker) field.marker.remove();
      field.marker = null;
      field.point = null;
      if (field.role === "origin") {
        origin = null;
        originMarker = null;
      } else if (field.role === "destination") {
        destination = null;
        destinationMarker = null;
      } else {
        const stop = stops.find((item) => item.id === field.stopId);
        if (stop) {
          stop.point = null;
          stop.marker = null;
        }
      }
      clearRouteDisplay();
    }
    updateRouteButton();
    clearTimeout(searchTimer);
    if (query.length < 3) {
      field.suggestions.hidden = true;
      return;
    }
    searchTimer = setTimeout(() => searchPlaces(field, query), 350);
  });
  field.input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") field.suggestions.hidden = true;
    if (event.key === "Enter") {
      const first = field.suggestions.querySelector("button");
      if (first) {
        event.preventDefault();
        first.click();
      }
    }
  });
}

function addCurrentLocationChoice(field) {
  const listItem = document.createElement("li");
  const button = document.createElement("button");
  button.type = "button";
  button.setAttribute("role", "option");
  button.textContent = "◎  Use my current location";
  button.addEventListener("click", setLocation);
  listItem.append(button);
  field.suggestions.replaceChildren(listItem);
}

registerPlaceField(originField);
registerPlaceField(destinationField);

function chooseSuggestion(field, item) {
  const point = L.latLng(Number(item.lat), Number(item.lon));
  if (
    updatePlaceValue(
      field,
      point,
      item.display_name.split(",").slice(0, 3).join(","),
    )
  ) {
    map.flyTo(point, 13, { duration: 0.8 });
  }
}

async function searchPlaces(field, query) {
  if (searchAbort) searchAbort.abort();
  const controller = new AbortController();
  searchAbort = controller;
  const list = field.suggestions;
  try {
    const wait = Math.max(0, 1000 - (Date.now() - lastSearchAt));
    if (wait) await new Promise((resolve) => setTimeout(resolve, wait));
    lastSearchAt = Date.now();
    const params = new URLSearchParams({
      q: query,
      format: "jsonv2",
      limit: "10",
      countrycodes: "ie,gb",
      viewbox: "-11,55.5,-5.3,51.3",
      bounded: "1",
    });
    const results = await fetchJson(`https://nominatim.openstreetmap.org/search?${params}`, {
      signal: controller.signal,
      headers: { "Accept-Language": navigator.language || "en" },
    });
    list.replaceChildren();
    if (field.role === "origin") addCurrentLocationChoice(field);
    const irelandResults = results.filter((item) =>
      isOnIrelandIsland(Number(item.lat), Number(item.lon))
    );
    if (!irelandResults.length) {
      list.hidden = field.role !== "origin";
      setRouteHint("No matching places found on the island of Ireland.", true);
      return;
    }
    for (const item of irelandResults.slice(0, 5)) {
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.setAttribute("role", "option");
      button.textContent = item.display_name;
      button.addEventListener("click", () => chooseSuggestion(field, item));
      li.append(button);
      list.append(li);
    }
    list.hidden = false;
  } catch (error) {
    if (error.name !== "AbortError") {
      list.hidden = true;
      setRouteHint("Place search is unavailable right now.", true);
    }
  }
}

function setLocation() {
  const button = $("location-button");
  if (!navigator.geolocation) {
    setRouteHint("Location is not available in this browser.", true);
    return;
  }
  button.disabled = true;
  button.textContent = "…";
  navigator.geolocation.getCurrentPosition(
    ({ coords }) => {
      const point = L.latLng(coords.latitude, coords.longitude);
      button.disabled = false;
      button.textContent = "◎";
      if (updatePlaceValue(originField, point, "Your current location")) {
        map.flyTo(point, 13, { duration: 0.8 });
      }
    },
    (error) => {
      button.disabled = false;
      button.textContent = "◎";
      setRouteHint(
        error.code === error.PERMISSION_DENIED
          ? "Allow location access, or pick your departure point on the map."
          : "Could not find your location. Pick your departure point on the map.",
        true,
      );
    },
    { enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 },
  );
}

function compassDirection(degrees) {
  return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][
    Math.round(Number(degrees) / 45) % 8
  ];
}

function weatherLabel(code) {
  if (code === 0) return ["Clear skies", "☀"];
  if (code <= 2) return ["Partly cloudy", "⛅"];
  if (code === 3) return ["Overcast", "☁"];
  if (code <= 48) return ["Misty", "☁"];
  if (code <= 67 || (code >= 80 && code <= 82)) return ["Rain", "☂"];
  if (code <= 77) return ["Snow", "❄"];
  if (code >= 95) return ["Thunderstorms", "⛈"];
  return ["Changing skies", "☁"];
}

function formatDateTimeInput(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function setScheduleDateTime(id, date) {
  const value = formatDateTimeInput(date);
  const dateId = id === "departure-time" ? "departure-date" : "return-by-date";
  const clockId = id === "departure-time" ? "departure-clock" : "return-by-clock";
  $(id).value = value;
  $(dateId).value = value.slice(0, 10);
  $(clockId).innerHTML = `${value.slice(11)} <span aria-hidden="true">◷</span>`;
  $(clockId).setAttribute(
    "aria-label",
    `Set ${id === "departure-time" ? "departure" : "return"} time, ${value.slice(11)}`,
  );
}

function syncScheduleDate(id) {
  const dateId = id === "departure-time" ? "departure-date" : "return-by-date";
  const current = parseDateTimeInput($(id).value) || new Date();
  const date = $(dateId).value;
  if (!date) return;
  setScheduleDateTime(id, parseDateTimeInput(`${date}T${formatDateTimeInput(current).slice(11)}`));
  refreshRouteSchedule();
}

function parseDateTimeInput(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}

function initializeScheduleInputs() {
  const departure = new Date();
  departure.setSeconds(0, 0);
  departure.setMinutes(departure.getMinutes() + 1);
  const returnBy = new Date(departure.getTime() + 4 * 60 * 60 * 1000);
  setScheduleDateTime("departure-time", departure);
  setScheduleDateTime("return-by-time", returnBy);
}

initializeScheduleInputs();

let clockTarget = null;
let clockHour = 17;
let clockMinute = 0;
let clockMode = "hour";

function updateClockReadout() {
  $("clock-hour-value").textContent = String(clockHour).padStart(2, "0");
  $("clock-minute-value").textContent = String(clockMinute).padStart(2, "0");
  $("clock-hour-value").classList.toggle("active", clockMode === "hour");
  $("clock-minute-value").classList.toggle("active", clockMode === "minute");
}

function renderClockFace() {
  const face = $("clock-face");
  face.replaceChildren();
  const values = clockMode === "hour"
    ? Array.from({ length: 24 }, (_, index) => index)
    : Array.from({ length: 12 }, (_, index) => index * 5);
  for (const value of values) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "clock-number";
    const hourRing = clockMode === "hour" && (value === 0 || value > 12);
    const radius = hourRing ? 78 : 49;
    const angle = (value % 12) * 30 - 90;
    button.style.setProperty("--x", `${Math.cos(angle * Math.PI / 180) * radius}px`);
    button.style.setProperty("--y", `${Math.sin(angle * Math.PI / 180) * radius}px`);
    button.textContent = String(value).padStart(2, "0");
    button.setAttribute("aria-pressed", String(
      clockMode === "hour" ? value === clockHour : value === clockMinute,
    ));
    button.addEventListener("click", () => {
      if (clockMode === "hour") {
        clockHour = value;
        clockMode = "minute";
        updateClockReadout();
        renderClockFace();
      } else {
        clockMinute = value;
        updateClockReadout();
        renderClockFace();
      }
    });
    face.append(button);
  }
  updateClockReadout();
}

function openClockPicker(target) {
  clockTarget = target;
  const current = parseDateTimeInput($(target).value) || new Date();
  clockHour = current.getHours();
  clockMinute = Math.round(current.getMinutes() / 5) * 5;
  if (clockMinute === 60) {
    clockHour = (clockHour + 1) % 24;
    clockMinute = 0;
  }
  clockMode = "hour";
  $("clock-dialog-title").textContent =
    target === "departure-time" ? "Departure time" : "Return deadline";
  renderClockFace();
  $("clock-dialog").showModal();
}

for (const [buttonId, targetId] of [
  ["departure-clock", "departure-time"],
  ["return-by-clock", "return-by-time"],
]) {
  $(buttonId).addEventListener("click", () => openClockPicker(targetId));
}
$("clock-hour-value").addEventListener("click", () => {
  clockMode = "hour";
  renderClockFace();
});
$("clock-minute-value").addEventListener("click", () => {
  clockMode = "minute";
  renderClockFace();
});
$("clock-dialog-close").addEventListener("click", () => $("clock-dialog").close());
$("clock-dialog-cancel").addEventListener("click", () => $("clock-dialog").close());
$("clock-dialog-confirm").addEventListener("click", () => {
  if (!clockTarget) return;
  const current = parseDateTimeInput($(clockTarget).value) || new Date();
  current.setHours(clockHour, clockMinute, 0, 0);
  setScheduleDateTime(clockTarget, current);
  $("clock-dialog").close();
  if (clockTarget === "return-by-time" &&
      $("return-sunset").getAttribute("aria-pressed") === "true") {
    $("return-sunset").setAttribute("aria-pressed", "false");
    $("return-sunset").textContent = "At sunset";
  }
  refreshRouteSchedule();
});
for (const [dateId, targetId] of [
  ["departure-date", "departure-time"],
  ["return-by-date", "return-by-time"],
]) {
  $(dateId).addEventListener("change", () => {
    syncScheduleDate(targetId);
    if (targetId === "return-by-time" &&
        $("return-sunset").getAttribute("aria-pressed") === "true") {
      $("return-sunset").setAttribute("aria-pressed", "false");
      $("return-sunset").textContent = "At sunset";
    }
  });
}

function queueMapWeatherUpdate() {
  clearTimeout(mapWeatherTimer);
  mapWeatherTimer = setTimeout(() => void loadMapWeather(), 500);
}

async function loadMapWeather() {
  const center = map.getCenter();
  const latitude = Number(center.lat.toFixed(2));
  const longitude = Number(center.lng.toFixed(2));
  const cacheKey = `${latitude},${longitude}`;
  if (cacheKey === lastMapWeatherKey) return;
  lastMapWeatherKey = cacheKey;
  const requestId = ++mapWeatherRequestId;
  $("map-weather-description").textContent = "Checking conditions here…";
  try {
    let current = mapWeatherCache.get(cacheKey);
    if (!current) {
      const params = new URLSearchParams({
        latitude: String(latitude),
        longitude: String(longitude),
        current: "temperature_2m,wind_speed_10m,wind_direction_10m,weather_code",
        wind_speed_unit: "kmh",
        timezone: "auto",
      });
      const response = await fetchJson(`https://api.open-meteo.com/v1/forecast?${params}`);
      current = response.current;
      if (!current) throw new Error("The weather service returned no current conditions.");
      mapWeatherCache.set(cacheKey, current);
      if (mapWeatherCache.size > 30) {
        mapWeatherCache.delete(mapWeatherCache.keys().next().value);
      }
    }
    if (requestId !== mapWeatherRequestId) return;
    const temperature = Number(current.temperature_2m);
    const wind = Number(current.wind_speed_10m);
    const direction = Number(current.wind_direction_10m);
    const code = Number(current.weather_code);
    if (![temperature, wind, direction, code].every(Number.isFinite)) {
      throw new Error("The weather service returned incomplete current conditions.");
    }
    const [description, icon] = weatherLabel(code);
    $("map-weather-icon").textContent = icon;
    $("map-weather-temperature").textContent = `${Math.round(temperature)}°`;
    $("map-weather-description").textContent = description;
    $("map-weather-wind").textContent =
      `${Math.round(wind)} km/h ${compassDirection(direction)}`;
  } catch (error) {
    if (requestId !== mapWeatherRequestId) return;
    $("map-weather-temperature").textContent = "Weather";
    $("map-weather-description").textContent = "Conditions unavailable";
    $("map-weather-wind").textContent = "—";
    console.warn("Could not load current weather at the map centre.", error);
  }
}

map.on("moveend", queueMapWeatherUpdate);
queueMapWeatherUpdate();

function routePointAt(coordinates, fraction) {
  return coordinates[Math.round((coordinates.length - 1) * fraction)];
}

function routeBearingAt(coordinates, fraction) {
  const index = Math.min(
    coordinates.length - 2,
    Math.max(0, Math.round((coordinates.length - 1) * fraction)),
  );
  const start = coordinates[index];
  const end = coordinates[index + 1];
  return bearingBetween(
    { lat: start[1], lon: start[0] },
    { lat: end[1], lon: end[0] },
  );
}

async function loadRouteWeather(coordinates) {
  const panel = $("route-wind-points");
  panel.replaceChildren();
  $("weather-description").textContent = "Checking wind along your route…";
  $("weather-wind").textContent = "—";
  const samples = [
    { label: "Departure", fraction: 0 },
    { label: "Halfway", fraction: 0.5 },
    { label: "Arrival", fraction: 1 },
  ].map((sample) => ({
    ...sample,
    point: routePointAt(coordinates, sample.fraction),
    bearing: routeBearingAt(coordinates, sample.fraction),
  }));
  try {
    const params = new URLSearchParams({
      latitude: samples.map(({ point }) => String(point[1])).join(","),
      longitude: samples.map(({ point }) => String(point[0])).join(","),
      current: "temperature_2m,apparent_temperature,wind_speed_10m,wind_direction_10m,weather_code",
      wind_speed_unit: "kmh",
      timezone: "auto",
    });
    const response = await fetchJson(`https://api.open-meteo.com/v1/forecast?${params}`);
    const locations = Array.isArray(response) ? response : [response];
    if (locations.length !== samples.length) {
      throw new Error("Weather provider returned incomplete route conditions.");
    }
    const weatherPoints = locations.map((location, index) => {
      const current = location.current;
      if (!current) {
        throw new Error("Weather provider returned incomplete route conditions.");
      }
      const direction = Number(current.wind_direction_10m);
      const temperature = Number(current.temperature_2m);
      const wind = Number(current.wind_speed_10m);
      const weatherCode = Number(current.weather_code);
      if (![direction, temperature, wind, weatherCode].every(Number.isFinite)) {
        throw new Error("Weather provider returned incomplete route conditions.");
      }
      const diff = Math.abs(((direction - samples[index].bearing + 540) % 360) - 180);
      const windEffect = diff < 60 ? "headwind" : diff > 120 ? "tailwind" : "crosswind";
      return {
        ...samples[index],
        temperature,
        wind,
        direction,
        effect: windEffect,
        weatherCode,
      };
    });
    const effectCounts = new Map();
    for (const point of weatherPoints) {
      effectCounts.set(point.effect, (effectCounts.get(point.effect) || 0) + 1);
    }
    const mostCommonEffects = [...effectCounts.entries()]
      .filter(([, count]) => count === Math.max(...effectCounts.values()))
      .map(([effect]) => effect);
    const primaryWind = mostCommonEffects.length === 1
      ? `Mostly ${mostCommonEffects[0]}`
      : "Mixed winds";
    const [description, icon] = weatherLabel(weatherPoints[2].weatherCode);
    $("weather-icon").textContent = icon;
    $("weather-description").textContent = `${description} · ${primaryWind}`;
    $("weather-wind").textContent =
      `${Math.round(weatherPoints[2].wind)} km/h from ${compassDirection(weatherPoints[2].direction)}`;
    $("route-weather-temp").textContent = `${Math.round(weatherPoints[2].temperature)}°`;
    for (const point of weatherPoints) {
      const row = document.createElement("div");
      row.className = "route-wind-point";
      const place = document.createElement("strong");
      place.textContent = point.label;
      const detail = document.createElement("span");
      detail.textContent =
        `${Math.round(point.wind)} km/h from ${compassDirection(point.direction)} · ${point.effect}`;
      row.append(place, detail);
      panel.append(row);
    }
  } catch (error) {
    $("weather-description").textContent = "Route wind is unavailable right now.";
    $("weather-wind").textContent = "—";
    $("route-weather-temp").textContent = "—";
    setRouteHint(error.message, true);
  }
}

function bearingBetween(start, end) {
  const toRadians = (degrees) => degrees * Math.PI / 180;
  const toDegrees = (radians) => (radians * 180 / Math.PI + 360) % 360;
  const lat1 = toRadians(start.lat);
  const lat2 = toRadians(end.lat);
  const lon = toRadians(end.lon - start.lon);
  const y = Math.sin(lon) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) -
    Math.sin(lat1) * Math.cos(lat2) * Math.cos(lon);
  return toDegrees(Math.atan2(y, x));
}

function drawSelectedRoute(option) {
  const outbound = option.geometry.coordinates;
  if (routeLayer) routeLayer.remove();
  const layers = [
    L.polyline(outbound.map(([lon, lat]) => [lat, lon]), {
      color: "#fff",
      weight: 8,
      opacity: 0.88,
      lineCap: "round",
      lineJoin: "round",
    }),
    L.polyline(outbound.map(([lon, lat]) => [lat, lon]), {
      color: "#355f4a",
      weight: 4,
      opacity: 0.95,
      lineCap: "round",
      lineJoin: "round",
    }),
  ];
  const returnCoordinates = returnRoute?.geometry?.coordinates || [];
  if (returnCoordinates.length > 1) {
    layers.push(
      L.polyline(returnCoordinates.map(([lon, lat]) => [lat, lon]), {
        color: "#fff",
        weight: 8,
        opacity: 0.7,
        dashArray: "7 7",
        lineCap: "round",
        lineJoin: "round",
      }),
      L.polyline(returnCoordinates.map(([lon, lat]) => [lat, lon]), {
        color: "#789a84",
        weight: 4,
        opacity: 0.95,
        dashArray: "7 7",
        lineCap: "round",
        lineJoin: "round",
      }),
    );
  }
  routeLayer = L.layerGroup(layers).addTo(map);
  map.fitBounds(
    L.latLngBounds([
      ...outbound.map(([lon, lat]) => [lat, lon]),
      ...returnCoordinates.map(([lon, lat]) => [lat, lon]),
    ]),
    { paddingTopLeft: [40, 65], paddingBottomRight: [45, 50], maxZoom: 15 },
  );
}

function renderRouteOptions(options) {
  const list = $("route-options");
  list.replaceChildren();
  for (const option of options) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "route-option";
    button.dataset.routeId = option.id;
    button.setAttribute("aria-pressed", String(option.id === selectedRouteId));
    const title = document.createElement("strong");
    title.textContent = option.history_route
      ? "Your recorded Strava route"
      : option.categories.length
        ? option.categories.join(" · ")
        : "Alternative route";
    const details = document.createElement("span");
    const elevation = option.elevation_gain_m === null
      ? "Elevation unavailable"
      : `${option.elevation_gain_m} m climbing`;
    const mainRoads = option.main_road_distance_m === null ||
      option.main_road_distance_m === undefined
      ? "road references unavailable"
      : `${(option.main_road_distance_m / 1000).toFixed(1)} km on M/N/R roads`;
    if (option.history_route) {
      const routeName = option.recorded_activity_name || "Recorded ride";
      const recordedDate = option.recorded_date
        ? new Date(option.recorded_date).toLocaleDateString(undefined, {
          day: "numeric",
          month: "short",
          year: "numeric",
        })
        : "";
      details.textContent =
        `${routeName}${recordedDate ? ` · ${recordedDate}` : ""} · ${option.distance_km} km · ${formatDuration(option.personal_seconds)} median time · exact recorded GPS route${option.history_ride_count > 1 ? ` · matched ${option.history_ride_count} rides` : ""}`;
    } else {
      details.textContent =
        `${option.distance_km} km · ${formatDuration(option.personal_seconds)} · ${elevation} · ${mainRoads}`;
    }
    button.append(title, details);
    button.addEventListener("click", () => {
      selectedRouteId = option.id;
      renderRouteOptions(routeOptions);
      void selectRouteOption(option);
    });
    list.append(button);
  }
}

function formatClockTime(date) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

async function getReturnDeadline() {
  const targetDate = $("return-by-time").value.slice(0, 10);
  if (!targetDate) throw new Error("Choose a date for your return deadline.");
  if ($("return-sunset").getAttribute("aria-pressed") !== "true") {
    const deadline = parseDateTimeInput($("return-by-time").value);
    if (!deadline) throw new Error("Choose a valid return date and time.");
    return deadline;
  }

  const key = `${origin.lat.toFixed(3)},${origin.lon.toFixed(3)},${targetDate}`;
  let sunset = sunsetCache.get(key);
  if (!sunset) {
    const params = new URLSearchParams({
      latitude: String(origin.lat),
      longitude: String(origin.lon),
      daily: "sunset",
      timezone: "Europe/Dublin",
      start_date: targetDate,
      end_date: targetDate,
    });
    const response = await fetchJson(`https://api.open-meteo.com/v1/forecast?${params}`);
    const sunsetText = response.daily?.sunset?.[0];
    sunset = parseDateTimeInput(sunsetText);
    if (!sunset) throw new Error("Sunset time is unavailable for the chosen date.");
    sunsetCache.set(key, sunset);
  }
  setScheduleDateTime("return-by-time", sunset);
  $("schedule-hint").textContent = `Sunset in Ireland · ${formatClockTime(sunset)}`;
  return new Date(sunset);
}

async function updateRouteSchedule(option) {
  const scheduleId = ++scheduleRequestId;
  const paceBasis = option.history_route
    ? `your median moving time across ${option.history_ride_count} matching ${option.history_ride_count === 1 ? "ride" : "rides"}`
    : `your ${statusData.activity_count.toLocaleString()} imported rides (${statusData.personal_speed_kmh} km/h typical pace)`;
  const comfortSeconds = Number($("comfort-margin").value) * 60;
  const outboundSeconds = option.personal_seconds + comfortSeconds;
  const returnSeconds = returnRoute
    ? returnRoute.personal_seconds + comfortSeconds
    : 0;
  const totalSeconds = outboundSeconds + returnSeconds;
  const departureInput = $("departure-time");
  const useReturnDeadline = $("return-trip").checked &&
    $("plan-return-time").checked;
  try {
    let departure = parseDateTimeInput(departureInput.value);
    if (useReturnDeadline) {
      if (!returnRoute) throw new Error("The return route is not available.");
      const deadline = await getReturnDeadline();
      if (scheduleId !== scheduleRequestId) return;
      departure = new Date(deadline.getTime() - totalSeconds * 1000);
      setScheduleDateTime("departure-time", departure);
      $("departure-label").textContent = "LATEST DEPARTURE";
      const deadlineHasPassed = departure.getTime() < Date.now();
      $("schedule-hint").textContent = deadlineHasPassed
        ? `To make this deadline, depart by ${formatClockTime(departure)} (already passed) and leave the arrival point by ${formatClockTime(new Date(deadline.getTime() - returnSeconds * 1000))}.`
        : `Depart by ${formatClockTime(departure)} · leave the arrival point ${formatClockTime(new Date(deadline.getTime() - returnSeconds * 1000))} · home by ${formatClockTime(deadline)}. Assumes no break at the arrival point.`;
      $("estimate-note").textContent =
        `Scheduled backwards from your return deadline using ${paceBasis}${returnRoute ? ` and your typical ${statusData.personal_speed_kmh} km/h pace for the return leg` : ""}${comfortSeconds ? `, with ${comfortSeconds / 60} minutes of comfort margin per arrival` : ""}. This estimate excludes traffic and breaks at stops.`;
      $("arrival-time").textContent =
        `Depart ${formatClockTime(departure)} · arrival ${formatClockTime(new Date(departure.getTime() + outboundSeconds * 1000))} · home by ${formatClockTime(deadline)}`;
    } else {
      if (!departure) throw new Error("Choose a valid departure date and time.");
      $("departure-label").textContent = "DEPARTURE TIME";
      $("estimate-note").textContent =
        `Based on ${paceBasis}${returnRoute ? `; the return leg uses your typical ${statusData.personal_speed_kmh} km/h pace` : ""}${comfortSeconds ? `, plus ${comfortSeconds / 60} minutes of comfort margin per arrival` : ""}. Moving time excludes stops and traffic.`;
      $("arrival-time").textContent = returnRoute
        ? `Arrival ${formatClockTime(new Date(departure.getTime() + outboundSeconds * 1000))} · home ${formatClockTime(new Date(departure.getTime() + totalSeconds * 1000))}`
        : `Arrive around ${formatClockTime(new Date(departure.getTime() + outboundSeconds * 1000))}`;
      $("schedule-hint").textContent = $("return-sunset").getAttribute("aria-pressed") === "true"
        ? "Sunset is based on the date selected for your return."
        : "We’ll estimate the latest departure and when to leave the arrival point.";
    }
    if (scheduleId !== scheduleRequestId) return;
    $("departure-date").disabled = useReturnDeadline;
    $("departure-clock").disabled = useReturnDeadline;
    $("estimate-time").textContent = formatDuration(totalSeconds);
  } catch (error) {
    if (scheduleId !== scheduleRequestId) return;
    $("departure-date").disabled = false;
    $("departure-clock").disabled = false;
    $("departure-label").textContent = "DEPARTURE TIME";
    $("schedule-hint").textContent = error.message;
    $("arrival-time").textContent = "Check your departure and return times";
  }
}

function refreshRouteSchedule() {
  if ($("route-result").hidden || !routeOptions.length || routeRequestPending) return;
  const option = routeOptions.find((item) => item.id === selectedRouteId);
  if (option) void updateRouteSchedule(option);
}

async function selectRouteOption(option) {
  drawSelectedRoute(option);
  await updateRouteSchedule(option);
  const totalDistance = option.distance_km + (returnRoute?.distance_km || 0);
  $("route-distance").textContent = `${totalDistance.toFixed(1)} km`;
  const selectedPace = option.history_route && option.personal_seconds > 0
    ? (option.distance_km * 3600 / option.personal_seconds).toFixed(1)
    : statusData.personal_speed_kmh;
  $("route-pace").textContent = `${selectedPace} km/h`;
  $("route-result").hidden = false;
  const roadDistance = option.main_road_distance_m;
  const roadDetails = option.history_route
    ? "Following the exact GPS line recorded by Strava. Turn cues are inferred from the track."
    : roadDistance === null || roadDistance === undefined
    ? "Road reference data is unavailable for this route."
    : `${(roadDistance / 1000).toFixed(1)} km on signed M/N/R main roads; quieter-road preference picks the available alternative with the least signed-main-road distance.`;
  $("route-road-note").textContent = roadDetails;
  setMapStatus("Your route, at your pace");
  setRouteHint(
    option.history_route
      ? `Based on the median moving time from ${option.history_ride_count} matching Strava ${option.history_ride_count === 1 ? "ride" : "rides"}.`
      : `Based on your ${statusData.activity_count.toLocaleString()} rides; route time uses your typical speed.`,
  );
  await loadRouteWeather(option.geometry.coordinates);
}

function metersBetweenCoordinates(first, second) {
  const radians = (degrees) => degrees * Math.PI / 180;
  const latitudeDelta = radians(second[1] - first[1]);
  const longitudeDelta = radians(second[0] - first[0]);
  const latitude1 = radians(first[1]);
  const latitude2 = radians(second[1]);
  const haversine = Math.sin(latitudeDelta / 2) ** 2 +
    Math.cos(latitude1) * Math.cos(latitude2) *
    Math.sin(longitudeDelta / 2) ** 2;
  return 6_371_000 * 2 * Math.atan2(Math.sqrt(haversine), Math.sqrt(1 - haversine));
}

function prepareNavigationRoute(option) {
  navigationCoordinates = option.geometry.coordinates;
  navigationCumulativeMeters = [0];
  for (let index = 1; index < navigationCoordinates.length; index += 1) {
    navigationCumulativeMeters.push(
      navigationCumulativeMeters[index - 1] +
      metersBetweenCoordinates(
        navigationCoordinates[index - 1],
        navigationCoordinates[index],
      ),
    );
  }
  if (!option.navigation_instructions?.length) {
    option.navigation_instructions = [
      { distance_from_start_m: 0, text: "Follow the selected bike route" },
      {
        distance_from_start_m:
          navigationCumulativeMeters[navigationCumulativeMeters.length - 1] || 0,
        text: "Arrive at your arrival point",
      },
    ];
  }
}

function nearestNavigationProgress(position) {
  const latitudeRadians = position.lat * Math.PI / 180;
  const metersPerLongitude = 111_320 * Math.cos(latitudeRadians);
  let nearest = { progress: navigationProgressMeters, distance: Infinity };
  const totalMeters =
    navigationCumulativeMeters[navigationCumulativeMeters.length - 1] || 0;
  const findSegment = (distance) => {
    let low = 0;
    let high = navigationCumulativeMeters.length - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (navigationCumulativeMeters[middle] < distance) low = middle + 1;
      else high = middle;
    }
    return Math.max(0, low - 1);
  };
  const firstSegment = navigationLastPosition
    ? findSegment(Math.max(0, navigationProgressMeters - 100))
    : 0;
  const lastSegment = navigationLastPosition
    ? Math.min(
      navigationCoordinates.length - 2,
      findSegment(Math.min(totalMeters, navigationProgressMeters + 5_000)) + 1,
    )
    : navigationCoordinates.length - 2;
  for (let index = firstSegment; index <= lastSegment; index += 1) {
    const start = navigationCoordinates[index];
    const end = navigationCoordinates[index + 1];
    const startX = (start[0] - position.lon) * metersPerLongitude;
    const startY = (start[1] - position.lat) * 111_320;
    const deltaX = (end[0] - start[0]) * metersPerLongitude;
    const deltaY = (end[1] - start[1]) * 111_320;
    const segmentLengthSquared = deltaX * deltaX + deltaY * deltaY;
    const fraction = segmentLengthSquared
      ? Math.max(0, Math.min(1, -(startX * deltaX + startY * deltaY) / segmentLengthSquared))
      : 0;
    const distance = Math.hypot(startX + fraction * deltaX, startY + fraction * deltaY);
    const segmentLength = navigationCumulativeMeters[index + 1] -
      navigationCumulativeMeters[index];
    const progress = navigationCumulativeMeters[index] + segmentLength * fraction;
    if (progress < navigationProgressMeters - 20) continue;
    if (distance < nearest.distance) nearest = { progress, distance };
  }
  return nearest;
}

function formatNavigationDistance(meters) {
  if (meters < 1000) return `${Math.max(0, Math.round(meters / 10) * 10)} m`;
  return `${(meters / 1000).toFixed(meters >= 10_000 ? 0 : 1)} km`;
}

function navigationManeuverIcon(instruction) {
  const text = instruction.toLowerCase();
  if (text.includes("left")) return "↰";
  if (text.includes("right")) return "↱";
  if (text.includes("u-turn")) return "↶";
  if (text.includes("roundabout")) return "⟳";
  if (text.includes("arrive")) return "●";
  return "↑";
}

function updateNavigationDisplay(position, accuracy, speedMetersPerSecond) {
  const nearest = nearestNavigationProgress(position);
  const totalMeters =
    navigationCumulativeMeters[navigationCumulativeMeters.length - 1] || 0;
  const isOnRoute = nearest.distance <= Math.max(100, accuracy * 1.5);
  if (isOnRoute) {
    navigationProgressMeters = Math.max(
      navigationProgressMeters,
      Math.min(nearest.progress, totalMeters),
    );
  }
  const remaining = Math.max(0, totalMeters - navigationProgressMeters);
  const instructions = activeNavigationRoute.navigation_instructions || [];
  const nextInstruction = instructions.find(
    (item) => item.distance_from_start_m >= navigationProgressMeters + 8,
  ) || instructions[instructions.length - 1];
  const turnDistance = Math.max(
    0,
    (nextInstruction?.distance_from_start_m || totalMeters) - navigationProgressMeters,
  );
  const instructionText = nextInstruction?.text || "Follow the selected bike route";
  $("navigation-instruction").textContent = instructionText;
  $("navigation-maneuver-icon").textContent = navigationManeuverIcon(instructionText);
  $("navigation-subtext").textContent = turnDistance <= 15
    ? "Now"
    : `In ${formatNavigationDistance(turnDistance)}`;
  $("navigation-distance").textContent = formatNavigationDistance(remaining);
  $("navigation-progress").style.width =
    `${totalMeters ? Math.min(100, navigationProgressMeters / totalMeters * 100) : 0}%`;
  const routePace = totalMeters / Math.max(1, activeNavigationRoute.personal_seconds);
  const pace = speedMetersPerSecond > 1 && speedMetersPerSecond < 14
    ? speedMetersPerSecond
    : routePace;
  const eta = new Date(Date.now() + remaining / Math.max(1, pace) * 1000);
  $("navigation-eta").textContent = remaining < 20 ? "Arrived" : formatClockTime(eta);
  $("navigation-speed").textContent = speedMetersPerSecond === null
    ? "—"
    : `${Math.round(speedMetersPerSecond * 3.6)} km/h`;
  $("navigation-location-status").textContent = !isOnRoute
    ? "Off route · return to the highlighted line"
    : `GPS accuracy ±${Math.max(1, Math.round(accuracy))} m`;

  const latLng = [position.lat, position.lon];
  if (!navigationMarker) {
    navigationMarker = L.circleMarker(latLng, {
      radius: 8,
      color: "#fff",
      weight: 3,
      fillColor: "#2876e2",
      fillOpacity: 1,
    }).addTo(map);
    navigationAccuracy = L.circle(latLng, {
      radius: accuracy,
      color: "#2876e2",
      weight: 1,
      fillColor: "#2876e2",
      fillOpacity: 0.1,
    }).addTo(map);
  } else {
    navigationMarker.setLatLng(latLng);
    navigationAccuracy.setLatLng(latLng).setRadius(accuracy);
  }
  navigationLastPosition = latLng;
  map.setView(latLng, Math.max(map.getZoom(), 16), { animate: true, duration: 0.4 });
}

function startNavigation() {
  const option = routeOptions.find((item) => item.id === selectedRouteId);
  if (!option) return;
  if (!navigator.geolocation) {
    $("navigation-error").textContent = "Live navigation needs location access in this browser.";
    $("navigation-error").hidden = false;
    return;
  }
  $("navigation-error").hidden = true;
  activeNavigationRoute = option;
  prepareNavigationRoute(option);
  navigationProgressMeters = 0;
  navigationLastPosition = null;
  $("navigation-ui").hidden = false;
  $("navigation-instruction").textContent = "Finding your location…";
  $("navigation-subtext").textContent = "Allow location access to begin guidance";
  $("navigation-location-status").textContent = "Waiting for GPS signal…";
  document.body.classList.add("nav-mode");
  void requestNavigationWakeLock();
  requestAnimationFrame(() => {
    map.invalidateSize();
    map.fitBounds(routeLayer.getBounds(), { padding: [100, 70], maxZoom: 16 });
  });
  navigationWatchId = navigator.geolocation.watchPosition(
    ({ coords }) => {
      updateNavigationDisplay(
        { lat: coords.latitude, lon: coords.longitude },
        coords.accuracy || 25,
        coords.speed,
      );
    },
    (error) => {
      $("navigation-instruction").textContent = "Location access is needed";
      $("navigation-subtext").textContent = error.code === error.PERMISSION_DENIED
        ? "Allow location access in your browser settings, then restart navigation"
        : "GPS signal unavailable · move to an open area and try again";
      $("navigation-location-status").textContent = "Waiting for GPS signal…";
    },
    { enableHighAccuracy: true, maximumAge: 1000, timeout: 20000 },
  );
}

async function requestNavigationWakeLock() {
  if (!document.body.classList.contains("nav-mode") || navigationWakeLock) return;
  if (!navigator.wakeLock) {
    $("navigation-location-status").textContent =
      "Keep your screen awake during navigation";
    return;
  }
  try {
    const wakeLock = await navigator.wakeLock.request("screen");
    if (!document.body.classList.contains("nav-mode")) {
      await wakeLock.release();
      return;
    }
    navigationWakeLock = wakeLock;
    navigationWakeLock.addEventListener("release", () => {
      navigationWakeLock = null;
    }, { once: true });
  } catch (error) {
    console.warn("Could not keep the screen awake during bike navigation.", error);
    $("navigation-location-status").textContent =
      "Screen wake lock unavailable · keep your screen awake";
  }
}

function endNavigation() {
  if (navigationWatchId !== null) {
    navigator.geolocation.clearWatch(navigationWatchId);
    navigationWatchId = null;
  }
  if (navigationMarker) navigationMarker.remove();
  if (navigationAccuracy) navigationAccuracy.remove();
  if (navigationWakeLock) {
    const wakeLock = navigationWakeLock;
    navigationWakeLock = null;
    void wakeLock.release().catch((error) => {
      console.warn("Could not release the bike-navigation screen wake lock.", error);
    });
  }
  navigationMarker = null;
  navigationAccuracy = null;
  activeNavigationRoute = null;
  navigationCoordinates = [];
  navigationCumulativeMeters = [];
  navigationLastPosition = null;
  $("navigation-ui").hidden = true;
  document.body.classList.remove("nav-mode");
  requestAnimationFrame(() => map.invalidateSize());
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    void requestNavigationWakeLock();
  }
});

async function planRoute() {
  if (!origin || !destination || routeButton.disabled || routeRequestPending) return;
  const requestId = ++routeRequestId;
  routeRequestPending = true;
  const routeRequest = {
    start: { ...origin },
    destination: { ...destination },
    stops: stops.map((stop) => stop.point && { ...stop.point }),
    return_trip: $("return-trip").checked,
  };
  routeButton.disabled = true;
  $("return-trip").disabled = true;
  routeButton.querySelector("span:first-child").textContent = "Comparing routes…";
  setRouteHint("Finding bike-route alternatives and sampling elevation…");
  try {
    const result = await fetchJson("/api/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(routeRequest),
    });
    if (requestId !== routeRequestId) return;
    routeOptions = [...(result.history_routes || []), ...result.routes];
    returnRoute = result.return_route;
    lastRoutedDestination = destination;
    setRidesVisible(false);
    $("route-result").hidden = false;
    $("estimate-note").textContent =
      `Based on your ${statusData.activity_count.toLocaleString()} imported rides (${statusData.personal_speed_kmh} km/h typical pace). Moving time excludes stops and traffic.`;
    $("route-road-note").textContent =
      "The quieter-roads option favors the available route with the least distance on signed M, N, and R roads. A short main-road link may still be needed.";
    selectedRouteId = routeOptions[0].id;
    renderRouteOptions(routeOptions);
    await selectRouteOption(routeOptions[0]);
    if (requestId !== routeRequestId) return;
    if (!result.elevation_available) {
      setRouteHint("Route options are ready, but terrain elevation could not be loaded.");
    }
  } catch (error) {
    setRouteHint(error.message, true);
  } finally {
    routeRequestPending = false;
    routeButton.querySelector("span:first-child").textContent = "Find route options";
    $("return-trip").disabled = false;
    updateRouteButton();
  }
}

$("add-stop-button").addEventListener("click", createStopField);
$("location-button").addEventListener("click", setLocation);
$("clear-origin").addEventListener("click", () => {
  originInput.value = "";
  originInput.dataset.selectedLabel = "";
  originField.point = null;
  if (originField.marker) originField.marker.remove();
  originField.marker = null;
  originMarker = null;
  origin = null;
  clearRouteDisplay();
  updateRouteButton();
});
$("clear-destination").addEventListener("click", () => {
  destinationInput.value = "";
  destinationInput.dataset.selectedLabel = "";
  destinationField.point = null;
  if (destinationField.marker) destinationField.marker.remove();
  destinationField.marker = null;
  destinationMarker = null;
  destination = null;
  destinationInput.focus();
  clearRouteDisplay();
  updateRouteButton();
});
$("map-locate").addEventListener("click", setLocation);
$("route-button").addEventListener("click", planRoute);
$("start-navigation").addEventListener("click", startNavigation);
$("end-navigation").addEventListener("click", endNavigation);
$("navigation-recenter").addEventListener("click", () => {
  if (navigationLastPosition) {
    map.setView(navigationLastPosition, Math.max(map.getZoom(), 16), { animate: true });
  }
});
$("return-trip").addEventListener("change", () => {
  const enabled = $("return-trip").checked;
  $("return-schedule").hidden = !enabled;
  if (!enabled) {
    $("plan-return-time").checked = false;
    $("return-sunset").setAttribute("aria-pressed", "false");
    $("departure-date").disabled = false;
    $("departure-clock").disabled = false;
    $("departure-label").textContent = "DEPARTURE TIME";
  }
  if (origin && destination && !routeRequestPending) void planRoute();
  else refreshRouteSchedule();
});
$("plan-return-time").addEventListener("change", () => {
  if ($("plan-return-time").checked && !$("return-trip").checked) {
    $("return-trip").checked = true;
  }
  $("return-schedule").hidden = !$("return-trip").checked;
  $("return-deadline-fields").hidden = !$("plan-return-time").checked;
  if (origin && destination && !routeRequestPending) void planRoute();
  else refreshRouteSchedule();
});
$("return-sunset").addEventListener("click", () => {
  const useSunset = $("return-sunset").getAttribute("aria-pressed") !== "true";
  $("return-sunset").setAttribute("aria-pressed", String(useSunset));
  $("return-sunset").textContent = useSunset ? "Using sunset" : "At sunset";
  if ($("plan-return-time").checked) refreshRouteSchedule();
});
$("comfort-margin").addEventListener("change", refreshRouteSchedule);
$("rides-toggle").addEventListener("click", () => {
  setRidesVisible(!map.hasLayer(ridesLayer));
});
$("route-form").addEventListener("click", (event) => {
  const picker = event.target.closest("[data-pick-map]");
  if (!picker) return;
  const field = picker.dataset.pickMap === "origin"
    ? originField
    : picker.dataset.pickMap === "destination"
      ? destinationField
      : placeFields.get(`${picker.dataset.pickMap}-input`);
  if (!field) return;
  activePlaceField = field;
  pendingMapField = field;
  map.getContainer().style.cursor = "crosshair";
  setRouteHint(
    `Tap the map to choose your ${field.role === "origin" ? "departure point" : field.role === "destination" ? "arrival point" : "stop"}.`,
  );
});
$("close-result").addEventListener("click", () => {
  $("route-result").hidden = true;
  if (routeLayer) routeLayer.remove();
  lastRoutedDestination = null;
});
map.on("click", (event) => {
  const field = pendingMapField || activePlaceField || (!origin ? originField : destinationField);
  updatePlaceValue(
    field,
    event.latlng,
    `${event.latlng.lat.toFixed(5)}, ${event.latlng.lng.toFixed(5)}`,
  );
});
document.addEventListener("click", (event) => {
  if (event.target.closest(".route-form")) return;
  for (const field of placeFields.values()) field.suggestions.hidden = true;
});
$("sync-button").addEventListener("click", async () => {
  $("sync-button").disabled = true;
  try {
    await fetchJson("/api/sync", { method: "POST" });
    await loadStatus();
  } catch (error) {
    setRouteHint(error.message, true);
    $("sync-button").disabled = false;
  }
});
function setAuthMode(mode) {
  authMode = mode;
  const registering = mode === "register";
  $("login-tab").classList.toggle("active", !registering);
  $("register-tab").classList.toggle("active", registering);
  $("login-tab").setAttribute("aria-selected", String(!registering));
  $("register-tab").setAttribute("aria-selected", String(registering));
  $("auth-submit").querySelector("span:first-child").textContent =
    registering ? "Create private account" : "Sign in";
  $("auth-password").autocomplete = registering ? "new-password" : "current-password";
  $("auth-password").minLength = registering ? 12 : 1;
  $("auth-hint").textContent = registering
    ? "Use a unique password with at least 12 characters."
    : "Your rides are visible only after you sign in.";
  $("auth-error").hidden = true;
}
$("login-tab").addEventListener("click", () => setAuthMode("login"));
$("register-tab").addEventListener("click", () => setAuthMode("register"));
$("auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = $("auth-submit");
  const originalText = authMode === "register" ? "Create private account" : "Sign in";
  submit.disabled = true;
  submit.querySelector("span:first-child").textContent = "One moment…";
  $("auth-error").hidden = true;
  try {
    const result = await fetchJson(`/auth/${authMode}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: $("auth-email").value,
        password: $("auth-password").value,
      }),
    });
    csrfToken = result.csrf_token;
    $("auth-password").value = "";
    await loadStatus();
  } catch (error) {
    $("auth-error").textContent = error.message;
    $("auth-error").hidden = false;
  } finally {
    submit.disabled = false;
    submit.querySelector("span:first-child").textContent = originalText;
  }
});
$("logout-button").addEventListener("click", async () => {
  try {
    await fetchJson("/auth/logout", { method: "POST" });
    csrfToken = null;
    await loadCsrfToken();
    renderSignedOut();
  } catch (error) {
    setRouteHint(error.message, true);
  }
});
$("delete-account-button").addEventListener("click", () => {
  $("delete-password").value = "";
  $("delete-error").hidden = true;
  $("delete-dialog").showModal();
});
$("delete-dialog-close").addEventListener("click", () => $("delete-dialog").close());
$("delete-account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("delete-confirm-button");
  button.disabled = true;
  button.textContent = "Deleting securely…";
  $("delete-error").hidden = true;
  try {
    await fetchJson("/api/account", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("delete-password").value }),
    });
    $("delete-dialog").close();
    csrfToken = null;
    await loadCsrfToken();
    renderSignedOut();
  } catch (error) {
    $("delete-error").textContent = error.message;
    $("delete-error").hidden = false;
  } finally {
    button.disabled = false;
    button.textContent = "Delete my account and data";
  }
});
$("archive-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("archive-file").files[0];
  if (!file) return;
  const button = $("archive-submit");
  button.disabled = true;
  button.querySelector("span:first-child").textContent = "Importing securely…";
  $("archive-status").classList.remove("error");
  $("archive-status").textContent = "Reading your cycling activities. This can take a little while.";
  const formData = new FormData();
  formData.append("archive", file);
  try {
    const result = await fetchJson("/api/archive", { method: "POST", body: formData });
    $("archive-status").textContent =
      `Imported ${result.imported.toLocaleString()} rides. ${result.skipped.toLocaleString()} non-cycling or incomplete activities skipped.`;
    $("archive-file").value = "";
    $("archive-file-name").textContent = "Choose Strava export ZIP";
    $("archive-file-detail").textContent = "Select the ZIP you downloaded from Strava";
    await loadStatus();
  } catch (error) {
    $("archive-status").classList.add("error");
    $("archive-status").textContent = error.message;
  } finally {
    button.disabled = false;
    button.querySelector("span:first-child").textContent = "Import ride history";
  }
});
$("archive-file").addEventListener("change", (event) => {
  const file = event.currentTarget.files[0];
  if (!file) {
    $("archive-file-name").textContent = "Choose Strava export ZIP";
    $("archive-file-detail").textContent = "Select the ZIP you downloaded from Strava";
    return;
  }
  const size = file.size < 1024 * 1024
    ? `${Math.max(1, Math.round(file.size / 1024))} KB`
    : `${(file.size / (1024 * 1024)).toFixed(1)} MB`;
  $("archive-file-name").textContent = file.name;
  $("archive-file-detail").textContent = `${size} · Ready to import`;
});
const infoDialog = $("info-dialog");
$("privacy-button").addEventListener("click", () => infoDialog.showModal());
$("menu-button").addEventListener("click", () => infoDialog.showModal());
$("dialog-close").addEventListener("click", () => infoDialog.close());
$("dialog-ok").addEventListener("click", () => infoDialog.close());
const toggleTheme = () => setTheme(
  document.documentElement.dataset.theme === "dark" ? "light" : "dark",
);
$("theme-toggle").addEventListener("click", toggleTheme);
$("dialog-theme-toggle").addEventListener("click", toggleTheme);

if (new URLSearchParams(location.search).has("connected")) {
  history.replaceState({}, "", "/");
}
async function initialize() {
  try {
    await loadCsrfToken();
    await loadStatus();
  } catch (error) {
    $("auth-error").textContent = error.message;
    $("auth-error").hidden = false;
  }
}
initialize();
setInterval(() => {
  if (statusData) loadStatus();
}, 60000);
