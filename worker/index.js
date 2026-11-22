// ChipHunter watcher + Telegram bot gateway — Cloudflare Worker (free plan).
//
// scheduled (every 2 min): poll the fast sources, keep NEW student/intern jobs in Israel,
//   hand them to GitHub Actions (process.yml) which evaluates, builds the CV and notifies.
// fetch POST /tg/<WEBHOOK_SECRET>: Telegram webhook — links, text, screenshots, buttons.
//
// Secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GITHUB_TOKEN, WEBHOOK_SECRET
// Vars:    GITHUB_REPO ("owner/name")
// KV:      SEEN
// AI:      Workers AI binding named AI (voice message transcription)

const STUDENT = /\b(students?|interns?|internship)\b|סטודנט/i;

// Big chip companies: checked every 2 minutes. The rest rotate (each about every 6 minutes) to stay within
// Cloudflare's free limits (50 outgoing requests per run).
const WORKDAY = [
  ["NVIDIA", "nvidia.wd5.myworkdayjobs.com", "NVIDIAExternalCareerSite"],
  ["Intel", "intel.wd1.myworkdayjobs.com", "External"],
  ["Marvell", "marvell.wd1.myworkdayjobs.com", "MarvellCareers"],
  ["Applied Materials", "amat.wd1.myworkdayjobs.com", "External"],
];
const WORKDAY_B = [
  ["Altera", "altera.wd1.myworkdayjobs.com", "Altera"], ["Broadcom", "broadcom.wd1.myworkdayjobs.com", "External_Career"],
  ["Cadence", "cadence.wd1.myworkdayjobs.com", "External_Careers"], ["KLA", "kla.wd1.myworkdayjobs.com", "Search"],
  ["Analog Devices", "analogdevices.wd1.myworkdayjobs.com", "External"],
];
const WORKDAY_C = [
  ["Motorola Solutions", "motorolasolutions.wd5.myworkdayjobs.com", "Careers"], ["Cisco", "cisco.wd5.myworkdayjobs.com", "Cisco_Careers"],
  ["Micron", "micron.wd1.myworkdayjobs.com", "External"], ["Medtronic", "medtronic.wd1.myworkdayjobs.com", "MedtronicCareers"],
  ["Ciena", "ciena.wd5.myworkdayjobs.com", "Careers"],
];
const COMEET_B = [["NextSilicon", "nextsilicon", "18.007"], ["proteanTecs", "proteantecs", "D5.00E"],
  ["Xsight Labs", "xsightlabs", "46.00C"], ["DriveNets", "drivenets", "72.006"]];
const COMEET_C = [["Innoviz", "innoviz", "52.004"], ["SolarEdge", "SolarEdge", "71.00A"],
  ["Quantum Machines", "quantummachines", "D6.000"], ["Kela Technologies", "kelatechnologies", "2A.007"], ["VAST Data", "vastdata", "43.001"]];
const LINKEDIN_KW = ["electrical engineering student", "hardware student", "סטודנט להנדסת חשמל", "סטודנט חומרה",
  "validation student", "VLSI student", "chip design student", "hardware intern", "NPI student", "integration student"];
const TELEGRAM_CHANNELS = ["hitechjobsisrael"];
const DRUSHIM_Q = ["סטודנט הנדסת חשמל", "סטודנט חשמל", "סטודנט אלקטרוניקה", "סטודנט חומרה", "electrical engineering student", "hardware student"];
const GREENHOUSE = [["Astera Labs", "asteralabs"], ["Samsung Semiconductor", "samsungsemiconductor"], ["HP", "hp"]];
const ISRAEL = /israel|tel aviv|haifa|herzliya|petah|jerusalem|yokneam|netanya|ra'?anana|kfar saba|rehovot|be'?er sheva|hod hasharon|caesarea|ramat gan|kiryat|\bisr\b/i;
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";

async function j(url, opts = {}) {
  const r = await fetch(url, { ...opts, headers: { "User-Agent": UA, Accept: "application/json", ...(opts.headers || {}) } });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

async function workdayFacet(env, host, site) {
  const st = env.__state;                     // facets live in the one shared state record (saves KV reads)
  if (st && st.facet[host] !== undefined) return st.facet[host];
  const tenant = host.split(".")[0];
  const d = await j(`https://${host}/wday/cxs/${tenant}/${site}/jobs`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ appliedFacets: {}, limit: 1, offset: 0, searchText: "" }),
  });
  let exact = null; const partial = {};
  const scan = (fs) => fs.forEach((f) => (f.values || []).forEach((v) => {
    if (v.values) return scan([v]);
    const desc = String(v.descriptor || "").trim().toLowerCase();
    if ((desc === "israel" || desc === "isr") && !exact) exact = [f.facetParameter, [v.id]];
    else if (/^(israel|il ?-)/.test(desc) || /,\s?isr$|israel$/.test(desc)) (partial[f.facetParameter] ||= []).push(v.id);
  }));
  scan(d.facets || []);
  const best = exact || Object.entries(partial).sort((a, b) => b[1].length - a[1].length)[0] || null;
  if (st) { st.facet[host] = best; st.dirty = true; }
  return best;
}

async function scanWorkday(env, [company, host, site]) {
  const tenant = host.split(".")[0];
  const facet = await workdayFacet(env, host, site);
  const out = [];
  for (const term of ["student", "intern"]) {     // gentle: Workday answers 429 when asked too often
    const d = await j(`https://${host}/wday/cxs/${tenant}/${site}/jobs`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ appliedFacets: facet ? { [facet[0]]: facet[1] } : {}, limit: 20, offset: 0, searchText: facet ? term : `${term} Israel` }),
    });
    for (const p of d.jobPostings || []) {
      if (!STUDENT.test(p.title)) continue;
      const url = `https://${host}/${site}${p.externalPath}`;
      out.push({ company, title: p.title, url, source: "workday", location: p.locationsText || "",
        req_id: (p.bulletFields || [""])[0], posted: p.postedOn || "", description: "", apply_url: url + "/apply",
        extra: { path: p.externalPath } });
    }
  }
  return out;
}

async function scanAmazon() {
  const out = [];
  for (const term of ["student", "intern"]) {
    const d = await j(`https://www.amazon.jobs/en/search.json?base_query=${term}&country=ISR&result_limit=50&sort=recent`);
    for (const p of d.jobs || []) {
      if (!STUDENT.test(p.title) || !/^(ISR|IL)$/i.test(p.country_code || "")) continue;
      out.push({ company: p.business_category === "annapurna" ? "Amazon Annapurna Labs" : "Amazon", title: p.title,
        url: "https://www.amazon.jobs" + p.job_path, source: "amazon", location: p.normalized_location || p.city || "",
        req_id: String(p.id_icims || p.id || ""), posted: p.posted_date || "", description: "",
        apply_url: p.url_next_step || "", extra: {} });
    }
  }
  return out;
}

async function scanGreenhouse([company, token]) {
  const d = await j(`https://boards-api.greenhouse.io/v1/boards/${token}/jobs`);
  return (d.jobs || []).filter((p) => STUDENT.test(p.title) && ISRAEL.test(p.location?.name || ""))
    .map((p) => ({ company, title: p.title, url: p.absolute_url, source: "greenhouse", location: p.location?.name || "",
      req_id: String(p.id), posted: p.updated_at || "", description: "", apply_url: p.absolute_url, extra: {} }));
}

async function scanQualcomm() {
  const d = await j("https://careers.qualcomm.com/api/pcsx/search?domain=qualcomm.com&query=intern&location=Israel&start=0");
  return (d.data?.positions || []).filter((p) => /israel/i.test((p.locations || []).join(" ")))
    .map((p) => ({ company: "Qualcomm", title: p.name, url: `https://careers.qualcomm.com/careers/job/${p.id}`,
      source: "eightfold", location: (p.locations || []).join("; "), req_id: String(p.displayJobId || p.id),
      posted: "", description: "", apply_url: `https://careers.qualcomm.com/careers/job/${p.id}`, extra: { pid: p.id } }));
}

async function text(url) {
  const r = await fetch(url, { headers: { "User-Agent": UA, "Accept-Language": "he,en;q=0.8" } });
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.text();
}
const unesc = (s) => String(s || "").replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;|&#x27;/g, "'")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&lrm;|‎/g, "").replace(/\s+/g, " ").trim();

async function scanLinkedIn() {
  const out = [];
  for (const kw of LINKEDIN_KW) {
    const h = await text(`https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=${encodeURIComponent(kw)}&geoId=101620260&location=Israel&f_TPR=r86400&sortBy=DD&start=0`);
    for (const c of h.split("<li>").slice(1)) {
      const id = (c.match(/urn:li:jobPosting:(\d+)/) || [])[1];
      if (!id) continue;
      const title = unesc((c.match(/base-search-card__title[^>]*>([\s\S]*?)<\/h3>/) || [])[1]);
      if (!STUDENT.test(title)) continue;
      const url = `https://www.linkedin.com/jobs/view/${id}/`;
      out.push({ company: unesc((c.match(/base-search-card__subtitle[^>]*>([\s\S]*?)<\/h4>/) || [])[1]) || "LinkedIn",
        title, url, source: "linkedin", location: unesc((c.match(/job-search-card__location[^>]*>([\s\S]*?)<\/span>/) || [])[1]),
        req_id: id, posted: "", description: "", apply_url: url, extra: { linkedin_id: id, via: "linkedin" } });
    }
  }
  return out;
}

async function scanTelegram() {
  const out = [];
  for (const ch of TELEGRAM_CHANNELS) {
    const h = await text(`https://t.me/s/${ch}`);
    for (const body of h.split('class="tgme_widget_message_text').slice(1)) {
      const post = body.split("</div>")[0];
      const re = /<a href="(https?:\/\/[^"]+)"[^>]*>([\s\S]*?)<\/a>([\s\S]*?)(?=<a href="https?:\/\/|$)/g;
      let m;
      while ((m = re.exec(post))) {
        const title = unesc(m[2]);
        if (!STUDENT.test(title)) continue;
        const rest = unesc(m[3]);
        const company = ((rest.match(/🏢\s*([^·📍🧑🔹]+)/u) || [])[1] || "").trim();
        const location = ((rest.match(/📍\s*([^·🧑🔹]+)/u) || [])[1] || "").trim();
        out.push({ company, title, url: m[1], source: "telegram", location, req_id: m[1].replace(/\W+/g, "").slice(-40),
          posted: "", description: "", apply_url: m[1], extra: { channel: ch } });
      }
    }
  }
  return out;
}

async function scanDrushim() {
  const out = [], seen = new Set();
  for (const q of DRUSHIM_Q) {
    const h = await text(`https://www.drushim.co.il/jobs/search/${encodeURIComponent(q)}/`);
    const re = /aria-label="שמור משרה ([\s\S]*?) למועדפים"[\s\S]*?href="(\/job\/(\d+)\/[0-9a-f]+\/)"/g;
    let m;
    while ((m = re.exec(h))) {
      const title = unesc(m[1]);
      if (seen.has(m[3]) || !/סטודנט|student|intern/i.test(title)) continue;
      seen.add(m[3]);
      const url = "https://www.drushim.co.il" + m[2];
      out.push({ company: "", title, url, source: "drushim", location: "", req_id: `drushim-${m[3]}`, posted: "",
        description: "", apply_url: url, extra: {} });
    }
  }
  return out;
}

async function scanLever([company, token, host]) {
  const d = await j(`https://${host}/v0/postings/${token}?mode=json`);
  return (d || []).filter((p) => STUDENT.test(p.text || "") &&
      (ISRAEL.test(p.categories?.location || "") || /^IL$/i.test(p.country || "")))
    .map((p) => ({ company, title: p.text, url: p.hostedUrl, source: "lever", location: p.categories?.location || "",
      req_id: p.id, posted: "", description: "", apply_url: p.applyUrl || p.hostedUrl, extra: {} }));
}

async function scanComeet([company, slug, uid]) {
  const h = await text(`https://www.comeet.com/jobs/${slug}/${uid}`);
  const m = h.match(/COMPANY_POSITIONS_DATA\s*=\s*(\[[\s\S]*?\]);\s*\n/);
  if (!m) return [];
  return JSON.parse(m[1]).filter((p) => STUDENT.test(p.name || "") &&
      (/^(IL|ISRAEL)$/i.test(p.location?.country || "") || ISRAEL.test(`${p.location?.name || ""} ${p.location?.city || ""}`)))
    .map((p) => {
      const url = p.url_comeet_hosted_page || p.url_active_page || "";
      return { company, title: p.name, url, source: "comeet", location: [p.location?.city, p.location?.country].filter(Boolean).join(", "),
        req_id: p.uid || "", posted: p.time_updated || "", description: "", apply_url: url, extra: {} };
    });
}

async function dispatch(env, mode, payload) {
  const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/process.yml/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
      "User-Agent": "chiphunter-worker", "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: { mode, payload: String(payload).slice(0, 60000) } }),
  });
  if (!r.ok) throw new Error(`GitHub dispatch ${r.status}: ${await r.text()}`);
}

async function tg(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
}

async function transcribe(env, fileId) {
  // Telegram voice (OGG/Opus) -> text with Cloudflare Workers AI Whisper (free tier). Needs an "AI" binding.
  const info = await (await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getFile?file_id=${fileId}`)).json();
  const buf = new Uint8Array(await (await fetch(`https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${info.result.file_path}`)).arrayBuffer());
  let bin = "";
  for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  try {
    const r = await env.AI.run("@cf/openai/whisper-large-v3-turbo", { audio: btoa(bin), language: "he" });
    if (r?.text) return r.text.trim();
  } catch (e) { console.log("turbo failed", e.message); }
  const r2 = await env.AI.run("@cf/openai/whisper", { audio: [...buf] });
  return (r2?.text || "").trim();
}

// GitHub's own hourly schedule is unreliable (it ran every ~5h), so this Worker — which runs exactly every 2 minutes —
// starts the slower jobs: full scan every hour (LinkedIn, Elbit, Apple, Google...), daily checks, Saturday-night summary.
async function clock(env) {
  const now = new Date();
  if (now.getUTCMinutes() >= 2) return;                       // once per hour (cron runs at :00 and :02)
  const key = `clock:${now.toISOString().slice(0, 13)}`;
  if (await env.SEEN.get(key)) return;
  await env.SEEN.put(key, "1", { expirationTtl: 7200 });
  // full scan (Apple, Google, Elbit + backup) every 3 hours — the Worker itself checks the rest every few minutes.
  // (GitHub's free minutes are limited: 2,000 a month.)
  if (now.getUTCHours() % 3 === 0) await dispatch(env, "scan", "-");
  if (now.getUTCHours() === 6) await dispatch(env, "remind", "-");
  // Saturday 21:00 Israel time = 18:00 UTC (summer) / 19:00 UTC (winter); the summary itself checks Israel time and
  // makes sure it's sent only once.
  if (now.getUTCDay() === 6 && (now.getUTCHours() === 18 || now.getUTCHours() === 19)) await dispatch(env, "weekly", "-");
}

async function watch(env) {
  const saved = (await env.SEEN.get("watch:v1", "json")) || {};
  env.__state = { seen: saved.seen || {}, facet: saved.facet || {}, dirty: false };
  const turn = Math.floor(new Date().getUTCMinutes() / 2) % 3;
  const named = (name, p) => p.then((jobs) => ({ name, jobs }));
  const big = Math.floor(new Date().getUTCMinutes() / 2) % 2 === 0;   // the 4 big Workday sites: every 4 minutes
  const tasks = [...(big ? WORKDAY.map((w) => named(`wd:${w[0]}`, scanWorkday(env, w))) : []), named("amazon", scanAmazon()),
    ...GREENHOUSE.map((g) => named(`gh:${g[0]}`, scanGreenhouse(g))), named("qualcomm", scanQualcomm())];
  if (turn === 0) tasks.push(named("linkedin", scanLinkedIn()), named("telegram", scanTelegram()), named("drushim", scanDrushim()));
  if (turn === 1) tasks.push(named("lv:Mobileye", scanLever(["Mobileye", "mobileye", "api.eu.lever.co"])), ...WORKDAY_B.map((w) => named(`wd:${w[0]}`, scanWorkday(env, w))), ...COMEET_B.map((c) => named(`cm:${c[0]}`, scanComeet(c))));
  if (turn === 2) tasks.push(...WORKDAY_C.map((w) => named(`wd:${w[0]}`, scanWorkday(env, w))), ...COMEET_C.map((c) => named(`cm:${c[0]}`, scanComeet(c))));
  const results = await Promise.allSettled(tasks);
  // ONE KV record for everything the watcher remembers: 1 read per run, 1 write only when something is new.
  // (Cloudflare's free KV allows ~1,000 writes and 100,000 reads a day.)
  const st = env.__state;
  const fresh = [];
  for (const r of results) {
    if (r.status !== "fulfilled") { console.log("source failed:", r.reason?.message); continue; }
    const { name, jobs } = r.value;
    const known = st.seen[name];
    const ids = new Set(known || []);
    const isNew = jobs.filter((job) => !ids.has(String(job.req_id || job.url)));
    if (!isNew.length && known) continue;
    isNew.forEach((job) => ids.add(String(job.req_id || job.url)));
    st.seen[name] = [...ids].slice(-800);
    st.dirty = true;
    if (known) fresh.push(...isNew);         // first time a source is seen: remember silently (the hourly scan sent those)
  }
  if (st.dirty) await env.SEEN.put("watch:v1", JSON.stringify({ seen: st.seen, facet: st.facet }));
  if (fresh.length) await dispatch(env, "jobs", JSON.stringify(fresh));
  return fresh.length;
}

const CHANNEL_HE = { direct: "🌐 באתר החברה", linkedin_apply: "💼 הגשה בלינקדאין", friend: "🤝 דרך מכר",
  linkedin_person: "👤 איש קשר בלינקדאין" };

// Buttons are answered here, instantly; GitHub only records the result in the background.
async function onButton(env, cb) {
  const chat = cb.message.chat.id, msgId = cb.message.message_id, data = cb.data || "";
  const urlRow = (cb.message.reply_markup?.inline_keyboard || []).filter((r) => r.some((b) => b.url));
  const setButtons = (rows) => tg(env, "editMessageReplyMarkup", { chat_id: chat, message_id: msgId, reply_markup: { inline_keyboard: rows } });
  if (data === "noop") return tg(env, "answerCallbackQuery", { callback_query_id: cb.id });

  const [action, a2, a3] = data.split(":");
  // a button in the "you haven't answered these yet" list: change only that job's row, keep the rest of the list
  if ((action === "applied" || action === "skip") && a3 === "L") {
    const uid = a2, rows = cb.message.reply_markup?.inline_keyboard || [];
    const i = rows.findIndex((r) => r.some((b) => (b.callback_data || "").includes(uid)));
    const label = (rows[i]?.[0]?.text || "").replace(/^✅\s*/, "").replace(/\s*הגשתי$/, "");
    if (i >= 0) rows[i] = [{ text: `${action === "applied" ? "✅ הוגש" : "⏭️ דילגת"} ${label}`.trim(), callback_data: "noop" }];
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: action === "applied" ? "נרשם: הוגש ✅" : "נרשם: דילגת" });
    await setButtons(rows);
    if (action === "applied") {
      await tg(env, "sendMessage", { chat_id: chat, reply_to_message_id: msgId, text: "איך הגשת?",
        reply_markup: { inline_keyboard: [
          [{ text: CHANNEL_HE.direct, callback_data: `channel:direct:${uid}` }, { text: CHANNEL_HE.linkedin_apply, callback_data: `channel:linkedin_apply:${uid}` }],
          [{ text: CHANNEL_HE.friend, callback_data: `channel:friend:${uid}` }, { text: CHANNEL_HE.linkedin_person, callback_data: `channel:linkedin_person:${uid}` }]] } });
    }
    await dispatch(env, "status", data);
    return;
  }
  if (action === "applied" || action === "skip") {
    const uid = a2;
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: action === "applied" ? "נרשם: הוגש ✅" : "נרשם: דילגת" });
    if (action === "skip") {        // ask why (optional) — the bot learns from it
      await setButtons([...urlRow,
        [{ text: "🔧 התחום", callback_data: `why:field:${uid}` }, { text: "📍 רחוק", callback_data: `why:far:${uid}` },
         { text: "🏢 החברה", callback_data: `why:company:${uid}` }],
        [{ text: "📋 הדרישות", callback_data: `why:reqs:${uid}` }, { text: "⏳ לא עכשיו", callback_data: `why:later:${uid}` },
         { text: "↩️ ביטול", callback_data: `undo:${uid}` }]]);
    } else {
      await setButtons([...urlRow, [{ text: "✅ הוגש", callback_data: "noop" }, { text: "↩️ ביטול", callback_data: `undo:${uid}` }]]);
    }
    if (action === "applied") {
      await tg(env, "sendMessage", { chat_id: chat, reply_to_message_id: msgId, text: "איך הגשת?",
        reply_markup: { inline_keyboard: [
          [{ text: CHANNEL_HE.direct, callback_data: `channel:direct:${uid}` }, { text: CHANNEL_HE.linkedin_apply, callback_data: `channel:linkedin_apply:${uid}` }],
          [{ text: CHANNEL_HE.friend, callback_data: `channel:friend:${uid}` }, { text: CHANNEL_HE.linkedin_person, callback_data: `channel:linkedin_person:${uid}` }]] } });
    }
    await dispatch(env, "status", data);
    return;
  }
  if (action === "why") {
    const WHY_HE = { field: "🔧 התחום לא מעניין", far: "📍 רחוק מדי", company: "🏢 החברה לא מעניינת",
      reqs: "📋 הדרישות לא מתאימות", later: "⏳ לא עכשיו" };
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "תודה, אני לומד מזה 🙏" });
    await setButtons([...urlRow, [{ text: `⏭️ דילגת · ${WHY_HE[a2] || a2}`, callback_data: "noop" },
                                  { text: "↩️ ביטול", callback_data: `undo:${a3}` }]]);
    await dispatch(env, "status", data);
    return;
  }
  if (action === "undo") {
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "בוטל ↩️" });
    await setButtons([...urlRow, [{ text: "✅ הגשתי", callback_data: `applied:${a2}` }, { text: "⏭️ דלג", callback_data: `skip:${a2}` }]]);
    await dispatch(env, "status", data);
    return;
  }
  if (action === "channel") {
    const channel = a2, uid = a3;
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "✔️ נרשם" });
    await setButtons([[{ text: `✔️ ${CHANNEL_HE[channel] || channel}`, callback_data: "noop" }]]);
    if (channel === "friend" || channel === "linkedin_person") {
      await tg(env, "sendMessage", { chat_id: chat, text: `✍️ מה השם? (לא חובה — אפשר להתעלם)\n[ref:${uid}]`,
        reply_markup: { force_reply: true, input_field_placeholder: "למשל: תומר" } });
    }
    await dispatch(env, "status", data);
    return;
  }
  await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "✔️" });
  await dispatch(env, "status", data);
}

// If Maor writes "הגשתי" while a job he just sent is still being analyzed, tell the chat step to wait for it.
async function chatPayload(env, text) {
  const t = await env.SEEN.get("pending_job");
  return t && Date.now() - Number(t) < 6 * 60 * 1000 ? `[pending:${t}] ${text}` : text;
}

async function onTelegram(env, update, ctx) {
  // Telegram re-sends an update if we answered slowly (or during a redeploy) -> handle each update only once
  if (update.update_id) {
    const k = `upd:${update.update_id}`;
    if (await env.SEEN.get(k)) return;
    await env.SEEN.put(k, "1", { expirationTtl: 3600 });
  }
  const cb = update.callback_query;
  if (cb) {
    if (String(cb.message?.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return;
    return onButton(env, cb);
  }
  const m = update.message;
  if (!m) return;
  const chat = String(m.chat.id);
  if (m.text === "/start" || m.text === "/id") {
    return tg(env, "sendMessage", { chat_id: chat, text: `ה-chat id שלך: ${chat}` });
  }
  if (chat !== String(env.TELEGRAM_CHAT_ID)) return;           // only Maor
  const reply = (text) => tg(env, "sendMessage", { chat_id: chat, text, reply_to_message_id: m.message_id });
  const text = m.text || m.caption || "";

  if (m.document && /zip/i.test((m.document.mime_type || "") + (m.document.file_name || ""))) {
    await dispatch(env, "library", m.document.file_id);
    return reply("⏳ מעלה את ספריית הקו\"ח ל-Drive (כמה דקות)...");
  }
  if (m.document && /pdf/i.test(m.document.mime_type || "") && /גליון|גיליון|transcript/i.test(text)) {
    const lang = /עבר|he/i.test(text) ? "he" : "en";
    await dispatch(env, "transcript", `${lang} ${m.document.file_id}`);
    return reply("⏳ שומר את גליון הציונים...");
  }
  if (m.voice || m.audio) {
    if (!env.AI) return reply("כדי להבין הקלטות צריך להוסיף ל-Worker חיבור AI (Bindings → Workers AI, שם: AI).");
    let said = "";
    try { said = await transcribe(env, (m.voice || m.audio).file_id); } catch (e) { console.log("stt", e.message); }
    if (!said) return reply("לא הצלחתי לשמוע את ההקלטה 🙉 נסה שוב או כתוב לי.");
    await dispatch(env, "chat", await chatPayload(env, said));
    return reply(`🎙️ שמעתי: "${said.slice(0, 300)}"\n⏳ חושב...`);
  }
  if (m.photo?.length) {
    // Screenshots go to the same "brain" as text. Several photos sent together (an album) = ONE message.
    const fid = m.photo[m.photo.length - 1].file_id, cap = m.caption || "";
    await env.SEEN.put("pending_job", String(Date.now()), { expirationTtl: 600 });
    if (!m.media_group_id) {
      await dispatch(env, "chat", `[images:${fid}] ${cap}`);
      return reply("👀 קיבלתי, מסתכל...");
    }
    const prefix = `album:${m.media_group_id}:`;
    await env.SEEN.put(prefix + String(m.message_id).padStart(12, "0"), JSON.stringify({ fid, cap }), { expirationTtl: 300 });
    const work = (async () => {
      await new Promise((r) => setTimeout(r, 4000));          // wait for the rest of the album
      const keys = (await env.SEEN.list({ prefix })).keys.map((k) => k.name).sort();
      const mine = prefix + String(m.message_id).padStart(12, "0");
      if (keys.length && keys[0] !== mine) return;             // the first photo of the album sends for everyone
      const items = [];
      for (const k of keys.length ? keys : [mine]) { const v = await env.SEEN.get(k); if (v) items.push(JSON.parse(v)); }
      if (!items.some((x) => x.fid === fid)) items.push({ fid, cap });
      const caption = items.map((x) => x.cap).filter(Boolean).join(" ");
      await dispatch(env, "chat", `[images:${items.map((x) => x.fid).join(",")}] ${caption}`);
      await tg(env, "sendMessage", { chat_id: chat, text: items.length > 1 ? `👀 קיבלתי ${items.length} תמונות, מסתכל...` : "👀 קיבלתי, מסתכל..." });
    })();
    ctx ? ctx.waitUntil(work) : await work;
    return;
  }
  const asked = m.reply_to_message?.text || "";
  const refm = asked.match(/\[ref:([0-9a-f]+)\]/) || (asked.includes("מה השם של") && asked.match(/#(\d+)/));
  if (refm && text) {
    await dispatch(env, "referrer", `${refm[1]} ${text}`);
    return reply(`✔️ נשמר: ${text}`);
  }
  if (/^\/list/.test(text) || /^\/status\s*$/.test(text)) {
    await dispatch(env, "list", "-");
    return reply("⏳ מביא את רשימת ההגשות...");
  }
  if (/^\/status\s+\d+\s+\w+/.test(text)) {
    const [, id, st] = text.match(/^\/status\s+(\d+)\s+(\w+)/);
    await dispatch(env, "status", `${st}:${id}`);
    return reply("⏳ מעדכן...");
  }
  if ((/(קיבלתי|יש לי|נקבע לי|זימנו אותי ל)\s*ראיון|^\/interview/i.test(text)) && text.length < 300) {
    await dispatch(env, "interview", text.replace(/^\/interview\s*/i, ""));
    return reply("🎯 מעולה! מתחיל להכין אותך לראיון — שאלות אמיתיות מהחברה + חוברת הכנה (5-10 דקות)...");
  }
  if (/^\/scan/.test(text)) { await dispatch(env, "scan", "manual"); return reply("⏳ סורק את כל המקורות (2-5 דקות)... אעדכן כשאסיים."); }
  const url = text.match(/https?:\/\/\S+/);
  if (url && text.trim().length < url[0].length + 40) {
    await dispatch(env, "url", url[0]);
    await env.SEEN.put("pending_job", String(Date.now()), { expirationTtl: 600 });
    return reply("⏳ קיבלתי קישור, בודק את המשרה (1-3 דקות)...");
  }
  const looksLikeJob = text.length > 400 || /דרישות|תיאור המשרה|requirements|responsibilities|qualifications|what you'll be doing/i.test(text);
  if (text && !looksLikeJob && !text.startsWith("/")) {
    await dispatch(env, "chat", await chatPayload(env, text));
    return reply("⏳ חושב...");
  }
  if (looksLikeJob || text.length > 80) {
    await dispatch(env, "text", text);
    await env.SEEN.put("pending_job", String(Date.now()), { expirationTtl: 600 });
    return reply("⏳ קיבלתי את טקסט המשרה, מנתח (1-3 דקות)...");
  }
  return reply("שלח לי קישור למשרה, טקסט של משרה, או צילום מסך.\nפקודות: /list הגשות פעילות · /scan סריקה עכשיו · /status <מספר> interview|rejected|offer\nקיבלת ראיון? כתוב: קיבלתי ראיון ל-<חברה> ואכין אותך.\nאפשר גם פשוט לדבר איתי — בכתב או בהקלטה 🎙️\nגליון ציונים: שלח PDF עם הכיתוב 'גליון אנגלית' או 'גליון עברית'.");
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(watch(env));
    ctx.waitUntil(clock(env));
  },
  async fetch(req, env, ctx) {
    const u = new URL(req.url);
    if (req.method === "POST" && u.pathname === `/tg/${env.WEBHOOK_SECRET}`) {
      try { await onTelegram(env, await req.json(), ctx); } catch (e) { console.log("tg error", e.message); }
      return new Response("ok");
    }
    if (u.pathname === "/health") return new Response("ok");
    if (u.pathname === `/watch/${env.WEBHOOK_SECRET}`) return new Response(`new jobs: ${await watch(env)}`);
    return new Response("not found", { status: 404 });
  },
};
