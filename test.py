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
import http.client
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
        scripts_dir = cls.scripts_dir = os.path.join(cls.temp_dir, "scripts")
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

    def _key_status(self, port, headers):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/config/read", headers=headers)
        return conn.getresponse().status

    def test_bad_api_key_is_ignored(self):
        """An empty, null or non-text api_key on /config/load or /config/update counts as no key:
        the current key keeps working, and no empty, missing or default key gets in"""
        try:
            for bad in ("", None, [], {}, True):
                self.post("/config/load", headers=self.headers, json={"api_key": bad})
                self.post("/config/update", headers=self.headers, json={"api_key": bad})
                self.assertEqual(self._key_status(self.port, self.headers), 200, repr(bad))
                for other in ({"Api-Key": ""}, {}, {"Api-Key": "your-secret-key"}):
                    self.assertEqual(self._key_status(self.port, other), 403, f"{bad!r} {other}")
        finally:
            self.post("/config/update", headers=self.headers, json={"api_key": self.api_key})

    def test_bad_api_key_in_config_file_means_default(self):
        """A config.json whose api_key is missing, empty, null or not text starts on the
        default key, never with an open or locked agent"""
        for stored in (None, "", "missing", ["x"]):
            temp = tempfile.mkdtemp(prefix="board_key_test_")
            port = find_free_port()
            config = {"name": "key-test"} if stored == "missing" else {"name": "key-test", "api_key": stored}
            with open(os.path.join(temp, "config.json"), "w") as f:
                json.dump(config, f)
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([self.mocks_dir, self.repo_dir])
            env["PICO_PORT"] = env["ESP32_PORT"] = str(port)
            proc = subprocess.Popen([sys.executable, self.app_py], cwd=temp, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 10
                while time.time() < deadline:
                    try:
                        if self._key_status(port, {"Api-Key": "your-secret-key"}) == 200:
                            break
                    except OSError:
                        time.sleep(0.1)
                self.assertEqual(self._key_status(port, {"Api-Key": "your-secret-key"}), 200, repr(stored))
                for other in ({"Api-Key": ""}, {}):
                    self.assertEqual(self._key_status(port, other), 403, f"{stored!r} {other}")
            finally:
                proc.terminate()
                proc.wait(timeout=5)
                shutil.rmtree(temp, ignore_errors=True)

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

    def test_gpio_scan_value_only_when_reversed(self):
        """A scan entry carries `value` and `reversed` only for a reversed pin, where value = 1 - level"""
        self.post("/gpio/config/26", headers=self.headers, json={"type": "output", "reversed": True})
        self.post("/gpio/config/5", headers=self.headers, json={"type": "output", "reversed": False})
        try:
            pins = {p["gpio"]: p for p in self.get("/gpio/scan", headers=self.headers).json()["gpios"]}
            self.assertEqual(pins[26]["reversed"], True)
            self.assertEqual(pins[26]["value"], 1 - pins[26]["level"])
            for gpio, p in pins.items():
                if gpio != 26:
                    self.assertNotIn("value", p, p)
                    self.assertNotIn("reversed", p, p)
        finally:
            self.post("/gpio/config/26", headers=self.headers, json={"type": "remove"})
            self.post("/gpio/config/5", headers=self.headers, json={"type": "remove"})

    def test_big_replies_exact_length(self):
        """Big replies carry an exact Content-Length and parse as JSON"""
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

    def _script(self, action, name, body=None, headers=None):
        if action == "read":
            return self.get(f"/script/read/{name}", headers=headers or self.headers)
        return self.post(f"/script/{action}/{name}", headers=headers or self.headers, json=body)

    def test_script_write_list_read_run_remove(self):
        """/script/* writes, lists, reads, runs (in-process: read(*args) or result) and removes a script"""
        name, err_name = "test_ci_script.py", "test_ci_script_err.py"
        src = "def read(*args):\n    return 'hi ' + ' '.join(args)\n"
        try:
            r = self._script("write", name, {"content": src})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"name": name, "size": len(src)})

            r = self.get("/script/list", headers=self.headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["write"], "enabled")
            entry = next(e for e in r.json()["scripts"] if e["name"] == name)
            self.assertEqual(entry["size"], len(src))
            self.assertRegex(entry["modified"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

            r = self._script("read", name)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"name": name, "content": src})

            r = self._script("run", name, {"args": ["a1", 22]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {"name": name, "exit_code": 0, "output": "hi a1 22"})

            # Overwrite with a `result` script, a dict reply comes back as JSON text, and a
            # script that raises exits 1. No body at all is fine.
            self.assertEqual(self._script("write", name, {"content": "result = 'v2'\n"}).status_code, 200)
            self.assertEqual(self._script("run", name).json()["output"], "v2")
            self.assertEqual(self._script("write", name, {"content": "result = {'a': 1}\n"}).status_code, 200)
            self.assertEqual(json.loads(self._script("run", name).json()["output"]), {"a": 1})
            self.assertEqual(self._script("write", err_name, {"content": "def read():\n    return 1 / 0\n"}).status_code, 200)
            r = self.post(f"/script/run/{err_name}", headers=self.headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["exit_code"], 1)

            r = self._script("write", name, {"remove": True})
            self.assertEqual(r.json(), {"name": name, "status": "removed"})
            self.assertEqual(self._script("read", name).status_code, 404)
            self.assertEqual(self._script("run", name).status_code, 404)
            self.assertEqual(self._script("write", name, {"remove": True}).json()["status"], "not found")
        finally:
            for n in (name, err_name):
                p = os.path.join(self.scripts_dir, n)
                if os.path.exists(p):
                    os.remove(p)

    def test_script_write_refused_with_default_key(self):
        """/script/write answers 403 while api_key is the shipped default"""
        default = {"Api-Key": "your-secret-key"}
        self.assertEqual(self.post("/config/update", headers=self.headers, json={"api_key": "your-secret-key"}).status_code, 200)
        try:
            self.assertEqual(self._script("write", "test_ci_default.py", {"content": "result = 1"}, default).status_code, 403)
            self.assertEqual(self.get("/script/list", headers=default).json()["write"], "default_key")
            self.assertFalse(os.path.exists(os.path.join(self.scripts_dir, "test_ci_default.py")))
        finally:
            self.post("/config/update", headers=default, json={"api_key": self.api_key})

    def test_script_write_lock(self):
        """/script/write can create write.lock, which then blocks every write and can't be changed or removed"""
        lock = os.path.join(self.scripts_dir, "write.lock")
        for name in ("write.lock", "WRITE.LOCK", "Write.Lock"):
            self.assertEqual(self._script("run", name).status_code, 400, name)
        self.assertFalse(os.path.exists(lock))
        try:
            r = self._script("write", "write.lock", {"content": "locked by the API"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(os.path.exists(lock))
            r = self.get("/script/list", headers=self.headers)
            self.assertEqual(r.json()["write"], "locked")
            self.assertIn("write.lock", [e["name"] for e in r.json()["scripts"]])
            self.assertEqual(self._script("write", "test_ci_locked.py", {"content": "result = 1"}).status_code, 403)
            self.assertEqual(self._script("write", self.test_script_name, {"remove": True}).status_code, 403)
            self.assertEqual(self._script("write", "write.lock", {"remove": True}).status_code, 403)
            self.assertEqual(self._script("write", "write.lock", {"content": ""}).status_code, 403)
            self.assertEqual(self._script("run", "write.lock").status_code, 400)
            self.assertTrue(os.path.exists(lock))
            self.assertTrue(os.path.exists(self.test_script_path))
            self.assertFalse(os.path.exists(os.path.join(self.scripts_dir, "test_ci_locked.py")))
            # Reads and runs still work while locked
            self.assertEqual(self._script("read", "write.lock").json()["content"], "locked by the API")
            self.assertEqual(self._script("read", self.test_script_name).status_code, 200)
            self.assertEqual(self._script("run", self.test_script_name).json()["exit_code"], 0)
        finally:
            if os.path.exists(lock):
                os.remove(lock)

    def test_script_bad_names_and_args(self):
        """/script/* rejects paths, hidden names, subfolders and non-alphanumeric args"""
        for action in ("read", "write", "run"):
            self.assertIn(self._script(action, "..%2Fmain.py", {"content": "x"}).status_code, (400, 404))
        for bad in [".hidden", "a%20b", "a..%5Cb"]:
            self.assertEqual(self._script("read", bad).status_code, 400, bad)
            self.assertEqual(self._script("write", bad, {"content": "x"}).status_code, 400, bad)
            self.assertEqual(self._script("run", bad).status_code, 400, bad)
        for bad in ["a%5Cb.py", "-rf", "x%00.py", "x" * 33]:
            self.assertEqual(self._script("write", bad, {"content": "x"}).status_code, 400, bad)
        # A subfolder is never listed, read, written or run
        sub = os.path.join(self.scripts_dir, "test_ci_dir")
        os.makedirs(sub, exist_ok=True)
        try:
            for action in ("read", "write", "run"):
                self.assertEqual(self._script(action, "test_ci_dir", {"content": "x"}).status_code, 400, action)
            r = self.get("/script/list", headers=self.headers)
            self.assertNotIn("test_ci_dir", [e["name"] for e in r.json()["scripts"]])
            self.assertEqual(os.listdir(sub), [])
        finally:
            os.rmdir(sub)
        self.assertEqual(self._script("run", self.test_script_name, {"args": ["a;b"]}).status_code, 400)
        self.assertEqual(self._script("run", self.test_script_name, {"args": "a"}).status_code, 422)
        self.assertEqual(self._script("write", "test_ci_x.py", {}).status_code, 400)
        self.assertEqual(self._script("read", "test_ci_missing.py").status_code, 404)
        # 32 characters is the longest name accepted
        long_name = "test_ci_" + "x" * 21 + "." + "py"
        self.assertEqual(len(long_name), 32)
        try:
            self.assertEqual(self._script("write", long_name, {"content": "result = 1"}).status_code, 200)
            self.assertEqual(self._script("read", long_name).status_code, 200)
        finally:
            p = os.path.join(self.scripts_dir, long_name)
            if os.path.exists(p):
                os.remove(p)
        self.assertEqual(self.get("/script/list").status_code, 403)

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
                try:
                    part = s.recv(4096)
                except ConnectionResetError:
                    # The agent closes without reading the body, so the body can draw a reset after the 400.
                    break
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


class _FakeConn:
    """A socket stand-in: recv() hands out `chunks` in order, send() records what went out."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False

    def settimeout(self, t):
        pass

    def setblocking(self, flag):
        pass

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b""

    def send(self, data):
        self.sent += bytes(data)
        return len(data)

    def close(self):
        self.closed = True


class TestRequestReading(unittest.TestCase):
    """_recv_request / _parse_request / handle_request in-process, on a fake socket."""

    @classmethod
    def setUpClass(cls):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path[:0] = [os.path.join(repo_dir, "test_mocks"), repo_dir]
        with open(os.path.join(repo_dir, "app.py")) as f:
            src = f.read().replace("\nmain()\n", "\n")
        cls.app = {"__name__": "app_under_test"}
        exec(compile(src, "app.py", "exec"), cls.app)

    def test_body_split_across_reads(self):
        body = json.dumps({"padding": "x" * 3000}).encode()
        head = b"POST /config/update HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(body)
        conn = _FakeConn([head + body[:500]] + [body[i:i + 1024] for i in range(500, len(body), 1024)])
        got_head, got_body = self.app["_recv_request"](conn)
        self.assertEqual(got_head, head)
        self.assertEqual(bytes(got_body), body)

    def test_chunked_body_refused(self):
        head = b"POST /config/load HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
        self.assertIsNone(self.app["_recv_request"](_FakeConn([head])))

    def test_out_of_memory_answers_500(self):
        body = json.dumps({"padding": "x" * 2000}).encode()
        conn = _FakeConn([b"POST /config/update HTTP/1.1\r\nContent-Length: %d\r\n\r\n" % len(body), body])
        manager = type("Manager", (), {"log": lambda s, m: None})()
        ujson = self.app["ujson"]
        real_loads = ujson.loads

        def no_memory(*_args, **_kwargs):
            raise MemoryError

        ujson.loads = no_memory
        try:
            self.app["handle_request"](conn, manager)
        finally:
            ujson.loads = real_loads
        self.assertTrue(conn.sent.startswith(b"HTTP/1.1 500 "), conn.sent[:40])
        self.assertTrue(conn.closed)


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
        self.probe_secs = self.app["_WIFI_PROBE_SECS"]
        self.reconnect_secs = self.app["_WIFI_RECONNECT_SECS"]

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

        self.manager = manager
        self.watch = self.app["HeapWatch"](manager, largest=lambda: self.largest,
                                           reset=reset, now=lambda: self.t)
        self.low = self.app["_HEAP_LOW_BYTES"]
        self.low_secs = self.app["_HEAP_LOW_SECS"]
        self.grace = self.app["_HEAP_BOOT_GRACE_SECS"]
        self.run_for(self.grace)   # a normal boot: healthy through the grace period

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

    def test_no_reset_in_the_first_minutes_after_boot(self):
        def reset():
            self.resets += 1
        self.watch = self.app["HeapWatch"](self.manager, largest=lambda: self.largest,
                                           reset=reset, now=lambda: self.t)
        self.largest = self.low - 1   # low from the start, like a C3 just after boot
        self.run_for(self.grace - 1)
        self.assertEqual(self.resets, 0)
        self.run_for(10)              # still low when the grace ends: reset then
        self.assertGreaterEqual(self.resets, 1)

    def test_unreadable_heap_never_resets(self):
        self.largest = None
        self.run_for(10 * self.low_secs)
        self.assertEqual(self.resets, 0)


class TestJoinSurrogates(unittest.TestCase):
    """ujson leaves an escaped surrogate pair (\\ud83c\\udf21) as two characters; a script write
    joins them, so an emoji sent escaped is stored as UTF-8. CPython's json joins pairs itself, so
    the HTTP tests can't show this: the helper is called directly."""

    @classmethod
    def setUpClass(cls):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path[:0] = [os.path.join(repo_dir, "test_mocks"), repo_dir]
        with open(os.path.join(repo_dir, "app.py")) as f:
            src = f.read().replace("\nmain()\n", "\n")
        ns = {"__name__": "app_under_test"}
        exec(compile(src, "app.py", "exec"), ns)
        cls.join = staticmethod(ns["_join_surrogates"])

    def test_join_pairs(self):
        self.assertEqual(self.join("a \ud83c\udf21 b"), "a \U0001F321 b")

    def test_plain_text_untouched(self):
        s = "# 21.5 \u00b0C\r\nno final newline"
        self.assertIs(self.join(s), s)

    def test_lone_halves_kept(self):
        self.assertEqual(self.join("\ud83c x \udf21"), "\ud83c x \udf21")


class TestUpgrade(unittest.TestCase):
    """POST /upgrade's upgrade.lock, and upgrader.py run against a local release folder."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading
        cls.repo_dir = os.path.dirname(os.path.abspath(__file__))
        cls.mocks_dir = os.path.join(cls.repo_dir, "test_mocks")
        sys.path[:0] = [cls.mocks_dir, cls.repo_dir]
        # app.py starts the server on its last line; load everything above it.
        with open(os.path.join(cls.repo_dir, "app.py")) as f:
            src = f.read().replace("\nmain()\n", "\n")
        cls.app = {"__name__": "app_under_test"}
        exec(compile(src, "app.py", "exec"), cls.app)
        cls.files = [list(item) for item in cls.app["UPGRADE_FILES"]]

        # The release: every UPGRADE_FILES entry, padded past its minimum size.
        cls.release = tempfile.mkdtemp(prefix="upgrade_release_")
        for name, kind, minimum in cls.files:
            body = b"M" + b"\x06" * minimum if kind == "mpy" else b"import new\n" + b"#" * minimum + b"\n"
            with open(os.path.join(cls.release, name), "wb") as f:
                f.write(body)
        release = cls.release

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=release, **kwargs)

            def log_message(self, *args):
                pass

        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = "http://127.0.0.1:%d/" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.release, ignore_errors=True)

    def setUp(self):
        self.board = tempfile.mkdtemp(prefix="upgrade_board_")
        self.addCleanup(shutil.rmtree, self.board, True)
        for name, kind, minimum in self.files:
            self.write(name, b"old " + name.encode())
        self.write("config.json", b'{"name": "kept"}')

    def write(self, name, data):
        with open(os.path.join(self.board, name), "wb") as f:
            f.write(data)

    def read(self, name):
        with open(os.path.join(self.board, name), "rb") as f:
            return f.read()

    def exists(self, name):
        return os.path.exists(os.path.join(self.board, name))

    def lock(self, url, attempt=0):
        self.write("upgrade.lock", json.dumps({"url": url, "files": self.files, "attempt": attempt}).encode())

    def run_upgrader(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ESP32_PORT", "PICO_PORT")}
        env["PYTHONPATH"] = self.mocks_dir
        proc = subprocess.run([sys.executable, os.path.join(self.repo_dir, "upgrader.py")],
                              cwd=self.board, env=env, capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())   # machine.reset() is sys.exit(0)

    def result(self):
        return json.loads(self.read("upgrade-result.json"))

    def test_upgrade_writes_the_lock(self):
        """POST /upgrade leaves upgrade.lock with the release URL and files, and says upgrading"""
        manager = type("Manager", (), {"log": lambda s, m: None})()
        cwd = os.getcwd()
        os.chdir(self.board)
        try:
            ok, payload, status = self.app["_do_upgrade"](manager)
            with open("upgrade.lock") as f:
                job = json.load(f)
        finally:
            os.chdir(cwd)
        self.assertEqual((ok, payload, status), (True, {"status": "upgrading"}, 200))
        self.assertEqual(job, {"url": self.app["UPGRADE_BASE_URL"], "files": self.files, "attempt": 0})

    def test_upgrade_without_upgrader_is_refused(self):
        """A board without upgrader.mpy answers 500 and writes no lock"""
        os.remove(os.path.join(self.board, "upgrader.mpy"))
        manager = type("Manager", (), {"log": lambda s, m: None})()
        cwd = os.getcwd()
        os.chdir(self.board)
        try:
            ok, payload, status = self.app["_do_upgrade"](manager)
        finally:
            os.chdir(cwd)
        self.assertEqual((ok, status), (False, 500))
        self.assertIn("USB", payload["error"])
        self.assertFalse(self.exists("upgrade.lock"))

    def test_upgrader_installs_the_release(self):
        """The upgrader swaps in every file, keeps .backup copies, and removes the lock"""
        self.lock(self.url)
        self.run_upgrader()
        for name, kind, minimum in self.files:
            with open(os.path.join(self.release, name), "rb") as f:
                self.assertEqual(self.read(name), f.read(), name)
            self.assertEqual(self.read(name + ".backup"), b"old " + name.encode())
            self.assertFalse(self.exists(name + ".new"))
        self.assertFalse(self.exists("upgrade.lock"))
        self.assertTrue(self.result()["ok"])
        self.assertEqual(self.read("config.json"), b'{"name": "kept"}')

    def test_upgrader_retries_then_gives_up(self):
        """A failed download keeps the lock for the next boot; the third failure gives up"""
        self.lock(self.url + "missing/")
        for attempt in (1, 2):
            self.run_upgrader()
            job = json.loads(self.read("upgrade.lock"))
            self.assertEqual(job["attempt"], attempt)
            self.assertIn("app.mpy: HTTP 404", job["error"])
            self.assertFalse(self.exists("upgrade-result.json"))
        self.run_upgrader()
        self.assertFalse(self.exists("upgrade.lock"))
        result = self.result()
        self.assertFalse(result["ok"])
        self.assertIn("gave up after 3 attempts", result["message"])
        for name, kind, minimum in self.files:
            self.assertEqual(self.read(name), b"old " + name.encode())
            self.assertFalse(self.exists(name + ".new"))

    def test_upgrader_restores_a_file_lost_mid_swap(self):
        """A file left only as .backup (a reset mid-swap) is put back before the next try"""
        os.rename(os.path.join(self.board, "app.mpy"), os.path.join(self.board, "app.mpy.backup"))
        self.lock(self.url + "missing/")
        self.run_upgrader()
        self.assertEqual(self.read("app.mpy"), b"old app.mpy")

    def test_unreadable_lock_ends_the_upgrade(self):
        """A broken upgrade.lock is removed so the agent boots"""
        self.write("upgrade.lock", b"{not json")
        self.run_upgrader()
        self.assertFalse(self.exists("upgrade.lock"))
        self.assertFalse(self.result()["ok"])

    def test_result_logged_once_at_boot(self):
        """The agent logs upgrade-result.json on boot and deletes it"""
        lines = []
        manager = type("Manager", (), {"log": lambda s, m: lines.append(m)})()
        self.write("upgrade-result.json", b'{"ok": true, "message": "app.mpy replaced (attempt 1)"}')
        cwd = os.getcwd()
        os.chdir(self.board)
        try:
            self.app["_log_upgrade_result"](manager)
            self.app["_log_upgrade_result"](manager)
        finally:
            os.chdir(cwd)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("[UPGRADE] Done, now " + self.app["VERSION"]))
        self.assertFalse(self.exists("upgrade-result.json"))


class TestAgentBlockCache(unittest.TestCase):
    """The agent block reads mac and SSID once, and sets config_updated on each config write."""

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
        self.board = tempfile.mkdtemp(prefix="agent_block_")
        self.addCleanup(shutil.rmtree, self.board, True)
        cwd = os.getcwd()
        os.chdir(self.board)
        self.addCleanup(os.chdir, cwd)
        with open("config.json", "w") as f:
            json.dump({"api_key": "k", "name": "t"}, f)
        old = time.time() - 3 * 86400
        os.utime("config.json", (old, old))
        self.manager = self.app["GPIOManager"]()

        calls = self.calls = []
        test = self

        class CountingWLAN(self.app["network"].WLAN):
            def config(self, param):
                calls.append(param)
                return super().config(param)

            def isconnected(self):
                return test.connected

        self.connected = True
        network = self.app["network"]
        real = network.WLAN
        network.WLAN = CountingWLAN
        self.addCleanup(setattr, network, "WLAN", real)

    def test_mac_and_ssid_read_once(self):
        """Two agent blocks cost one mac and one SSID read; a reconnect reads the SSID again"""
        first = self.manager._agent_info()
        second = self.manager._agent_info()
        self.assertEqual(first["mac"], second["mac"])
        self.assertTrue(first["mac"])
        self.assertEqual(first["wifi"], "TestWiFi")
        self.assertEqual(self.calls.count("mac"), 1)
        self.assertEqual(len([c for c in self.calls if c in ("essid", "ssid")]), 1)
        self.connected = False
        self.assertEqual(self.manager._agent_info()["wifi"], "")
        self.connected = True
        self.assertEqual(self.manager._agent_info()["wifi"], "TestWiFi")
        self.assertEqual(len([c for c in self.calls if c in ("essid", "ssid")]), 2)
        self.assertEqual(self.calls.count("mac"), 1)

    def test_config_updated_follows_writes(self):
        """config_updated starts at the file's time and moves when the agent writes the config"""
        boot = self.manager._agent_info()["config_updated"]
        self.assertEqual(boot, self.manager._config_stamp(os.stat("config.json")[8]))
        self.manager.config["name"] = "renamed"
        self.manager._save_config()
        self.assertEqual(self.manager._agent_info()["config_updated"], boot, "unchanged until the flush")
        self.manager._flush_config()
        flushed = self.manager._agent_info()["config_updated"]
        self.assertNotEqual(flushed, boot)
        self.assertEqual(flushed, self.manager._config_stamp(os.stat("config.json")[8]))
        self.manager._config_time = boot
        self.manager.load_config({"name": "loaded"})
        self.assertEqual(self.manager._agent_info()["config_updated"], self.manager._config_stamp(os.stat("config.json")[8]))


class TestResetNote(unittest.TestCase):
    """_reset() leaves last-reset.txt with its reason; the next boot logs it with reset_cause()."""

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
        self.dir = tempfile.mkdtemp(prefix="reset_note_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        cwd = os.getcwd()
        os.chdir(self.dir)
        self.addCleanup(os.chdir, cwd)
        self.calls = []
        machine = self.app["machine"]
        for name in ("reset", "soft_reset"):
            self.addCleanup(setattr, machine, name, getattr(machine, name))
            setattr(machine, name, lambda name=name: self.calls.append(name))
        self.lines = []
        self.manager = type("Manager", (), {"log": lambda s, m: self.lines.append(m)})()

    def test_note_written_then_logged_once(self):
        """The reason survives the reset and is logged, with the reset cause, at the next boot"""
        self.app["_reset"]("heap watchdog: test")
        self.assertEqual(self.calls, ["reset"])
        with open("last-reset.txt") as f:
            self.assertTrue(f.read().endswith(" heap watchdog: test"))
        self.app["_log_reset_cause"](self.manager)
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].startswith("[BOOT] Reset cause: power on; the agent reset itself at "))
        self.assertTrue(self.lines[0].endswith(" heap watchdog: test"))
        self.assertFalse(os.path.exists("last-reset.txt"))
        self.app["_log_reset_cause"](self.manager)
        self.assertEqual(self.lines[1], "[BOOT] Reset cause: power on")

    def test_soft_reset(self):
        """soft=True soft-resets (the /upgrade path) and still leaves the note"""
        self.app["_reset"]("POST /upgrade", soft=True)
        self.assertEqual(self.calls, ["soft_reset"])
        self.assertTrue(os.path.exists("last-reset.txt"))


class _FakeWebhookSock:
    """Answers one webhook POST with 200, or fails on write when broken."""
    def __init__(self, broken=False):
        self.broken = broken
        self.lines = [b"HTTP/1.1 200 OK\r\n", b"Content-Length: 0\r\n", b"\r\n"]
    def settimeout(self, t): pass
    def connect(self, addr): pass
    def write(self, data):
        if self.broken:
            raise OSError(104)
        return len(data)
    def readline(self):
        return self.lines.pop(0) if self.lines else b""
    def read(self, n): return b""
    def close(self): pass


class TestWebhookSender(unittest.TestCase):
    """A webhook delivery never holds the server for two slow attempts, and looks a host up once."""

    @classmethod
    def setUpClass(cls):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path[:0] = [os.path.join(repo_dir, "test_mocks"), repo_dir]
        with open(os.path.join(repo_dir, "app.py")) as f:
            src = f.read().replace("\nmain()\n", "\n")
        cls.app = {"__name__": "app_under_test"}
        exec(compile(src, "app.py", "exec"), cls.app)

    def setUp(self):
        self.sender = self.app["WebhookSender"]()

    def test_host_looked_up_once(self):
        """The address is cached per host and port, and dropped when a connect fails"""
        usocket = self.app["usocket"]
        lookups = []
        fail = [False]

        class Sock(_FakeWebhookSock):
            def connect(self, addr):
                if fail[0]:
                    raise OSError(113)

        for name, value in (("getaddrinfo", lambda h, p: lookups.append((h, p)) or [(0, 0, 0, "", ("10.0.0.5", p))]),
                            ("socket", lambda *a: Sock())):
            self.addCleanup(setattr, usocket, name, getattr(usocket, name))
            setattr(usocket, name, value)
        self.sender._connect("http://hooks.example/a")
        self.sender._connect("http://hooks.example/b")
        self.assertEqual(lookups, [("hooks.example", 80)])
        fail[0] = True
        with self.assertRaises(OSError):
            self.sender._connect("http://hooks.example/a")
        fail[0] = False
        self.sender._connect("http://hooks.example/a")
        self.assertEqual(len(lookups), 2, "looked up again after the failed connect")

    def test_fresh_connection_failure_not_retried_inline(self):
        """A new https connection that fails raises at once, for the retry queue to handle"""
        connects = []

        def connect(url):
            connects.append(url)
            raise OSError(116)

        self.sender._connect = connect
        with self.assertRaises(OSError):
            self.sender.send("https://hooks.example/x", {"gpio": 1}, "")
        self.assertEqual(len(connects), 1)

    def test_stale_reused_socket_resent_once(self):
        """A kept-alive socket the server closed is resent at once on a fresh connection"""
        connects = []
        self.sender._sock = _FakeWebhookSock(broken=True)
        self.sender._url, self.sender._host, self.sender._path = "https://hooks.example/x", "hooks.example", "/x"
        self.sender._last = self.app["utime"].ticks_ms()

        def connect(url):
            connects.append(url)
            return _FakeWebhookSock(), "hooks.example", "/x"

        self.sender._connect = connect
        self.sender.send("https://hooks.example/x", {"gpio": 1}, "")
        self.assertEqual(len(connects), 1)


class TestHttpsByChip(unittest.TestCase):
    """Only the S3 takes https webhook URLs; the classic and the C3 refuse them, each by name."""

    @staticmethod
    def load(chip):
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path[:0] = [os.path.join(repo_dir, "test_mocks"), repo_dir]
        with open(os.path.join(repo_dir, "app.py")) as f:
            src = f.read().replace("\nmain()\n", "\n")
        old = "_CHIP = PLATFORM.lower()"
        assert src.count(old) == 1
        app = {"__name__": "app_under_test"}
        exec(compile(src.replace(old, "_CHIP = %r" % chip), "app.py", "exec"), app)
        return app

    def test_s3_only(self):
        """https on the S3; a 400 message naming the board on the classic and the C3"""
        s3 = self.load("generic esp32s3 module with esp32-s3")
        self.assertTrue(s3["HTTPS_WEBHOOK_OK"])
        c3 = self.load("esp32c3 module with esp32-c3")
        self.assertFalse(c3["HTTPS_WEBHOOK_OK"])
        self.assertIn("ESP32-C3", c3["HTTPS_WEBHOOK_ERROR"])
        classic = self.load("generic esp32 module with esp32")
        self.assertFalse(classic["HTTPS_WEBHOOK_OK"])
        self.assertIn("classic ESP32", classic["HTTPS_WEBHOOK_ERROR"])


class TestNtpRetry(unittest.TestCase):
    """NTP is retried until the clock is set, and uptime survives the clock's jump."""

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
        self.clock = [946684800 + 600]   # 2000-01-01 00:10, a board whose clock was never set
        clock = self.clock

        class FakeTime:
            @staticmethod
            def time():
                return clock[0]

            @staticmethod
            def ticks_ms():
                return 12345

        for name, value in (("utime", FakeTime), ("_BOOT_TIME", clock[0] - 600)):
            self.addCleanup(self.app.__setitem__, name, self.app[name])
            self.app[name] = value
        self.addCleanup(self.app.__setitem__, "sync_time", self.app["sync_time"])
        self.lines = []
        self.manager = type("Manager", (), {"log": lambda s, m: self.lines.append(m)})()

    def test_failed_attempt_retries_later(self):
        """No answer from NTP: try again later, uptime untouched"""
        self.app["sync_time"] = lambda: False
        self.assertEqual(self.app["_retry_ntp"](self.manager), 12345)
        self.assertEqual(self.clock[0] - self.app["_BOOT_TIME"], 600)
        self.assertEqual(self.lines, [])

    def test_clock_set_keeps_uptime(self):
        """The clock jumps 26 years when NTP answers; uptime stays 10 minutes and retries stop"""
        def sync():
            self.clock[0] += 26 * 365 * 86400
            return True
        self.app["sync_time"] = sync
        self.assertIsNone(self.app["_retry_ntp"](self.manager))
        self.assertEqual(self.clock[0] - self.app["_BOOT_TIME"], 600)
        self.assertEqual(self.lines, ["[BOOT] Clock set over NTP"])
        self.assertEqual(self.app["_format_uptime"](self.clock[0] - self.app["_BOOT_TIME"]), "10m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
