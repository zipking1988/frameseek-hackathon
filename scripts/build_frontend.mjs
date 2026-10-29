import { build } from "esbuild";
import { readFile, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
await build({
  entryPoints: ["frontend/workbench.jsx"],
  bundle: true,
  minify: true,
  legalComments: "external",
  outfile: "src/frameseek/static/workbench.js",
  define: { "process.env.NODE_ENV": '"production"' },
  target: ["es2022"],
});
const base = "src/frameseek/static/";
let html = await readFile(base + "index.html", "utf8");
for (const file of [
  "app.js",
  "workspace.js",
  "demo-conversation.js",
  "investigation-canvas.js",
  "workbench-model.js",
  "investigation-report.js",
  "workbench.js",
  "styles.css",
  "workbench.css",
]) {
  const hash = createHash("sha256")
    .update(await readFile(base + file))
    .digest("hex")
    .slice(0, 12);
  html = html.replace(
    new RegExp(
      "/static/" + file.replaceAll(".", "\\.") + "(?:\\?v=[a-f0-9]+)?",
      "g",
    ),
    `/static/${file}?v=${hash}`,
  );
}
await writeFile(base + "index.html", html);

// Keep the marketing page independent of the private workspace's application bundle.
let landing = await readFile(base + "landing.html", "utf8");
for (const file of ["landing.css", "landing.js"]) {
  const hash = createHash("sha256")
    .update(await readFile(base + file))
    .digest("hex")
    .slice(0, 12);
  landing = landing.replace(
    new RegExp("/static/" + file.replaceAll(".", "\\.") + "(?:\\?v=[a-f0-9]+)?", "g"),
    `/static/${file}?v=${hash}`,
  );
}
await writeFile(base + "landing.html", landing);
