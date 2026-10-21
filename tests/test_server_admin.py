import io
import json
import subprocess
import tarfile
import threading
import unittest
import urllib.error
import urllib.request

from calsync.server import make_server, new_agent_token
from tests.fakes import Rig


class AdminAndInstallTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.r.cfg.data["agents"]["jared"] = {"admin": True, "calendars": []}
        tokens = self.r.cfg.path("agent_tokens_file")
        self.agent = new_agent_token(tokens, "worklaptop")
        self.admin = new_agent_token(tokens, "jared")
        self.srv = make_server(self.r.cfg, self.r.store, host="127.0.0.1", port=0, now=lambda: self.r.now)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.r.close()

    def get(self, path, token=None, headers=None):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get("Content-Type"), e.read()

    def test_bundle_needs_a_key_and_the_installer_holds_none(self):
        self.assertEqual(self.get("/agent/bundle.tar.gz")[0], 401)
        self.assertEqual(self.get("/agent/bundle.tar.gz", token="wrong")[0], 401)
        code, _, body = self.get("/agent/install")
        self.assertEqual(code, 200)
        self.assertNotIn(self.agent, body.decode())

    def test_install_script_uses_the_public_url_and_keeps_the_key_out_of_arguments(self):
        code, ctype, body = self.get("/agent/install", headers={"Host": "calsync.jaredcluff.com",
                                                               "X-Forwarded-Proto": "https"})
        self.assertEqual(code, 200)
        text = body.decode()
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("HUB='https://calsync.jaredcluff.com'", text)
        self.assertIn("read -r TOKEN </dev/tty", text)
        self.assertIn('-K "$TMP/auth.curl"', text)
        self.assertNotIn("Bearer $TOKEN", text.replace('header = "Authorization: Bearer %s', ""))
        self.assertNotIn('"$TOKEN"\n', text)
        # config is written before install.sh loads the launchd job
        self.assertLess(text.index("setkey.js\" \"$CONF\""), text.index('sh "$TMP/calsync-agent/install.sh"'))
        subprocess.run(["sh", "-n"], input=body, check=True)

    def test_bundle_contains_the_agent(self):
        code, ctype, body = self.get("/agent/bundle.tar.gz", self.agent)
        self.assertEqual((code, ctype), (200, "application/gzip"))
        names = tarfile.open(fileobj=io.BytesIO(body)).getnames()
        for name in ("calsync-agent/calsync-agent.js", "calsync-agent/install.sh", "calsync-agent/calsync-agent.sh"):
            self.assertIn(name, names)

    def test_home_and_privacy_pages_are_public(self):
        for path in ("/", "/privacy"):
            code, ctype, body = self.get(path)
            self.assertEqual((code, ctype), (200, "text/html; charset=utf-8"))
            self.assertIn(b"Jared Cluff", body)

    def test_status_is_admin_only(self):
        self.assertEqual(self.get("/status")[0], 401)
        self.assertEqual(self.get("/status", self.agent)[0], 403)
        code, _, body = self.get("/status", self.admin)
        self.assertEqual(code, 200)
        self.assertIn("calendars", json.loads(body))

    def test_sync_trigger_is_admin_only(self):
        req = urllib.request.Request(self.base + "/sync", method="POST", data=b"")
        req.add_header("Authorization", f"Bearer {self.agent}")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()


if __name__ == "__main__":
    unittest.main()
