#!/usr/bin/env python3
import sys
import os
import time
import unittest
import json
import tempfile
import socket
import shutil
import subprocess
import urllib.request
import urllib.error

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

class HttpResponse:
    def __init__(self, status_code, body_bytes):
        self.status_code = status_code
        self._body = body_bytes

    def json(self):
        return json.loads(self._body.decode("utf-8"))

    @property
    def text(self):
        return self._body.decode("utf-8")

class TestEsp32GpioApi(unittest.TestCase):
    def __str__(self):
        return self._testMethodName

    @classmethod
    def setUpClass(cls):
        cls.repo_dir = os.path.dirname(os.path.abspath(__file__))
        cls.app_py = os.path.join(cls.repo_dir, "app.py")
        cls.mocks_dir = os.path.join(cls.repo_dir, "test_mocks")

        cls.port = find_free_port()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.api_key = "test-secret-key"
        cls.headers = {"Api-Key": cls.api_key}

        # Isolated temporary working directory so state files (config.json,
        # status.json, etc.) never touch the repository.
        cls.temp_dir = tempfile.mkdtemp(prefix="esp32_gpio_api_test_")

        # Initial config.json with our test API key
        with open(os.path.join(cls.temp_dir, "config.json"), "w") as f:
            json.dump({"api_key": cls.api_key, "name": "test-esp32"}, f)

        # Set up a test sensor script in scripts/ inside the temp directory
        scripts_dir = os.path.join(cls.temp_dir, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        cls.test_script_name = "test_ci_sensor.py"
        cls.test_script_path = os.path.join(scripts_dir, cls.test_script_name)
        with open(cls.test_script_path, "w") as f:
            f.write("def read(*args):\n    return {'value': 42}\n")

        # Environment with test_mocks on PYTHONPATH and port override
        env = dict(os.environ)
        existing_pythonpath = env.get("PYTHONPATH", "")
        py_paths = [cls.mocks_dir, cls.repo_dir]
        if existing_pythonpath:
            py_paths.append(existing_pythonpath)
        env["PYTHONPATH"] = os.pathsep.join(py_paths)
        env["ESP32_PORT"] = str(cls.port)
        env["PICO_PORT"] = str(cls.port)

        cmd = [sys.executable, cls.app_py]
        cls.server_proc = subprocess.Popen(
            cmd,
            cwd=cls.temp_dir,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # Wait for the HTTP server to respond
        deadline = time.time() + 10.0
        started = False
        while time.time() < deadline:
            if cls.server_proc.poll() is not None:
                raise RuntimeError(f"ESP32 server process exited prematurely with code {cls.server_proc.returncode}")
            try:
                res = cls.request("GET", "/hello", timeout=0.5)
                if res.status_code == 200:
                    started = True
                    break
            except Exception:
                pass
            time.sleep(0.1)

        if not started:
            cls.server_proc.terminate()
            raise RuntimeError("Timed out waiting for esp32-gpio-api server to start")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "server_proc") and cls.server_proc:
            cls.server_proc.terminate()
            try:
                cls.server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.server_proc.kill()
                cls.server_proc.wait()

        if hasattr(cls, "temp_dir") and os.path.exists(cls.temp_dir):
            shutil.rmtree(cls.temp_dir, ignore_errors=True)

    @classmethod
    def request(cls, method, path, headers=None, json_data=None, data_bytes=None, timeout=3):
        url = f"{cls.base_url}{path}"
        hdrs = dict(headers or {})
        body = None
        if json_data is not None:
            body = json.dumps(json_data).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif data_bytes is not None:
            body = data_bytes

        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            return HttpResponse(e.code, e.read())

    def get(self, path, headers=None, timeout=3):
        return self.request("GET", path, headers=headers, timeout=timeout)

    def post(self, path, headers=None, json=None, data=None, timeout=3):
        return self.request("POST", path, headers=headers, json_data=json, data_bytes=data, timeout=timeout)

    def test_hello(self):
        """GET /hello succeeds without auth and returns device name"""
        res = self.get("/hello")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("name", data)

    def test_auth_enforced(self):
        """Protected endpoints reject requests with missing or invalid Api-Key with 403"""
        res_no_auth = self.get("/config/read")
        self.assertEqual(res_no_auth.status_code, 403)

        res_bad_auth = self.get("/config/read", headers={"Api-Key": "invalid-key"})
        self.assertEqual(res_bad_auth.status_code, 403)

        res_ok = self.get("/config/read", headers=self.headers)
        self.assertEqual(res_ok.status_code, 200)

    def test_config_read(self):
        """GET /config/read returns complete agent configuration"""
        res = self.get("/config/read", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("agent", data)
        self.assertIn("settings", data)
        self.assertIn("gpios", data)
        self.assertIn("sensors", data)
        self.assertIn("platform", data["agent"])
        self.assertIn("version", data["agent"])
        self.assertEqual(data["agent"].get("host"), "esp32")

    def test_config_update(self):
        """POST /config/update applies runtime configuration changes"""
        res = self.post("/config/update", headers=self.headers, json={"log_days": 14})
        self.assertEqual(res.status_code, 200)

        read_res = self.get("/config/read", headers=self.headers)
        self.assertEqual(read_res.json().get("settings", {}).get("log_days"), 14)

        # Restore
        self.post("/config/update", headers=self.headers, json={"log_days": 7})

    def test_numbers_stored_as_text(self):
        """A number sent for a key, name, URL or script is stored as text"""
        name = self.get("/config/read", headers=self.headers).json().get("name")
        res = self.post("/config/update", headers=self.headers, json={"name": 77, "webhook_key": 7, "matter_key": 8})
        self.assertEqual(res.status_code, 200)
        cfg = self.get("/config/read", headers=self.headers).json()
        self.assertEqual(cfg["name"], "77")
        self.assertEqual(cfg["notifications"]["webhook_key"], "7")
        self.assertEqual(cfg["bridges"]["matter_key"], "8")
        # A numeric api_key becomes "4242", which then authenticates.
        self.assertEqual(self.post("/config/update", headers=self.headers, json={"api_key": 4242}).status_code, 200)
        try:
            self.assertEqual(self.get("/config/read", headers={"Api-Key": "4242"}).status_code, 200)
        finally:
            self.post("/config/update", headers={"Api-Key": "4242"}, json={"api_key": self.api_key})
        self.post("/config/update", headers=self.headers, json={"name": name, "webhook_key": "", "matter_key": ""})
        # The same on /config/load, and for a pin name and a sensor script.
        res = self.post("/config/load", headers=self.headers, json={"name": 9, "gpios": {"26": {"type": "output", "name": 777}}})
        self.assertEqual(res.status_code, 200)
        cfg = self.get("/config/read", headers=self.headers).json()
        self.assertEqual(cfg["name"], "9")
        self.assertEqual(cfg["gpios"]["26"]["name"], "777")
        self.post("/config/load", headers=self.headers, json={"name": name})
        self.assertEqual(self.post("/gpio/config/26", headers=self.headers, json={"type": "output", "name": 778}).status_code, 200)
        self.assertEqual(self.get("/gpio/read/26", headers=self.headers).json().get("name"), "778")
        res = self.post("/sensor/config/numeric", headers=self.headers, json={"script": 123})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get("script"), "123")
        self.post("/sensor/config/numeric", headers=self.headers, json={"remove": True})
        self.post("/gpio/config/26", headers=self.headers, json={"type": "remove"})

    def test_https_webhook_refused_on_classic(self):
        """A classic ESP32 (the mocked chip) refuses an https webhook URL, like the Pico W"""
        res = self.post("/config/update", headers=self.headers, json={"webhook_url": "https://example.com/hook"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("classic ESP32", res.json().get("error", ""))
        cfg = self.get("/config/read", headers=self.headers).json()
        self.assertNotEqual(cfg["notifications"].get("webhook"), "https://example.com/hook")
        # Plain http is accepted.
        res = self.post("/config/update", headers=self.headers, json={"webhook_url": "http://example.com/hook"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["notifications"]["webhook"], "http://example.com/hook")
        # /config/load drops an https URL instead of storing it.
        res = self.post("/config/load", headers=self.headers, json={"notifications": {"webhook": "https://example.com/hook"}})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["notifications"].get("webhook"))
        self.post("/config/update", headers=self.headers, json={"webhook_url": ""})

    def test_config_load(self):
        """POST /config/load loads bulk JSON configuration"""
        payload = {
            "gpios": {
                "26": {"name": "loaded_pin", "type": "output", "init": 0}
            }
        }
        res = self.post("/config/load", headers=self.headers, json=payload)
        self.assertEqual(res.status_code, 200)
        read_res = self.get("/config/read", headers=self.headers)
        self.assertIn("26", read_res.json().get("gpios", {}))

    def test_gpio_config_and_read(self):
        """POST /gpio/config configures a pin; GET /gpio/read reads by pin and alias"""
        res = self.post(
            "/gpio/config/26",
            headers=self.headers,
            json={"name": "relay", "type": "output", "init": 0, "reversed": True}
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("name"), "relay")
        self.assertEqual(data.get("reversed"), True)
        self.assertEqual(data.get("gpio"), 26)

        # Read by pin number
        read_pin = self.get("/gpio/read/26", headers=self.headers)
        self.assertEqual(read_pin.status_code, 200)
        pin_data = read_pin.json()
        self.assertEqual(pin_data.get("value"), 0)
        self.assertEqual(pin_data.get("name"), "relay")

        # Read by alias name
        read_alias = self.get("/gpio/read/relay", headers=self.headers)
        self.assertEqual(read_alias.status_code, 200)
        self.assertEqual(read_alias.json().get("gpio"), 26)

    def test_gpio_write_and_toggle(self):
        """POST /gpio/write supports setting 1, 0, and 'toggle'"""
        self.post("/gpio/config/26", headers=self.headers, json={"type": "output", "init": 0})

        # Write 1
        w1 = self.post("/gpio/write/26", headers=self.headers, json={"value": 1})
        self.assertEqual(w1.status_code, 200)
        self.assertEqual(w1.json().get("value"), 1)

        r1 = self.get("/gpio/read/26", headers=self.headers)
        self.assertEqual(r1.json().get("value"), 1)

        # Toggle to 0
        wt = self.post("/gpio/write/26", headers=self.headers, json={"value": "toggle"})
        self.assertEqual(wt.status_code, 200)
        self.assertEqual(wt.json().get("value"), 0)

        # Write 0
        w0 = self.post("/gpio/write/26", headers=self.headers, json={"value": 0})
        self.assertEqual(w0.status_code, 200)
        self.assertEqual(w0.json().get("value"), 0)

    def test_gpio_pulse_duration(self):
        """POST /gpio/write with duration pulses the output and reverts"""
        self.post("/gpio/config/26", headers=self.headers, json={"type": "output", "init": 0})
        pulse = self.post("/gpio/write/26", headers=self.headers, json={"value": 1, "duration": 0.2})
        self.assertEqual(pulse.status_code, 200)
        self.assertEqual(pulse.json().get("value"), 1)

        # Wait for duration pulse to revert
        time.sleep(0.35)
        r = self.get("/gpio/read/26", headers=self.headers)
        self.assertEqual(r.json().get("value"), 0)

    def test_gpio_read_all(self):
        """GET /gpio/read returns all configured pins"""
        self.post("/gpio/config/26", headers=self.headers, json={"type": "output", "init": 0})
        r = self.get("/gpio/read", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIsInstance(data, dict)
        self.assertIn("26", data)

    def test_gpio_watched(self):
        """GET /gpio/watched returns the map/list of watched pins"""
        r = self.get("/gpio/watched", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("watched", data)
        self.assertIsInstance(data["watched"], (dict, list))

    def test_gpio_remove(self):
        """POST /gpio/config with type='remove' deletes pin configuration"""
        self.post("/gpio/config/19", headers=self.headers, json={"type": "output"})
        r_rem = self.post("/gpio/config/19", headers=self.headers, json={"type": "remove"})
        self.assertEqual(r_rem.status_code, 200)
        self.assertEqual(r_rem.json().get("status"), "removed")

        r_read = self.get("/gpio/read", headers=self.headers)
        self.assertNotIn("19", r_read.json())

    def test_gpio_scan(self):
        """GET /gpio/scan reports scanned ESP32 GPIO pins"""
        r = self.get("/gpio/scan", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("gpios", data)
        self.assertIsInstance(data["gpios"], list)

    def test_big_replies_exact_length(self):
        """Replies sent in pieces still carry an exact Content-Length and parse as JSON"""
        for path in ("/gpio/scan", "/config/read", "/logs", "/gpio/read"):
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nApi-Key: {self.api_key}\r\n\r\n".encode())
                raw = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
            head, _, body = raw.partition(b"\r\n\r\n")
            length = next(int(l.split(b":", 1)[1]) for l in head.split(b"\r\n")
                          if l.lower().startswith(b"content-length:"))
            self.assertEqual(length, len(body), path)
            json.loads(body)

    def test_status_led_not_reconfigured(self):
        """Scan and read report the status LED pin (GPIO2 here) as the blinker's output, not as a re-created input"""
        r = self.get("/gpio/read/2", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["type"], "output")
        r = self.get("/gpio/scan", headers=self.headers)
        led = [p for p in r.json()["gpios"] if p["gpio"] == 2]
        self.assertEqual(len(led), 1)
        self.assertEqual(led[0]["type"], "output")

    def test_sensor_config_and_read(self):
        """POST /sensor/config and GET /sensor/read correctly execute and manage scripts"""
        r_cfg = self.post(
            "/sensor/config/test_sens",
            headers=self.headers,
            json={"script": self.test_script_name}
        )
        self.assertEqual(r_cfg.status_code, 200)
        self.assertEqual(r_cfg.json().get("name"), "test_sens")

        # Single-field unwraps value
        r_read = self.get("/sensor/read/test_sens", headers=self.headers)
        self.assertEqual(r_read.status_code, 200)
        self.assertEqual(r_read.json().get("test_sens"), 42)

        # Merged read
        r_all = self.get("/sensor/read", headers=self.headers)
        self.assertEqual(r_all.status_code, 200)
        self.assertIn("test_sens", r_all.json())

        # Remove sensor
        r_del = self.post("/sensor/config/test_sens", headers=self.headers, json={"remove": True})
        self.assertEqual(r_del.status_code, 200)
        self.assertEqual(r_del.json().get("status"), "removed")

        # 404 after removal
        r_gone = self.get("/sensor/read/test_sens", headers=self.headers)
        self.assertEqual(r_gone.status_code, 404)

    def test_sensor_security_path_traversal(self):
        """Sensor configuration rejects path traversal attempts with 400"""
        for bad in ["../app.py", "../boot.py", "/bin/sh", "/etc/passwd", "scripts/test.py", ".hidden"]:
            r = self.post("/sensor/config/pwn", headers=self.headers, json={"script": bad})
            self.assertEqual(r.status_code, 400, f"Expected 400 for bad path: {bad}")

    def test_sensor_security_arg_injection(self):
        """Sensor configuration rejects shell injection characters in arguments with 400"""
        for bad in [
            f"{self.test_script_name} ; rm -rf /",
            f"{self.test_script_name} && echo hi",
            f"{self.test_script_name} $(whoami)"
        ]:
            r = self.post("/sensor/config/pwn", headers=self.headers, json={"script": bad})
            self.assertEqual(r.status_code, 400, f"Expected 400 for injection: {bad}")

    def test_logs(self):
        """GET /logs returns log tail list"""
        r = self.get("/logs", headers=self.headers)
        self.assertEqual(r.status_code, 200)
        self.assertIn("logs", r.json())
        self.assertIsInstance(r.json()["logs"], list)

    def test_body_size_limit(self):
        """Requests exceeding 8KB payload limit are refused cleanly with 400 or 413"""
        huge_data = {"dummy": "x" * 12000}
        r = self.post("/config/load", headers=self.headers, json=huge_data)
        self.assertIn(r.status_code, (400, 413))

    def test_body_near_cap_accepted(self):
        """A body just under the 8KB cap is read whole and accepted"""
        r = self.post("/config/update", headers=self.headers, json={"padding": "x" * 7500})
        self.assertEqual(r.status_code, 200)

    def test_chunked_body_refused(self):
        """A chunked POST /config/load is refused with 400 and leaves the config unchanged"""
        before = self.get("/config/read", headers=self.headers).json()
        body = json.dumps({"name": "chunked-reset"}).encode()
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            # Headers alone first, so the agent's first recv() holds no body at all.
            s.sendall((f"POST /config/load HTTP/1.1\r\nHost: 127.0.0.1\r\nApi-Key: {self.api_key}\r\n"
                       "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n").encode())
            time.sleep(0.3)
            s.sendall(b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n")
            resp = b""
            while True:
                part = s.recv(4096)
                if not part:
                    break
                resp += part
        self.assertTrue(resp.startswith(b"HTTP/1.1 400"), resp[:60])
        after = self.get("/config/read", headers=self.headers)
        self.assertEqual(after.status_code, 200)
        self.assertEqual(after.json().get("name"), before.get("name"))
        self.assertEqual(after.json().get("gpios"), before.get("gpios"))

    def test_esp32_input_only_pins(self):
        """ESP32 input-only pins (34-39) reject output mode and pullups with 400"""
        # Configuring output on pin 34 must fail
        r_out = self.post("/gpio/config/34", headers=self.headers, json={"type": "output"})
        self.assertEqual(r_out.status_code, 400)

        # Configuring pullup on pin 34 must fail
        r_pull = self.post("/gpio/config/34", headers=self.headers, json={"type": "input", "pullup": "up"})
        self.assertEqual(r_pull.status_code, 400)

        # Configuring input with pullup none on pin 34 must succeed
        r_ok = self.post("/gpio/config/34", headers=self.headers, json={"type": "input", "pullup": "none"})
        self.assertEqual(r_ok.status_code, 200)

    def test_esp32_reserved_pins(self):
        """ESP32 reserved flash, REPL and PSRAM pins (1, 3, 6-11, 16, 17) reject configuration with 400"""
        for pin in [1, 3, 6, 7, 8, 9, 10, 11, 16, 17]:
            r = self.post(f"/gpio/config/{pin}", headers=self.headers, json={"type": "input"})
            self.assertEqual(r.status_code, 400, f"Expected 400 for reserved pin {pin}")

    def test_esp32_cpu_temperature(self):
        """GET /config/read returns cpu_temperature from ESP32 internal sensor"""
        res = self.get("/config/read", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        temp = res.json().get("agent", {}).get("cpu_temperature")
        self.assertIsNotNone(temp)
        self.assertIsInstance(temp, (int, float))


class TestWifiWatchdog(unittest.TestCase):
    """WifiWatchdog on a fake clock, Wi-Fi link and gateway probe (no network)."""

    @classmethod
    def setUpClass(cls):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path[:0] = [os.path.join(repo_dir, "test_mocks"), repo_dir]
        # app.py starts the server on its last line; load everything above it.
        with open(os.path.join(repo_dir, "app.py")) as f:
            src = f.read().replace("\nmain()\n", "\n")
        cls.app = {"__name__": "app_under_test"}
        exec(compile(src, "app.py", "exec"), cls.app)

    def setUp(self):
        self.t = 1000
        self.gateway_up = True
        self.probes = self.reconnects = self.resets = 0
        wlan = type("Wlan", (), {"isconnected": lambda s: True})()
        manager = type("Manager", (), {"log": lambda s, m: None, "_flush_all": lambda s: None})()

        def probe(_):
            self.probes += 1
            return self.gateway_up

        def reconnect(_):
            self.reconnects += 1

        def reset():
            self.resets += 1

        self.dog = self.app["WifiWatchdog"](wlan, manager, probe=probe, reconnect=reconnect,
                                            reset=reset, now=lambda: self.t)
        self.probe_secs = self.app["WIFI_PROBE_SECS"]
        self.reconnect_secs = self.app["WIFI_RECONNECT_SECS"]

    def idle(self, seconds, busy=False):
        for _ in range(seconds):
            self.t += 1
            self.dog.tick(busy=busy)

    def test_no_probe_while_traffic_flows(self):
        for _ in range(3 * self.probe_secs):
            self.t += 1
            self.dog.tick()
            self.dog.traffic()
        self.assertEqual(self.probes, 0)

    def test_healthy_idle_probes_once_per_interval(self):
        self.idle(3 * self.probe_secs)
        self.assertEqual(self.probes, 3)
        self.assertEqual((self.reconnects, self.resets), (0, 0))

    def test_no_probe_while_a_pulse_is_pending(self):
        self.idle(3 * self.probe_secs, busy=True)
        self.assertEqual(self.probes, 0)

    def test_one_failure_does_not_reconnect(self):
        self.gateway_up = False
        self.idle(self.probe_secs)
        self.gateway_up = True
        self.idle(2 * self.probe_secs)
        self.assertEqual((self.reconnects, self.resets), (0, 0))

    def test_two_failures_reconnect_and_recover(self):
        self.gateway_up = False
        self.idle(2 * self.probe_secs)
        self.assertEqual(self.reconnects, 1)
        self.gateway_up = True
        self.idle(self.reconnect_secs + 5)
        self.assertEqual(self.resets, 0)

    def test_still_dead_after_reconnect_resets(self):
        self.gateway_up = False
        self.idle(2 * self.probe_secs + self.reconnect_secs)
        self.assertEqual(self.reconnects, 1)
        self.assertGreaterEqual(self.resets, 1)

    def test_traffic_during_reconnect_counts_as_recovered(self):
        self.gateway_up = False
        self.idle(2 * self.probe_secs)
        self.dog.traffic()
        self.idle(self.reconnect_secs + 5)
        self.assertEqual(self.resets, 0)



class TestHeapWatch(unittest.TestCase):
    """HeapWatch on a fake clock and a fake largest-free-block reader."""

    @classmethod
    def setUpClass(cls):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path[:0] = [os.path.join(repo_dir, "test_mocks"), repo_dir]
        # app.py starts the server on its last line; load everything above it.
        with open(os.path.join(repo_dir, "app.py")) as f:
            src = f.read().replace("\nmain()\n", "\n")
        cls.app = {"__name__": "app_under_test"}
        exec(compile(src, "app.py", "exec"), cls.app)

    def setUp(self):
        self.t = 1000
        self.largest = 20000
        self.resets = 0
        manager = type("Manager", (), {"log": lambda s, m: None, "_flush_all": lambda s: None})()

        def reset():
            self.resets += 1

        self.watch = self.app["HeapWatch"](manager, largest=lambda: self.largest,
                                           reset=reset, now=lambda: self.t)
        self.low = self.app["HEAP_LOW_BYTES"]
        self.low_secs = self.app["HEAP_LOW_SECS"]

    def run_for(self, seconds, busy=False):
        for _ in range(seconds):
            self.t += 1
            self.watch.tick(busy=busy)

    def test_healthy_heap_never_resets(self):
        self.run_for(10 * self.low_secs)
        self.assertEqual(self.resets, 0)

    def test_low_heap_resets_after_the_grace_period(self):
        self.largest = self.low - 1
        self.run_for(self.low_secs - 10)
        self.assertEqual(self.resets, 0)
        self.run_for(20)
        self.assertGreaterEqual(self.resets, 1)

    def test_a_short_dip_does_not_reset(self):
        self.largest = self.low - 1
        self.run_for(self.low_secs // 2)
        self.largest = self.low + 1000
        self.run_for(10)
        self.largest = self.low - 1
        self.run_for(self.low_secs // 2)
        self.assertEqual(self.resets, 0)

    def test_no_reset_while_a_pulse_is_pending(self):
        self.largest = self.low - 1
        self.run_for(3 * self.low_secs, busy=True)
        self.assertEqual(self.resets, 0)
        self.run_for(10)
        self.assertGreaterEqual(self.resets, 1)

    def test_unreadable_heap_never_resets(self):
        self.largest = None
        self.run_for(10 * self.low_secs)
        self.assertEqual(self.resets, 0)


class TestJsonPieces(unittest.TestCase):
    """_json_pieces joins to exactly what ujson.dumps gives for the same object."""

    @classmethod
    def setUpClass(cls):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path[:0] = [os.path.join(repo_dir, "test_mocks"), repo_dir]
        with open(os.path.join(repo_dir, "app.py")) as f:
            src = f.read().replace("\nmain()\n", "\n")
        cls.app = {"__name__": "app_under_test"}
        exec(compile(src, "app.py", "exec"), cls.app)

    def test_matches_dumps(self):
        samples = [
            {},
            [],
            {"a": 1},
            {"agent": {"ip": "1.2.3.4", "uptime": "3m"}, "gpios": [{"gpio": 2, "value": 1}, {"gpio": 4, "value": 0}]},
            {"logs": ["2026-10-23 [Webhook] GPIO 23 \u2192 http://x", "line \"two\""]},
            {"gpios": {"23": {"type": "output", "init": 0}, "19": {"name": "b", "pullup": None}}, "name": "esp32"},
            {"nested": {"deep": {"deeper": [1, [2, 3], {"x": True}]}}},
            "plain", 7, None,
        ]
        for obj in samples:
            self.assertEqual("".join(self.app["_json_pieces"](obj)), json.dumps(obj), repr(obj))


if __name__ == "__main__":
    unittest.main(verbosity=2)
