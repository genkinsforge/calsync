import io
import json
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

    def test_install_and_bundle_need_a_key(self):
        self.assertEqual(self.get("/agent/install")[0], 401)
        self.assertEqual(self.get("/agent/bundle.tar.gz", token="wrong")[0], 401)

    def test_install_script_embeds_public_url_and_the_callers_key(self):
        code, ctype, body = self.get("/agent/install", self.agent,
                                     {"Host": "calsync.jaredcluff.com", "X-Forwarded-Proto": "https"})
        self.assertEqual(code, 200)
        text = body.decode()
        self.assertIn("HUB='https://calsync.jaredcluff.com'", text)
        self.assertIn(f"TOKEN='{self.agent}'", text)
        self.assertTrue(text.startswith("#!/bin/sh"))

    def test_bundle_contains_the_agent(self):
        code, ctype, body = self.get("/agent/bundle.tar.gz", self.agent)
        self.assertEqual((code, ctype), (200, "application/gzip"))
        names = tarfile.open(fileobj=io.BytesIO(body)).getnames()
        for name in ("calsync-agent/calsync-agent.js", "calsync-agent/install.sh", "calsync-agent/calsync-agent.sh"):
            self.assertIn(name, names)

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
