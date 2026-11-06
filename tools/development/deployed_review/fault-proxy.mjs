// A transport fault fixture, never an API-response substitute: forwards one
// review, records its actual upstream status, then discards the client response.
import http from "node:http";
let decisions = 0;
const server = http.createServer((request, response) => {
  if (request.method !== "POST" || !request.url.endsWith("/review") || ++decisions !== 1) {
    response.writeHead(409).end();
    return;
  }
  const upstream = http.request({hostname: "api", port: 8400, path: request.url,
    method: request.method, headers: request.headers}, (reply) => {
    reply.resume();
    reply.on("end", () => {
      console.log(JSON.stringify({event: "upstream-response-discarded", status: reply.statusCode, decisions}));
      response.destroy();
    });
  });
  upstream.on("error", () => { console.log(JSON.stringify({event: "upstream-error", decisions})); response.destroy(); });
  request.pipe(upstream);
});
server.listen(8081, "0.0.0.0", () => console.log(JSON.stringify({event: "ready"})));
process.on("SIGTERM", () => server.close());
