import io
import unittest
import urllib.error
import urllib.parse

from calsync import google, microsoft, net
from calsync.connectors import MarkerMismatch
from calsync.model import busy_payload, details_payload, qualifies
from tests.fakes import at

MARK = "aaaaaaaaaaaa.gigabug." + "1" * 24


class StaticAuth:
    def token(self):
        return "tok"


class FakeHttp:
    """Records requests and answers from a queue of (predicate, response) pairs."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers=None, json_body=None, **kw):
        self.calls.append((method, url, json_body))
        return self.responses.pop(0) if self.responses else {}


class GoogleTests(unittest.TestCase):
    def test_normalize_instance_declined_free_marker(self):
        base = {"id": "abc_20261021T150000Z", "summary": "Weekly", "status": "confirmed",
                "start": {"dateTime": "2026-10-21T10:00:00-05:00"}, "end": {"dateTime": "2026-10-21T11:00:00-05:00"},
                "hangoutLink": "https://meet.google.com/xyz"}
        e = google.normalize(base)
        self.assertEqual(e.start.hour, 15)
        self.assertEqual(e.online_url, "https://meet.google.com/xyz")
        self.assertTrue(qualifies(e))
        declined = dict(base, attendees=[{"email": "x@y", "self": True, "responseStatus": "declined"}])
        self.assertFalse(qualifies(google.normalize(declined)))
        tentative = dict(base, attendees=[{"email": "x@y", "self": True, "responseStatus": "tentative"}])
        self.assertTrue(qualifies(google.normalize(tentative)))
        self.assertFalse(qualifies(google.normalize(dict(base, transparency="transparent"))))
        self.assertFalse(qualifies(google.normalize(dict(base, eventType="workingLocation"))))
        marked = dict(base, extendedProperties={"private": {"calsync": MARK}})
        self.assertEqual(google.normalize(marked).marker, MARK)
        allday = dict(base, start={"date": "2026-10-21"}, end={"date": "2026-10-22"})
        self.assertTrue(google.normalize(allday).all_day)

    def test_body_is_private_opaque_without_reminders(self):
        from calsync.model import Event
        b = google.event_body(busy_payload(Event("x", at(1), at(2), location="L", description="D")), MARK)
        self.assertEqual(b["visibility"], "private")
        self.assertEqual(b["transparency"], "opaque")
        self.assertEqual(b["reminders"], {"useDefault": False, "overrides": []})
        self.assertEqual(b["extendedProperties"]["private"]["calsync"], MARK)
        self.assertEqual((b["summary"], b["location"], b["description"]), ("Busy", "", ""))
        self.assertNotIn("attendees", b)

    def test_list_uses_single_events_and_pages(self):
        item = {"id": "1", "start": {"dateTime": "2026-10-21T15:00:00Z"}, "end": {"dateTime": "2026-10-21T16:00:00Z"}}
        http = FakeHttp([{"items": [item], "nextPageToken": "p2"}, {"items": [dict(item, id="2")]}])
        c = google.GoogleConnector("gigabug", StaticAuth(), request=http)
        evs = c.list_events(at(0), at(48))
        self.assertEqual([e.id for e in evs], ["1", "2"])
        q = urllib.parse.parse_qs(urllib.parse.urlparse(http.calls[0][1]).query)
        self.assertEqual(q["singleEvents"], ["true"])
        self.assertIn("pageToken=p2", http.calls[1][1])

    def test_update_and_delete_refuse_unmarked_events(self):
        http = FakeHttp([{"id": "x"}])  # GET returns an event with no marker
        c = google.GoogleConnector("gigabug", StaticAuth(), request=http)
        with self.assertRaises(MarkerMismatch):
            c.delete("x", MARK)
        self.assertEqual([m for m, *_ in http.calls], ["GET"])


class GraphTests(unittest.TestCase):
    def item(self, **kw):
        d = {"id": "AAMk1", "subject": "Sprint", "type": "occurrence", "isAllDay": False,
             "start": {"dateTime": "2026-10-21T15:00:00.0000000", "timeZone": "UTC"},
             "end": {"dateTime": "2026-10-21T16:00:00.0000000", "timeZone": "UTC"},
             "showAs": "busy", "isCancelled": False, "responseStatus": {"response": "accepted"},
             "body": {"contentType": "text", "content": "notes"}, "location": {"displayName": "Room"},
             "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/1"}}
        d.update(kw)
        return d

    def test_normalize_rules(self):
        e = microsoft.normalize(self.item())
        self.assertEqual((e.start.hour, e.online_url), (15, "https://teams.microsoft.com/l/meetup-join/1"))
        self.assertTrue(qualifies(e))
        self.assertTrue(qualifies(microsoft.normalize(self.item(showAs="tentative"))))
        self.assertFalse(qualifies(microsoft.normalize(self.item(showAs="free"))))
        self.assertFalse(qualifies(microsoft.normalize(self.item(isCancelled=True))))
        self.assertFalse(qualifies(microsoft.normalize(self.item(responseStatus={"response": "declined"}))))
        marked = self.item(singleValueExtendedProperties=[{"id": microsoft.PROP_ID.upper(), "value": MARK}])
        self.assertEqual(microsoft.normalize(marked).marker, MARK)

    def test_calendar_view_skips_series_master_and_follows_next_link(self):
        http = FakeHttp([
            {"value": [self.item(), self.item(id="M", type="seriesMaster")], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"},
            {"value": [self.item(id="AAMk2")]},
        ])
        c = microsoft.GraphConnector("peregryn", StaticAuth(), request=http)
        self.assertEqual([e.id for e in c.list_events(at(0), at(48))], ["AAMk1", "AAMk2"])
        self.assertIn("/me/calendar/calendarView?", http.calls[0][1])
        self.assertEqual(http.calls[1][1], "https://graph.microsoft.com/v1.0/next")

    def test_body_private_busy_marked(self):
        from calsync.model import Event
        b = microsoft.event_body(details_payload(Event("x", at(1), at(2), title="T"), "Kiewit"), MARK)
        self.assertEqual((b["showAs"], b["sensitivity"], b["isReminderOn"]), ("busy", "private", False))
        self.assertEqual(b["categories"], ["calsync"])
        self.assertEqual(b["singleValueExtendedProperties"], [{"id": microsoft.PROP_ID, "value": MARK}])
        self.assertEqual(b["start"], {"dateTime": at(1).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "UTC"})


class BackoffTests(unittest.TestCase):
    def test_retries_429_and_5xx_then_succeeds(self):
        attempts = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def opener(req, timeout):
            attempts.append(req.full_url)
            if len(attempts) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "slow", {"Retry-After": "7"}, io.BytesIO(b"{}"))
            if len(attempts) == 2:
                raise urllib.error.HTTPError(req.full_url, 503, "down", {}, io.BytesIO(b"{}"))
            return Resp(b'{"ok": true}')

        sleeps = []
        out = net.request("GET", "https://example.invalid/x", opener=opener, sleep=sleeps.append)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(sleeps[0], 7.0)
        self.assertGreaterEqual(sleeps[1], 2.0)

    def test_4xx_is_not_retried(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b"denied"))
        with self.assertRaises(net.HttpError) as cm:
            net.request("GET", "https://example.invalid/x", opener=opener, sleep=lambda s: None)
        self.assertEqual(cm.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
