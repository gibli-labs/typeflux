import { pathToFileURL } from "node:url";
const [relative, ...files] = process.argv.slice(2);
const module = await import(pathToFileURL(`/environment/repository/${relative}/node_modules/vitest/dist/node.js`).href);
const context = await module.startVitest(files, {
  root: `/work/${relative}`, config: false, run: true, watch: false, cache: false,
  reporters: ["json"], outputFile: `/out/${relative.split("/").at(-1)}.json`,
}, { cacheDir: "/tmp/vite-cache", server: { fs: { allow: ["/work", "/environment/repository"] } } });
await context.close();
