// node tools/worker_probe.mjs "message" — runs the Worker's fast chat once (fake KV, Telegram calls printed, nothing sent).
import fs from "fs";
const src = fs.readFileSync(new URL("../worker/index.js", import.meta.url), "utf8") + "\nexport { fastChat };\n";
fs.writeFileSync("/tmp/w.mjs", src.replace(/^async function tg\(env, method, body\) \{/m,
  'async function tg(env, method, body) { if (env.DRY) { console.log("TG", method, JSON.stringify(body).slice(0, 700)); return { ok: true }; }'));
const W = await import("/tmp/w.mjs");
const mem = new Map();
const env = { DRY: true, GEMINI_API_KEY: process.env.GEMINI_API_KEY, GITHUB_TOKEN: process.env.DATA_REPO_TOKEN,
  SEEN: { get: async (k, t) => { const v = mem.get(k); return v && t === "json" ? JSON.parse(v) : v ?? null; }, put: async (k, v) => mem.set(k, v) } };
globalThis.dispatchLog = [];
for (const msg of process.argv.slice(2)) {
  const t0 = Date.now();
  const ok = await W.fastChat(env, "1", msg, 1).catch((e) => "ERR " + e.message);
  console.log("RESULT", ok, `${Date.now() - t0}ms`, "|", msg);
}
