/* Arena organizer / platform dashboard.
 * Tabs are shown according to the caller's permissions (data-perms), but every
 * action is authorized again by the server; hidden UI is not access control. */
(function () {
  "use strict";
  function ready(fn) { if (window.Arena) fn(window.Arena); else setTimeout(function () { ready(fn); }, 20); }
  ready(function (A) {
    var h = A.h, D = A.root.dataset, EID = D.eventId, perms = (D.perms || "").split(",");
    var can = function (p) { return D.superadmin === "true" || perms.indexOf(p) >= 0; };
    var main = document.getElementById("arena-main");
    var P = "/admin/events/" + EID;

    // ---------------------------------------------------------- helpers
    function field(spec, value) {
      var id = "f-" + spec.name.replace(/\W/g, "-");
      var input;
      if (spec.type === "select") {
        input = h("select", { class: "form-select form-select-sm", id: id }, spec.options.map(function (o) {
          return h("option", { value: o, text: o, selected: String(value) === String(o) });
        }));
      } else if (spec.type === "textarea" || spec.type === "json") {
        input = h("textarea", { class: "form-control form-control-sm" + (spec.type === "json" ? " arena-mono" : ""), id: id, rows: spec.rows || 3 });
        input.value = spec.type === "json" ? JSON.stringify(value || spec.default || {}, null, 2) : (value || "");
      } else if (spec.type === "checkbox") {
        input = h("input", { class: "form-check-input", type: "checkbox", id: id });
        input.checked = !!value;
        return h("div", { class: "form-check mb-2" }, input, h("label", { class: "form-check-label", for: id, text: spec.label }));
      } else {
        input = h("input", { class: "form-control form-control-sm", id: id, type: spec.type || "text" });
        if (spec.type === "datetime-local" && value) input.value = value.replace("Z", "").slice(0, 16);
        else if (value !== undefined && value !== null) input.value = value;
      }
      input.dataset.name = spec.name; input.dataset.kind = spec.type || "text";
      return h("div", { class: "mb-2" }, h("label", { class: "form-label small mb-0", for: id, text: spec.label }), input);
    }
    function form(specs, values, submitLabel, onSubmit) {
      var f = h("form", { class: "arena-card", onsubmit: function (ev) {
        ev.preventDefault();
        var out = {};
        f.querySelectorAll("[data-name]").forEach(function (el) {
          var k = el.dataset.kind, v;
          if (el.type === "checkbox") v = el.checked;
          else if (k === "number") v = el.value === "" ? null : Number(el.value);
          else if (k === "json") { try { v = JSON.parse(el.value || "null"); } catch (e) { throw new Error(el.dataset.name + ": invalid JSON"); } }
          else if (k === "datetime-local") v = el.value ? new Date(el.value).toISOString() : null;
          else v = el.value;
          if (!(v === "" && k !== "textarea")) out[el.dataset.name] = v;
        });
        Promise.resolve(onSubmit(out)).catch(A.fail);
      } }, specs.map(function (s) { return field(s, values ? values[s.name] : undefined); }),
        h("button", { class: "btn btn-primary btn-sm mt-2", type: "submit", text: submitLabel }));
      return f;
    }
    function ok(msg) { A.flash(msg, "success"); }
    function paged(path, columns, rowFn, opts) {
      var box = h("div"), page = 1;
      function load() {
        return A.get(path + (path.indexOf("?") < 0 ? "?" : "&") + "page=" + page + "&per_page=" + ((opts && opts.perPage) || 50)).then(function (r) {
          var pg = r.meta && r.meta.pagination;
          box.replaceChildren(r.data.length ? A.table(columns, r.data.map(rowFn)) : A.empty("Nothing here yet."),
            pg && pg.pages > 1 ? h("div", { class: "d-flex gap-2 align-items-center" },
              A.button("‹ Prev", function () { if (page > 1) { page--; return load(); } }, "outline-secondary"),
              h("span", { class: "small", text: "Page " + pg.page + " / " + pg.pages + " (" + pg.total + ")" }),
              A.button("Next ›", function () { if (page < pg.pages) { page++; return load(); } }, "outline-secondary")) : null);
        }).catch(function (e) { box.replaceChildren(A.empty(e.message)); });
      }
      box.reload = load;
      load();
      return box;
    }
    function tabs(defs) {
      var nav = h("ul", { class: "nav nav-pills flex-wrap gap-1 mb-3", role: "tablist" });
      var pane = h("div");
      var visible = defs.filter(function (d) { return !d.perm || can(d.perm); });
      function show(d, btn) {
        nav.querySelectorAll(".nav-link").forEach(function (n) { n.classList.remove("active"); n.removeAttribute("aria-selected"); });
        btn.classList.add("active"); btn.setAttribute("aria-selected", "true");
        pane.replaceChildren(h("div", { class: "arena-loading text-muted", text: "Loading…" }));
        Promise.resolve(d.render()).then(function (node) { pane.replaceChildren(node); }).catch(function (e) { pane.replaceChildren(A.empty(e.message)); });
        try { history.replaceState(null, "", "#" + d.key); } catch (e) { /* ignore */ }
      }
      visible.forEach(function (d) {
        var btn = h("button", { type: "button", class: "nav-link", role: "tab", text: d.label, onclick: function () { show(d, btn); } });
        d.btn = btn;
        nav.appendChild(h("li", { class: "nav-item" }, btn));
      });
      var start = visible.filter(function (d) { return "#" + d.key === location.hash; })[0] || visible[0];
      if (start) show(start, start.btn);
      return [nav, pane];
    }

    // ----------------------------------------------------- admin home
    A.pages["admin-home"] = function () {
      var list = paged("/admin/events", ["Event", "Type", "Status", "Phase", "Starts", "Seats", ""], function (e) {
        return [h("a", { href: A.BASE + "/arena/admin/e/" + e.id, text: e.name }), e.type, e.status, A.phaseBadge(e.phase),
          A.fmt(e.starts_at), String(e.seat_count) + (e.max_participants ? " / " + e.max_participants : ""),
          h("a", { href: A.BASE + "/arena/e/" + e.slug, text: "Participant view" })];
      });
      var create = D.superadmin === "true" ? A.section("Create a competition", form([
        { name: "name", label: "Name" }, { name: "slug", label: "Slug (URL)" },
        { name: "type", label: "Type", type: "select", options: ["jeopardy", "ad", "mixed"] },
        { name: "team_mode", label: "Team mode", type: "select", options: ["individual", "team"] },
        { name: "visibility", label: "Visibility", type: "select", options: ["public", "unlisted", "private"] },
        { name: "starts_at", label: "Starts", type: "datetime-local" }, { name: "ends_at", label: "Ends", type: "datetime-local" },
        { name: "description", label: "Description (Markdown)", type: "textarea" }
      ], null, "Create", function (data) {
        if (data.team_mode === "team") { data.team_size_min = 1; data.team_size_max = 4; }
        return A.post("/admin/events", data).then(function (r) { location = A.BASE + "/arena/admin/e/" + r.data.id; });
      })) : null;
      A.render(A.section("Competitions you can manage", list), create);
    };

    // ------------------------------------------------- platform page
    A.pages["admin-platform"] = function () {
      A.render.apply(null, tabs([
        { key: "health", label: "System health", render: function () {
          return Promise.all([A.get("/admin/platform/workers"), A.get("/admin/platform/jobs/stats"),
            A.get("/admin/platform/runner/health"), fetch(A.BASE + "/arena/readyz").then(function (r) { return r.json(); })]).then(function (r) {
            var w = r[0].data, s = r[1].data, rn = r[2].data, ready = r[3];
            return h("div", {}, A.section("Readiness", A.table(["Check", "Status"], Object.keys(ready.checks).map(function (k) {
              return [k, A.badge(ready.checks[k] ? "ok" : "failing", ready.checks[k] ? "success" : "danger")];
            }))), A.section("Workers (" + w.alive + " alive)", A.table(["Worker", "Status", "Heartbeat age", "Done", "Failed"], w.workers.map(function (x) {
              return [h("code", { class: "arena-mono", text: x.id }), x.status, x.heartbeat_age_s + " s", String(x.jobs_done), String(x.jobs_failed)];
            }))), A.section("Job queue", A.table(["Kind", "Queued", "Running", "Done", "Dead"], Object.keys(s.by_kind).sort().map(function (k) {
              var v = s.by_kind[k]; return [k, String(v.queued || 0), String(v.running || 0), String(v.done || 0), String(v.dead || 0)];
            })), h("p", { class: "small text-muted", text: "Oldest queued job: " + A.fmt(s.oldest_queued) })),
            A.section("Runner", h("p", {}, "Mode: ", A.badge(rn.mode), " ", rn.error ? h("span", { class: "text-danger", text: rn.error }) : A.badge((rn.health && rn.health.status) || "?", "success"))));
          });
        } },
        { key: "jobs", label: "Dead jobs", render: function () {
          var p = paged("/admin/platform/jobs?status=dead", ["ID", "Kind", "Attempts", "Error", "Finished", ""], function (j) {
            return [String(j.id), j.kind, String(j.attempts), h("span", { class: "small", text: j.last_error || "" }), A.fmt(j.finished_at),
              A.button("Retry", function () { return A.post("/admin/platform/jobs/" + j.id + "/retry").then(function () { p.reload(); }); }, "outline-primary")];
          });
          return p;
        } },
        { key: "images", label: "Challenge images", render: function () {
          return A.get("/admin/platform/images").then(function (r) {
            return h("div", {}, A.table(["Name", "Version", "Reference", "Validated", ""], r.data.map(function (i) {
              return [i.name, i.version, h("code", { class: "arena-mono small", text: i.reference }), i.validated_at ? A.fmt(i.validated_at) : A.badge("not validated", "warning"),
                A.button("Validate with runner", function () { return A.post("/admin/platform/images/" + i.id + "/validate").then(function () { ok("Image validated."); }); }, "outline-secondary")];
            })), A.section("Register image (pinned by digest)", form([
              { name: "name", label: "Name" }, { name: "version", label: "Version" },
              { name: "reference", label: "Reference (repo@sha256:…)" },
              { name: "ports", label: "Ports (JSON list)", type: "json", default: [80], rows: 1 },
              { name: "memory_mb", label: "Memory (MB)", type: "number" }, { name: "cpu_millis", label: "CPU (millicores)", type: "number" },
              { name: "ttl_seconds", label: "Default TTL (s)", type: "number" }
            ], null, "Register", function (d) { return A.post("/admin/platform/images", d).then(function () { ok("Registered."); }); })));
          });
        } },
        { key: "providers", label: "Lab providers", render: function () {
          return A.get("/admin/platform/lab-providers").then(function (r) {
            return h("div", {}, A.table(["Name", "Adapter", "URL", "Credentials", "Health", ""], r.data.map(function (p) {
              return [p.name, p.adapter, p.base_url || "—", p.has_credentials ? "stored" : "none",
                p.last_health ? A.badge(p.last_health.ok ? "ok" : "failing", p.last_health.ok ? "success" : "danger") : "—",
                A.button("Test", function () { return A.post("/admin/platform/lab-providers/" + p.id + "/test").then(function (x) { ok("Health: " + JSON.stringify(x.data.health) + " — " + x.data.labs.length + " labs"); }); }, "outline-secondary")];
            })), A.section("Add provider", form([
              { name: "name", label: "Name" }, { name: "adapter", label: "Adapter", type: "select", options: ["mock", "generic_http"] },
              { name: "base_url", label: "Base URL (https://…)" }, { name: "token", label: "API token (write-only)", type: "password" }
            ], null, "Add", function (d) { return A.post("/admin/platform/lab-providers", d).then(function () { ok("Provider added."); }); })));
          });
        } }
      ]));
    };

    // ---------------------------------------------------- event admin
    A.pages["admin-event"] = function () {
      return A.get(P).then(function (r) {
        var ev = r.data;
        var defs = [
          { key: "overview", label: "Overview", perm: "event.view_admin", render: function () {
            return Promise.all([A.get(P + "/analytics/overview").catch(function () { return { data: null }; }),
              A.get(P + "/analytics/timeline?hours=24").catch(function () { return { data: [] }; })]).then(function (x) {
              var o = x[0].data;
              return h("div", {}, h("div", { class: "d-flex flex-wrap gap-2 mb-3" }, A.phaseBadge(ev.phase), A.badge(ev.type.toUpperCase(), "light"),
                ev.allowed_transitions.map(function (t) {
                  return can("event.manage") ? A.button("→ " + t, function () {
                    if (confirm("Change status to " + t + "?")) return A.post(P + "/status", { status: t }).then(function () { location.reload(); });
                  }, "outline-secondary") : null;
                })),
                o ? h("div", { class: "arena-grid" },
                  A.card(h("h3", { text: "Registrations" }), h("div", { class: "fs-3", text: o.registrations.total }), h("div", { class: "small text-muted", text: JSON.stringify(o.registrations.by_status) })),
                  A.card(h("h3", { text: "Teams" }), h("div", { class: "fs-3", text: o.teams.total })),
                  A.card(h("h3", { text: "Submissions" }), h("div", { class: "fs-3", text: o.submissions }), h("div", { class: "small", text: Math.round(o.success_rate * 100) + "% correct, " + o.failed_submissions + " failed" })),
                  A.card(h("h3", { text: "Active (last hour)" }), h("div", { class: "fs-3", text: o.active_users_last_hour })),
                  A.card(h("h3", { text: "Completion" }), h("div", { class: "small", text: o.completion.entities_with_solves + " with solves · " + o.completion.entities_completed_all + " solved all · avg " + o.completion.avg_solves_per_entity }))) : A.empty("Analytics require the analytics permission."),
                x[1].data.length ? A.section("Last 24 hours", A.table(["Hour", "Active users", "Submissions", "Correct", "Registrations"], x[1].data.map(function (b) {
                  return [A.fmt(b.bucket), String(b.active_users), String(b.submissions), String(b.correct), String(b.registrations)];
                }))) : null,
                o ? h("p", { class: "small text-muted", text: "Rollups updated " + A.fmt(o.rollup_updated_at) + " (refreshed by the worker every minute)." }) : null);
            });
          } },
          { key: "settings", label: "Settings", perm: "event.manage", render: function () {
            return form([
              { name: "name", label: "Name" }, { name: "description", label: "Description (Markdown)", type: "textarea", rows: 5 },
              { name: "rules", label: "Rules (Markdown)", type: "textarea", rows: 4 },
              { name: "visibility", label: "Visibility", type: "select", options: ["public", "unlisted", "private"] },
              { name: "starts_at", label: "Starts", type: "datetime-local" }, { name: "ends_at", label: "Ends", type: "datetime-local" },
              { name: "registration_open", label: "Registration open", type: "checkbox" },
              { name: "requires_approval", label: "Registrations need approval", type: "checkbox" },
              { name: "registration_opens_at", label: "Registration opens", type: "datetime-local" },
              { name: "registration_closes_at", label: "Registration closes", type: "datetime-local" },
              { name: "max_participants", label: "Capacity (blank = unlimited)", type: "number" },
              { name: "team_size_min", label: "Team size min", type: "number" }, { name: "team_size_max", label: "Team size max", type: "number" },
              { name: "settings", label: "Advanced settings (JSON)", type: "json", rows: 10 }
            ], ev, "Save", function (d) {
              ["starts_at", "ends_at", "registration_opens_at", "registration_closes_at"].forEach(function (k) { if (d[k] === null) delete d[k]; });
              return A.api("PATCH", P, d).then(function () { ok("Saved."); });
            });
          } },
          { key: "registrations", label: "Registrations", perm: "event.view_admin", render: function () {
            var list = paged(P + "/registrations", ["User", "Email", "Status", "Since", ""], function (g) {
              var act = function (a) { return A.button(a, function () { return A.post(P + "/registrations/" + g.id + "/" + a).then(function () { list.reload(); }); }, "outline-secondary"); };
              return [g.user ? g.user.name : "#" + g.user_id, g.user ? g.user.email : "", A.badge(g.status), A.fmt(g.created_at),
                can("registrations.manage") ? h("span", { class: "d-flex gap-1 flex-wrap" },
                  g.status === "pending" ? [act("approve"), act("reject")] : null,
                  g.status === "banned" ? act("unban") : act("ban"), ["pending", "approved"].indexOf(g.status) >= 0 ? act("remove") : null) : ""];
            });
            var invites = can("registrations.manage") ? A.get(P + "/invites").then(function (x) {
              return A.section("Invitation codes", A.table(["Label", "Uses", "Expires", "State", ""], x.data.map(function (i) {
                return [i.label || "—", i.uses + (i.max_uses ? " / " + i.max_uses : ""), A.fmt(i.expires_at), i.usable ? A.badge("usable", "success") : A.badge("unusable"),
                  i.revoked ? "" : A.button("Revoke", function () { return A.del(P + "/invites/" + i.id).then(function () { ok("Revoked."); }); }, "outline-danger")];
              })), form([{ name: "label", label: "Label" }, { name: "max_uses", label: "Max uses", type: "number" }, { name: "expires_at", label: "Expires", type: "datetime-local" }],
                null, "Create code", function (d) { return A.post(P + "/invites", d).then(function (y) { A.flash("New code (shown once): " + y.data.code, "success"); }); }),
                A.section("Enroll a user", form([{ name: "user_id", label: "User ID", type: "number" }, { name: "force", label: "Exceed capacity", type: "checkbox" }],
                  null, "Enroll", function (d) { return A.post(P + "/registrations", d).then(function () { ok("Enrolled."); list.reload(); }); })));
            }) : Promise.resolve(null);
            return invites.then(function (inv) { return h("div", {}, list, inv); });
          } },
          { key: "teams", label: "Teams", perm: "event.view_admin", render: function () {
            var list = paged(P + "/teams", ["Team", "Status", "Members", "Visibility", ""], function (t) {
              return [t.name, A.badge(t.status), t.members.map(function (m) { return m.name + (m.role === "captain" ? " ★" : ""); }).join(", "), t.visibility,
                can("teams.manage") ? h("span", { class: "d-flex gap-1" },
                  A.button(t.status === "banned" ? "Unban" : "Ban", function () { return A.post(P + "/teams/" + t.id + "/" + (t.status === "banned" ? "unban" : "ban")).then(function () { list.reload(); }); }, "outline-secondary"),
                  A.button("Disband", function () { if (confirm("Disband " + t.name + "?")) return A.post(P + "/teams/" + t.id + "/disband").then(function () { list.reload(); }); }, "outline-danger")) : ""];
            });
            return list;
          } },
          { key: "challenges", label: "Challenges", perm: "event.view_admin", render: function () {
            return Promise.all([A.get(P + "/challenges"), A.get(P + "/challenges/meta")]).then(function (x) {
              var meta = x[1].data;
              var rows = A.table(["#", "Name", "Category", "Value", "Solves", "Visible", "Enabled", ""], x[0].data.map(function (c) {
                var toggle = function (f) { return A.button(c[f] ? "Hide" : "Show", function () { var b = {}; b[f] = !c[f]; return A.api("PATCH", P + "/challenges/" + c.id, b).then(function () { ok("Updated."); }); }, "outline-secondary"); };
                return [String(c.id), c.name, c.category, String(c.current_value), String(c.solve_count), c.visible ? "yes" : "no", c.enabled ? "yes" : "no",
                  can("challenges.manage") ? h("span", { class: "d-flex gap-1" }, toggle("visible"),
                    A.button(c.enabled ? "Disable" : "Enable", function () { return A.api("PATCH", P + "/challenges/" + c.id, { enabled: !c.enabled }).then(function () { ok("Updated."); }); }, "outline-secondary")) : ""];
              }));
              var create = can("challenges.manage") ? A.section("Create challenge", form([
                { name: "name", label: "Name" }, { name: "type", label: "Type", type: "select", options: meta.types },
                { name: "category", label: "Category", type: "select", options: meta.categories },
                { name: "difficulty", label: "Difficulty", type: "select", options: ["easy", "medium", "hard", "insane"] },
                { name: "points", label: "Points", type: "number" },
                { name: "scoring", label: "Scoring", type: "select", options: ["static", "dynamic"] },
                { name: "minimum_points", label: "Dynamic: minimum", type: "number" }, { name: "decay", label: "Dynamic: decay", type: "number" },
                { name: "description", label: "Description (Markdown)", type: "textarea", rows: 4 },
                { name: "connection_info", label: "Connection info" },
                { name: "max_attempts", label: "Max attempts (blank = event default)", type: "number" },
                { name: "flags", label: "Flags (JSON)", type: "json", default: [{ type: "static", content: "flag{...}", case_insensitive: false }], rows: 3 },
                { name: "hints", label: "Hints (JSON)", type: "json", default: [], rows: 2 },
                { name: "visible", label: "Visible", type: "checkbox" }
              ], null, "Create", function (d) { return A.post(P + "/challenges", d).then(function () { ok("Challenge created."); }); })) : null;
              return h("div", {}, rows, create);
            });
          } },
          { key: "submissions", label: "Submissions", perm: "event.view_admin", render: function () {
            return paged(P + "/submissions", ["When", "Challenge", "Entity", "User", "Status", "Provided"], function (s) {
              return [A.fmt(s.date), "#" + s.challenge_id, s.entity_key, "#" + s.user_id, A.badge(s.status, s.status === "correct" ? "success" : "secondary"),
                s.provided ? h("code", { class: "arena-mono small", text: s.provided }) : ""];
            });
          } },
          { key: "scoreboard", label: "Scoreboard", perm: "event.view_admin", render: function () {
            return Promise.all([A.get(P + "/scoreboard?per_page=200"), A.get(P + "/scoring/configs")]).then(function (x) {
              var sb = x[0].data, cfg = x[1].data;
              return h("div", {}, A.table(["#", "Name", "Score", "Jeopardy", "A&D", "Adj.", "Status"], sb.standings.map(function (s) {
                return [String(s.rank), s.name || s.key, String(s.score), String(s.breakdown.jeopardy), String(s.breakdown.attack_defense), String(s.breakdown.adjustments), s.status];
              })), A.section("Scoring rules", h("p", { class: "small", text: "Active version " + cfg.active.version + ": weights jeopardy " + cfg.active.jeopardy_weight + ", A&D " + cfg.active.ad_weight + ", tie-break " + cfg.active.tie_break })),
              can("scoring.manage") ? h("div", {},
                A.section("Change scoring (creates a new version)", form([
                  { name: "jeopardy_weight", label: "Jeopardy weight", type: "number" }, { name: "ad_weight", label: "A&D weight", type: "number" },
                  { name: "tie_break", label: "Tie-break", type: "select", options: ["time", "solves", "attack"] }, { name: "reason", label: "Reason (required)" }
                ], cfg.active, "Apply", function (d) {
                  if (ev.type !== "mixed") { delete d.jeopardy_weight; delete d.ad_weight; }
                  return A.post(P + "/scoring/configs", d).then(function (y) { ok("Version " + y.data.config.version + " active; " + y.data.changes.length + " standings changed."); });
                })),
                A.section("Bonus / penalty", form([{ name: "entity_key", label: "Entity (t:<team id> or u:<user id>)" }, { name: "points", label: "Points (negative = penalty)", type: "number" }, { name: "reason", label: "Reason" }],
                  null, "Apply adjustment", function (d) { return A.post(P + "/score-adjustments", d).then(function () { ok("Adjustment recorded."); }); })),
                A.section("Freeze", form([{ name: "freeze_at", label: "Freeze public scoreboard at", type: "datetime-local" }], { freeze_at: ev.freeze_at }, "Set freeze",
                  function (d) { return A.post(P + "/scoreboard/freeze", d).then(function () { ok("Freeze updated."); }); }))) : null,
              can("audit.read") ? A.section("Verify", A.button("Verify scoreboard against database", function () {
                return A.get(P + "/scoreboard/verify").then(function (y) { A.flash(y.data.consistent ? "Consistent (" + y.data.entities + " entities)." : "Problems: " + y.data.problems.join("; "), y.data.consistent ? "success" : "danger"); });
              }, "outline-secondary")) : null);
            });
          } },
          ev.ad_enabled ? { key: "ad", label: "Attack & Defense", perm: "event.view_admin", render: function () {
            return Promise.all([A.get(P + "/ad/services"), A.get(P + "/ad/rounds?per_page=10"), A.get(P + "/ad/uptime")]).then(function (x) {
              return h("div", {}, h("div", { class: "d-flex gap-2 mb-2 flex-wrap" }, can("ad.operate") ? [
                A.button("Provision team services", function () { return A.post(P + "/ad/provision").then(function (y) { ok(y.data.created + " services queued."); }); }),
                A.button("Open round", function () { return A.post(P + "/ad/rounds/open").then(function () { ok("Round opened."); }); }, "outline-secondary"),
                A.button("Close round", function () { return A.post(P + "/ad/rounds/close").then(function () { ok("Round closed and scored."); }); }, "outline-secondary")] : null),
                A.section("Services", A.table(["Name", "Checker", "Attack", "Defense", "Loss", "SLA"], x[0].data.map(function (s) {
                  return [s.name, h("code", { class: "arena-mono small", text: s.checker }), String(s.attack_points), String(s.defense_points), String(s.loss_points), String(s.sla_points)];
                }))),
                A.section("Recent rounds", A.table(["#", "Status", "Starts", "Ends"], x[1].data.map(function (rd) { return [String(rd.number), rd.status, A.fmt(rd.starts_at), A.fmt(rd.ends_at)]; }))),
                A.section("Uptime", A.table(["Team", "Service", "Uptime"], x[2].data.map(function (u) { return ["#" + u.team_id, "#" + u.service_id, Math.round(u.uptime * 100) + "%"]; }))),
                can("ad.operate") ? A.section("Add service", form([{ name: "name", label: "Name" }, { name: "image_id", label: "Image ID", type: "number" },
                  { name: "checker", label: "Checker (mock or repo@sha256:…)" }, { name: "attack_points", label: "Attack points", type: "number" },
                  { name: "defense_points", label: "Defense points", type: "number" }, { name: "loss_points", label: "Loss points", type: "number" }, { name: "sla_points", label: "SLA points", type: "number" }],
                  null, "Add", function (d) { return A.post(P + "/ad/services", d).then(function () { ok("Service added."); }); })) : null);
            });
          } } : null,
          { key: "labs", label: "Labs & instances", perm: "event.view_admin", render: function () {
            return h("div", {}, A.section("Challenge instances", paged(P + "/instances", ["#", "Status", "Entity", "Challenge", "Expires", ""], function (i) {
              return [String(i.id), A.badge(i.status), i.entity_key, "#" + i.challenge_id, A.fmt(i.expires_at),
                can("labs.manage") && i.status === "running" ? A.button("Stop", function () { return A.post(P + "/instances/" + i.id + "/stop").then(function () { ok("Stopping."); }); }, "outline-danger") : ""];
            })), A.section("Lab sessions", paged(P + "/labs/sessions", ["#", "Lab", "Status", "Entity", "Completed", ""], function (s) {
              return [String(s.id), "#" + s.lab_id, A.badge(s.status), s.entity_key, s.completed ? "yes" : "no",
                can("labs.manage") && ["ready", "provisioning"].indexOf(s.status) >= 0 ? A.button("Release", function () { return A.post(P + "/labs/sessions/" + s.id + "/release").then(function () { ok("Releasing."); }); }, "outline-danger") : ""];
            })), can("labs.manage") ? A.section("Attach a provider lab", form([{ name: "provider_id", label: "Provider ID", type: "number" },
              { name: "template_id", label: "Template ID" }, { name: "name", label: "Name" }, { name: "ttl_minutes", label: "TTL (minutes)", type: "number" },
              { name: "completion_points", label: "Completion points", type: "number" }], null, "Attach",
              function (d) { return A.post(P + "/labs", d).then(function () { ok("Lab attached."); }); })) : null);
          } },
          D.certificates === "true" ? { key: "certificates", label: "Certificates", perm: "event.view_admin", render: function () {
            return Promise.all([A.get(P + "/certificates/templates"), A.get(P + "/certificates")]).then(function (x) {
              return h("div", {}, A.section("Templates", A.table(["Name", "Kind", ""], x[0].data.map(function (t) {
                return [t.name, t.kind, can("certificates.manage") ? A.button("Issue", function () { return A.post(P + "/certificates/templates/" + t.id + "/issue").then(function () { ok("Issuing in the background."); }); }) : ""];
              }))), A.section("Issued", A.table(["Recipient", "Kind", "Status", "Issued", ""], x[1].data.map(function (c) {
                return [c.recipient + (c.team ? " (" + c.team + ")" : ""), c.kind, A.badge(c.status), A.fmt(c.issued_at),
                  can("certificates.manage") && c.status === "issued" ? A.button("Revoke", function () {
                    var reason = prompt("Reason for revocation"); if (reason) return A.post(P + "/certificates/" + c.db_id + "/revoke", { reason: reason }).then(function () { ok("Revoked."); });
                  }, "outline-danger") : ""];
              }))), can("certificates.manage") ? A.section("New template", form([{ name: "name", label: "Name" },
                { name: "kind", label: "Kind", type: "select", options: ["participation", "winner", "top_n", "team", "custom"] },
                { name: "title", label: "Title" }, { name: "criteria", label: "Criteria (JSON)", type: "json", default: { min_solves: 1 }, rows: 2 }],
                null, "Create", function (d) { return A.post(P + "/certificates/templates", d).then(function () { ok("Template created."); }); })) : null);
            });
          } } : null,
          { key: "announcements", label: "Announcements", perm: "event.manage", render: function () {
            return A.get("/events/" + EID + "/announcements").then(function (x) {
              return h("div", {}, x.data.map(function (a) {
                return A.card(h("strong", { text: a.title }), h("div", { class: "arena-desc", html: a.html }),
                  A.button("Delete", function () { return A.del(P + "/announcements/" + a.id).then(function () { ok("Deleted."); }); }, "outline-danger"));
              }), A.section("Post announcement", form([{ name: "title", label: "Title" }, { name: "body", label: "Body (Markdown)", type: "textarea" },
                { name: "pinned", label: "Pinned", type: "checkbox" }], null, "Post", function (d) { return A.post(P + "/announcements", d).then(function () { ok("Posted."); }); })));
            });
          } },
          { key: "audit", label: "Audit log", perm: "audit.read", render: function () {
            return A.get(P + "/activity?limit=100").then(function (x) {
              return A.table(["When", "Actor", "Type", "Action", "Target", "Details"], x.data.map(function (a) {
                return [A.fmt(a.created_at), a.actor_user_id ? "#" + a.actor_user_id : "system", a.actor_type, a.action,
                  (a.target_type || "") + " " + (a.target_id || ""), h("code", { class: "arena-mono small", text: a.meta ? JSON.stringify(a.meta) : "" })];
              }));
            });
          } },
          { key: "exports", label: "Exports", perm: "analytics.read", render: function () {
            return h("ul", {}, ["registrations", "teams", "challenges", "scoreboard", "solves", "submissions", "activity"].map(function (k) {
              return h("li", {}, h("a", { href: A.root.dataset.api + P + "/export/" + k + ".csv", text: k + ".csv" }));
            }));
          } }
        ].filter(Boolean);
        A.render.apply(null, tabs(defs));
      });
    };

    document.dispatchEvent(new Event("arena-admin-ready"));
  });
})();
