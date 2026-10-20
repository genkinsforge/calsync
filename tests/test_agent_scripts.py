"""Checks for the work-Mac agent that run on any Mac without touching a calendar.

- the JXA agent compiles and its built-in logic tests pass;
- the shell wrapper's curl/plutil flow works against a real hub server, with a stub
  JXA script standing in for EventKit.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from calsync.server import make_server, new_agent_token
from tests.fakes import Rig, at

ROOT = Path(__file__).resolve().parent.parent
AGENT = ROOT / "agent"
HAVE_OSA = shutil.which("osascript") is not None and shutil.which("osacompile") is not None

STUB = r"""
ObjC.import('Foundation');
function run(argv) {
  var out = ObjC.unwrap($.NSProcessInfo.processInfo.environment.objectForKey('STUB_OUT'));
  function write(p, s) { $(s).writeToFileAtomicallyEncodingError(p, true, $.NSUTF8StringEncoding, null); }
  if (argv.indexOf('--calendar-names') >= 0) return 'phdata';
  var i = argv.indexOf('--export');
  if (i >= 0) {
    write(argv[i + 1], JSON.stringify({calendars: {phdata: {events: [
      {id: 'evt1@2026-10-20T13:00:00Z', title: 'Client sync', start: '%START%', end: '%END%', all_day: false,
       location: 'Zoom', notes: 'agenda', url: null, availability: 'busy', status: 'confirmed',
       self_status: 'accepted', marker: null}]}}}));
    return '';
  }
  i = argv.indexOf('--apply');
  if (i >= 0) {
    var s = ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError(argv[i + 1], $.NSUTF8StringEncoding, null));
    write(out + '/applied-' + argv[argv.indexOf('--calendar') + 1] + (argv.indexOf('--dry-run') >= 0 ? '-dry' : '') + '.json', s);
    return '';
  }
  return '';
}
"""


@unittest.skipUnless(HAVE_OSA, "needs macOS osascript")
class JxaAgentTests(unittest.TestCase):
    def test_compiles(self):
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(["osacompile", "-l", "JavaScript", "-o", f"{d}/a.scpt", str(AGENT / "calsync-agent.js")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_help_and_self_test(self):
        r = subprocess.run(["osascript", "-l", "JavaScript", str(AGENT / "calsync-agent.js"), "--help"],
                           capture_output=True, text=True)
        self.assertIn("--list-calendars", r.stdout)
        r = subprocess.run(["osascript", "-l", "JavaScript", str(AGENT / "calsync-agent.js"), "--self-test"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("self-test OK", r.stdout)

    def test_shell_scripts_parse_and_plist_lints(self):
        for s in ("calsync-agent.sh", "install.sh", "uninstall.sh"):
            self.assertEqual(subprocess.run(["sh", "-n", str(AGENT / s)]).returncode, 0, s)
        tpl = (AGENT / "com.genkinsforge.calsync-agent.plist.template").read_text().replace("__HOME__", "/Users/x")
        r = subprocess.run(["plutil", "-lint", "-"], input=tpl, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


@unittest.skipUnless(HAVE_OSA, "needs macOS osascript")
class WrapperRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.token = new_agent_token(self.r.cfg.path("agent_tokens_file"), "worklaptop")
        self.srv = make_server(self.r.cfg, self.r.store, host="127.0.0.1", port=0, now=lambda: self.r.now)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tmp = Path(tempfile.mkdtemp())
        stub = STUB.replace("%START%", at(1).strftime("%Y-%m-%dT%H:%M:%SZ")).replace("%END%", at(2).strftime("%Y-%m-%dT%H:%M:%SZ"))
        (self.tmp / "stub.js").write_text(stub)
        (self.tmp / "config.json").write_text(json.dumps({
            "hub_url": f"http://127.0.0.1:{self.srv.server_address[1]}/", "token": self.token,
            "calendars": {"phdata": {"title": "Calendar"}}}))
        self.env = dict(os.environ, HOME=str(self.tmp), STUB_OUT=str(self.tmp),
                        CALSYNC_AGENT_JS=str(self.tmp / "stub.js"), CALSYNC_AGENT_CONFIG=str(self.tmp / "config.json"))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.r.close()
        shutil.rmtree(self.tmp)

    def wrapper(self, *args):
        return subprocess.run(["sh", str(AGENT / "calsync-agent.sh"), *args], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def test_push_then_apply(self):
        first = self.wrapper()
        self.assertEqual(first.returncode, 1, "no plan yet, so the calendar is left alone")
        self.assertIn("could not fetch blocks", first.stderr)
        self.assertNotIn(self.token, first.stderr)
        self.assertEqual(self.r.store.get_push("phdata")["events"][0]["title"], "Client sync")
        self.r.cals["personal"].add("p1", at(5), at(6), title="Dentist")
        self.r.push("kiewit", [])
        self.r.sync()
        self.assertEqual(self.r.cals["personal"].mirrors()[0].title, "[phData] Client sync")
        second = self.wrapper()
        self.assertEqual(second.returncode, 0, second.stderr)
        applied = json.loads((self.tmp / "applied-phdata.json").read_text())
        self.assertEqual([b["title"] for b in applied["blocks"]], ["Busy"])
        dry = self.wrapper("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertTrue((self.tmp / "applied-phdata-dry.json").exists())

    def test_kill_switch_file(self):
        (self.tmp / ".config" / "calsync-agent").mkdir(parents=True)
        (self.tmp / ".config" / "calsync-agent" / "DISABLED").write_text("")
        r = self.wrapper()
        self.assertEqual(r.returncode, 0)
        self.assertIn("kill switch", r.stderr)
        self.assertIsNone(self.r.store.get_push("phdata"))


if __name__ == "__main__":
    unittest.main()
