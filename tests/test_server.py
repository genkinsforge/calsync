import json
import threading
import unittest
import urllib.error
import urllib.request

from calsync.server import authenticate, make_server, new_agent_token
from tests.fakes import Rig, agent_event, at


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.token = new_agent_token(self.r.cfg.path("agent_tokens_file"), "worklaptop")
        self.srv = make_server(self.r.cfg, self.r.store, host="127.0.0.1", port=0, now=lambda: self.r.now)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.r.close()

    def call(self, method, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if token is not False:
            req.add_header("Authorization", f"Bearer {token or self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def test_token_file_stores_only_hash(self):
        raw = self.r.cfg.path("agent_tokens_file").read_text()
        self.assertNotIn(self.token, raw)
        self.assertEqual(authenticate(f"Bearer {self.token}", json.loads(raw)["agents"]), "worklaptop")
        self.assertIsNone(authenticate("Bearer nope", json.loads(raw)["agents"]))

    def test_auth_required(self):
        self.assertEqual(self.call("GET", "/agent/blocks?calendar=phdata", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/agent/blocks?calendar=phdata", token="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/healthz", token=False)[0], 200)

    def test_push_sync_and_fetch_blocks(self):
        code, _ = self.call("GET", "/agent/blocks?calendar=phdata")
        self.assertEqual(code, 409, "no plan before the first sync")
        code, out = self.call("POST", "/agent/events", {"calendars": {
            "phdata": {"events": [agent_event("p1", at(1), at(2), title="Client sync")]},
            "kiewit": {"events": [agent_event("k1", at(3), at(4))]},
        }})
        self.assertEqual((code, out["stored"]), (200, {"phdata": 1, "kiewit": 1}))
        self.r.sync()
        code, out = self.call("GET", "/agent/blocks?calendar=phdata")
        self.assertEqual(code, 200)
        self.assertEqual(out["install_id"], "aaaaaaaaaaaa")
        self.assertEqual([b["title"] for b in out["blocks"]], ["Busy"])
        self.assertTrue(out["blocks"][0]["mirror_id"].startswith("aaaaaaaaaaaa.kiewit."))
        self.assertIn("start", out["window"])
        self.assertEqual(self.r.cals["personal"].mirrors()[0].title, "[phData] Client sync")

    def test_wrong_calendar_forbidden_and_bad_body_rejected(self):
        self.assertEqual(self.call("GET", "/agent/blocks?calendar=gigabug")[0], 403)
        self.assertEqual(self.call("POST", "/agent/events", {"calendars": {"gigabug": {"events": []}}})[0], 403)
        self.assertEqual(self.call("POST", "/agent/events", {"calendars": {"phdata": {"events": [{"id": 1}]}}})[0], 400)

    def test_agent_error_keeps_last_events(self):
        self.call("POST", "/agent/events", {"calendars": {"phdata": {"events": [agent_event("p1", at(1), at(2))]}}})
        self.call("POST", "/agent/events", {"calendars": {"phdata": {"error": "calendar not found"}}})
        push = self.r.store.get_push("phdata")
        self.assertEqual((len(push["events"]), push["error"]), (1, "calendar not found"))

    def test_kill_switch_blocks_agent(self):
        self.r.store.save_plan("phdata", [])
        self.r.cfg.kill_switch.write_text("")
        self.assertEqual(self.call("GET", "/agent/blocks?calendar=phdata")[0], 503)


if __name__ == "__main__":
    unittest.main()
