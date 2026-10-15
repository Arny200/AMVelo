from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


HOST = "127.0.0.1"
PORT = 8000
PROJECT_DIR = Path(__file__).resolve().parent


class PlanespottingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.serve_index()
        elif self.path == "/api/aircraft":
            self.proxy_json(
                "https://api.adsb.lol/v2/lat/51.8117/lon/-8.3989/dist/38"
            )
        elif self.path == "/api/weather":
            self.proxy_json(
                "https://api.open-meteo.com/v1/forecast"
                "?latitude=51.8117&longitude=-8.3989"
                "&current=temperature_2m,relative_humidity_2m,"
                "apparent_temperature,is_day,precipitation,cloud_cover,"
                "wind_speed_10m,weather_code&timezone=Europe%2FLondon"
            )
        else:
            self.send_error(404, "Not found")

    def serve_index(self):
        try:
            content = (PROJECT_DIR / "index.html").read_bytes()
        except OSError as error:
            self.send_error(500, f"Could not read index.html: {error}")
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def proxy_json(self, url):
        request = Request(url, headers={"User-Agent": "CarrigalinePlanespotter/1.0"})
        try:
            with urlopen(request, timeout=15) as response:
                content = response.read()
        except (URLError, TimeoutError) as error:
            self.send_json(502, {"error": f"Live data source unavailable: {error}"})
            return

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            self.send_json(502, {"error": f"Live data source returned invalid JSON: {error}"})
            return

        self.send_json(200, payload)

    def send_json(self, status, payload):
        content = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), PlanespottingHandler)
    print(f"Carrigaline Plane Spotter is running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
