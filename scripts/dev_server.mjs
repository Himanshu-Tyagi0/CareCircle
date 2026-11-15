/**
 * Development preview proxy & Streamlit launcher on port 3000 for AI Studio.
 * - Sets Referrer-Policy: no-referrer and Cache-Control: no-store
 * - Redacts ?token= / ?action_token= / ?verify= / ?invite= from server logs
 * - Runs Streamlit in explicit isolated Demo Mode only when SUPABASE_URL is unset in dev
 */
import http from 'node:http';
import net from 'node:net';
import { spawn } from 'node:child_process';

const LISTEN_PORT = Number(process.env.PORT || 3000);
const STREAMLIT_PORT = 8501;

function redactUrl(rawUrl = '/') {
  return rawUrl.replace(/([?&](?:action_token|token|verify|invite)=)[^&\s]+/gi, '$1[REDACTED]');
}

const env = { ...process.env };
if (!env.CARECIRCLE_DEMO_MODE && !env.SUPABASE_URL) {
  env.CARECIRCLE_DEMO_MODE = 'true';
  env.CARECIRCLE_ENV = 'development';
}

const pythonBin = process.env.PYTHON_BIN || '.venv/bin/python';
const stProc = spawn(
  pythonBin,
  [
    '-m',
    'streamlit',
    'run',
    'app.py',
    '--server.port',
    String(STREAMLIT_PORT),
    '--server.address',
    '127.0.0.1',
    '--server.headless',
    'true',
    '--browser.gatherUsageStats',
    'false',
  ],
  { env, stdio: ['ignore', 'pipe', 'pipe'] },
);

stProc.stdout.on('data', (d) => process.stdout.write(`[streamlit] ${redactUrl(String(d))}`));
stProc.stderr.on('data', (d) => process.stderr.write(`[streamlit] ${redactUrl(String(d))}`));
stProc.on('exit', (code) => {
  console.error(`Streamlit exited with code ${code}`);
  process.exit(code ?? 1);
});

const server = http.createServer((req, res) => {
  const proxyReq = http.request(
    {
      hostname: '127.0.0.1',
      port: STREAMLIT_PORT,
      path: req.url,
      method: req.method,
      headers: req.headers,
    },
    (proxyRes) => {
      const headers = {
        ...proxyRes.headers,
        'referrer-policy': 'no-referrer',
        'cache-control': 'no-store, no-cache, must-revalidate, private',
        'x-content-type-options': 'nosniff',
      };
      res.writeHead(proxyRes.statusCode || 502, headers);
      proxyRes.pipe(res);
    },
  );
  proxyReq.on('error', () => {
    if (!res.headersSent) {
      res.writeHead(503, { 'content-type': 'text/plain', 'referrer-policy': 'no-referrer' });
      res.end('CareCircle starting...');
    }
  });
  req.pipe(proxyReq);
});

server.on('upgrade', (req, socket, head) => {
  const upstream = net.connect(STREAMLIT_PORT, '127.0.0.1', () => {
    let rawHeaders = `${req.method} ${req.url} HTTP/${req.httpVersion}\r\n`;
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      rawHeaders += `${req.rawHeaders[i]}: ${req.rawHeaders[i + 1]}\r\n`;
    }
    rawHeaders += '\r\n';
    upstream.write(rawHeaders);
    if (head && head.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on('error', () => socket.destroy());
  socket.on('error', () => upstream.destroy());
});

server.listen(LISTEN_PORT, '0.0.0.0', () => {
  console.log(`CareCircle proxy listening on http://0.0.0.0:${LISTEN_PORT} -> 127.0.0.1:${STREAMLIT_PORT}`);
});

for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    stProc.kill(sig);
    server.close(() => process.exit(0));
  });
}
