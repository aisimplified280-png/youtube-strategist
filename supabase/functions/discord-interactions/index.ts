// Discord HTTP interactions for owner-only metadata approvals.
// Deploy with JWT verification disabled: Discord authenticates requests using Ed25519.
const encoder = new TextEncoder();
const EPHEMERAL = 64;
const MAX_CLOCK_SKEW_SECONDS = 300;

function hexToBytes(value: string): Uint8Array {
  if (!/^[0-9a-f]{128}$/i.test(value)) throw new Error("Invalid signature encoding");
  return new Uint8Array(value.match(/.{2}/g)!.map((part) => parseInt(part, 16)));
}

async function verifyDiscordRequest(request: Request, rawBody: string, publicKey: string): Promise<boolean> {
  const signature = request.headers.get("x-signature-ed25519") || "";
  const timestamp = request.headers.get("x-signature-timestamp") || "";
  if (!/^\d{10,}$/.test(timestamp)) return false;
  const age = Math.abs(Math.floor(Date.now() / 1000) - Number(timestamp));
  if (!Number.isFinite(age) || age > MAX_CLOCK_SKEW_SECONDS) return false;
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      hexToBytes(publicKey),
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      hexToBytes(signature),
      encoder.encode(timestamp + rawBody),
    );
  } catch {
    return false;
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function interactionMessage(content: string, embeds: unknown[] = [], components: unknown[] = [], ephemeral = true) {
  return {
    type: 4,
    data: {
      content,
      embeds,
      components,
      ...(ephemeral ? { flags: EPHEMERAL } : {}),
    },
  };
}

async function resolveSingleActiveOwnerKey(): Promise<string> {
  const params = new URLSearchParams({
    purpose: "eq.youtube",
    revoked: "eq.false",
    select: "owner_key",
    limit: "10",
  });
  const rows = await supabaseRequest(\`owner_oauth_credentials?\${params.toString()}\`);
  const keys = [...new Set((rows || []).map((row: any) => String(row.owner_key || "")).filter(Boolean))];
  // This Discord app is intentionally single-owner. If the database ever contains
  // multiple active YouTube owners, fail closed rather than guessing which channel.
  if (keys.length !== 1) throw new Error("Expected exactly one active YouTube owner credential.");
  return keys[0];
}

function discordUserId(payload: any): string {
  return String(payload?.member?.user?.id || payload?.user?.id || "");
}

async function supabaseRequest(path: string, method = "GET", body?: unknown): Promise<any[]> {
  const base = Deno.env.get("SUPABASE_URL")?.replace(/\/$/, "");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!base || !serviceKey) throw new Error("Supabase server configuration is incomplete.");
  const response = await fetch(`${base}/rest/v1/${path}`, {
    method,
    headers: {
      apikey: serviceKey,
      authorization: `Bearer ${serviceKey}`,
      "content-type": "application/json",
      prefer: method === "PATCH" ? "return=representation" : "return=representation",
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 400);
    throw new Error(`Supabase request failed (${response.status}): ${detail}`);
  }
  return response.status === 204 ? [] : await response.json();
}

function safeText(value: unknown, max = 180): string {
  return String(value ?? "").replace(/[\r\n\t]+/g, " ").slice(0, max);
}

async function listApprovals(ownerKey: string) {
  const params = new URLSearchParams({
    owner_key: `eq.${ownerKey}`,
    status: "eq.pending_review",
    select: "id,video_id,video_title,updated_parts,approval_digest,created_at",
    order: "created_at.asc",
    limit: "5",
  });
  const rows = await supabaseRequest(`metadata_editor_audits?${params.toString()}`);
  if (!rows.length) {
    return interactionMessage("There are no pending metadata proposals. Your review queue is clear.");
  }
  const embeds = rows.map((row: any) => ({
    title: safeText(row.video_title || "Untitled video", 240),
    url: `https://www.youtube.com/watch?v=${encodeURIComponent(String(row.video_id || ""))}`,
    description: [
      `**Video ID:** ${safeText(row.video_id, 20)}`,
      `**Changed:** ${(Array.isArray(row.updated_parts) ? row.updated_parts : []).map((v: string) => safeText(v, 40)).join(", ") || "Metadata"}`,
      "**Review:** Open the video in YouTube if you want to inspect it before approving.",
    ].join("\n"),
    color: 3447003,
  }));
  const components = rows.map((row: any) => {
    const digest = String(row.approval_digest || "");
    const token = digest.slice(0, 16);
    return {
      type: 1,
      components: [
        { type: 2, style: 3, label: "Approve & publish", custom_id: `meta:approve:${row.id}:${token}` },
        { type: 2, style: 4, label: "Reject", custom_id: `meta:reject:${row.id}:${token}` },
      ],
    };
  });
  return interactionMessage(
    "Pending metadata proposals — approving starts the existing verified publishing worker. Rejection does not change YouTube.",
    embeds,
    components,
  );
}

async function decideMetadata(payload: any, ownerKey: string, actorId: string) {
  const customId = String(payload?.data?.custom_id || "");
  const match = /^meta:(approve|reject):([0-9a-f-]{36}):([0-9a-f]{16})$/i.exec(customId);
  if (!match) return { type: 7, data: { content: "This approval button is invalid or expired.", components: [] } };
  const [, decision, auditId, digestPrefix] = match;
  const params = new URLSearchParams({
    id: `eq.${auditId}`,
    owner_key: `eq.${ownerKey}`,
    status: "eq.pending_review",
    select: "id,video_id,video_title,approval_digest",
    limit: "1",
  });
  const rows = await supabaseRequest(`metadata_editor_audits?${params.toString()}`);
  const audit = rows[0];
  if (!audit || !String(audit.approval_digest || "").startsWith(digestPrefix)) {
    return { type: 7, data: { content: "This proposal is no longer pending or has changed. Run /approvals again to refresh.", components: [] } };
  }
  const now = new Date().toISOString();
  const updateParams = new URLSearchParams({
    id: `eq.${auditId}`,
    owner_key: `eq.${ownerKey}`,
    status: "eq.pending_review",
    approval_digest: `eq.${audit.approval_digest}`,
  });
  const result = await supabaseRequest(`metadata_editor_audits?${updateParams.toString()}`, "PATCH", {
    status: decision === "approve" ? "approved" : "rejected",
    approved_by: decision === "approve" ? ownerKey : null,
    approved_at: decision === "approve" ? now : null,
    result: {
      source: "discord",
      action: decision,
      discord_user_id: actorId,
      discord_interaction_id: String(payload.id || ""),
      decided_at: now,
    },
    error: null,
    updated_at: now,
  });
  if (!result.length) {
    return { type: 7, data: { content: "Another action already changed this proposal. No duplicate action was applied.", components: [] } };
  }
  const title = safeText(audit.video_title || audit.video_id || "Metadata proposal", 160);
  const content = decision === "approve"
    ? `Approved: **${title}**. It is queued for the existing publishing worker, which will re-check the live metadata and verify the YouTube update.`
    : `Rejected: **${title}**. No YouTube changes were made.`;
  return { type: 7, data: { content, embeds: [], components: [] } };
}

Deno.serve(async (request: Request) => {
  if (request.method !== "POST") return jsonResponse({ error: "Method not allowed" }, 405);
  const rawBody = await request.text();
  const publicKey = Deno.env.get("DISCORD_APPLICATION_PUBLIC_KEY") || "";
  if (!publicKey || !(await verifyDiscordRequest(request, rawBody, publicKey))) {
    return jsonResponse({ error: "Invalid Discord request signature." }, 401);
  }

  let payload: any;
  try {
    payload = JSON.parse(rawBody);
  } catch {
    return jsonResponse({ error: "Invalid JSON payload." }, 400);
  }

  // Discord endpoint verification handshake.
  if (payload?.type === 1) return jsonResponse({ type: 1 });

  const ownerId = Deno.env.get("DISCORD_OWNER_USER_ID") || "";
  const actorId = discordUserId(payload);
  if (!ownerId || !actorId || actorId !== ownerId) {
    if (payload?.type === 3) {
      return jsonResponse({ type: 4, data: { content: "You are not authorised to use this approval control.", flags: EPHEMERAL } });
    }
    return jsonResponse(interactionMessage("You are not authorised to use this bot."));
  }

  try {
    const ownerKey = await resolveSingleActiveOwnerKey();

    if (payload?.type === 2 && String(payload?.data?.name || "") === "approvals") {
      return jsonResponse(await listApprovals(ownerKey));
    }
    if (payload?.type === 3) {
      return jsonResponse(await decideMetadata(payload, ownerKey, actorId));
    }
    return jsonResponse(interactionMessage("Unsupported command. Use /approvals to review metadata proposals."));
  } catch (error) {
    console.error("Discord interaction failed:", error instanceof Error ? error.message : "unknown error");
    return jsonResponse(interactionMessage("The request could not be completed safely. Please retry or open the Streamlit review queue."), 500);
  }
});
