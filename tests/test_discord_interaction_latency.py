"""Regression checks for low-latency Discord interaction acknowledgements."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "supabase" / "functions" / "discord-interactions" / "index.ts").read_text(encoding="utf-8")
HANDLER = SOURCE.split("Deno.serve(async (request: Request) => {", 1)[1]


class DiscordInteractionLatencyTests(unittest.TestCase):
    def test_approvals_command_is_deferred_before_owner_lookup(self):
        command = HANDLER.split('if (payload?.type === 2 && String(payload?.data?.name || "") === "approvals") {', 1)[1]
        command = command.split("if (payload?.type === 3) {", 1)[0]
        ack = command.index("const deferred = jsonResponse(await listApprovals());")
        owner_lookup = command.index("await resolveSingleActiveOwnerKey()")
        self.assertLess(ack, owner_lookup)
        self.assertIn("EdgeRuntime.waitUntil", command)

    def test_component_click_is_deferred_before_owner_lookup(self):
        component = HANDLER.split("if (payload?.type === 3) {", 1)[1]
        ack = component.index("const deferred = jsonResponse({ type: 6 });")
        owner_lookup = component.index("await resolveSingleActiveOwnerKey()")
        self.assertLess(ack, owner_lookup)
        self.assertIn("editOriginalResponse", component)

    def test_bulk_approval_uses_one_conditional_write_per_proposal(self):
        block = SOURCE.split("async function approveAllBatch", 1)[1].split("async function decideMetadata", 1)[0]
        self.assertIn("Promise.all(refs.map", block)
        self.assertIn('status: "eq.pending_review"', block)
        self.assertIn('approval_digest: `eq.${ref.digest}`', block)
        self.assertNotIn("const found = await supabaseRequest", block)


if __name__ == "__main__":
    unittest.main()
