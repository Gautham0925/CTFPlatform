// Playwright end-to-end check of the Arena dashboards (driven by run_e2e.py).
// Usage: node ui.e2e.js <base_url> <event_slug> <event_id> <screenshot_dir>
const { chromium, devices } = require("playwright");
const [base, slug, eventId, shots] = process.argv.slice(2);
const results = [];
const errors = [];
function step(name, ok, detail) { results.push({ name, ok: !!ok, detail: detail || "" }); }

async function login(page, name) {
  await page.goto(base + "/login");
  await page.fill("input[name=name]", name);
  await page.fill("input[name=password]", "password");
  await Promise.all([page.waitForNavigation(), page.click("form [type=submit]")]);
}
const EXPECTED = ["/arena/e/hidden-draft", "/arena/admin/e/"];  // deliberate negative checks
function watch(page, label) {
  page.on("pageerror", (e) => errors.push(label + " JS error: " + e.message));
  page.on("response", (r) => {
    if (r.status() < 400) return;
    const url = r.url();
    const path = url.replace(base, "");
    const deliberate = r.request().isNavigationRequest() && EXPECTED.some((p) => url.includes(p)) && label === "participant";
    // Native CTFd: the core theme opens /events (SSE) on the anonymous /login
    // page and CTFd answers 403 to anonymous users. Not Arena behaviour.
    const nativeLoginSse = path === "/events" && page.url().endsWith("/login");
    // The seeded XSS payload <img src=x onerror=...> is sanitized to a
    // harmless <img src="x">; the browser then requests ".../x" (404).
    const neutralizedPayload = /\/arena\/e\/[^/]+\/x$/.test(path);
    if (!deliberate && !nativeLoginSse && !neutralizedPayload) errors.push(label + ": " + r.status() + " " + path);
  });
}

(async () => {
  const browser = await chromium.launch();
  try {
    // ------------------------------------------------ participant (desktop)
    const ctx = await browser.newContext({ viewport: { width: 1366, height: 900 } });
    const page = await ctx.newPage();
    watch(page, "participant");
    await login(page, "player1");
    await page.goto(base + "/arena");
    await page.waitForSelector(".arena-card a");
    step("event discovery lists the event", (await page.textContent("#arena-main")).includes("Spring CTF"));
    await page.goto(base + "/arena/e/" + slug);
    await page.click("text=Register");
    await page.waitForSelector("text=Your registration");
    step("registration via UI", (await page.textContent("#arena-main")).includes("approved"));
    await page.goto(base + "/arena/e/" + slug + "/challenges");
    await page.waitForSelector(".arena-chal");
    step("challenge board renders", (await page.locator(".arena-chal").count()) === 2);
    await page.click(".arena-chal >> text=Warmup");
    await page.waitForSelector("#arena-detail form");
    step("description is sanitized (no script)", (await page.locator("#arena-detail script").count()) === 0);
    await page.fill("#arena-detail input[aria-label=Flag]", "wrong");
    await page.click("#arena-detail button[type=submit]");
    await page.waitForSelector("#arena-detail .alert-warning");
    step("wrong flag feedback", true);
    await page.fill("#arena-detail input[aria-label=Flag]", "flag{e2e}");
    await page.click("#arena-detail button[type=submit]");
    await page.waitForSelector("#arena-detail .alert-success");
    step("correct flag accepted", true);
    await page.waitForSelector(".arena-chal.solved");
    step("board marks solved", (await page.locator(".arena-chal.solved").count()) === 1);
    await page.goto(base + "/arena/e/" + slug + "/scoreboard");
    await page.waitForSelector("table");
    const board = await page.textContent("#arena-main");
    step("scoreboard shows the solver", board.includes("player1") && board.includes("100"));
    await page.screenshot({ path: shots + "/scoreboard-desktop.png", fullPage: true });
    await page.goto(base + "/arena/e/" + slug + "/me");
    await page.waitForSelector("text=Recent submissions");
    step("my progress page", (await page.textContent("#arena-main")).includes("correct"));

    // ------------------------------------------------ participant (mobile)
    const mctx = await browser.newContext({ ...devices["iPhone 12"] });
    const m = await mctx.newPage();
    watch(m, "mobile");
    await login(m, "player1");
    await m.goto(base + "/arena/e/" + slug + "/challenges");
    await m.waitForSelector(".arena-chal");
    const overflow = await m.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    step("mobile layout has no horizontal overflow", overflow <= 1, "overflow=" + overflow);
    await m.screenshot({ path: shots + "/challenges-mobile.png", fullPage: true });

    // ------------------------------------------------ cross-event isolation
    const other = await page.goto(base + "/arena/e/hidden-draft");
    step("draft event is not reachable", other.status() === 404);

    // ------------------------------------------------ organizer
    const actx = await browser.newContext({ viewport: { width: 1366, height: 900 } });
    const a = await actx.newPage();
    watch(a, "admin");
    await login(a, "admin");
    await a.goto(base + "/arena/admin/e/" + eventId);
    await a.waitForSelector(".nav-pills .nav-link");
    const tabs = await a.locator(".nav-pills .nav-link").allTextContents();
    step("admin tabs render", tabs.includes("Challenges") && tabs.includes("Scoreboard") && tabs.includes("Audit log"), tabs.join("|"));
    await a.click(".nav-pills >> text=Scoreboard");
    await a.waitForSelector("text=Scoring rules");
    step("admin scoreboard tab", (await a.textContent("#arena-main")).includes("player1"));
    await a.click(".nav-pills >> text=Audit log");
    await a.waitForSelector("table");
    step("audit log shows organizer actions", (await a.textContent("#arena-main")).includes("challenge.create"));
    await a.screenshot({ path: shots + "/admin-audit.png", fullPage: true });
    await a.goto(base + "/arena/admin/platform");
    await a.waitForSelector("text=Readiness");
    step("platform health page", (await a.textContent("#arena-main")).includes("Workers"));

    // ------------------------------------------------ plain participant on admin
    const forbidden = await page.goto(base + "/arena/admin/e/" + eventId);
    step("participant cannot open organizer dashboard", forbidden.status() === 403);
  } catch (e) {
    step("unexpected exception", false, String(e));
  } finally {
    await browser.close();
  }
  step("no browser console errors", errors.length === 0, errors.join(" | "));
  console.log(JSON.stringify(results));
})();
