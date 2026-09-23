#!/usr/bin/env python3
"""Read-only tier status distinguishes proof, absence, and unexpected routing."""

import json
import pathlib
import tempfile
import unittest

import relay_tier_status as status


class TierStatusTests(unittest.TestCase):
    def test_status_and_fail_closed_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = pathlib.Path(directory)
            parent.chmod(0o700)
            path = parent / "tier.json"
            self.assertEqual(status.inspect(path)["status"], "unobserved")
            receipt = {
                "schema": "khenrix-maka-relay-tier-v1", "time_utc": "2026-09-23T00:00:00+00:00",
                "requested_model": "gpt-6-sol", "observed_model": "gpt-6-sol",
                "requested_service_tier": "default", "observed_service_tier": "default",
                "http_status": 200,
            }
            path.write_text(json.dumps(receipt), encoding="ascii")
            path.chmod(0o600)
            self.assertEqual(status.inspect(path)["status"], "standard")
            receipt["observed_service_tier"] = "priority"
            path.write_text(json.dumps(receipt), encoding="ascii")
            self.assertEqual(status.inspect(path)["status"], "drift")
            receipt["observed_service_tier"] = None
            path.write_text(json.dumps(receipt), encoding="ascii")
            self.assertEqual(status.inspect(path)["status"], "unverified")
            receipt["unexpected"] = "private"
            path.write_text(json.dumps(receipt), encoding="ascii")
            with self.assertRaisesRegex(status.StatusError, "fields are invalid"):
                status.inspect(path)


if __name__ == "__main__":
    unittest.main()
