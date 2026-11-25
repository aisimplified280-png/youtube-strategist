// Discord HTTP interactions for owner-only metadata approvals.
// Deploy with JWT verification disabled: Discord authenticates requests using Ed25519.
const encoder = new TextEncoder();
const EPHEMERAL = 64;
const MAX_CLOCK_SKEW_SECONDS = 300;

function hexToBytes(value: string, expectedHexLength: number): Uint8Array {
  if (value.length !== expectedHexLength || !/^[0-9a-f]+$/i.test(value)) {
    throw new Error("Invalid hexadecimal encoding");
  }
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
      hexToBytes(publicKey, 64),
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      hexToBytes(signature, 128),
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
  const rows = await supabaseRequest(`owner_oauth_credentials?${params.toString()}`);
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

function displayMetadata(value: any, field: "title" | "description" | "tags"): string {
  if (field === "tags") {
    const tags = Array.isArray(value?.tags) ? value.tags : [];
    return tags.length ? tags.join(", ") : "(no tags)";
  }
  const raw = String(value?.[field] ?? "");
  return raw.trim() || (field === "description" ? "(empty description)" : "(empty title)");
}

function clipEmbed(value: string, max = 1000): string {
  if (value.length <= max) return value;
  return value.slice(0, max - 18) + "\n… [truncated]";
}

function proposalEmbed(row: any) {
  const before = row.before_metadata || {};
  const after = row.proposed_metadata || {};
  const fields: any[] = [];
  for (const [key, label] of [["title", "TITLE"], ["description", "DESCRIPTION"], ["tags", "TAGS"]] as const) {
    const oldValue = displayMetadata(before, key);
    const newValue = displayMetadata(after, key);
    if (oldValue === newValue) continue;
    fields.push({ name: `BEFORE — ${label}`, value: clipEmbed(oldValue), inline: false });
    fields.push({ name: `PROPOSED — ${label}`, value: clipEmbed(newValue), inline: false });
  }
  if (!fields.length) {
    fields.push({ name: "Metadata", value: "No title, description or tag difference was found in the stored proposal. Review it in YouTube Strategist before taking action.", inline: false });
  }
  const evidence = row.evidence || {};
  const confidence = evidence.confidence;
  const rationale = evidence.rationale || evidence.factual_evidence || row.reason || "No additional rationale was recorded.";
  fields.push({ name: "WHY THIS CHANGE", value: clipEmbed(String(rationale), 1000), inline: false });
  if (confidence !== null && confidence !== undefined && confidence !== "") {
    const score = Number(confidence);
    fields.push({ name: "MODEL CONFIDENCE", value: Number.isFinite(score) ? `${Math.round(score <= 1 ? score * 100 : score)}%` : safeText(confidence, 40), inline: true });
  }
  const changed = Array.isArray(evidence.changed_fields) ? evidence.changed_fields : (Array.isArray(row.updated_parts) ? row.updated_parts : []);
  if (changed.length) fields.push({ name: "FIELDS CHANGED", value: changed.map((v: string) => safeText(v, 40)).join(", "), inline: true });
  return {
    title: safeText(row.video_title || "Untitled video", 240),
    url: `https://www.youtube.com/watch?v=${encodeURIComponent(String(row.video_id || ""))}`,
    description: [
      `**Source:** ${row.source === "strategy" ? "Channel Strategist" : "Metadata Editor"}`,
      `**Video ID:** ${safeText(row.video_id, 20)}`,
      `**Review reason:** ${safeText(row.reason || evidence.rationale || "Owner review requested.", 700)}`,
    ].join("\n"),
    fields,
    color: 3447003,
  };
}

async function sendFollowup(applicationId: string, token: string, body: unknown) {
  const response = await fetch(`https://discord.com/api/v10/webhooks/${applicationId}/${token}?wait=true`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...(body as any), flags: EPHEMERAL }),
  });
  if (!response.ok) {
    const detail = (await response.text()).slice(0, 300);
    throw new Error(`Discord follow-up failed (${response.status}): ${detail}`);
  }
}

async function sendApprovalReview(payload: any, ownerKey: string) {
  const common = { owner_key: `eq.${ownerKey}`, status: "eq.pending_review", order: "created_at.asc", limit: "5" };
  const [editorRows, strategyRows] = await Promise.all([
    supabaseRequest(`metadata_editor_audits?${new URLSearchParams({
      ...common, select: "id,video_id,video_title,before_metadata,proposed_metadata,updated_parts,approval_digest,created_at",
    }).toString()}`),
    supabaseRequest(`channel_strategy_actions?${new URLSearchParams({
      ...common, select: "id,video_id,video_title,before_metadata,proposed_metadata,approval_digest,created_at,evidence,reason,classification",
    }).toString()}`),
  ]);
  const rows = [
    ...(editorRows || []).map((row: any) => ({ ...row, source: "meta" })),
    ...(strategyRows || []).map((row: any) => ({ ...row, source: "strategy" })),
  ].sort((a: any, b: any) => String(a.created_at || "").localeCompare(String(b.created_at || ""))).slice(0, 5);
  const applicationId = String(payload.application_id || "");
  const token = String(payload.token || "");
  if (!applicationId || !token) throw new Error("Discord interaction follow-up credentials are missing.");
  if (!rows.length) {
    await sendFollowup(applicationId, token, { content: "There are no pending metadata proposals. Your review queue is clear." });
    return;
  }

  const refs = rows.map((row: any) => ({
    source: row.source,
    id: String(row.id),
    digest: String(row.approval_digest || ""),
  }));
  const batchRows = await supabaseRequest("discord_approval_batches", "POST", {
    owner_key: ownerKey,
    proposal_refs: refs,
    status: "pending",
    expires_at: new Date(Date.now() + 15 * 60 * 1000).toISOString(),
  });
  const batchId = String(batchRows?.[0]?.id || "");
  if (!batchId) throw new Error("Could not create the approval snapshot for this Discord review.");

  for (const row of rows) {
    const digest = String(row.approval_digest || "");
    const customPrefix = `${row.source}:`;
    await sendFollowup(applicationId, token, {
      embeds: [proposalEmbed(row)],
      components: [{
        type: 1,
        components: [
          { type: 2, style: 3, label: "Approve & Publish", custom_id: `${customPrefix}approve:${row.id}:${digest.slice(0, 16)}` },
          { type: 2, style: 4, label: "Reject", custom_id: `${customPrefix}reject:${row.id}:${digest.slice(0, 16)}` },
        ],
      }],
    });
  }
  await sendFollowup(applicationId, token, {
    content: `Review batch: **${rows.length} proposal(s)** shown above. Approve All applies only to these exact proposals, and expires in 15 minutes.`,
    components: [{
      type: 1,
      components: [{ type: 2, style: 3, label: `Approve All ${rows.length}`, custom_id: `batch:approve_all:${batchId}` }],
    }],
  });
}

async function listApprovals(ownerKey: string) {
  return { type: 5, data: { flags: EPHEMERAL } };
}
async function approveAllBatch(payload: any, ownerKey: string, actorId: string) {
  const customId = String(payload?.data?.custom_id || "");
  const match = /^batch:approve_all:([0-9a-f-]{36})$/i.exec(customId);
  if (!match) return { type: 7, data: { content: "This bulk approval button is invalid.", components: [] } };
  const batchId = match[1];
  const rows = await supabaseRequest(`discord_approval_batches?${new URLSearchParams({
    id: `eq.${batchId}`, owner_key: `eq.${ownerKey}`, status: "eq.pending",
    expires_at: `gt.${new Date().toISOString()}`, select: "id,proposal_refs", limit: "1",
  }).toString()}`);
  const batch = rows[0];
  if (!batch) return { type: 7, data: { content: "This review batch has expired or was already used. Run /approvals again.", components: [] } };
  let approved = 0;
  let skipped = 0;
  for (const ref of (Array.isArray(batch.proposal_refs) ? batch.proposal_refs : [])) {
    const table = ref.source === "strategy" ? "channel_strategy_actions" : ref.source === "meta" ? "metadata_editor_audits" : "";
    if (!table || !ref.id || !ref.digest) { skipped++; continue; }
    const params = new URLSearchParams({
      id: `eq.${ref.id}`, owner_key: `eq.${ownerKey}`, status: "eq.pending_review",
      approval_digest: `eq.${ref.digest}`, select: "id", limit: "1",
    });
    const found = await supabaseRequest(`${table}?${params.toString()}`);
    if (!found.length) { skipped++; continue; }
    const now = new Date().toISOString();
    const patchParams = new URLSearchParams({
      id: `eq.${ref.id}`, owner_key: `eq.${ownerKey}`, status: "eq.pending_review",
      approval_digest: `eq.${ref.digest}`,
    });
    const result = await supabaseRequest(`${table}?${patchParams.toString()}`, "PATCH", {
      status: "approved",
      ...(table === "metadata_editor_audits" ? { approved_by: ownerKey, approved_at: now, error: null } : {}),
      result: {
        source: "discord", action: "approve", discord_user_id: actorId,
        discord_interaction_id: String(payload.id || ""), bulk_batch_id: batchId, decided_at: now,
      },
      updated_at: now,
    });
    if (result.length) approved++; else skipped++;
  }
  await supabaseRequest(`discord_approval_batches?id=eq.${batchId}&owner_key=eq.${ownerKey}&status=eq.pending`, "PATCH", {
    status: "completed", completed_at: new Date().toISOString(), updated_at: new Date().toISOString(),
  });
  return { type: 7, data: { content: `Bulk approval complete: **${approved}** proposal(s) queued for verified publishing; **${skipped}** skipped because they were no longer pending or had changed. No unlisted proposals were approved.`, components: [] } };
}

async function decideMetadata(payload: any, ownerKey: string, actorId: string) {
  const customId = String(payload?.data?.custom_id || "");
  const match = /^(meta|strategy):(approve|reject):([0-9a-f-]{36}):([0-9a-f]{16})$/i.exec(customId);
  if (!match) return { type: 7, data: { content: "This approval button is invalid or expired.", components: [] } };
  const [, source, decision, auditId, digestPrefix] = match;
  const table = source === "strategy" ? "channel_strategy_actions" : "metadata_editor_audits";
  const select = "id,video_id,video_title,approval_digest";
  const params = new URLSearchParams({
    id: `eq.${auditId}`, owner_key: `eq.${ownerKey}`,
    status: "eq.pending_review", select, limit: "1",
  });
  const rows = await supabaseRequest(`${table}?${params.toString()}`);
  const audit = rows[0];
  if (!audit || !String(audit.approval_digest || "").startsWith(digestPrefix)) {
    return { type: 7, data: { content: "This proposal is no longer pending or has changed. Run /approvals again to refresh.", components: [] } };
  }
  const now = new Date().toISOString();
  const updateParams = new URLSearchParams({
    id: `eq.${auditId}`, owner_key: `eq.${ownerKey}`,
    status: "eq.pending_review", approval_digest: `eq.${audit.approval_digest}`,
  });
  const result = await supabaseRequest(`${table}?${updateParams.toString()}`, "PATCH", {
    status: decision === "approve" ? "approved" : "rejected",
    ...(source === "meta" ? {
      approved_by: decision === "approve" ? ownerKey : null,
      approved_at: decision === "approve" ? now : null,
      error: null,
    } : {}),
    result: {
      source: "discord", action: decision, discord_user_id: actorId,
      discord_interaction_id: String(payload.id || ""), decided_at: now,
    },
    updated_at: now,
  });
  if (!result.length) {
    return { type: 7, data: { content: "Another action already changed this proposal. No duplicate action was applied.", components: [] } };
  }
  const title = safeText(audit.video_title || audit.video_id || "Metadata proposal", 160);
  const content = decision === "approve"
    ? `Approved: **${title}**. It is queued for the verified publishing worker.`
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
      const deferred = jsonResponse(await listApprovals(ownerKey));
      EdgeRuntime.waitUntil(sendApprovalReview(payload, ownerKey).catch(async (error) => {
        console.error("Could not build Discord approval review:", error instanceof Error ? error.message : "unknown error");
        const applicationId = String(payload.application_id || "");
        const token = String(payload.token || "");
        if (applicationId && token) {
          try { await sendFollowup(applicationId, token, { content: "The review cards could not be loaded safely. Please retry /approvals or use the Streamlit review queue." }); } catch { /* interaction token may have expired */ }
        }
      }));
      return deferred;
    }
    if (payload?.type === 3) {
      const customId = String(payload?.data?.custom_id || "");
      if (customId.startsWith("batch:approve_all:")) {
        return jsonResponse(await approveAllBatch(payload, ownerKey, actorId));
      }
      return jsonResponse(await decideMetadata(payload, ownerKey, actorId));
    }
    return jsonResponse(interactionMessage("Unsupported command. Use /approvals to review metadata proposals."));
  } catch (error) {
    console.error("Discord interaction failed:", error instanceof Error ? error.message : "unknown error");
    return jsonResponse(interactionMessage("The request could not be completed safely. Please retry or open the Streamlit review queue."), 500);
  }
});
