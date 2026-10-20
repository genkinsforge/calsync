import unittest
from datetime import timedelta

from calsync.engine import KillSwitch
from calsync.model import mirror_id
from tests.fakes import NOW, Rig, agent_event, at

WORK_API = ("gigabug", "labalicious", "peregryn")


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.r = Rig()
        self.p, self.g, self.l, self.m = (self.r.cals[n] for n in ("personal", "gigabug", "labalicious", "peregryn"))
        # Agent calendars start with an empty but valid push.
        self.r.push("phdata", [])
        self.r.push("kiewit", [])

    def tearDown(self):
        self.r.close()


class PrivacyMatrixTests(EngineTestCase):
    def test_work_event_goes_to_personal_with_details_and_busy_elsewhere(self):
        self.g.add("g1", at(1), at(2), title="Board call", location="HQ", description="numbers",
                   online_url="https://meet.google.com/abc")
        self.r.sync()
        [copy] = self.p.mirrors()
        self.assertEqual(copy.title, "[Gigabug] Board call")
        self.assertEqual(copy.location, "HQ")
        self.assertIn("numbers", copy.description)
        self.assertIn("https://meet.google.com/abc", copy.description)
        for cal in (self.l, self.m):
            [b] = cal.mirrors()
            self.assertEqual((b.title, b.location, b.description), ("Busy", "", ""))
            self.assertEqual((b.start, b.end), (at(1), at(2)))
        self.assertEqual(self.g.mirrors(), [], "source calendar never gets its own mirror")
        for agent_cal in ("phdata", "kiewit"):
            [blk] = self.r.blocks(agent_cal)
            self.assertEqual(blk["title"], "Busy")
            self.assertNotIn("location", blk)

    def test_personal_event_is_busy_on_every_work_calendar(self):
        self.p.add("p1", at(3), at(4), title="Dentist", location="Main St")
        self.r.sync()
        self.assertEqual(self.p.mirrors(), [])
        for cal in (self.g, self.l, self.m):
            [b] = cal.mirrors()
            self.assertEqual(b.title, "Busy")
            self.assertEqual(b.location, "")
        self.assertEqual(len(self.r.blocks("phdata")), 1)
        self.assertEqual(len(self.r.blocks("kiewit")), 1)

    def test_agent_events_reach_personal_with_details(self):
        self.r.push("kiewit", [agent_event("k1@2026", at(5), at(6), title="Bid review", notes="Sheet link")])
        self.r.sync()
        [copy] = self.p.mirrors()
        self.assertEqual(copy.title, "[Kiewit] Bid review")
        self.assertIn("Sheet link", copy.description)
        self.assertEqual(len(self.r.blocks("phdata")), 1)
        self.assertEqual(self.r.blocks("kiewit"), [])
        for cal in (self.g, self.l, self.m):
            self.assertEqual([e.title for e in cal.mirrors()], ["Busy"])


class LoopAndIdempotenceTests(EngineTestCase):
    def test_second_run_changes_nothing_and_mirrors_are_not_mirrored(self):
        self.g.add("g1", at(1), at(2), title="A")
        self.p.add("p1", at(3), at(4), title="B")
        self.r.sync()
        counts = {n: len(c.events) for n, c in self.r.cals.items()}
        ops = {n: len(c.ops) for n, c in self.r.cals.items()}
        for _ in range(3):
            self.r.sync()
        self.assertEqual(counts, {n: len(c.events) for n, c in self.r.cals.items()})
        self.assertEqual(ops, {n: len(c.ops) for n, c in self.r.cals.items()})
        # gigabug holds exactly one Busy (from personal), not a Busy of personal's copy of g1.
        self.assertEqual(len(self.g.mirrors()), 1)


class ChangeTests(EngineTestCase):
    def test_title_change_updates_personal_only(self):
        self.g.add("g1", at(1), at(2), title="Old")
        self.r.sync()
        self.g.events["g1"].title = "New"
        self.l.ops.clear()
        self.r.sync()
        self.assertEqual(self.p.mirrors()[0].title, "[Gigabug] New")
        self.assertEqual(self.l.ops, [], "Busy blocks do not change when only the title changes")

    def test_time_change_updates_busy_blocks(self):
        self.g.add("g1", at(1), at(2))
        self.r.sync()
        self.g.events["g1"].start, self.g.events["g1"].end = at(5), at(6)
        self.r.sync()
        self.assertEqual([(e.start, e.end) for e in self.l.mirrors()], [(at(5), at(6))])
        self.assertEqual(self.l.ops[-1][0], "update")

    def test_disappearing_source_deletes_mirrors(self):
        self.g.add("g1", at(1), at(2))
        self.r.sync()
        del self.g.events["g1"]
        self.r.sync()
        for c in (self.p, self.l, self.m):
            self.assertEqual(c.mirrors(), [])
        self.assertEqual(self.r.blocks("phdata"), [])

    def test_declining_later_deletes_mirrors(self):
        self.m.add("m1", at(1), at(2))
        self.r.sync()
        self.assertEqual(len(self.p.mirrors()), 1)
        self.m.events["m1"].declined = True
        self.r.sync()
        self.assertEqual(self.p.mirrors(), [])
        self.assertEqual(self.g.mirrors(), [])

    def test_mirror_deleted_by_hand_is_recreated(self):
        self.g.add("g1", at(1), at(2))
        self.r.sync()
        [copy] = self.p.mirrors()
        del self.p.events[copy.id]
        self.r.sync()
        self.assertEqual(len(self.p.mirrors()), 1)

    def test_lost_state_db_adopts_existing_mirrors(self):
        self.g.add("g1", at(1), at(2))
        self.r.sync()
        self.r.store.db.execute("DELETE FROM mapping")
        before = {n: set(c.events) for n, c in self.r.cals.items()}
        self.r.sync()
        self.assertEqual(before, {n: set(c.events) for n, c in self.r.cals.items()})

    def test_aged_out_mirrors_are_kept_not_deleted(self):
        self.g.add("g1", at(1), at(2))
        self.r.sync()
        self.r.now = NOW + timedelta(days=3)
        self.r.sync()
        self.assertEqual(len(self.p.mirrors()), 1, "past copies stay on personal")
        self.assertEqual(self.r.store.mappings("personal"), {})


class SafetyTests(EngineTestCase):
    def test_unmarked_events_on_targets_are_never_touched(self):
        for c in self.r.cals.values():
            c.add(f"{c.name}-own", at(1), at(2), title="mine", free=True)
        self.g.add("g1", at(1), at(2))
        self.r.sync()
        del self.g.events["g1"]
        self.r.sync()
        for c in self.r.cals.values():
            touched = [eid for _, eid in c.ops if eid == f"{c.name}-own"]
            self.assertEqual(touched, [])
            self.assertIn(f"{c.name}-own", c.events)

    def test_orphans_deleted_only_for_this_installation(self):
        ours = mirror_id("aaaaaaaaaaaa", "gigabug", "gone", "personal")
        theirs = mirror_id("bbbbbbbbbbbb", "gigabug", "gone", "personal")
        self.p.add("o1", at(1), at(2), title="Busy", marker=ours)
        self.p.add("o2", at(1), at(2), title="Busy", marker=theirs)
        self.r.sync()
        self.assertNotIn("o1", self.p.events)
        self.assertIn("o2", self.p.events)

    def test_failed_source_keeps_its_mirrors(self):
        self.g.add("g1", at(1), at(2))
        self.r.sync()
        self.g.fail = RuntimeError("token expired")
        res = self.r.sync()
        self.assertIn("gigabug", res["frozen"])
        self.assertEqual(len(self.p.mirrors()), 1)
        self.assertEqual(len(self.l.mirrors()), 1)
        self.assertEqual(len(self.r.blocks("kiewit")), 1)
        self.assertEqual(self.r.store.statuses()["gigabug"]["read_ok"], 0)

    def test_one_failing_target_does_not_stop_the_others(self):
        self.p.add("p1", at(1), at(2))

        def boom(*a, **k):
            raise RuntimeError("503 forever")
        self.l.create = boom
        res = self.r.sync()
        self.assertEqual(res["summary"]["labalicious"]["errors"], 1)
        self.assertEqual(len(self.g.mirrors()), 1)
        self.assertEqual(len(self.m.mirrors()), 1)
        self.assertEqual(self.r.store.statuses()["labalicious"]["write_ok"], 0)

    def test_dry_run_writes_nothing(self):
        self.g.add("g1", at(1), at(2))
        self.r.sync(dry_run=True)
        self.assertEqual(sum(len(c.ops) for c in self.r.cals.values()), 0)
        self.assertEqual(self.r.blocks("phdata"), [])
        self.assertIn("DRY-RUN [personal] create", self.r.out.getvalue())

    def test_only_limits_targets(self):
        self.g.add("g1", at(1), at(2))
        self.r.sync(only="personal")
        self.assertEqual(len(self.p.mirrors()), 1)
        self.assertEqual(self.l.mirrors(), [])

    def test_kill_switch(self):
        self.r.cfg.kill_switch.write_text("")
        with self.assertRaises(KillSwitch):
            self.r.sync()


class AgentStalenessTests(EngineTestCase):
    def test_stale_push_is_still_used(self):
        self.r.push("phdata", [agent_event("x1", at(1), at(2))], minutes_ago=600)
        res = self.r.sync()
        self.assertEqual(res["stale"], ["phdata"])
        self.assertEqual(len(self.p.mirrors()), 1)
        self.assertEqual(self.r.store.statuses()["phdata"]["stale"], 1)

    def test_no_push_ever_deletes_nothing(self):
        self.r.store.db.execute("DELETE FROM agent_push")
        self.r.push("phdata", [agent_event("x1", at(1), at(2))])
        self.r.sync()
        self.assertEqual(len(self.g.mirrors()), 1)
        # Now the agent reports an error for phdata: keep its mirrors.
        self.r.store.save_push("phdata", "worklaptop", [], error="calendar not found")
        res = self.r.sync()
        self.assertIn("phdata", res["frozen"])
        self.assertEqual(len(self.g.mirrors()), 1)
        self.assertEqual(len(self.r.blocks("kiewit")), 1, "kiewit's plan keeps phdata's blocks")


class RecurrenceAndAllDayTests(EngineTestCase):
    def test_each_instance_becomes_a_single_mirror(self):
        for d in range(5):
            self.m.add(f"series_{d}", at(24 * d + 1), at(24 * d + 2), title="Daily")
        self.r.sync()
        self.assertEqual(len(self.p.mirrors()), 5)
        self.assertEqual(len(self.g.mirrors()), 5)
        del self.m.events["series_3"]   # one occurrence cancelled
        self.r.sync()
        self.assertEqual(len(self.p.mirrors()), 4)

    def test_all_day_skipped_by_default(self):
        day = NOW.replace(hour=0) + timedelta(days=1)
        self.g.add("ad", day, day + timedelta(days=1), all_day=True)
        self.r.sync()
        self.assertEqual(self.p.mirrors(), [])

    def test_all_day_included_with_flag(self):
        r = Rig(include_all_day=True)
        r.push("phdata", []); r.push("kiewit", [])
        day = NOW.replace(hour=0) + timedelta(days=1)
        r.cals["gigabug"].add("ad", day, day + timedelta(days=1), all_day=True, title="Offsite")
        r.sync()
        [m] = r.cals["personal"].mirrors()
        self.assertTrue(m.all_day)
        self.assertEqual(r.blocks("kiewit")[0]["all_day"], True)
        r.close()

    def test_declined_and_free_never_mirrored(self):
        self.g.add("d", at(1), at(2), declined=True)
        self.g.add("f", at(1), at(2), free=True)
        self.g.add("t", at(1), at(2), title="maybe")  # tentative = busy
        self.r.sync()
        self.assertEqual([e.title for e in self.p.mirrors()], ["[Gigabug] maybe"])


if __name__ == "__main__":
    unittest.main()
