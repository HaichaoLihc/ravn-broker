"use strict";
const $ = (id) => document.getElementById(id);
const el = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
let state,
  busy = false,
  // One key per logical draft. Reused only for an explicit retry of that same
  // draft after an uncertain outcome; editing the draft starts a new action.
  pendingDraftKey = null;
const DEMO_THREAD = "18f2a0c4d5e6f701";
const definitions = {
  gmail: {
    title: "Gmail",
    icon: "@",
    summary:
      "Search customer email, read threads, and save draft replies. Drafts are never sent.",
    tools: [
      ["search_threads", "Search threads"],
      ["get_thread", "Read a thread"],
      ["get_message", "Read a message"],
      ["list_labels", "List labels"],
      ["list_drafts", "List drafts"],
      ["create_draft", "Draft a reply (not sent)"],
    ],
  },
  github: {
    title: "GitHub",
    icon: "⌘",
    summary:
      "Read repository issues and get the context behind a support request.",
    tools: [
      ["issue_read", "Read an issue"],
      ["list_issues", "List repository issues"],
    ],
  },
  slack: {
    title: "Slack",
    icon: "#",
    summary: "Find conversations and read threads in your connected workspace.",
    tools: [
      ["slack_search_public", "Search public messages"],
      ["slack_search_public_and_private", "Search public + private messages"],
      ["slack_read_thread", "Read a thread"],
    ],
  },
};
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = "notice" + (error ? " error" : "");
  $("notice").hidden = !message;
}
async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    redirect: "error",
    headers:
      body === undefined
        ? {}
        : {
            "Content-Type": "application/json",
            "X-CSRF-Token": state?.csrf || "",
          },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const value = await response.json();
  if (!response.ok) {
    const err = new Error(
      value.error?.message || "The request could not be completed.",
    );
    err.status = response.status;
    err.code = value.error?.code;
    throw err;
  }
  return value;
}
async function action(fn) {
  if (busy) return;
  busy = true;
  setBusy(true);
  notice("");
  try {
    await fn();
  } catch (e) {
    notice(e.message, true);
  } finally {
    busy = false;
    setBusy(false);
  }
}
function setBusy(value) {
  document
    .querySelectorAll("#workspace button")
    .forEach((b) => (b.disabled = value));
  $("run-button").disabled = value || !$("account").value;
  $("run-button").textContent = value
    ? "Working…"
    : $("tool").value !== "create_draft"
      ? "Run read tool ↗"
      : pendingDraftKey
        ? "Try again with the same draft ↗"
        : "Create draft (not sent) ↗";
  $("account").disabled = value || !$("account").options[0]?.value;
  $("tool").disabled = value || !$("account").value;
}
function button(text, fn, cls) {
  const b = el("button", text, cls);
  b.type = "button";
  b.addEventListener("click", () => action(fn));
  return b;
}
function connection() {
  return state?.connections.find((c) => c.id === $("account").value);
}
async function connect(provider, existing) {
  const result = await api("/api/connect", {
    integration_id: provider,
    ...(existing ? { reconnect_connection_id: existing } : {}),
  });
  const target = new URL(result.authorization_url);
  if (!["http:", "https:"].includes(target.protocol))
    throw new Error("Invalid authorization destination.");
  location.assign(target.href);
}
function renderConnections() {
  const root = $("connection-cards");
  root.replaceChildren();
  for (const [id, def] of Object.entries(definitions)) {
    // Old disconnected rows remain in RAVN for audit, not as duplicate cards.
    const matches = state.connections.filter(
      (c) =>
        c.integration_id === id &&
        ["active", "reconnect_required"].includes(c.status),
    );
    for (const c of matches.length ? matches : [null]) {
      const card = el("div", undefined, "card");
      const top = el("div", undefined, "card-top");
      top.append(el("div", def.icon, "service-icon " + id));
      const info = el("div");
      info.append(
        el("h3", def.title),
        el(
          "div",
          c?.status === "active"
            ? "● Connected"
            : c?.status === "reconnect_required"
              ? "Reconnect required"
              : "Not connected",
          "status" + (c?.status === "active" ? " connected" : ""),
        ),
      );
      top.append(info);
      card.append(top, el("p", def.summary));
      if (c)
        card.append(
          el("p", c.display_name || c.provider_account_id, "account-name"),
        );
      const actions = el("div", undefined, "card-actions");
      if (c?.status === "active" || c?.status === "reconnect_required") {
        actions.append(button("Reconnect", () => connect(id, c.id)));
        actions.append(
          button(
            "Disconnect",
            async () => {
              if (
                !confirm(
                  "Disconnect " +
                    def.title +
                    " from Support Desk? Existing RAVN sessions will stop working. This does not uninstall or revoke the provider app itself.",
                )
              )
                return;
              await api("/api/connections/" + c.id + "/disconnect", {});
              clearResult();
              await refresh();
              notice(
                def.title +
                  " disconnected. Future calls through this connection are blocked; already-running reads may finish.",
              );
            },
            "danger",
          ),
        );
      } else
        actions.append(
          button(
            "Connect " + def.title + " ↗",
            () => connect(id),
            "connect-button",
          ),
        );
      card.append(actions);
      root.append(card);
    }
  }
  $("connection-count").textContent =
    state.connections.filter((c) => c.status === "active").length +
    " connected";
}
function renderSelector() {
  const old = $("account").value,
    oldTool = $("tool").value;
  $("account").replaceChildren();
  for (const c of state.connections.filter(
    (c) => c.status === "active" && definitions[c.integration_id],
  )) {
    const option = el(
      "option",
      definitions[c.integration_id].title +
        " · " +
        (c.display_name || c.provider_account_id),
    );
    option.value = c.id;
    $("account").append(option);
  }
  if (!$("account").options.length) {
    const option = el("option", "Connect an account first");
    option.value = "";
    $("account").append(option);
  }
  if ([...$("account").options].some((o) => o.value === old))
    $("account").value = old;
  const same = old === $("account").value;
  renderTools(same ? oldTool : undefined, same);
  setBusy(busy);
}
function renderTools(selected, preserve = false) {
  $("tool").replaceChildren();
  for (const [name, title] of definitions[connection()?.integration_id]
    ?.tools || []) {
    const opt = el("option", title);
    opt.value = name;
    $("tool").append(opt);
  }
  if (selected && [...$("tool").options].some((o) => o.value === selected))
    $("tool").value = selected;
  renderFields(preserve);
  $("run-help").textContent = connection()
    ? "Your backend starts a 10-minute session when needed. RAVN checks access on every call."
    : "Connect Gmail, Slack, or GitHub to get started.";
}
function renderFields(preserve = false) {
  const root = $("tool-fields"),
    previous = {};
  if (preserve)
    root
      .querySelectorAll("input, textarea")
      .forEach((i) => (previous[i.name] = i.value));
  root.replaceChildren();
  const tool = $("tool").value,
    demo = state?.mode === "simulated";
  const MAX = { query: 2000, subject: 998, body: 50000, to: 254 };
  const input = (
    name,
    title,
    value,
    wide = false,
    number = false,
    optional = false,
    multiline = false,
  ) => {
    const label = el("label", title, wide ? "wide" : "");
    const field = el(multiline ? "textarea" : "input");
    field.name = name;
    field.required = !optional;
    if (!multiline) field.type = number ? "number" : "text";
    field.value = previous[name] ?? value;
    field.maxLength = MAX[name] || 100;
    field.autocomplete = "off";
    if (number) {
      field.min = "1";
      field.max = "2147483647";
      field.step = "1";
    }
    // Changing a draft makes it a different action with a new key.
    if (tool === "create_draft")
      field.addEventListener("input", () => {
        pendingDraftKey = null;
        setBusy(busy);
      });
    label.append(field);
    root.append(label);
  };
  if (tool === "issue_read" || tool === "list_issues") {
    input("owner", "Repository owner", demo ? "acme" : "");
    input("repo", "Repository name", demo ? "help-center" : "");
    if (tool === "issue_read")
      input("issue_number", "Issue number", "42", false, true);
  } else if (tool.startsWith("slack_search"))
    input("query", "Search query", demo ? "refund" : "", true);
  else if (tool === "slack_read_thread") {
    input("channel_id", "Channel ID", demo ? "C0123" : "");
    input("message_ts", "Thread timestamp", demo ? "1789142400.000100" : "");
  } else if (tool === "search_threads")
    input("query", "Gmail search", demo ? "refund" : "", true);
  else if (tool === "get_thread")
    input("threadId", "Thread ID", demo ? DEMO_THREAD : "");
  else if (tool === "get_message")
    input("messageId", "Message ID", demo ? DEMO_THREAD : "");
  else if (tool === "create_draft") {
    input("to", "To (one address)", demo ? "dana@example.com" : "");
    input(
      "replyToMessageId",
      "Reply to message ID",
      demo ? DEMO_THREAD : "",
      false,
      false,
      true,
    );
    input(
      "subject",
      "Subject",
      demo ? "Re: Refund for order #1042 has no confirmation" : "",
      true,
      false,
      true,
    );
    input(
      "body",
      "Draft body",
      demo
        ? "Hi Dana,\n\nYour refund for order #1042 is approved and processing. The confirmation email was delayed; we have queued it again and you should see it shortly.\n\nThanks,\nAcme support"
        : "",
      true,
      false,
      false,
      true,
    );
  }
}
function renderSessions() {
  const list = $("session-list");
  list.replaceChildren();
  const active = state.sessions.filter(
    (s) => !s.revoked_at && Date.parse(s.expires_at) > Date.now(),
  );
  for (const s of active) {
    const c = state.connections.find((c) => c.id === s.connection_id),
      row = el("div", undefined, "session-row"),
      desc = el("div", undefined, "row-description");
    desc.append(
      el(
        "strong",
        (definitions[c?.integration_id]?.title || "Account") +
          (c?.integration_id === "gmail"
            ? " · session (reads + drafts)"
            : " · read-only session"),
      ),
      el(
        "small",
        s.id +
          " · expires " +
          new Date(s.expires_at).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          }),
      ),
    );
    row.append(
      el("span", "ACTIVE", "tag"),
      desc,
      button("Stop session", async () => {
        await api("/api/sessions/" + s.id + "/revoke", {});
        await refresh();
        notice(
          "Session stopped. The account stays connected. Your next explicit run can request a new session.",
        );
      }),
    );
    list.append(row);
  }
  if (!active.length)
    list.append(
      el("div", "No active sessions. Run a tool to start one.", "empty-line"),
    );
  if (state.more_sessions)
    list.append(
      el(
        "p",
        "Showing the first 100 sessions. Older sessions may not appear here.",
        "small",
      ),
    );
}
function renderActivity() {
  const root = $("activity-list");
  root.replaceChildren();
  for (const a of state.activity.slice(0, 6)) {
    const row = el("div", undefined, "activity-row"),
      desc = el("div", undefined, "row-description");
    desc.append(
      el("strong", a.tool + " · " + a.status.replaceAll("_", " ")),
      el("small", a.call_id || a.error_code || "Waiting for the tool response"),
    );
    row.append(
      el("span", a.status === "succeeded" ? "↗" : "·", "activity-icon"),
      desc,
      el(
        "time",
        new Date(a.created_at * 1000).toLocaleTimeString([], {
          hour: "2-digit",
          minute: "2-digit",
        }),
      ),
    );
    root.append(row);
  }
  if (!state.activity.length)
    root.append(
      el(
        "div",
        "Your tool calls will appear here, without storing their arguments or results in the activity log.",
        "empty-line",
      ),
    );
}
function clearResult() {
  $("empty-result").hidden = false;
  $("result").hidden = true;
  $("result").textContent = "";
  $("trace").hidden = true;
  $("trace").replaceChildren();
  $("result-label").textContent = "RESULT";
  $("result-meta").textContent = "Nothing sent yet";
}
async function refresh() {
  state = await api("/api/state");
  $("locked").hidden = true;
  $("workspace").hidden = false;
  $("user-name").textContent = state.user;
  const demo = state.mode === "simulated";
  $("mode-label").textContent = demo ? "SIMULATED PROVIDERS" : "LIVE PROVIDERS";
  $("mode-detail").textContent = demo
    ? "Real app → RAVN → MCP flow. GitHub, Slack, Gmail, consent and returned data are local simulations. No real accounts are used."
    : "Your real GitHub, Slack, and Gmail accounts. Only approved tools are exposed; Gmail drafts are never sent.";
  renderConnections();
  renderSelector();
  renderSessions();
  renderActivity();
  if (state.notice) notice(state.notice);
  if (state.more_connections)
    notice(
      "Showing the first 100 connections; older connections may not appear here.",
    );
}
$("account").addEventListener("change", () => {
  renderTools();
  clearResult();
  setBusy(busy);
});
$("tool").addEventListener("change", () => {
  pendingDraftKey = null;
  renderFields();
  clearResult();
  setBusy(busy);
});
$("refresh").addEventListener("click", () => action(refresh));
$("run-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    const args = Object.fromEntries(new FormData(event.target)),
      tool = $("tool").value;
    if (tool === "issue_read") {
      args.method = "get";
      args.issue_number = Number(args.issue_number);
    }
    if (tool.startsWith("slack_search")) args.limit = 5;
    if (tool === "search_threads" || tool === "list_drafts") args.pageSize = 5;
    let idempotencyKey;
    if (tool === "create_draft") {
      args.to = [args.to];
      for (const optional of ["subject", "replyToMessageId"])
        if (!args[optional]) delete args[optional];
      idempotencyKey = pendingDraftKey || crypto.randomUUID();
    }
    $("result-meta").textContent = "Checking access and calling MCP…";
    try {
      const result = await api("/api/run", {
        connection_id: $("account").value,
        tool,
        arguments: args,
        ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
      });
      pendingDraftKey = null;
      $("empty-result").hidden = true;
      $("result").hidden = false;
      let text = result.text;
      try {
        text = JSON.stringify(JSON.parse(text), null, 2);
      } catch {
        /* Plain-text tools are valid. */
      }
      $("result").textContent = text;
      $("result-label").textContent =
        state.mode === "simulated" ? "SIMULATED DATA" : "PROVIDER RESULT";
      $("result-meta").textContent =
        result.status.replaceAll("_", " ") + " · " + result.duration_ms + " ms";
      $("trace").replaceChildren(...result.steps.map((s) => el("li", s)));
      $("trace").hidden = false;
    } catch (e) {
      // Only an uncertain outcome keeps the key, so "Try again" can never
      // save the same draft twice. A definite failure starts fresh.
      pendingDraftKey =
        idempotencyKey &&
        ["outcome_unknown", "call_in_progress"].includes(e.code)
          ? idempotencyKey
          : null;
      $("result-meta").textContent = idempotencyKey
        ? "Draft outcome needs checking"
        : "Read did not complete";
      $("empty-result").hidden = true;
      $("result").hidden = false;
      $("result").textContent = e.message;
      $("trace").hidden = true;
      throw e;
    } finally {
      await refresh();
    }
  });
});
async function initialize() {
  const ticket = new URLSearchParams(location.hash.slice(1)).get("login");
  history.replaceState(null, "", "/");
  try {
    if (ticket) {
      $("workspace").hidden = true;
      clearResult();
      await api("/api/login", { ticket });
    }
    await refresh();
  } catch (e) {
    notice(e.message, true);
    $("locked").hidden = e.status !== 401;
    $("mode-label").textContent =
      e.status === 401 ? "LOCAL TEST LOGIN" : "SETUP NEEDED";
    $("mode-detail").textContent =
      "This is the customer app, not the RAVN operator console.";
  }
}
// Opening a fresh launcher link in the same tab may only change its fragment.
window.addEventListener("hashchange", () => {
  if (location.hash.startsWith("#login=")) initialize();
});
initialize();
