'use strict';
// FlashQuiz — local real-time quiz server. Zero dependencies (Node >= 16).
//   node server.js [--port 8080] [--ip 192.168.43.1]
// Real-time transport: Server-Sent Events (down) + small POSTs (up).
// SSE works on every phone browser, auto-reconnects after screen lock,
// and needs no WebSocket library.

const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const qr = require('./lib/qr');

const args = parseArgs(process.argv.slice(2));
const PORT = Number(args.port || process.env.PORT || 8080);
const FORCED_IP = args.ip || process.env.JOIN_IP || '';
const PACKS_DIR = path.join(__dirname, 'packs');
const PUBLIC_DIR = path.join(__dirname, 'public');
// Lets the host page be used from another device (e.g. teacher's phone).
// Loopback (the machine running the server) never needs it.
const HOST_KEY = process.env.HOST_KEY || crypto.randomBytes(3).toString('hex');

// ---------------------------------------------------------------- state ---
const game = {
  phase: 'lobby',        // lobby | question | reveal | end
  pack: null,            // { id, title, questions: [...] }
  qIndex: -1,
  qStartedAt: 0,
  qDeadline: 0,
  answers: new Map(),    // playerId -> { choice, ms }
  timer: null,
  wifi: { ssid: '', pass: '' },
  joinIp: FORCED_IP,
};
const players = new Map(); // id -> { id, name, score, results: [], streams: Set }
const hostStreams = new Set();

// ---------------------------------------------------------------- packs ---
function listPacks() {
  try {
    return fs.readdirSync(PACKS_DIR).filter((f) => f.endsWith('.json')).map((f) => {
      try {
        const p = validatePack(JSON.parse(fs.readFileSync(path.join(PACKS_DIR, f), 'utf8')));
        return { id: f, title: p.title, count: p.questions.length };
      } catch (e) {
        return { id: f, title: f, error: e.message };
      }
    });
  } catch { return []; }
}

function validatePack(p) {
  if (!p || !Array.isArray(p.questions) || !p.questions.length) throw new Error('missing "questions" array');
  const defTime = clampInt(p.time, 5, 300, 20);
  return {
    title: String(p.title || 'Untitled pack').slice(0, 120),
    speedBonus: p.speedBonus !== false,
    questions: p.questions.map((q, i) => {
      const n = `question ${i + 1}`;
      if (!q || typeof q.q !== 'string' || !q.q.trim()) throw new Error(`${n}: "q" text required`);
      if (!Array.isArray(q.choices) || q.choices.length < 2 || q.choices.length > 4)
        throw new Error(`${n}: "choices" must have 2-4 items`);
      const answer = Number(q.answer);
      if (!Number.isInteger(answer) || answer < 0 || answer >= q.choices.length)
        throw new Error(`${n}: "answer" must be a choice index (0-${q.choices.length - 1})`);
      return { q: q.q.trim(), choices: q.choices.map(String), answer, time: clampInt(q.time, 5, 300, defTime) };
    }),
  };
}

function loadPack(id) {
  const file = path.basename(String(id));
  const p = validatePack(JSON.parse(fs.readFileSync(path.join(PACKS_DIR, file), 'utf8')));
  p.id = file;
  return p;
}

// ----------------------------------------------------------- game flow ---
function currentQ() { return game.pack && game.pack.questions[game.qIndex]; }

function startQuestion(index) {
  const q = game.pack.questions[index];
  if (!q) return endGame();
  clearTimeout(game.timer);
  game.phase = 'question';
  game.qIndex = index;
  game.answers = new Map();
  game.qStartedAt = Date.now();
  game.qDeadline = game.qStartedAt + q.time * 1000;
  game.timer = setTimeout(reveal, q.time * 1000 + 300); // small grace for network lag
  broadcastAll();
}

function reveal() {
  if (game.phase !== 'question') return;
  clearTimeout(game.timer);
  const q = currentQ();
  for (const p of players.values()) {
    const a = game.answers.get(p.id);
    const correct = !!a && a.choice === q.answer;
    let pts = 0;
    if (correct) {
      pts = 500;
      if (game.pack.speedBonus) pts += Math.round(500 * Math.max(0, 1 - a.ms / (q.time * 1000)));
    }
    p.score += pts;
    p.results[game.qIndex] = { choice: a ? a.choice : null, ms: a ? a.ms : null, correct, pts };
  }
  game.phase = 'reveal';
  broadcastAll();
}

function endGame() {
  clearTimeout(game.timer);
  game.phase = 'end';
  broadcastAll();
}

function resetGame(keepPlayers) {
  clearTimeout(game.timer);
  game.phase = 'lobby';
  game.qIndex = -1;
  game.answers = new Map();
  if (keepPlayers) for (const p of players.values()) { p.score = 0; p.results = []; }
  else {
    for (const p of players.values()) send(p.streams, { type: 'kicked' });
    players.clear();
  }
  broadcastAll();
}

function connectedCount() {
  let n = 0;
  for (const p of players.values()) if (p.streams.size) n++;
  return n;
}

function leaderboard() {
  return [...players.values()]
    .sort((a, b) => b.score - a.score)
    .map((p, i) => ({ rank: i + 1, name: p.name, score: p.score, correct: p.results.filter((r) => r && r.correct).length }));
}

// ------------------------------------------------------------ snapshots ---
function publicQuestion(withAnswer) {
  const q = currentQ();
  if (!q) return null;
  return {
    index: game.qIndex,
    total: game.pack.questions.length,
    text: q.q,
    choices: q.choices,
    time: q.time,
    remainingMs: Math.max(0, game.qDeadline - Date.now()),
    answer: withAnswer ? q.answer : undefined,
  };
}

function hostSnapshot() {
  const q = currentQ();
  const counts = q ? q.choices.map(() => 0) : [];
  for (const a of game.answers.values()) counts[a.choice]++;
  return {
    type: 'state',
    phase: game.phase,
    pack: game.pack && { id: game.pack.id, title: game.pack.title, count: game.pack.questions.length },
    question: publicQuestion(game.phase !== 'question'),
    answered: game.answers.size,
    counts,
    players: [...players.values()].map((p) => ({
      id: p.id, name: p.name, score: p.score, online: p.streams.size > 0, answered: game.answers.has(p.id),
    })),
    online: connectedCount(),
    leaderboard: game.phase === 'reveal' || game.phase === 'end' ? leaderboard().slice(0, 10) : [],
    join: joinInfo(),
  };
}

function playerSnapshot(p) {
  const a = game.answers.get(p.id);
  const res = p.results[game.qIndex];
  let rank = 0;
  if (game.phase === 'reveal' || game.phase === 'end') rank = leaderboard().findIndex((r) => r.name === p.name) + 1;
  return {
    type: 'state',
    phase: game.phase,
    packTitle: game.pack ? game.pack.title : '',
    question: publicQuestion(game.phase === 'reveal'),
    you: {
      name: p.name, score: p.score, rank, players: players.size,
      answered: a ? a.choice : null,
      result: game.phase === 'reveal' && res ? res : null,
    },
  };
}

// ------------------------------------------------------------------ SSE ---
function send(streams, msg) {
  const data = `data: ${JSON.stringify(msg)}\n\n`;
  for (const res of streams) res.write(data);
}

let hostPending = null;
function broadcastHost() {
  // Coalesce bursts (30 students answering in the same second) into one push.
  if (hostPending) return;
  hostPending = setTimeout(() => { hostPending = null; send(hostStreams, hostSnapshot()); }, 120);
}

function broadcastAll() {
  for (const p of players.values()) send(p.streams, playerSnapshot(p));
  broadcastHost();
}

function openStream(req, res) {
  req.socket.setTimeout(0);
  req.socket.setNoDelay(true);
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache, no-transform',
    Connection: 'keep-alive',
  });
  res.write('retry: 1500\n\n');
}

setInterval(() => {
  // Heartbeat keeps phone hotspots / power-saving radios from dropping idle streams.
  const ping = ': ping\n\n';
  for (const r of hostStreams) r.write(ping);
  for (const p of players.values()) for (const r of p.streams) r.write(ping);
}, 15000).unref();

// -------------------------------------------------------------- network ---
function lanAddresses() {
  let ifaces = {};
  try { ifaces = os.networkInterfaces(); } catch { /* Android/Termux may forbid this */ }
  const out = [];
  for (const [name, list] of Object.entries(ifaces)) {
    for (const a of list || []) {
      if (a.family !== 'IPv4' && a.family !== 4) continue;
      if (a.internal || a.address.startsWith('169.254.')) continue;
      let score = 0;
      if (/^(wl|wlan|wifi|ap|swlan|en|eth)/i.test(name)) score += 2;
      if (/^(docker|br-|veth|virbr|vmnet|vbox|tun|tap|tailscale|zt|utun|lxc)/i.test(name)) score -= 5;
      if (a.address.startsWith('192.168.')) score += 2;
      else if (a.address.startsWith('10.') || /^172\.(1[6-9]|2\d|3[01])\./.test(a.address)) score += 1;
      out.push({ ip: a.address, iface: name, score });
    }
  }
  return out.sort((a, b) => b.score - a.score);
}

function joinInfo() {
  const addrs = lanAddresses();
  const ip = game.joinIp || (addrs[0] && addrs[0].ip) || '';
  const url = ip ? `http://${ip}${PORT === 80 ? '' : ':' + PORT}/` : '';
  return {
    ip, url, port: PORT, addresses: addrs.map((a) => ({ ip: a.ip, iface: a.iface })),
    wifi: game.wifi.ssid ? { ssid: game.wifi.ssid, v: game.wifi.v } : null,
  };
}

function wifiQrText() {
  const esc = (s) => s.replace(/([\\;,:"])/g, '\\$1');
  const { ssid, pass } = game.wifi;
  return pass ? `WIFI:T:WPA;S:${esc(ssid)};P:${esc(pass)};;` : `WIFI:T:nopass;S:${esc(ssid)};;`;
}

// --------------------------------------------------------------- http ---
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json' };

function isHost(req, url) {
  const ra = req.socket.remoteAddress || '';
  if (ra === '127.0.0.1' || ra === '::1' || ra === '::ffff:127.0.0.1') return true;
  return url.searchParams.get('key') === HOST_KEY || req.headers['x-host-key'] === HOST_KEY;
}

function json(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(obj));
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > 256 * 1024) { reject(new Error('body too large')); req.destroy(); }
      else chunks.push(c);
    });
    req.on('end', () => {
      try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {}); }
      catch { reject(new Error('invalid JSON')); }
    });
    req.on('error', reject);
  });
}

function serveStatic(res, file) {
  const fp = path.join(PUBLIC_DIR, file);
  if (!fp.startsWith(PUBLIC_DIR)) return json(res, 404, { error: 'not found' });
  fs.readFile(fp, (err, buf) => {
    if (err) return json(res, 404, { error: 'not found' });
    res.writeHead(200, { 'Content-Type': MIME[path.extname(fp)] || 'application/octet-stream', 'Cache-Control': 'no-cache' });
    res.end(buf);
  });
}

function uniqueName(raw) {
  let name = String(raw || '').replace(/[\u0000-\u001f<>]/g, '').trim().slice(0, 20);
  if (!name) name = 'Player';
  const taken = new Set([...players.values()].map((p) => p.name.toLowerCase()));
  if (!taken.has(name.toLowerCase())) return name;
  for (let i = 2; ; i++) if (!taken.has(`${name} ${i}`.toLowerCase())) return `${name} ${i}`;
}

const hostActions = {
  pack: (b) => { if (game.phase !== 'lobby') resetGame(true); game.pack = loadPack(b.id); },
  upload: (b) => {
    if (game.phase !== 'lobby') resetGame(true);
    game.pack = validatePack(b.pack);
    game.pack.id = '(uploaded) ' + String(b.name || 'pack.json').slice(0, 60);
  },
  start: () => { if (!game.pack) throw new Error('load a question pack first'); resetGame(true); startQuestion(0); },
  reveal: () => reveal(),
  next: () => {
    if (!game.pack) throw new Error('load a question pack first');
    if (game.phase === 'question') return reveal();
    if (game.phase === 'lobby') return startQuestion(0);
    if (game.qIndex + 1 >= game.pack.questions.length) return endGame();
    startQuestion(game.qIndex + 1);
  },
  end: () => endGame(),
  reset: (b) => resetGame(!!b.keepPlayers),
  kick: (b) => {
    const p = players.get(b.id);
    if (p) { send(p.streams, { type: 'kicked' }); for (const r of p.streams) r.end(); players.delete(b.id); }
  },
  wifi: (b) => { game.wifi = { ssid: String(b.ssid || '').slice(0, 32), pass: String(b.pass || '').slice(0, 63), v: Date.now() }; },
  joinip: (b) => { game.joinIp = String(b.ip || '').replace(/[^0-9a-zA-Z.:\-]/g, '').slice(0, 64); },
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  const p = url.pathname;
  try {
    // ---- pages
    if (req.method === 'GET' && (p === '/' || p === '/play')) return serveStatic(res, 'play.html');
    if (req.method === 'GET' && p === '/host') {
      if (!isHost(req, url)) return json(res, 403, { error: 'host key required: /host?key=…' });
      return serveStatic(res, 'host.html');
    }
    if (req.method === 'GET' && /^\/static\/[\w.-]+$/.test(p)) return serveStatic(res, p.slice(8));

    // ---- QR codes (SVG, generated locally)
    if (req.method === 'GET' && p === '/qr/join.svg') {
      const text = url.searchParams.get('url') || joinInfo().url || `http://localhost:${PORT}/`;
      res.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Cache-Control': url.searchParams.has('url') ? 'max-age=86400' : 'no-store' });
      return res.end(qr.toSvg(text));
    }
    if (req.method === 'GET' && p === '/qr/wifi.svg') {
      if (!isHost(req, url) || !game.wifi.ssid) return json(res, 404, { error: 'no wifi set' });
      res.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'private, max-age=86400' });
      return res.end(qr.toSvg(wifiQrText()));
    }

    // ---- player API
    if (req.method === 'POST' && p === '/api/join') {
      const b = await readBody(req);
      let pl = b.id && players.get(b.id);
      if (!pl) {
        pl = { id: crypto.randomBytes(8).toString('hex'), name: uniqueName(b.name), score: 0, results: [], streams: new Set() };
        players.set(pl.id, pl);
        broadcastHost();
      }
      return json(res, 200, { id: pl.id, name: pl.name });
    }
    if (req.method === 'GET' && p === '/events') {
      const id = url.searchParams.get('id');
      const pl = players.get(id);
      if (!pl) { openStream(req, res); res.end(`data: ${JSON.stringify({ type: 'unknown' })}\n\n`); return; }
      openStream(req, res);
      pl.streams.add(res);
      res.write(`data: ${JSON.stringify(playerSnapshot(pl))}\n\n`);
      broadcastHost();
      req.on('close', () => { pl.streams.delete(res); broadcastHost(); });
      return;
    }
    if (req.method === 'POST' && p === '/api/answer') {
      const b = await readBody(req);
      const pl = players.get(b.id);
      const q = currentQ();
      if (!pl) return json(res, 404, { error: 'unknown player' });
      if (game.phase !== 'question' || b.index !== game.qIndex) return json(res, 409, { error: 'too late' });
      if (game.answers.has(pl.id)) return json(res, 409, { error: 'already answered' });
      const choice = Number(b.choice);
      if (!Number.isInteger(choice) || choice < 0 || choice >= q.choices.length) return json(res, 400, { error: 'bad choice' });
      const ms = Date.now() - game.qStartedAt;
      if (ms > q.time * 1000 + 1500) return json(res, 409, { error: 'too late' });
      game.answers.set(pl.id, { choice, ms });
      send(pl.streams, playerSnapshot(pl));
      broadcastHost();
      // Everyone online has answered -> reveal right away.
      if (game.answers.size >= connectedCount() && connectedCount() > 0) {
        const qi = game.qIndex;
        setTimeout(() => { if (game.qIndex === qi) reveal(); }, 600);
      }
      return json(res, 200, { ok: true });
    }

    // ---- host API
    if (p === '/host/events' || p.startsWith('/api/host/') || p === '/results.csv') {
      if (!isHost(req, url)) return json(res, 403, { error: 'forbidden' });
    }
    if (req.method === 'GET' && p === '/host/events') {
      openStream(req, res);
      hostStreams.add(res);
      res.write(`data: ${JSON.stringify(hostSnapshot())}\n\n`);
      req.on('close', () => hostStreams.delete(res));
      return;
    }
    if (req.method === 'GET' && p === '/api/host/packs') return json(res, 200, { packs: listPacks() });
    if (req.method === 'POST' && p.startsWith('/api/host/')) {
      const action = hostActions[p.slice(10)];
      if (!action) return json(res, 404, { error: 'unknown action' });
      const b = await readBody(req);
      try { action(b); } catch (e) { return json(res, 400, { error: e.message }); }
      broadcastAll();
      return json(res, 200, { ok: true });
    }
    if (req.method === 'GET' && p === '/results.csv') {
      const qs = game.pack ? game.pack.questions : [];
      const L = 'ABCD';
      const cell = (v) => `"${String(v).replace(/"/g, '""')}"`;
      const rows = [['rank', 'name', 'score', 'correct', ...qs.map((_, i) => `Q${i + 1}`)].map(cell).join(',')];
      const byName = new Map([...players.values()].map((pl) => [pl.name, pl]));
      for (const r of leaderboard()) {
        const pl = byName.get(r.name);
        rows.push([r.rank, r.name, r.score, r.correct, ...qs.map((_, i) => {
          const x = pl.results[i];
          return !x ? '' : x.choice === null ? '-' : L[x.choice] + (x.correct ? ' ✓' : ' ✗');
        })].map(cell).join(','));
      }
      res.writeHead(200, { 'Content-Type': 'text/csv; charset=utf-8', 'Content-Disposition': 'attachment; filename="flashquiz-results.csv"' });
      return res.end('﻿' + rows.join('\r\n'));
    }

    json(res, 404, { error: 'not found' });
  } catch (e) {
    json(res, 400, { error: e.message });
  }
});

server.listen(PORT, '0.0.0.0', () => {
  const info = joinInfo();
  console.log('\n  FlashQuiz is running\n');
  console.log(`  Main display / control:  http://localhost:${PORT}/host`);
  console.log(`  Students join at:        ${info.url || '(no LAN address found — use --ip <address>)'}`);
  for (const a of info.addresses.slice(1)) console.log(`                            http://${a.ip}:${PORT}/  (${a.iface})`);
  console.log(`  Control from another device: http://<ip>:${PORT}/host?key=${HOST_KEY}\n`);
});

// ---------------------------------------------------------------- utils ---
function clampInt(v, min, max, def) {
  const n = Math.round(Number(v));
  return Number.isFinite(n) && n > 0 ? Math.min(max, Math.max(min, n)) : def;
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const m = argv[i].match(/^--([\w-]+)(?:=(.*))?$/);
    if (m) out[m[1]] = m[2] !== undefined ? m[2] : argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true;
  }
  return out;
}
