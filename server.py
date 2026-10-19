#!/usr/bin/env python3
# FlashQuiz — local real-time quiz server. Zero dependencies (Python >= 3.7).
#   python server.py [--port 8080] [--ip 192.168.43.1]
# Python port of server.js: same endpoints, same payloads, same pages.
# Real-time transport: Server-Sent Events (down) + small POSTs (up).
# Each connection gets its own thread; one lock guards all game state.

import errno
import json
import math
import os
import queue
import re
import secrets
import select
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from lib import qr

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PACKS_DIR = os.path.join(BASE_DIR, 'packs')
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')
IS_WINDOWS = sys.platform == 'win32'


def parse_args(argv):
    out = {}
    i = 0
    while i < len(argv):
        m = re.match(r'^--([\w-]+)(?:=(.*))?$', argv[i])
        if m:
            if m.group(2) is not None:
                out[m.group(1)] = m.group(2)
            elif i + 1 < len(argv) and not argv[i + 1].startswith('--'):
                i += 1
                out[m.group(1)] = argv[i]
            else:
                out[m.group(1)] = True
        i += 1
    return out


args = parse_args(sys.argv[1:])
PORT = int(args.get('port') or os.environ.get('PORT') or 8080)
FORCED_IP = args.get('ip') or os.environ.get('JOIN_IP') or ''
# Lets the host page be used from another device (e.g. teacher's phone).
# Loopback (the machine running the server) never needs it.
HOST_KEY = os.environ.get('HOST_KEY') or secrets.token_hex(3)


# ---------------------------------------------------------------- utils ---
def now_ms():
    return int(time.time() * 1000)


def js_round(x):
    return int(math.floor(x + 0.5))


def js_number(v):
    """Rough equivalent of JS Number(v): returns a float/int, or None for NaN."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return v if not (isinstance(v, float) and math.isnan(v)) else None
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return 0
        try:
            return float(s)
        except ValueError:
            return None
    return None


def js_int(v):
    """Number(v) if it is an integer (Number.isInteger), else None."""
    n = js_number(v)
    if n is None or (isinstance(n, float) and (math.isinf(n) or not n.is_integer())):
        return None
    return int(n)


def js_str(v):
    if v is True:
        return 'true'
    if v is False:
        return 'false'
    if v is None:
        return 'null'
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def clamp_int(v, lo, hi, default):
    n = js_number(v)
    if n is None or math.isinf(n):
        return default
    n = js_round(n)
    return min(hi, max(lo, n)) if n > 0 else default


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


# ---------------------------------------------------------------- state ---
lock = threading.RLock()


class Game:
    def __init__(self):
        self.phase = 'lobby'      # lobby | question | reveal | end
        self.pack = None          # { id, title, speedBonus, questions: [...] }
        self.q_index = -1
        self.q_started_at = 0
        self.q_deadline = 0
        self.answers = {}         # playerId -> { choice, ms }
        self.timer = None
        self.wifi = {'ssid': '', 'pass': ''}
        self.join_ip = FORCED_IP


class Player:
    def __init__(self, pid, name):
        self.id = pid
        self.name = name
        self.score = 0
        self.results = {}         # question index -> result
        self.streams = set()      # set of Stream


game = Game()
players = {}                      # id -> Player (insertion-ordered)
host_streams = set()


# ---------------------------------------------------------------- packs ---
def read_json_file(fp):
    # utf-8-sig tolerates the BOM that Windows Notepad likes to add.
    with open(fp, encoding='utf-8-sig') as f:
        return json.load(f)


def list_packs():
    try:
        files = sorted(f for f in os.listdir(PACKS_DIR) if f.endswith('.json'))
    except OSError:
        return []
    out = []
    for f in files:
        try:
            p = validate_pack(read_json_file(os.path.join(PACKS_DIR, f)))
            out.append({'id': f, 'title': p['title'], 'count': len(p['questions'])})
        except Exception as e:
            out.append({'id': f, 'title': f, 'error': str(e)})
    return out


def validate_pack(p):
    if not isinstance(p, dict) or not isinstance(p.get('questions'), list) or not p['questions']:
        raise ValueError('falta la lista "questions"')
    def_time = clamp_int(p.get('time'), 5, 300, 20)
    questions = []
    for i, q in enumerate(p['questions']):
        n = f'pregunta {i + 1}'
        if not isinstance(q, dict) or not isinstance(q.get('q'), str) or not q['q'].strip():
            raise ValueError(f'{n}: falta el texto "q"')
        choices = q.get('choices')
        if not isinstance(choices, list) or len(choices) < 2 or len(choices) > 4:
            raise ValueError(f'{n}: "choices" debe tener entre 2 y 4 opciones')
        answer = js_int(q.get('answer'))
        if answer is None or answer < 0 or answer >= len(choices):
            raise ValueError(f'{n}: "answer" debe ser el número de una opción (0-{len(choices) - 1})')
        questions.append({
            'q': q['q'].strip(),
            'choices': [js_str(c) for c in choices],
            'answer': answer,
            'time': clamp_int(q.get('time'), 5, 300, def_time),
        })
    return {
        'title': js_str(p.get('title') or 'Cuestionario sin título')[:120],
        'speedBonus': p.get('speedBonus') is not False,
        'questions': questions,
    }


def load_pack(pack_id):
    file = os.path.basename(js_str(pack_id))
    p = validate_pack(read_json_file(os.path.join(PACKS_DIR, file)))
    p['id'] = file
    return p


# ----------------------------------------------------------- game flow ---
def current_q():
    if not game.pack or not (0 <= game.q_index < len(game.pack['questions'])):
        return None
    return game.pack['questions'][game.q_index]


def cancel_timer():
    if game.timer:
        game.timer.cancel()
        game.timer = None


def later(seconds, fn):
    t = threading.Timer(seconds, fn)
    t.daemon = True
    t.start()
    return t


def start_question(index):
    qs = game.pack['questions']
    if index >= len(qs):
        return end_game()
    q = qs[index]
    cancel_timer()
    game.phase = 'question'
    game.q_index = index
    game.answers = {}
    game.q_started_at = now_ms()
    game.q_deadline = game.q_started_at + q['time'] * 1000
    # Small grace for network lag. The index guard drops a timer that fires
    # after the host already moved on.
    game.timer = later(q['time'] + 0.3, lambda: reveal_if(index))
    broadcast_all()


def reveal_if(index):
    with lock:
        if game.q_index == index:
            reveal()


def reveal():
    if game.phase != 'question':
        return
    cancel_timer()
    q = current_q()
    for p in players.values():
        a = game.answers.get(p.id)
        correct = bool(a) and a['choice'] == q['answer']
        pts = 0
        if correct:
            pts = 500
            if game.pack['speedBonus']:
                pts += js_round(500 * max(0, 1 - a['ms'] / (q['time'] * 1000)))
        p.score += pts
        p.results[game.q_index] = {
            'choice': a['choice'] if a else None, 'ms': a['ms'] if a else None, 'correct': correct, 'pts': pts,
        }
    game.phase = 'reveal'
    broadcast_all()


def end_game():
    cancel_timer()
    game.phase = 'end'
    broadcast_all()


def reset_game(keep_players):
    cancel_timer()
    game.phase = 'lobby'
    game.q_index = -1
    game.answers = {}
    if keep_players:
        for p in players.values():
            p.score = 0
            p.results = {}
    else:
        for p in players.values():
            send(p.streams, {'type': 'kicked'})
        players.clear()
    broadcast_all()


def connected_count():
    return sum(1 for p in players.values() if p.streams)


def leaderboard():
    ranked = sorted(players.values(), key=lambda p: -p.score)
    return [{
        'rank': i + 1, 'name': p.name, 'score': p.score,
        'correct': sum(1 for r in p.results.values() if r and r['correct']),
    } for i, p in enumerate(ranked)]


# ------------------------------------------------------------ snapshots ---
def public_question(with_answer):
    q = current_q()
    if not q:
        return None
    out = {
        'index': game.q_index,
        'total': len(game.pack['questions']),
        'text': q['q'],
        'choices': q['choices'],
        'time': q['time'],
        'remainingMs': max(0, game.q_deadline - now_ms()),
    }
    if with_answer:
        out['answer'] = q['answer']
    return out


def host_snapshot():
    q = current_q()
    counts = [0] * len(q['choices']) if q else []
    for a in game.answers.values():
        counts[a['choice']] += 1
    pack = game.pack
    return {
        'type': 'state',
        'phase': game.phase,
        'pack': {'id': pack['id'], 'title': pack['title'], 'count': len(pack['questions'])} if pack else None,
        'question': public_question(game.phase != 'question'),
        'answered': len(game.answers),
        'counts': counts,
        'players': [{
            'id': p.id, 'name': p.name, 'score': p.score, 'online': bool(p.streams), 'answered': p.id in game.answers,
        } for p in players.values()],
        'online': connected_count(),
        'leaderboard': leaderboard()[:10] if game.phase in ('reveal', 'end') else [],
        'join': join_info(),
    }


def player_snapshot(p):
    a = game.answers.get(p.id)
    res = p.results.get(game.q_index)
    rank = 0
    if game.phase in ('reveal', 'end'):
        rank = next((i + 1 for i, r in enumerate(leaderboard()) if r['name'] == p.name), 0)
    return {
        'type': 'state',
        'phase': game.phase,
        'packTitle': game.pack['title'] if game.pack else '',
        'question': public_question(game.phase == 'reveal'),
        'you': {
            'name': p.name, 'score': p.score, 'rank': rank, 'players': len(players),
            'answered': a['choice'] if a else None,
            'result': res if game.phase == 'reveal' and res else None,
        },
    }


# ------------------------------------------------------------------ SSE ---
CLOSE = object()


class Stream:
    """One open SSE response. Other threads only enqueue; the request's own
    thread does all socket writes, so writes never interleave."""

    def __init__(self):
        self.q = queue.Queue()

    def put(self, item):
        self.q.put(item)


def send(streams, msg):
    data = f'data: {dumps(msg)}\n\n'
    for s in list(streams):
        s.put(data)


host_pending = None


def broadcast_host():
    # Coalesce bursts (30 students answering in the same second) into one push.
    global host_pending
    if host_pending:
        return

    def flush():
        global host_pending
        with lock:
            host_pending = None
            send(host_streams, host_snapshot())

    host_pending = later(0.12, flush)


def broadcast_all():
    for p in players.values():
        send(p.streams, player_snapshot(p))
    broadcast_host()


# Laptops hop networks (school Wi-Fi -> phone hotspot) while the server runs.
# Push the new join URL so the QR on screen never points at a stale IP.
def watch_network():
    last_key = ''
    while True:
        try:
            with lock:
                j = join_info()
            key = j['url'] + '|' + ','.join(a['ip'] for a in j['addresses'])
            if key != last_key:
                if last_key:
                    print(f"  Network changed — students join at: {j['url'] or '(no LAN address)'}", flush=True)
                last_key = key
                with lock:
                    broadcast_host()
        except Exception:
            pass
        time.sleep(3)


# -------------------------------------------------------------- network ---
GOOD_IFACE = re.compile(r'^(wl|wlan|wi-?fi|ap|swlan|en|eth)', re.I)
BAD_IFACE = re.compile(r'^(docker|br-|veth|virbr|vmnet|vbox|tun|tap|tailscale|zt|utun|lxc|'
                       r'virtualbox|vmware|hyper-v|zerotier|bluetooth|loopback)', re.I)
PRIVATE_172 = re.compile(r'^172\.(1[6-9]|2\d|3[01])\.')


def _windows_interfaces():
    # GetAdaptersAddresses gives the adapter names people see in Windows
    # ("Wi-Fi", "Ethernet", "vEthernet (WSL)") without parsing the localized
    # output of ipconfig. Only the leading struct fields we read are declared.
    import ctypes

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [('lpSockaddr', ctypes.c_void_p), ('iSockaddrLength', ctypes.c_int)]

    class UNICAST(ctypes.Structure):
        pass
    UNICAST._fields_ = [('Alignment', ctypes.c_ulonglong), ('Next', ctypes.POINTER(UNICAST)),
                        ('Address', SOCKET_ADDRESS)]

    class ADAPTER(ctypes.Structure):
        pass
    ADAPTER._fields_ = [
        ('Alignment', ctypes.c_ulonglong), ('Next', ctypes.POINTER(ADAPTER)), ('AdapterName', ctypes.c_char_p),
        ('FirstUnicastAddress', ctypes.POINTER(UNICAST)), ('FirstAnycastAddress', ctypes.c_void_p),
        ('FirstMulticastAddress', ctypes.c_void_p), ('FirstDnsServerAddress', ctypes.c_void_p),
        ('DnsSuffix', ctypes.c_wchar_p), ('Description', ctypes.c_wchar_p), ('FriendlyName', ctypes.c_wchar_p),
        ('PhysicalAddress', ctypes.c_ubyte * 8), ('PhysicalAddressLength', ctypes.c_ulong),
        ('Flags', ctypes.c_ulong), ('Mtu', ctypes.c_ulong), ('IfType', ctypes.c_ulong), ('OperStatus', ctypes.c_int),
    ]

    fn = ctypes.windll.iphlpapi.GetAdaptersAddresses
    fn.restype = ctypes.c_ulong
    size = ctypes.c_ulong(16384)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        # AF_INET; skip anycast, multicast and DNS server lists.
        rc = fn(2, 0x2 | 0x4 | 0x8, None, buf, ctypes.byref(size))
        if rc == 0:
            break
        if rc != 111:  # ERROR_BUFFER_OVERFLOW -> retry with the size it asked for
            return []
    else:
        return []

    out = []
    ad = ctypes.cast(buf, ctypes.POINTER(ADAPTER))
    while ad:
        a = ad.contents
        if a.OperStatus == 1 and a.IfType != 24:  # IfOperStatusUp, not loopback
            u = a.FirstUnicastAddress
            while u:
                sa = u.contents.Address
                if sa.lpSockaddr and sa.iSockaddrLength >= 8:
                    raw = ctypes.string_at(sa.lpSockaddr, 8)
                    if raw[0] == 2 and raw[1] == 0:  # sa_family == AF_INET
                        out.append((a.FriendlyName or '?', socket.inet_ntoa(raw[4:8])))
                u = u.contents.Next
        ad = a.Next
    return out


def _linux_interfaces():
    import fcntl
    import struct
    out = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _, name in socket.if_nameindex():
            try:
                res = fcntl.ioctl(s.fileno(), 0x8915, struct.pack('256s', name.encode()[:15]))  # SIOCGIFADDR
            except OSError:
                continue
            out.append((name, socket.inet_ntoa(res[20:24])))
    finally:
        s.close()
    return out


def _fallback_interfaces():
    ips = []
    try:
        ips = [ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    if not ips:
        # Asks the OS which local address would route outward; sends nothing.
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.255.255.255', 1))
            ips = [s.getsockname()[0]]
        except OSError:
            pass
        finally:
            s.close()
    return [('?', ip) for ip in dict.fromkeys(ips)]


def interfaces():
    try:
        if IS_WINDOWS:
            found = _windows_interfaces()
        elif sys.platform.startswith('linux'):
            found = _linux_interfaces()
        else:
            found = []
    except Exception:  # Android/Termux may forbid this
        found = []
    return found or _fallback_interfaces()


def lan_addresses():
    out = []
    for name, ip in interfaces():
        if ip.startswith('127.') or ip.startswith('169.254.') or ip == '0.0.0.0':
            continue
        score = 0
        if GOOD_IFACE.match(name):
            score += 2
        if BAD_IFACE.match(name):
            score -= 5
        if ip.startswith('192.168.'):
            score += 2
        elif ip.startswith('10.') or PRIVATE_172.match(ip):
            score += 1
        out.append({'ip': ip, 'iface': name, 'score': score})
    return sorted(out, key=lambda a: -a['score'])


def join_info():
    addrs = lan_addresses()
    ip = game.join_ip or (addrs[0]['ip'] if addrs else '')
    url = f"http://{ip}{'' if PORT == 80 else ':' + str(PORT)}/" if ip else ''
    return {
        'ip': ip, 'url': url, 'port': PORT, 'addresses': [{'ip': a['ip'], 'iface': a['iface']} for a in addrs],
        'wifi': {'ssid': game.wifi['ssid'], 'v': game.wifi.get('v')} if game.wifi['ssid'] else None,
    }


def wifi_qr_text():
    def esc(s):
        return re.sub(r'([\\;,:"])', r'\\\1', s)
    ssid, pw = game.wifi['ssid'], game.wifi['pass']
    return f'WIFI:T:WPA;S:{esc(ssid)};P:{esc(pw)};;' if pw else f'WIFI:T:nopass;S:{esc(ssid)};;'


# ------------------------------------------------------------ host API ---
class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def unique_name(raw):
    name = re.sub(r'[\x00-\x1f<>]', '', js_str(raw or '')).strip()[:20]
    if not name:
        name = 'Jugador'
    taken = {p.name.lower() for p in players.values()}
    if name.lower() not in taken:
        return name
    i = 2
    while f'{name} {i}'.lower() in taken:
        i += 1
    return f'{name} {i}'


def require_pack():
    if not game.pack:
        raise ValueError('primero cargá un cuestionario')


def act_pack(b):
    if game.phase != 'lobby':
        reset_game(True)
    game.pack = load_pack(b.get('id'))


def act_upload(b):
    if game.phase != 'lobby':
        reset_game(True)
    game.pack = validate_pack(b.get('pack'))
    game.pack['id'] = '(uploaded) ' + js_str(b.get('name') or 'pack.json')[:60]


def act_start(b):
    require_pack()
    reset_game(True)
    start_question(0)


def act_next(b):
    require_pack()
    if game.phase == 'question':
        return reveal()
    if game.phase == 'lobby':
        return start_question(0)
    if game.q_index + 1 >= len(game.pack['questions']):
        return end_game()
    start_question(game.q_index + 1)


def act_kick(b):
    pid = b.get('id')
    p = players.get(pid) if isinstance(pid, str) else None
    if p:
        send(p.streams, {'type': 'kicked'})
        for s in list(p.streams):
            s.put(CLOSE)
        del players[pid]


def act_wifi(b):
    game.wifi = {'ssid': js_str(b.get('ssid') or '')[:32], 'pass': js_str(b.get('pass') or '')[:63], 'v': now_ms()}


def act_joinip(b):
    game.join_ip = re.sub(r'[^0-9a-zA-Z.:\-]', '', js_str(b.get('ip') or ''))[:64]


HOST_ACTIONS = {
    'pack': act_pack,
    'upload': act_upload,
    'start': act_start,
    'reveal': lambda b: reveal(),
    'next': act_next,
    'end': lambda b: end_game(),
    'reset': lambda b: reset_game(bool(b.get('keepPlayers'))),
    'kick': act_kick,
    'wifi': act_wifi,
    'joinip': act_joinip,
}


def results_csv():
    qs = game.pack['questions'] if game.pack else []
    letters = 'ABCD'

    def cell(v):
        return '"' + js_str(v).replace('"', '""') + '"'

    def answer_cell(x):
        if not x:
            return ''
        if x['choice'] is None:
            return '-'
        return letters[x['choice']] + (' ✓' if x['correct'] else ' ✗')

    # ';' separator: Spanish-locale Excel uses ',' as the decimal mark.
    rows = [';'.join(cell(v) for v in ['puesto', 'nombre', 'puntaje', 'correctas'] + [f'P{i + 1}' for i in range(len(qs))])]
    by_name = {p.name: p for p in players.values()}
    for r in leaderboard():
        pl = by_name[r['name']]
        vals = [r['rank'], r['name'], r['score'], r['correct']] + [answer_cell(pl.results.get(i)) for i in range(len(qs))]
        rows.append(';'.join(cell(v) for v in vals))
    return '﻿' + '\r\n'.join(rows)


# --------------------------------------------------------------- http ---
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
        '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.json': 'application/json'}
STATIC_RE = re.compile(r'^/static/[\w.-]+$', re.ASCII)
MAX_BODY = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'FlashQuiz'
    timeout = 75  # idle keep-alive sockets from vanished phones eventually free their thread

    def log_message(self, *a):
        pass

    # ---- helpers
    def param(self, name):
        v = self.query.get(name)
        return v[0] if v else None

    def is_host(self):
        if self.client_address[0] in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            return True
        return self.param('key') == HOST_KEY or self.headers.get('x-host-key') == HOST_KEY

    def respond(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, code, obj):
        self.respond(code, dumps(obj), 'application/json', {'Cache-Control': 'no-store'})

    def read_body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            self.close_connection = True
            raise HttpError(400, 'Archivo demasiado grande')
        raw = self.rfile.read(length) if length else b''
        if not raw:
            return {}
        try:
            b = json.loads(raw.decode('utf-8'))
        except ValueError:
            raise HttpError(400, 'JSON inválido')
        if not isinstance(b, dict):
            raise HttpError(400, 'JSON inválido')
        return b

    def serve_static(self, file):
        fp = os.path.normpath(os.path.join(PUBLIC_DIR, file))
        if not fp.startswith(PUBLIC_DIR + os.sep):
            return self.json(404, {'error': 'not found'})
        try:
            with open(fp, 'rb') as f:
                buf = f.read()
        except OSError:
            return self.json(404, {'error': 'not found'})
        ext = os.path.splitext(fp)[1]
        self.respond(200, buf, MIME.get(ext, 'application/octet-stream'), {'Cache-Control': 'no-cache'})

    def stream(self, register, first_msg):
        """Hold this connection open as an SSE stream until the client leaves
        or the server closes it. `register(stream)` runs under the lock and
        hooks the stream into game state; it returns a cleanup function."""
        self.close_connection = True
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache, no-transform')
        self.end_headers()
        sock = self.connection
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        s = Stream()
        with lock:
            s.put(f'retry: 1500\n\ndata: {dumps(first_msg())}\n\n')
            cleanup = register(s)
        last_write = time.monotonic()
        try:
            while True:
                try:
                    item = s.q.get(timeout=1)
                except queue.Empty:
                    item = None
                if item is CLOSE:
                    break
                if item is None:
                    # Notice clients that went away (tab closed, EventSource
                    # reconnecting) within a second instead of at the next write.
                    r, _, _ = select.select([sock], [], [], 0)
                    if r and not sock.recv(1, socket.MSG_PEEK):
                        break
                    # Heartbeat keeps phone hotspots / power-saving radios from dropping idle streams.
                    if time.monotonic() - last_write < 15:
                        continue
                    item = ': ping\n\n'
                self.wfile.write(item.encode('utf-8'))
                last_write = time.monotonic()
        except OSError:
            pass
        finally:
            if cleanup:
                with lock:
                    cleanup()

    # ---- routing
    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')

    def handle_request(self, method):
        url = urlsplit(self.path)
        self.query = parse_qs(url.query, keep_blank_values=True)
        p = url.path
        try:
            self.route(method, p)
        except HttpError as e:
            self.json(e.code, {'error': str(e)})
        except (BrokenPipeError, ConnectionError):
            self.close_connection = True
        except Exception as e:
            self.json(400, {'error': str(e)})

    def route(self, method, p):
        # ---- pages
        if method == 'GET' and p in ('/', '/play'):
            return self.serve_static('play.html')
        if method == 'GET' and p == '/host':
            if not self.is_host():
                return self.json(403, {'error': 'Falta la clave del panel: /host?key=…'})
            return self.serve_static('host.html')
        if method == 'GET' and STATIC_RE.match(p):
            return self.serve_static(p[8:])

        # ---- QR codes (SVG, generated locally)
        if method == 'GET' and p == '/qr/join.svg':
            with lock:
                text = self.param('url') or join_info()['url'] or f'http://localhost:{PORT}/'
            cache = 'max-age=86400' if 'url' in self.query else 'no-store'
            return self.respond(200, qr.to_svg(text), 'image/svg+xml', {'Cache-Control': cache})
        if method == 'GET' and p == '/qr/wifi.svg':
            with lock:
                if not self.is_host() or not game.wifi['ssid']:
                    return self.json(404, {'error': 'no wifi set'})
                text = wifi_qr_text()
            return self.respond(200, qr.to_svg(text), 'image/svg+xml', {'Cache-Control': 'private, max-age=86400'})

        # ---- player API
        if method == 'POST' and p == '/api/join':
            b = self.read_body()
            with lock:
                pid = b.get('id')
                pl = players.get(pid) if isinstance(pid, str) and pid else None
                if not pl:
                    pl = Player(secrets.token_hex(8), unique_name(b.get('name')))
                    players[pl.id] = pl
                    broadcast_host()
                out = {'id': pl.id, 'name': pl.name}
            return self.json(200, out)
        if method == 'GET' and p == '/events':
            pid = self.param('id')
            with lock:
                pl = players.get(pid)
            if not pl:
                return self.stream(lambda s: s.put(CLOSE), lambda: {'type': 'unknown'})

            def register(s):
                pl.streams.add(s)
                broadcast_host()

                def cleanup():
                    pl.streams.discard(s)
                    broadcast_host()
                return cleanup
            return self.stream(register, lambda: player_snapshot(pl))
        if method == 'POST' and p == '/api/answer':
            b = self.read_body()
            with lock:
                pid = b.get('id')
                pl = players.get(pid) if isinstance(pid, str) else None
                q = current_q()
                if not pl:
                    return self.json(404, {'error': 'Jugador desconocido'})
                idx = b.get('index')
                if game.phase != 'question' or isinstance(idx, bool) or idx != game.q_index:
                    return self.json(409, {'error': 'Se terminó el tiempo'})
                if pl.id in game.answers:
                    return self.json(409, {'error': 'Ya respondiste'})
                choice = js_int(b.get('choice'))
                if choice is None or choice < 0 or choice >= len(q['choices']):
                    return self.json(400, {'error': 'Opción inválida'})
                ms = now_ms() - game.q_started_at
                if ms > q['time'] * 1000 + 1500:
                    return self.json(409, {'error': 'Se terminó el tiempo'})
                game.answers[pl.id] = {'choice': choice, 'ms': ms}
                send(pl.streams, player_snapshot(pl))
                broadcast_host()
                # Everyone online has answered -> reveal right away.
                online = connected_count()
                if online > 0 and len(game.answers) >= online:
                    qi = game.q_index
                    later(0.6, lambda: reveal_if(qi))
            return self.json(200, {'ok': True})

        # ---- host API
        if p == '/host/events' or p.startswith('/api/host/') or p == '/results.csv':
            if not self.is_host():
                return self.json(403, {'error': 'Acceso denegado'})
        if method == 'GET' and p == '/host/events':
            def register(s):
                host_streams.add(s)
                return lambda: host_streams.discard(s)
            return self.stream(register, host_snapshot)
        if method == 'GET' and p == '/api/host/packs':
            return self.json(200, {'packs': list_packs()})
        if method == 'POST' and p.startswith('/api/host/'):
            action = HOST_ACTIONS.get(p[10:])
            if not action:
                return self.json(404, {'error': 'Acción desconocida'})
            b = self.read_body()
            with lock:
                try:
                    action(b)
                except Exception as e:
                    return self.json(400, {'error': str(e)})
                broadcast_all()
            return self.json(200, {'ok': True})
        if method == 'GET' and p == '/results.csv':
            with lock:
                csv = results_csv()
            return self.respond(200, csv, 'text/csv; charset=utf-8', {
                'Content-Disposition': 'attachment; filename="flashquiz-resultados.csv"'})

        self.json(404, {'error': 'not found'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server silently share the port.
    allow_reuse_address = not IS_WINDOWS
    request_queue_size = 64


def main():
    try:
        sys.stdout.reconfigure(errors='replace')
    except Exception:
        pass
    try:
        server = Server(('0.0.0.0', PORT), Handler)
    except OSError as e:
        code = getattr(e, 'winerror', None) or e.errno
        if code in (errno.EADDRINUSE, 10048):
            print(f'\n  Port {PORT} is already in use (is FlashQuiz already running?). '
                  f'Try: python server.py --port {PORT + 1}\n', file=sys.stderr)
        elif code in (errno.EACCES, 10013):
            print(f'\n  No permission to use port {PORT}. Ports below 1024 need admin rights '
                  f'(and Windows reserves some ranges); try --port 8080.\n', file=sys.stderr)
        else:
            print(e, file=sys.stderr)
        sys.exit(1)

    with lock:
        info = join_info()
    print('\n  FlashQuiz is running\n')
    print(f'  Main display / control:  http://localhost:{PORT}/host')
    print(f"  Students join at:        {info['url'] or '(no LAN address found — use --ip <address>)'}")
    for a in info['addresses'][1:]:
        print(f"                            http://{a['ip']}:{PORT}/  ({a['iface']})")
    print(f'  Control from another device: http://<ip>:{PORT}/host?key={HOST_KEY}')
    print('  Press Ctrl+C to stop.\n', flush=True)

    threading.Thread(target=watch_network, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print('\n  FlashQuiz stopped.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
