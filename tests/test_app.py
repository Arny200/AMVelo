from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch


APP_PATH = Path(__file__).resolve().parents[1] / "1.py"
SPEC = importlib.util.spec_from_file_location("cadence_test_application", APP_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load the Cadence application for tests.")
APPLICATION = importlib.util.module_from_spec(SPEC)


class AccountIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_directory = tempfile.TemporaryDirectory()
        os.environ["BIKE_MAP_DATABASE"] = str(Path(cls.temp_directory.name) / "test.sqlite3")
        os.environ["SECRET_KEY"] = "test-secret-" + "x" * 40
        os.environ["STRAVA_VERIFY_TOKEN"] = "test-webhook-token"
        SPEC.loader.exec_module(APPLICATION)
        APPLICATION.app.config.update(TESTING=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_directory.cleanup()
        os.environ.pop("BIKE_MAP_DATABASE", None)
        os.environ.pop("SECRET_KEY", None)
        os.environ.pop("STRAVA_VERIFY_TOKEN", None)

    def csrf(self, client) -> str:
        response = client.get("/api/csrf")
        self.assertEqual(response.status_code, 200)
        return response.json["csrf_token"]

    def register(self, client):
        csrf = self.csrf(client)
        response = client.post(
            "/auth/register",
            json={
                "email": f"{uuid.uuid4().hex}@example.test",
                "password": "private-test-password-123",
            },
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 201, response.json)
        return response.json["csrf_token"]

    def test_anonymous_users_cannot_read_private_apis(self) -> None:
        client = APPLICATION.app.test_client()
        for endpoint in ("/api/status", "/api/activities"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(client.get(endpoint).status_code, 401)
        csrf = self.csrf(client)
        self.assertEqual(
            client.post(
                "/api/route", json={}, headers={"X-CSRF-Token": csrf}
            ).status_code,
            401,
        )
        self.assertEqual(client.post("/api/sync").status_code, 400)
        self.assertEqual(
            client.post(
                "/auth/register",
                json={"email": "no-csrf@example.test", "password": "private-test-password-123"},
            ).status_code,
            400,
        )

    def test_activity_ids_data_and_encrypted_routes_are_account_scoped(self) -> None:
        first = APPLICATION.app.test_client()
        second = APPLICATION.app.test_client()
        first_csrf = self.register(first)
        second_csrf = self.register(second)
        with first.session_transaction() as state:
            first_user_id = state["user_id"]
        with second.session_transaction() as state:
            second_user_id = state["user_id"]

        activity_id = "same-strava-id"
        APPLICATION.save_activity(
            first_user_id,
            {
                "id": activity_id,
                "type": "Ride",
                "name": "Private street name",
                "distance": 10_000,
                "moving_time": 2_000,
                "average_speed": 5,
                "map": {"summary_polyline": "private-route-polyline"},
            },
        )
        APPLICATION.save_activity(
            second_user_id,
            {
                "id": activity_id,
                "type": "Ride",
                "name": "Another private ride",
                "distance": 10_000,
                "moving_time": 5_000,
                "average_speed": 2,
                "map": {"summary_polyline": "another-private-route"},
            },
        )

        first_status = first.get("/api/status").json
        second_status = second.get("/api/status").json
        self.assertEqual(first_status["activity_count"], 1)
        self.assertEqual(second_status["activity_count"], 1)
        self.assertNotEqual(
            first_status["personal_speed_kmh"],
            second_status["personal_speed_kmh"],
        )
        self.assertEqual(first.get("/api/activities").json[0]["name"], "Private street name")
        self.assertEqual(second.get("/api/activities").json[0]["name"], "Another private ride")
        with APPLICATION.connect_db() as connection:
            saved = connection.execute(
                "SELECT name, polyline FROM activities WHERE user_id = ? AND id = ?",
                (first_user_id, activity_id),
            ).fetchone()
        self.assertNotIn("Private street name", saved["name"])
        self.assertNotIn("private-route-polyline", saved["polyline"])
        self.assertEqual(
            first.delete(
                "/api/account",
                json={"password": "private-test-password-123"},
                headers={"X-CSRF-Token": first_csrf},
            ).status_code,
            200,
        )
        self.assertEqual(second.get("/api/status").status_code, 200)

    def test_recorded_routes_match_endpoints_rank_by_frequency_and_stay_private(self) -> None:
        with APPLICATION.connect_db() as connection:
            connection.execute("DELETE FROM auth_attempts")
        owner = APPLICATION.app.test_client()
        other = APPLICATION.app.test_client()
        self.register(owner)
        self.register(other)
        with APPLICATION.connect_db() as connection:
            connection.execute("DELETE FROM auth_attempts")
        with owner.session_transaction() as state:
            owner_id = state["user_id"]
        with other.session_transaction() as state:
            other_id = state["user_id"]

        track = [
            (51.898 + index * 0.00001, -8.475 + index * 0.0008)
            for index in range(201)
        ]
        encoded_track = APPLICATION.encode_polyline(track)
        for index, moving_time in enumerate((1800, 2000, 2200)):
            APPLICATION.save_activity(
                owner_id,
                {
                    "id": f"repeated-route-{index}",
                    "type": "Ride",
                    "name": "Harbour loop",
                    "distance": 12_000,
                    "moving_time": moving_time,
                    "average_speed": 6,
                    "start_date": f"2025-01-0{index + 1}T12:00:00Z",
                    "map": {"summary_polyline": encoded_track},
                },
            )
        APPLICATION.save_activity(
            other_id,
            {
                "id": "private-other-account",
                "type": "Ride",
                "distance": 12_000,
                "moving_time": 900,
                "average_speed": 13,
                "map": {"summary_polyline": encoded_track},
            },
        )

        start, destination = track[0], track[-1]
        matches = APPLICATION.matched_history_routes(owner_id, start, destination)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["personal_seconds"], 2000)
        self.assertEqual(matches[0]["history_ride_count"], 3)
        self.assertEqual(matches[0]["categories"], ["Your recorded route"])
        self.assertEqual(matches[0]["recorded_activity_name"], "Harbour loop")
        self.assertEqual(len(matches[0]["geometry"]["coordinates"]), len(track))
        self.assertTrue(matches[0]["navigation_instructions"])
        slightly_offset_start = (start[0] + 0.004, start[1])
        offset_matches = APPLICATION.matched_history_routes(
            owner_id, slightly_offset_start, destination
        )
        self.assertEqual(len(offset_matches), 1)
        reverse_matches = APPLICATION.matched_history_routes(
            owner_id, destination, start
        )
        reversed_coordinates = reverse_matches[0]["geometry"]["coordinates"]
        self.assertEqual(len(reversed_coordinates), len(track))
        for coordinate, expected_point in zip(reversed_coordinates, reversed(track)):
            self.assertAlmostEqual(coordinate[0], expected_point[1], places=5)
            self.assertAlmostEqual(coordinate[1], expected_point[0], places=5)
        self.assertEqual(
            APPLICATION.matched_history_routes(other_id, start, destination)[0][
                "personal_seconds"
            ],
            900,
        )
        decoded_track = APPLICATION.decode_polyline(encoded_track)
        for decoded_point, original_point in zip(decoded_track, track):
            self.assertAlmostEqual(decoded_point[0], original_point[0], places=5)
            self.assertAlmostEqual(decoded_point[1], original_point[1], places=5)
        self.assertEqual(APPLICATION.decode_polyline("!"), [])

    def test_archive_upload_imports_only_into_the_signed_in_account(self) -> None:
        owner = APPLICATION.app.test_client()
        other = APPLICATION.app.test_client()
        owner_csrf = self.register(owner)
        self.register(other)
        with owner.session_transaction() as state:
            owner_id = state["user_id"]

        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "export/activities.csv",
                (
                    "Activity ID,Activity Name,Activity Type,Activity Date,"
                    "Distance,Moving Time,Elevation Gain,Filename\n"
                    "42,Private ride name,Ride,2025-02-03T12:00:00Z,12.5,2500,55,"
                    "activities/custom-route.gpx\n"
                    "43,Not a bike ride,Run,2025-02-04T12:00:00Z,5,1800,10,\n"
                ),
            )
            archive.writestr(
                "export/activities/custom-route.gpx",
                (
                    '<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>'
                    '<trkpt lat="51.5000" lon="-0.1000"/><trkpt lat="51.5010" lon="-0.1010"/>'
                    "</trkseg></trk></gpx>"
                ),
            )
            archive.writestr("export/profile.csv", "First Name,Last Name\nPrivate,Profile\n")
            archive.writestr("export/privacy_zones.csv", "Address,Radius\nPrivate,1000\n")
        archive_bytes.seek(0)
        imported = owner.post(
            "/api/archive",
            data={"archive": (archive_bytes, "my-strava-export.zip")},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": owner_csrf},
        )
        self.assertEqual(imported.status_code, 200, imported.json)
        self.assertEqual(imported.json["imported"], 1)
        self.assertEqual(imported.json["skipped"], 1)
        self.assertEqual(owner.get("/api/activities").json[0]["name"], "Private ride name")
        self.assertTrue(owner.get("/api/activities").json[0]["polyline"])
        self.assertEqual(other.get("/api/activities").json, [])
        with APPLICATION.connect_db() as connection:
            saved = connection.execute(
                "SELECT name, polyline FROM activities WHERE user_id = ?",
                (owner_id,),
            ).fetchone()
        self.assertNotIn("Private ride name", saved["name"])
        self.assertNotIn("51.5000", saved["polyline"])

    def test_fit_parser_skips_records_without_gps_fields(self) -> None:
        missing_coordinates = MagicMock(spec=APPLICATION.fitdecode.FitDataMessage)
        missing_coordinates.name = "record"
        missing_coordinates.get_value.return_value = None
        located_record = MagicMock(spec=APPLICATION.fitdecode.FitDataMessage)
        located_record.name = "record"
        located_record.get_value.side_effect = [51.5, -0.1]
        fit_reader = MagicMock()
        fit_reader.__enter__.return_value = iter(
            [missing_coordinates, located_record]
        )
        with patch.object(APPLICATION.fitdecode, "FitReader", return_value=fit_reader):
            points = APPLICATION.track_from_fit(io.BytesIO(b"test-fit-data"))
        self.assertEqual(points, [(51.5, -0.1)])

    def test_strava_webhook_uses_matching_accounts(self) -> None:
        owner = APPLICATION.app.test_client()
        other = APPLICATION.app.test_client()
        self.register(owner)
        self.register(other)
        with owner.session_transaction() as state:
            owner_id = state["user_id"]
        with other.session_transaction() as state:
            other_id = state["user_id"]
        with APPLICATION.connect_db() as connection:
            connection.execute(
                "UPDATE users SET athlete_id = ?, access_token = ? WHERE id = ?",
                ("athlete-test", APPLICATION.encrypt_private_data("token"), owner_id),
            )
        webhook_activity = {
            "id": 88,
            "type": "Ride",
            "name": "Webhook ride",
            "distance": 5000,
            "moving_time": 900,
            "average_speed": 5.55,
            "start_date": "2025-02-03T12:00:00Z",
            "map": {"summary_polyline": "encoded-private-route"},
        }
        event = {
            "object_type": "activity",
            "object_id": 88,
            "aspect_type": "create",
            "owner_id": "athlete-test",
        }
        with patch.object(APPLICATION, "strava_request", return_value=webhook_activity):
            response = owner.post("/webhooks/strava", json=event)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(owner.get("/api/status").json["activity_count"], 1)
        self.assertEqual(other.get("/api/status").json["activity_count"], 0)
        event["aspect_type"] = "delete"
        self.assertEqual(owner.post("/webhooks/strava", json=event).status_code, 200)
        self.assertEqual(owner.get("/api/status").json["activity_count"], 0)

    def test_personalized_route_requires_session_and_uses_own_speed(self) -> None:
        owner = APPLICATION.app.test_client()
        csrf = self.register(owner)
        with owner.session_transaction() as state:
            owner_id = state["user_id"]
        APPLICATION.save_activity(
            owner_id,
            {
                "id": "route-test",
                "type": "Ride",
                "name": "Private",
                "distance": 10_000,
                "moving_time": 2_000,
                "average_speed": 5,
                "map": {
                    "summary_polyline": APPLICATION.encode_polyline(
                        [
                            (53.35 + index * 0.002, -6.26 + index * 0.004)
                            for index in range(16)
                        ]
                    )
                },
            },
        )
        route_response = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 18_000,
                    "duration": 3_600,
                    "legs": [
                        {
                            "steps": [
                                {
                                    "distance": 200,
                                    "name": "Main Street",
                                    "maneuver": {"type": "depart"},
                                },
                                {
                                    "distance": 250,
                                    "name": "Oak Road",
                                    "maneuver": {"type": "turn", "modifier": "left"},
                                },
                            ]
                        }
                    ],
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-6.26, 53.35], [-6.20, 53.38]],
                    },
                }
            ],
        }
        with patch.object(APPLICATION, "request_json", return_value=route_response):
            response = owner.post(
                "/api/route",
                json={
                    "start": {"lat": 53.35, "lon": -6.26},
                    "destination": {"lat": 53.38, "lon": -6.20},
                },
                headers={"X-CSRF-Token": csrf},
            )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["personal_seconds"], 3_600)
        self.assertEqual(len(response.json["history_routes"]), 1)
        self.assertEqual(response.json["history_routes"][0]["personal_seconds"], 2_000)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("Turn left onto Oak Road", response.json["navigation_instructions"][1]["text"])
        self.assertIn("Your recorded route", response.json["history_routes"][0]["categories"])
        self.assertNotIn(
            "most scenic",
            [category.lower() for route in response.json["routes"] for category in route["categories"]],
        )

    def test_route_alternatives_stops_and_return_trip_use_personal_speed(self) -> None:
        owner = APPLICATION.app.test_client()
        csrf = self.register(owner)
        with owner.session_transaction() as state:
            owner_id = state["user_id"]
        APPLICATION.save_activity(
            owner_id,
            {
                "id": "multi-route-test",
                "type": "Ride",
                "name": "Private",
                "distance": 10_000,
                "moving_time": 2_000,
                "average_speed": 5,
                "map": {"summary_polyline": ""},
            },
        )
        route_response = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 10_000,
                    "duration": 1_000,
                    "legs": [{"steps": [{"distance": 400, "ref": "R807"}]}],
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [-6.26, 53.35],
                            [-6.24, 53.36],
                            [-6.20, 53.38],
                        ],
                    },
                },
                {
                    "distance": 12_000,
                    "duration": 1_100,
                    "legs": [{"steps": [{"distance": 12_000, "ref": "L1311"}]}],
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [-6.26, 53.35],
                            [-6.23, 53.37],
                            [-6.20, 53.38],
                        ],
                    },
                },
            ],
        }
        return_response = {
            "code": "Ok",
            "routes": [
                {
                    "distance": 11_000,
                    "duration": 1_050,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [-6.20, 53.38],
                            [-6.24, 53.36],
                            [-6.26, 53.35],
                        ],
                    },
                }
            ],
        }
        with patch.object(
            APPLICATION,
            "request_json",
            side_effect=[
                route_response,
                {"elevation": [10, 20, 15]},
                {"elevation": [10, 20, 40]},
                return_response,
            ],
        ) as route_request:
            response = owner.post(
                "/api/route",
                json={
                    "start": {"lat": 53.35, "lon": -6.26},
                    "stops": [{"lat": 53.36, "lon": -6.24}],
                    "destination": {"lat": 53.38, "lon": -6.20},
                    "return_trip": True,
                },
                headers={"X-CSRF-Token": csrf},
            )

        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(len(response.json["routes"]), 2)
        self.assertEqual(
            response.json["routes"][0]["categories"], ["fastest", "least climbing"]
        )
        self.assertEqual(
            response.json["routes"][1]["categories"],
            ["most climbing", "quieter roads"],
        )
        self.assertEqual(response.json["routes"][0]["main_road_distance_m"], 400)
        self.assertEqual(response.json["routes"][1]["main_road_distance_m"], 0)
        self.assertEqual(response.json["personal_seconds"], 2_000)
        self.assertEqual(response.json["return_route"]["personal_seconds"], 2_200)
        self.assertTrue(response.json["return_trip"])
        self.assertIn("-6.24,53.36", route_request.call_args_list[0].args[0])
        self.assertEqual(route_request.call_args_list[0].kwargs["params"]["alternatives"], "3")
        self.assertEqual(route_request.call_args_list[0].kwargs["params"]["steps"], "true")
        self.assertEqual(route_request.call_args_list[3].kwargs["params"]["alternatives"], "false")

    def test_route_main_road_exposure_counts_motorway_national_and_regional_refs(self) -> None:
        route = {
            "legs": [
                {
                    "steps": [
                        {"distance": 300, "ref": "R807"},
                        {"distance": 450, "ref": "N11"},
                        {"distance": 100, "ref": "M50"},
                        {"distance": 700, "ref": "L1311"},
                        {"distance": 250, "name": "Local road"},
                    ]
                }
            ]
        }
        self.assertEqual(APPLICATION.route_main_road_distance(route), 850)
        self.assertIsNone(APPLICATION.route_main_road_distance({"geometry": {}}))

    def test_ireland_geofence_includes_belfast_and_rejects_outside_points(self) -> None:
        for latitude, longitude in (
            (53.35, -6.26),  # Dublin
            (51.90, -8.47),  # Cork
            (53.2707, -9.0568),  # Galway
            (54.60, -5.93),  # Belfast
            (54.99, -7.31),  # Derry
            (53.49, -10.02),  # Clifden
            (52.14, -10.27),  # Dingle
        ):
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertTrue(APPLICATION.is_on_ireland_island(latitude, longitude))
        for latitude, longitude in (
            (51.51, -0.13),  # London
            (51.48, -3.18),  # Cardiff
            (53.00, -11.00),  # Atlantic Ocean
            (55.30, -4.50),  # Scotland
        ):
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertFalse(APPLICATION.is_on_ireland_island(latitude, longitude))

    def test_detailed_cork_harbour_coastline_includes_town_points(self) -> None:
        for latitude, longitude in (
            (51.798, -8.294),  # Crosshaven
            (51.872, -8.344),  # Passage West
            (51.798, -8.217),  # Roches Point
        ):
            with self.subTest(latitude=latitude, longitude=longitude):
                self.assertTrue(APPLICATION.is_on_ireland_island(latitude, longitude))

    def test_greenways_overlay_contains_named_irish_routes(self) -> None:
        greenways_path = APP_PATH.parent / "static" / "greenways.geojson"
        greenways = json.loads(greenways_path.read_text(encoding="utf-8"))
        self.assertEqual(greenways["type"], "FeatureCollection")
        self.assertGreater(len(greenways["features"]), 500)
        route_names = {feature["properties"]["name"] for feature in greenways["features"]}
        self.assertTrue(
            {
                "The Great Western Greenway",
                "Waterford Greenway",
                "Royal Canal Greenway",
                "Grand Canal Greenway",
                "Limerick Greenway",
                "South East Greenway",
                "Boyne Valley to Lakelands Greenway",
                "Old Rail Trail",
                "Cork Harbour Greenway",
                "Passage Railway Greenway",
                "Carrigaline to Crosshaven Greenway",
                "North Kerry Greenway",
            }.issubset(route_names)
        )
        for feature in greenways["features"]:
            self.assertEqual(feature["geometry"]["type"], "LineString")
            self.assertGreaterEqual(len(feature["geometry"]["coordinates"]), 2)
        for route_name in ("Cork Harbour Greenway", "Passage Railway Greenway"):
            cork_features = [
                feature for feature in greenways["features"]
                if feature["properties"]["name"] == route_name
            ]
            self.assertTrue(cork_features)
            for feature in cork_features:
                for longitude, latitude in feature["geometry"]["coordinates"]:
                    self.assertGreaterEqual(longitude, -8.45)
                    self.assertLessEqual(longitude, -8.35)
                    self.assertGreaterEqual(latitude, 51.86)
                    self.assertLessEqual(latitude, 51.91)
        crosshaven_features = [
            feature for feature in greenways["features"]
            if feature["properties"]["name"] == "Carrigaline to Crosshaven Greenway"
        ]
        self.assertEqual(
            {feature["properties"]["osm_way_id"] for feature in crosshaven_features},
            {1568008330, 189650238, 1568008331},
        )
        for feature in crosshaven_features:
            self.assertEqual(
                feature["properties"]["osm_name"], "Old Crosshaven Railway Path"
            )
            for longitude, latitude in feature["geometry"]["coordinates"]:
                self.assertGreaterEqual(longitude, -8.38)
                self.assertLessEqual(longitude, -8.30)
                self.assertGreaterEqual(latitude, 51.79)
                self.assertLessEqual(latitude, 51.82)

        passage_features = [
            feature for feature in greenways["features"]
            if feature["properties"].get("osm_relation_id") == 15860148
        ]
        self.assertEqual(len(passage_features), 24)
        passage_path = [
            feature for feature in passage_features
            if feature["properties"]["osm_way_id"] == 137087622
        ]
        self.assertEqual(len(passage_path), 1)
        self.assertEqual(
            passage_path[0]["properties"]["name"],
            "Cork Marina to Passage West Greenway",
        )
        self.assertEqual(
            passage_path[0]["properties"]["osm_way_name"], "Old Crosshaven Railway Path"
        )
        for longitude, latitude in passage_path[0]["geometry"]["coordinates"]:
            self.assertGreaterEqual(longitude, -8.39)
            self.assertLessEqual(longitude, -8.34)
            self.assertGreaterEqual(latitude, 51.87)
            self.assertLessEqual(latitude, 51.89)

    def test_amvelo_brand_and_greenways_toggle_are_present(self) -> None:
        index_path = APP_PATH.parent / "static" / "index.html"
        index_html = index_path.read_text(encoding="utf-8")
        self.assertIn("<title>AMVelo — your bike map</title>", index_html)
        self.assertIn('aria-label="AMVelo home"', index_html)
        self.assertIn('id="greenways-toggle"', index_html)
        self.assertIn('aria-label="Hide mapped Irish greenways"', index_html)
        self.assertIn('id="route-form"', index_html)
        self.assertIn('id="origin-input"', index_html)
        self.assertIn('id="destination-input"', index_html)
        self.assertIn('id="rides-toggle"', index_html)
        self.assertIn('id="archive-file-name"', index_html)
        self.assertIn('id="departure-time"', index_html)
        self.assertIn('id="return-by-time"', index_html)
        self.assertIn('id="comfort-margin"', index_html)
        self.assertIn('id="clock-dialog"', index_html)
        self.assertIn("DEPARTURE TIME", index_html)
        self.assertIn("ARRIVAL · IRELAND", index_html)
        self.assertIn('id="return-sunset"', index_html)
        self.assertIn('id="map-weather"', index_html)
        self.assertIn('id="start-navigation"', index_html)
        self.assertIn('id="navigation-ui"', index_html)
        self.assertNotIn("Most scenic", index_html)

    def test_route_api_rejects_points_outside_ireland(self) -> None:
        owner = APPLICATION.app.test_client()
        csrf = self.register(owner)
        with patch.object(APPLICATION, "request_json") as route_request:
            response = owner.post(
                "/api/route",
                json={
                    "start": {"lat": 51.51, "lon": -0.13},
                    "destination": {"lat": 53.35, "lon": -6.26},
                },
                headers={"X-CSRF-Token": csrf},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("island of Ireland", response.json["error"])
        route_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
