import unittest
from datetime import timezone

from calsync.agentdata import PushError, events_from_push, validate_events
from calsync.model import (Event, busy_payload, details_payload, mirror_id, parse_dt, parse_mirror_id,
                           qualifies)
from tests.fakes import NOW, agent_event, at


def ev(**kw):
    base = dict(id="e1", start=at(1), end=at(2), title="Standup")
    base.update(kw)
    return Event(**base)


class QualificationTests(unittest.TestCase):
    def test_plain_event_qualifies(self):
        self.assertTrue(qualifies(ev()))

    def test_marked_event_never_qualifies(self):
        self.assertFalse(qualifies(ev(marker="aaaaaaaaaaaa.gigabug." + "0" * 24)))

    def test_free_declined_cancelled_skipped(self):
        self.assertFalse(qualifies(ev(free=True)))
        self.assertFalse(qualifies(ev(declined=True)))
        self.assertFalse(qualifies(ev(cancelled=True)))

    def test_all_day_skipped_unless_enabled(self):
        e = ev(all_day=True, start=NOW.replace(hour=0), end=at(12).replace(hour=0))
        self.assertFalse(qualifies(e))
        self.assertTrue(qualifies(e, include_all_day=True))


class PayloadTests(unittest.TestCase):
    def test_details_payload_carries_everything(self):
        p = details_payload(ev(location="Room 4", description="Agenda", online_url="https://teams.microsoft.com/l/x"),
                            "Kiewit")
        self.assertEqual(p["title"], "[Kiewit] Standup")
        self.assertEqual(p["location"], "Room 4")
        self.assertIn("Agenda", p["description"])
        self.assertIn("https://teams.microsoft.com/l/x", p["description"])

    def test_busy_payload_is_opaque(self):
        p = busy_payload(ev(location="Room 4", description="secret", online_url="https://zoom.us/j/1"))
        self.assertEqual(p["title"], "Busy")
        self.assertEqual((p["location"], p["description"], p["online_url"]), ("", "", ""))

    def test_mirror_id_round_trip(self):
        mid = mirror_id("abcdef012345", "kiewit", "evt@2026", "personal")
        self.assertEqual(parse_mirror_id(mid)[:2], ("abcdef012345", "kiewit"))
        self.assertNotEqual(mid, mirror_id("abcdef012345", "kiewit", "evt@2026", "gigabug"))
        self.assertIsNone(parse_mirror_id("not-a-mirror"))


class TimeParsingTests(unittest.TestCase):
    def test_graph_seven_digit_fraction_and_offsets(self):
        self.assertEqual(parse_dt("2026-10-20T15:00:00.0000000"), parse_dt("2026-10-20T10:00:00-05:00"))
        self.assertEqual(parse_dt("2026-10-20T15:00:00Z").tzinfo, timezone.utc)


class AgentPushTests(unittest.TestCase):
    def test_declined_free_cancelled_and_marker_from_agent(self):
        evs = events_from_push(validate_events([
            agent_event("a", at(1), at(2), self_status="declined"),
            agent_event("b", at(1), at(2), availability="free"),
            agent_event("c", at(1), at(2), status="canceled"),
            agent_event("d", at(1), at(2), url="calsync://aaaaaaaaaaaa.personal." + "1" * 24),
            agent_event("e", at(1), at(2), availability="tentative", notes="join https://teams.microsoft.com/l/abc"),
        ]))
        q = {e.id: qualifies(e) for e in evs}
        self.assertEqual(q, {"a": False, "b": False, "c": False, "d": False, "e": True})
        self.assertEqual(evs[4].online_url, "https://teams.microsoft.com/l/abc")

    def test_validation_rejects_garbage(self):
        with self.assertRaises(PushError):
            validate_events([{"id": "x"}])
        with self.assertRaises(PushError):
            validate_events({"not": "a list"})
        with self.assertRaises(PushError):
            validate_events([dict(agent_event("x", at(1), at(2)), start="yesterday")])


if __name__ == "__main__":
    unittest.main()
