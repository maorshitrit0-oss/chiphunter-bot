// node tools/worker_probe.mjs — runs the Worker's watcher with an in-memory KV and counts KV operations.
import fs from "fs";
const src = fs.readFileSync(new URL("../worker/index.js", import.meta.url), "utf8") + "\nexport { watch };\n";
fs.writeFileSync("/tmp/w.mjs", src);
const W = await import("/tmp/w.mjs");
const mem = new Map(); let reads = 0, writes = 0;
const env = { SEEN: { get: async (k, t) => { reads++; const v = mem.get(k); return v && t === "json" ? JSON.parse(v) : v ?? null; },
                      put: async (k, v) => { writes++; mem.set(k, v); } }, GITHUB_REPO: "x/y", GITHUB_TOKEN: "none" };
for (let i = 0; i < 3; i++) {
  try { console.log("WP run", i, "fresh:", await W.watch(env)); } catch (e) { console.log("WP run", i, "dispatch:", e.message.slice(0, 60)); }
  console.log("WP   kv reads", reads, "writes", writes, "state bytes", (mem.get("watch:v1") || "").length,
              "sources", Object.keys(JSON.parse(mem.get("watch:v1") || '{"seen":{}}').seen).length);
}
