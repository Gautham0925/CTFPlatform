/* Arena participant dashboard (vanilla JS, no framework).
 *
 * Security notes
 *  - All data comes from the Arena JSON API, which enforces authorization.
 *  - The DOM builder `h()` sets text with textContent. Only fields already
 *    sanitized by the server (*_html) are inserted with the `html` attribute.
 *  - Mutating requests send the CTFd CSRF nonce (CSRF-Token header).
 */
(function () {
  "use strict";
  var root = document.getElementById("arena-app");
  if (!root) return;
  var D = root.dataset;
  var API = D.api;
  var BASE = D.base || "";
  var main = document.getElementById("arena-main");
  var alerts = document.getElementById("arena-alerts");
  var EID = D.eventId;

  // ---------------------------------------------------------------- utils
  function nonce() { return (window.init && window.init.csrfNonce) || ""; }

  function api(method, path, body, extraHeaders) {
    var headers = { Accept: "application/json", "CSRF-Token": nonce() };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    Object.assign(headers, extraHeaders || {});
    return fetch(API + path, {
      method: method, credentials: "same-origin", headers: headers,
      body: body !== undefined ? JSON.stringify(body) : undefined
    }).then(function (r) {
      if (r.status === 304) return { notModified: true, response: r };
      return r.json().catch(function () { return null; }).then(function (data) {
        if (!r.ok || !data || data.success === false) {
          var err = new Error((data && data.errors && data.errors[0]) || ("Request failed (" + r.status + ")"));
          err.status = r.status; err.code = data && data.code;
          throw err;
        }
        data.response = r;
        return data;
      });
    });
  }
  var get = function (p, hdrs) { return api("GET", p, undefined, hdrs); };
  var post = function (p, b) { return api("POST", p, b || {}); };
  var del = function (p) { return api("DELETE", p, {}); };

  function h(tag, attrs) {
    var el = document.createElement(tag);
    attrs = attrs || {};
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === undefined || v === null || v === false) return;
      if (k === "text") el.textContent = v;
      else if (k === "html") el.innerHTML = v; // server-sanitized fields only
      else if (k === "class") el.className = v;
      else if (k.indexOf("on") === 0) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function append(el, child) {
    if (child === undefined || child === null || child === false) return;
    if (Array.isArray(child)) { child.forEach(function (c) { append(el, c); }); return; }
    el.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  function render() { // null/false children are skipped (never rendered as text)
    var nodes = [];
    for (var i = 0; i < arguments.length; i++) { var c = arguments[i]; if (c !== null && c !== undefined && c !== false) nodes.push(c); }
    main.replaceChildren.apply(main, nodes);
  }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
  function flash(msg, kind) {
    var box = h("div", { class: "alert alert-" + (kind || "danger") + " alert-dismissible", role: "alert" },
      msg, h("button", { type: "button", class: "btn-close", "aria-label": "Close", onclick: function () { box.remove(); } }));
    alerts.replaceChildren(box);
    if (kind === "success") setTimeout(function () { box.remove(); }, 4000);
  }
  function fail(e) { flash(e.message || String(e)); }
  function fmt(iso) { return iso ? new Date(iso).toLocaleString() : "—"; }
  function badge(text, kind) { return h("span", { class: "badge text-bg-" + (kind || "secondary"), text: text }); }
  function empty(text) { return h("p", { class: "text-muted", text: text }); }
  function card() { var c = h("div", { class: "arena-card" }); for (var i = 0; i < arguments.length; i++) append(c, arguments[i]); return c; }
  function section(title) {
    var s = h("section", { class: "arena-section" }, h("h2", { class: "h5", text: title }));
    for (var i = 1; i < arguments.length; i++) append(s, arguments[i]);
    return s;
  }
  function table(headers, rows) {
    return h("div", { class: "table-responsive" },
      h("table", { class: "table table-sm arena-table" },
        h("thead", {}, h("tr", {}, headers.map(function (x) { return h("th", { scope: "col", text: x }); }))),
        h("tbody", {}, rows.map(function (r) { return h("tr", {}, r.map(function (c) { return h("td", {}, c); })); }))));
  }
  function button(label, onclick, kind) {
    return h("button", { type: "button", class: "btn btn-sm btn-" + (kind || "primary"), onclick: function (ev) {
      var b = ev.currentTarget; b.disabled = true;
      Promise.resolve(onclick(ev)).catch(fail).finally(function () { b.disabled = false; });
    }, text: label });
  }
  function countdown(target, prefix) {
    var el = h("span", { class: "arena-countdown" });
    function tick() {
      var ms = new Date(target) - new Date();
      if (ms <= 0) { el.textContent = prefix + " now"; return; }
      var s = Math.floor(ms / 1000), d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600),
        mm = Math.floor(s % 3600 / 60), ss = s % 60;
      el.textContent = prefix + " " + (d ? d + "d " : "") + [hh, mm, ss].map(function (n) { return String(n).padStart(2, "0"); }).join(":");
      setTimeout(tick, 1000);
    }
    tick();
    return el;
  }
  var PHASE = { draft: "secondary", upcoming: "info", active: "success", paused: "warning", completed: "dark", archived: "secondary" };
  function phaseBadge(p) { return badge(p, PHASE[p] || "secondary"); }

  function poll(fn, ms) { // pauses while the tab is hidden
    var timer = null, stopped = false;
    function loop() { if (stopped) return; if (!document.hidden) fn(); timer = setTimeout(loop, ms()); }
    timer = setTimeout(loop, ms());
    return function () { stopped = true; clearTimeout(timer); };
  }

  // ---------------------------------------------------------------- pages
  var pages = {};

  pages.events = function () {
    return Promise.all([get("/events?per_page=100"), D.userId ? get("/me/events") : Promise.resolve({ data: [] })])
      .then(function (res) {
        var all = res[0].data, mine = res[1].data;
        function cardFor(e) {
          return card(
            h("h2", {}, h("a", { href: BASE + "/arena/e/" + e.slug, text: e.name })),
            h("div", { class: "d-flex gap-1 flex-wrap mb-2" }, phaseBadge(e.phase), badge(e.type.toUpperCase(), "light"),
              e.team_mode === "team" ? badge("Teams of " + e.team_size_min + "–" + e.team_size_max, "light") : badge("Individual", "light"),
              e.my_registration ? badge("You: " + e.my_registration, "primary") : null),
            h("div", { class: "small text-muted" }, "Starts " + fmt(e.starts_at), h("br"), "Ends " + fmt(e.ends_at)),
            e.registration.seats_left !== null ? h("div", { class: "small mt-1", text: e.registration.seats_left + " seats left" }) : null);
        }
        render(
          mine.length ? section("My competitions", h("div", { class: "arena-grid" }, mine.map(cardFor))) : null,
          section("All competitions", all.length ? h("div", { class: "arena-grid" }, all.map(cardFor)) : empty("No competitions are open yet.")));
      });
  };

  pages.join = function () {
    var input = h("input", { class: "form-control", id: "invite", autocomplete: "off", required: true, maxlength: "128" });
    var form = h("form", { class: "arena-card", style: "max-width:28rem", onsubmit: function (ev) {
      ev.preventDefault();
      post("/events/join", { invite_code: input.value.trim() }).then(function (r) {
        window.location = BASE + "/arena/e/" + r.data.event.slug;
      }).catch(fail);
    } }, h("label", { for: "invite", class: "form-label", text: "Invitation code" }), input,
      h("button", { class: "btn btn-primary mt-3", type: "submit", text: "Join competition" }));
    render(form);
  };

  function eventInfo() { return get("/events/" + EID); }

  function setPhase(ev) {
    var el = document.querySelector("[data-arena-phase]");
    if (!el) return;
    el.replaceChildren(phaseBadge(ev.phase), " ",
      ev.phase === "upcoming" && ev.starts_at ? countdown(ev.starts_at, "Starts in") :
        ev.phase === "active" && ev.ends_at ? countdown(ev.ends_at, "Ends in") : "");
  }

  pages["event-overview"] = function () {
    return Promise.all([eventInfo(), get("/events/" + EID + "/announcements")]).then(function (res) {
      var ev = res[0].data, anns = res[1].data, reg = ev.registration;
      setPhase(ev);
      var regBox;
      if (!D.userId) {
        regBox = h("p", {}, h("a", { href: BASE + "/login?next=" + encodeURIComponent(location.pathname), text: "Log in" }), " to register.");
      } else if (ev.my_registration === "approved" || ev.my_registration === "pending") {
        regBox = h("div", {}, h("p", {}, "Your registration: ", badge(ev.my_registration, ev.my_registration === "approved" ? "success" : "warning")),
          button("Withdraw", function () {
            if (!confirm("Withdraw from this competition? You will leave your team.")) return;
            return post("/events/" + EID + "/withdraw").then(function () { location.reload(); });
          }, "outline-danger"));
      } else if (!reg.open) {
        regBox = h("p", { class: "text-muted", text: "Registration is closed." });
      } else {
        var code = reg.invite_required ? h("input", { class: "form-control mb-2", placeholder: "Invitation code", "aria-label": "Invitation code" }) : null;
        regBox = h("div", {}, code, button(reg.requires_approval ? "Request to join" : "Register", function () {
          return post("/events/" + EID + "/register", code ? { invite_code: code.value.trim() } : {}).then(function (r) {
            flash("Registration " + r.data.status + ".", "success");
            setTimeout(function () { location.reload(); }, 600);
          });
        }));
      }
      render(h("div", { class: "row g-3" },
        h("div", { class: "col-lg-8" },
          ev.description_html ? card(h("div", { class: "arena-desc", html: ev.description_html })) : null,
          ev.rules_html ? section("Rules", card(h("div", { class: "arena-desc", html: ev.rules_html }))) : null,
          section("Announcements", anns.length ? anns.map(function (a) {
            return card(h("h3", {}, a.pinned ? "📌 " : "", a.title), h("div", { class: "small text-muted mb-1", text: fmt(a.created_at) }),
              h("div", { class: "arena-desc", html: a.html }));
          }) : empty("No announcements yet."))),
        h("div", { class: "col-lg-4" },
          card(h("h2", { text: "Registration" }), regBox,
            h("dl", { class: "arena-kv small mt-3" },
              h("dt", { text: "Starts" }), h("dd", { text: fmt(ev.starts_at) }),
              h("dt", { text: "Ends" }), h("dd", { text: fmt(ev.ends_at) }),
              reg.closes_at ? [h("dt", { text: "Registration closes" }), h("dd", { text: fmt(reg.closes_at) })] : null,
              reg.max_participants ? [h("dt", { text: "Seats" }), h("dd", { text: reg.seats_left + " / " + reg.max_participants + " left" })] : null,
              h("dt", { text: "Format" }), h("dd", { text: ev.team_mode === "team" ? "Teams (" + ev.team_size_min + "–" + ev.team_size_max + ")" : "Individual" }))))));
    });
  };

  // -------------------------------------------------------- challenges
  pages["event-challenges"] = function () {
    var state = { category: "", status: "", q: "" };
    var grid = h("div", { class: "arena-grid", role: "list" });
    var detail = h("div", { id: "arena-detail", class: "mt-3" });
    function load() {
      var qs = new URLSearchParams();
      Object.keys(state).forEach(function (k) { if (state[k]) qs.set(k, state[k]); });
      return get("/events/" + EID + "/challenges?" + qs).then(function (r) {
        if (!r.data.length) { grid.replaceChildren(empty("No challenges match.")); return r; }
        grid.replaceChildren.apply(grid, r.data.map(function (c) {
          if (c.state === "locked") {
            return h("div", { class: "arena-card arena-chal locked", role: "listitem" },
              h("div", { class: "small text-muted", text: c.category }), h("div", { text: "🔒 Locked — solve its prerequisites" }));
          }
          return h("button", { type: "button", role: "listitem", class: "arena-card arena-chal " + (c.solved ? "solved " : "") + c.state,
            "aria-label": c.name + ", " + c.value + " points" + (c.solved ? ", solved" : ""),
            onclick: function () { openChallenge(c.id); } },
            h("div", { class: "small text-muted", text: c.category + (c.difficulty ? " · " + c.difficulty : "") }),
            h("div", { class: "fw-semibold", text: c.name }),
            h("div", { class: "d-flex justify-content-between align-items-end mt-2" },
              h("span", { class: "value", text: c.value }),
              h("span", { class: "small text-muted", text: (c.solves !== undefined ? plural(c.solves, "solve") : "") + (c.solved ? " ✔" : "") })));
        }));
        return r;
      });
    }
    function openChallenge(id) {
      return get("/events/" + EID + "/challenges/" + id).then(function (r) {
        var c = r.data;
        var input = h("input", { class: "form-control", placeholder: "flag{...}", "aria-label": "Flag", maxlength: "2048", autocomplete: "off" });
        var result = h("div", { class: "mt-2", "aria-live": "polite" });
        var form = h("form", { class: "d-flex gap-2 mt-3", onsubmit: function (ev) {
          ev.preventDefault();
          post("/events/" + EID + "/challenges/" + id + "/attempt", { submission: input.value }).then(function (res) {
            var d = res.data, ok = d.status === "correct";
            result.replaceChildren(h("div", { class: "alert alert-" + (ok ? "success" : d.status === "already_solved" ? "info" : "warning"),
              text: (d.message || d.status) + (d.attempts_remaining !== undefined ? " (" + d.attempts_remaining + " attempts left)" : "") }));
            if (ok) { input.value = ""; load(); }
          }).catch(function (e) { result.replaceChildren(h("div", { class: "alert alert-danger", text: e.message })); });
        } }, input, h("button", { class: "btn btn-primary", type: "submit", text: "Submit", disabled: c.state !== "open" || c.solved }));
        var hints = c.hints.map(function (hi) {
          if (hi.unlocked) return card(h("strong", { text: hi.title || "Hint" }), h("div", { class: "arena-desc", html: hi.content_html }));
          return card(h("strong", { text: hi.title || "Hint" }), " ",
            button("Unlock (" + hi.cost + " points)", function () {
              if (!confirm("Unlock this hint for " + hi.cost + " points?")) return;
              return post("/events/" + EID + "/hints/" + hi.id + "/unlock").then(function () { openChallenge(id); });
            }, "outline-secondary"));
        });
        var inst = c.has_instance ? instancePanel(id) : null;
        detail.replaceChildren(card(
          h("div", { class: "d-flex justify-content-between flex-wrap gap-2" },
            h("h2", { class: "h4", text: c.name }), h("span", { class: "value fs-5", text: c.value + " pts" })),
          h("div", { class: "d-flex gap-1 flex-wrap mb-2" }, badge(c.category, "light"), c.difficulty ? badge(c.difficulty, "light") : null,
            c.solved ? badge("Solved", "success") : null, c.state !== "open" ? badge(c.state, "warning") : null,
            c.max_attempts ? badge((c.attempts || 0) + " / " + c.max_attempts + " attempts", "light") : null),
          h("div", { class: "arena-desc", html: c.description_html }),
          c.connection_info ? h("p", {}, "Connection: ", h("code", { class: "arena-mono", text: c.connection_info })) : null,
          c.files.length ? h("ul", { class: "list-unstyled" }, c.files.map(function (f) {
            return h("li", {}, "📎 ", h("a", { href: f.url, text: f.name, download: true }));
          })) : null,
          inst, hints.length ? section("Hints", hints) : null, form, result));
        detail.scrollIntoView({ behavior: "auto", block: "nearest" });
      }).catch(fail);
    }
    function instancePanel(id) {
      var box = h("div", { class: "arena-card my-2", "aria-live": "polite" });
      var path = "/events/" + EID + "/challenges/" + id + "/instance";
      var stop = null;
      function show(i) {
        if (!i) { box.replaceChildren(h("strong", { text: "Challenge instance " }), button("Start instance", function () { return post(path).then(function (r) { show(r.data); watch(); }); })); return; }
        var endpoint = i.status === "running" && i.ports ? Object.keys(i.ports).map(function (p) { return i.host + ":" + i.ports[p]; }).join(", ") : null;
        box.replaceChildren(h("div", {}, "Instance: ", badge(i.status, i.status === "running" ? "success" : i.status === "failed" ? "danger" : "info")),
          endpoint ? h("p", { class: "mt-2 mb-1" }, "Endpoint: ", h("code", { class: "arena-mono", text: endpoint })) : null,
          i.expires_at ? h("p", { class: "small text-muted", text: "Expires " + fmt(i.expires_at) }) : null,
          i.error ? h("p", { class: "text-danger small", text: i.error }) : null,
          h("div", { class: "d-flex gap-2" },
            i.status === "running" ? button("Restart", function () { return post(path + "/restart"); }, "outline-secondary") : null,
            ["failed", "expired", "stopped"].indexOf(i.status) < 0 ? button("Stop", function () { return del(path).then(function () { show(null); }); }, "outline-danger") : null));
      }
      function refresh() { return get(path).then(function (r) { show(r.data); if (r.data && r.data.status === "running" && stop) { stop(); stop = null; } }); }
      function watch() { if (!stop) stop = poll(refresh, function () { return 2000; }); }
      refresh().then(function () { watch(); }).catch(fail);
      return box;
    }
    var search = h("input", { class: "form-control form-control-sm", type: "search", placeholder: "Search", "aria-label": "Search challenges",
      oninput: function (e) { state.q = e.target.value; clearTimeout(search._t); search._t = setTimeout(load, 250); } });
    var catSel = h("select", { class: "form-select form-select-sm", "aria-label": "Category", onchange: function (e) { state.category = e.target.value; load(); } });
    var statusSel = h("select", { class: "form-select form-select-sm", "aria-label": "Status", onchange: function (e) { state.status = e.target.value; load(); } },
      h("option", { value: "", text: "All" }), h("option", { value: "unsolved", text: "Unsolved" }), h("option", { value: "solved", text: "Solved" }));
    render(h("div", { class: "row g-2 mb-3" },
      h("div", { class: "col-12 col-md-5" }, search), h("div", { class: "col-6 col-md-4" }, catSel), h("div", { class: "col-6 col-md-3" }, statusSel)),
      grid, detail);
    return eventInfo().then(function (r) { setPhase(r.data); return load(); }).then(function (r) {
      catSel.replaceChildren(h("option", { value: "", text: "All categories" }),
        ((r && r.meta && r.meta.categories) || []).map(function (c) { return h("option", { value: c, text: c }); }));
    }).catch(function (e) { render(empty(e.code === "not_started" ? "Challenges become available when the competition starts." : e.message)); });
  };

  // -------------------------------------------------------- scoreboard
  pages["event-scoreboard"] = function () {
    var etag = null, view = "default", refresh = 15;
    var body = h("div"), meta = h("div", { class: "small text-muted mb-2" });
    var chart = h("div");
    function draw(payload) {
      refresh = payload.refresh_seconds || 15;
      meta.textContent = (payload.frozen ? "Scoreboard frozen at " + fmt(payload.freeze_at) + ". " : "") + "Updated " + fmt(payload.generated_at);
      var rows = payload.standings;
      body.replaceChildren(rows.length ? table(["#", payload.mode === "team" && view !== "individuals" ? "Team" : "Participant", "Score", "Jeopardy", "A&D", "Adj.", "Solves"],
        rows.map(function (r) {
          return [String(r.rank), h("span", {}, r.name || r.key, r.affiliation ? h("span", { class: "text-muted small", text: " · " + r.affiliation }) : null),
            h("strong", { text: r.score }), String(r.breakdown.jeopardy), String(r.breakdown.attack_defense),
            String(r.breakdown.adjustments), String(r.solves)];
        })) : empty("No scores yet."));
    }
    function load() {
      var q = view === "individuals" ? "?view=individuals&per_page=200" : "?per_page=200";
      return get("/events/" + EID + "/scoreboard" + q, etag ? { "If-None-Match": etag } : {}).then(function (r) {
        if (r.notModified) return;
        etag = r.response.headers.get("ETag");
        draw(r.data);
      });
    }
    function loadChart() {
      return get("/events/" + EID + "/scoreboard/history?top=10").then(function (r) {
        var series = r.data.filter(function (s) { return s.series.length; });
        if (!series.length) { chart.replaceChildren(); return; }
        var pts = [].concat.apply([], series.map(function (s) { return s.series; }));
        var t0 = Math.min.apply(null, pts.map(function (p) { return +new Date(p[0]); })), t1 = Math.max.apply(null, pts.map(function (p) { return +new Date(p[0]); })) + 1;
        var vmax = Math.max(1, Math.max.apply(null, pts.map(function (p) { return p[1]; })));
        var NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
        svg.setAttribute("viewBox", "0 0 1000 300"); svg.setAttribute("class", "arena-spark"); svg.setAttribute("role", "img");
        svg.setAttribute("aria-label", "Score history of the top " + series.length);
        var colors = ["#0d6efd", "#dc3545", "#198754", "#fd7e14", "#6f42c1", "#20c997", "#d63384", "#6c757d", "#0dcaf0", "#ffc107"];
        series.forEach(function (s, i) {
          var line = document.createElementNS(NS, "polyline");
          line.setAttribute("fill", "none"); line.setAttribute("stroke", colors[i % 10]); line.setAttribute("stroke-width", "3");
          line.setAttribute("points", s.series.map(function (p) {
            return ((+new Date(p[0]) - t0) / (t1 - t0) * 990 + 5).toFixed(1) + "," + (295 - p[1] / vmax * 285).toFixed(1);
          }).join(" "));
          var title = document.createElementNS(NS, "title"); title.textContent = s.name; line.appendChild(title);
          svg.appendChild(line);
          s.series.forEach(function (p) { // markers: a single point is otherwise invisible
            var dot = document.createElementNS(NS, "circle");
            dot.setAttribute("cx", ((+new Date(p[0]) - t0) / (t1 - t0) * 990 + 5).toFixed(1));
            dot.setAttribute("cy", (295 - p[1] / vmax * 285).toFixed(1));
            dot.setAttribute("r", "6"); dot.setAttribute("fill", colors[i % 10]);
            svg.appendChild(dot);
          });
        });
        chart.replaceChildren(section("Top " + series.length + " over time", svg,
          h("div", { class: "small d-flex flex-wrap gap-3" }, series.map(function (s, i) {
            return h("span", {}, h("span", { style: "color:" + colors[i % 10], text: "■ " }), s.name);
          }))));
      }).catch(function () { chart.replaceChildren(); });
    }
    var toggle = D.teamMode === "team" ? h("div", { class: "btn-group btn-group-sm mb-2", role: "group", "aria-label": "View" },
      button("Teams", function () { view = "default"; etag = null; return load(); }, "outline-primary"),
      button("Individuals", function () { view = "individuals"; etag = null; return load().catch(function (e) { view = "default"; throw e; }); }, "outline-primary")) : null;
    render(toggle, meta, body, chart);
    return eventInfo().then(function (r) { setPhase(r.data); return load(); }).then(function () {
      loadChart();
      poll(function () { load().catch(function () { /* keep last board on errors */ }); }, function () { return refresh * 1000; });
    }).catch(function (e) { render(empty(e.message)); });
  };

  // -------------------------------------------------------------- team
  pages["event-team"] = function () {
    var path = "/events/" + EID;
    return Promise.all([get(path + "/my-team"), get(path + "/my-invites")]).then(function (res) {
      var team = res[0].data, invites = res[1].data;
      if (!team) {
        var name = h("input", { class: "form-control", maxlength: "64", placeholder: "Team name", "aria-label": "Team name" });
        var vis = h("select", { class: "form-select", "aria-label": "Team visibility" }, h("option", { value: "public", text: "Public" }), h("option", { value: "private", text: "Private (join by code only)" }));
        var code = h("input", { class: "form-control", maxlength: "64", placeholder: "Join code", "aria-label": "Join code", autocomplete: "off" });
        render(h("div", { class: "row g-3" },
          h("div", { class: "col-md-6" }, card(h("h2", { text: "Create a team" }), name, h("div", { class: "my-2" }, vis),
            button("Create team", function () {
              return post(path + "/teams", { name: name.value, visibility: vis.value }).then(function (r) {
                flash("Team created. Share this join code with your teammates now (it is shown only once): " + r.data.join_code, "success");
                setTimeout(function () { pages["event-team"](); }, 50);
              });
            }))),
          h("div", { class: "col-md-6" }, card(h("h2", { text: "Join a team" }), code, h("div", { class: "mt-2" },
            button("Join", function () { return post(path + "/teams/join", { join_code: code.value.trim() }).then(function () { pages["event-team"](); }); })))),
          invites.length ? h("div", { class: "col-12" }, section("Invitations", table(["Team", "Expires", ""], invites.map(function (i) {
            return [i.team_name, fmt(i.expires_at), h("span", { class: "d-flex gap-2" },
              button("Accept", function () { return post(path + "/my-invites/" + i.id + "/accept").then(function () { pages["event-team"](); }); }),
              button("Decline", function () { return post(path + "/my-invites/" + i.id + "/reject").then(function () { pages["event-team"](); }); }, "outline-secondary"))];
          })))) : null));
        return;
      }
      var me = Number(D.userId), captain = team.captain_user_id === me;
      var inviteId = h("input", { class: "form-control form-control-sm", type: "number", min: "1", placeholder: "User ID", "aria-label": "User ID to invite" });
      render(card(
        h("div", { class: "d-flex flex-wrap gap-2 align-items-center" }, h("h2", { class: "h4 mb-0 me-auto", text: team.name }),
          badge(team.status, team.status === "active" ? "success" : "danger"), badge(team.visibility, "light"), badge(team.member_count + " members", "light")),
        section("Members", table(["Name", "Role", "Joined", ""], team.members.map(function (m) {
          return [m.name + (m.user_id === me ? " (you)" : ""), m.role, fmt(m.joined_at),
            captain && m.user_id !== me ? h("span", { class: "d-flex gap-2" },
              button("Make captain", function () { return post("/events/" + EID + "/my-team/captain", { user_id: m.user_id }).then(function () { pages["event-team"](); }); }, "outline-secondary"),
              button("Remove", function () { if (confirm("Remove " + m.name + "?")) return post("/events/" + EID + "/my-team/members/" + m.user_id + "/remove").then(function () { pages["event-team"](); }); }, "outline-danger")) : ""];
        }))),
        captain ? section("Captain tools",
          h("div", { class: "d-flex gap-2 flex-wrap" }, inviteId, button("Invite", function () {
            return post("/events/" + EID + "/my-team/invites", { user_id: Number(inviteId.value) }).then(function () { flash("Invitation sent.", "success"); pages["event-team"](); });
          }), button("New join code", function () {
            return post("/events/" + EID + "/my-team/join-code").then(function (r) { flash("New join code (old one no longer works): " + r.data.join_code, "success"); });
          }, "outline-secondary")),
          team.pending_invites && team.pending_invites.length ? table(["User", "Expires", ""], team.pending_invites.map(function (i) {
            return ["#" + i.user_id, fmt(i.expires_at), button("Cancel", function () { return del("/events/" + EID + "/my-team/invites/" + i.id).then(function () { pages["event-team"](); }); }, "outline-secondary")];
          })) : null) : null,
        h("div", { class: "mt-3" }, button("Leave team", function () {
          if (confirm("Leave " + team.name + "?")) return post("/events/" + EID + "/my-team/leave").then(function () { pages["event-team"](); });
        }, "outline-danger"))));
    }).catch(function (e) { render(empty(e.message)); });
  };

  // --------------------------------------------------------------- A&D
  pages["event-ad"] = function () {
    var results = h("div", { "aria-live": "polite" });
    function load() {
      return get("/events/" + EID + "/ad/status").then(function (r) {
        var s = r.data, rnd = s.round;
        var flags = h("textarea", { class: "form-control arena-mono", rows: "4", placeholder: "One flag per line", "aria-label": "Captured flags" });
        render(h("div", { class: "row g-3" },
          h("div", { class: "col-lg-7" },
            card(h("h2", { text: rnd ? "Round " + rnd.number : "Waiting for the first round" }),
              rnd ? countdown(rnd.ends_at, "Ends in") : null),
            section("Your services", s.my_services.length ? table(["Service", "Status", "Endpoint", "Last check"], s.my_services.map(function (m) {
              return [m.service, h("span", { class: "arena-status-" + m.status, text: m.status }),
                m.endpoint ? h("code", { class: "arena-mono", text: m.endpoint.host + " " + JSON.stringify(m.endpoint.ports) }) : "—", fmt(m.last_check_at)];
            })) : empty("Your services are being provisioned.")),
            section("Targets", s.targets.length ? table(["Team", "Service", "Endpoint"], s.targets.map(function (t) {
              return ["#" + t.team_id, t.service, t.endpoint ? h("code", { class: "arena-mono", text: t.endpoint.host + " " + JSON.stringify(t.endpoint.ports) }) : "—"];
            })) : empty("No targets yet."))),
          h("div", { class: "col-lg-5" }, card(h("h2", { text: "Submit flags" }), flags, h("div", { class: "mt-2" },
            button("Submit", function () {
              var list = flags.value.split(/\s+/).map(function (x) { return x.trim(); }).filter(Boolean).slice(0, 50);
              if (!list.length) return;
              return post("/events/" + EID + "/ad/flags", { flags: list }).then(function (res) {
                results.replaceChildren(table(["Flag", "Result"], res.data.results.map(function (x) {
                  return [h("code", { class: "arena-mono", text: x.flag }), badge(x.result, x.result === "accepted" ? "success" : "secondary")];
                })));
                flags.value = "";
              });
            })), results),
            section("Uptime", s.uptime.length ? table(["Service", "Uptime"], s.uptime.map(function (u) {
              return ["#" + u.service_id, Math.round(u.uptime * 100) + "% (" + u.up + "/" + u.checks + ")"];
            })) : empty("No checks yet.")))));
      });
    }
    return load().then(function () { poll(function () { return; }, function () { return 60000; }); }).catch(function (e) { render(empty(e.message)); });
  };

  // -------------------------------------------------------------- labs
  pages["event-labs"] = function () {
    var stop = null;
    function load() {
      return get("/events/" + EID + "/labs").then(function (r) {
        var pending = false;
        render(r.data.length ? h("div", { class: "arena-grid" }, r.data.map(function (lab) {
          var s = lab.session, path = "/events/" + EID + "/labs/" + lab.id + "/session";
          if (s && (s.status === "requested" || s.status === "provisioning")) pending = true;
          var conn = s && s.connection ? h("dl", { class: "arena-kv small mt-2" }, Object.keys(s.connection).map(function (k) {
            return [h("dt", { text: k }), h("dd", { class: "arena-mono", text: s.connection[k] })];
          })) : null;
          return card(h("h2", { text: lab.name }), h("p", { class: "small text-muted", text: lab.description || "" }),
            lab.completion_points ? badge(lab.completion_points + " points on completion", "light") : null,
            s ? h("div", { class: "mt-2" }, "Status: ", badge(s.status, s.status === "ready" ? "success" : s.status === "failed" ? "danger" : "info"),
              s.completed ? badge("Completed", "success") : null, conn,
              s.expires_at ? h("div", { class: "small text-muted", text: "Expires " + fmt(s.expires_at) }) : null,
              h("div", { class: "d-flex gap-2 mt-2" },
                s.status === "ready" ? button("Reset", function () { return post(path + "/reset").then(load); }, "outline-secondary") : null,
                button("Release", function () { return del(path).then(load); }, "outline-danger"))) :
              h("div", { class: "mt-2" }, button("Start lab", function () { return post(path).then(load); })));
        })) : empty("No labs are available for this competition."));
        if (pending && !stop) stop = poll(load, function () { return 3000; });
        if (!pending && stop) { stop(); stop = null; }
      });
    }
    return load().catch(function (e) { render(empty(e.message)); });
  };

  // ---------------------------------------------------------------- me
  pages["event-me"] = function () {
    var p = "/events/" + EID;
    var certs = D.certificates === "true" ? get(p + "/my-certificates").catch(function () { return { data: [] }; }) : Promise.resolve({ data: [] });
    return Promise.all([get(p + "/my-score"), get(p + "/my-submissions"), get(p + "/my-activity?limit=50"), certs, get(p + "/my-instances").catch(function () { return { data: [] }; })])
      .then(function (res) {
        var score = res[0].data, subs = res[1].data, act = res[2].data, cert = res[3].data, inst = res[4].data;
        render(h("div", { class: "row g-3" },
          h("div", { class: "col-lg-4" }, card(h("h2", { text: "Score" }),
            score ? h("div", {}, h("div", { class: "display-6", text: score.score }), score.rank ? h("div", { text: "Rank #" + score.rank }) : null,
              score.breakdown ? h("div", { class: "small text-muted", text: "Jeopardy " + score.breakdown.jeopardy + " · A&D " + score.breakdown.attack_defense + " · Adjustments " + score.breakdown.adjustments }) : null)
              : empty("Not ranked yet.")),
            cert.length ? section("Certificates", cert.map(function (c) {
              return card(h("strong", { text: c.kind + " certificate" }), h("div", { class: "small text-muted", text: fmt(c.issued_at) }),
                h("a", { class: "btn btn-sm btn-outline-primary mt-2", href: c.download_url, text: "Download PDF" }));
            })) : null,
            inst.length ? section("Running instances", inst.map(function (i) { return card(badge(i.status, "info"), " challenge #" + i.challenge_id); })) : null),
          h("div", { class: "col-lg-8" },
            section("Recent submissions", subs.length ? table(["Challenge", "Result", "When"], subs.map(function (s) {
              return [s.challenge || "#" + s.challenge_id, badge(s.status, s.status === "correct" ? "success" : "secondary"), fmt(s.date)];
            })) : empty("No submissions yet.")),
            section("Activity", act.length ? h("ul", { class: "list-unstyled small" }, act.map(function (a) {
              return h("li", {}, h("span", { class: "text-muted", text: fmt(a.created_at) + " — " }), a.action.replace(".", " "));
            })) : empty("No activity yet.")))));
      }).catch(function (e) { render(empty(e.message)); });
  };

  // ------------------------------------------------------------ verify
  pages.verify = function () {
    return get("/certificates/verify/" + encodeURIComponent(D.publicId) + "?code=" + encodeURIComponent(D.code || "")).then(function (r) {
      var d = r.data;
      render(card(d.valid ? h("div", { class: "alert alert-success", text: "This certificate is valid." }) :
        h("div", { class: "alert alert-danger", text: d.status === "revoked" ? "This certificate was revoked." : "This certificate could not be verified." }),
        d.recipient ? h("dl", { class: "arena-kv" },
          h("dt", { text: "Recipient" }), h("dd", { text: d.recipient }),
          d.team ? [h("dt", { text: "Team" }), h("dd", { text: d.team })] : null,
          h("dt", { text: "Competition" }), h("dd", { text: d.event }),
          h("dt", { text: "Type" }), h("dd", { text: d.kind }),
          h("dt", { text: "Issued" }), h("dd", { text: fmt(d.issued_at) })) : null));
    }).catch(fail);
  };

  window.Arena = { api: api, get: get, post: post, del: del, h: h, render: render, flash: flash, fail: fail, fmt: fmt,
    badge: badge, empty: empty, card: card, section: section, table: table, button: button, phaseBadge: phaseBadge,
    countdown: countdown, poll: poll, pages: pages, root: root, BASE: BASE };

  function start() {
    var fn = pages[D.page];
    if (fn) Promise.resolve(fn()).catch(fail);
  }
  if (D.page.indexOf("admin") === 0) document.addEventListener("arena-admin-ready", start, { once: true });
  else start();
})();
