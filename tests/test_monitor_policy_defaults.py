import unittest
from unittest.mock import Mock

from core.persistent_store import SupabasePersistentStore


class MonitorPolicyDefaultTests(unittest.TestCase):
    def test_missing_owner_policy_is_disabled_by_default(self):
        store = object.__new__(SupabasePersistentStore)
        store.owner_key = "owner-hash"
        store._request = Mock(return_value=[])

        policy = store.get_monitor_publish_policy()

        self.assertFalse(policy["monitor_auto_publish_enabled"])
        self.assertEqual(policy["max_daily_publishes"], 5)
        store._request.assert_called_once()
        self.assertEqual(store._request.call_args.args[:2], ("GET", "owner_publish_policies"))


if __name__ == "__main__":
    unittest.main()
