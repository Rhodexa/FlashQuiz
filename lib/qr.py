# Minimal QR code encoder (byte mode, ECC level M, versions 1-10).
# Zero dependencies so the app works fully offline. Algorithm follows
# the QR spec (ISO/IEC 18004); structure inspired by Nayuki's reference.
# Python port of lib/qr.js — produces byte-identical SVG.

# Indexed by version (1-10), ECC level M.
ECC_PER_BLOCK = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26]
NUM_BLOCKS = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5]
ECL_M_FORMAT_BITS = 0
MAX_VERSION = 10


def raw_data_modules(ver):
    r = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        r -= (25 * n - 10) * n - 55
        if ver >= 7:
            r -= 36
    return r


def data_codewords(ver):
    return raw_data_modules(ver) // 8 - ECC_PER_BLOCK[ver] * NUM_BLOCKS[ver]


def alignment_positions(ver, size):
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = -(-(ver * 4 + 4) // (n * 2 - 2)) * 2  # ceil division
    res = [6]
    pos = size - 7
    while len(res) < n:
        res.insert(1, pos)
        pos -= step
    return res


# --- Reed-Solomon over GF(256), poly 0x11D ---
def gf_mul(x, y):
    z = 0
    for i in range(7, -1, -1):
        z = ((z << 1) ^ ((z >> 7) * 0x11D)) & 0x1FF
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree):
    res = [0] * degree
    res[degree - 1] = 1
    root = 1
    for _ in range(degree):
        for j in range(len(res)):
            res[j] = gf_mul(res[j], root)
            if j + 1 < len(res):
                res[j] ^= res[j + 1]
        root = gf_mul(root, 0x02)
    return res


def rs_remainder(data, divisor):
    res = [0] * len(divisor)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(divisor):
            res[i] ^= gf_mul(c, factor)
    return res


def _bit(x, i):
    return ((x >> i) & 1) != 0


MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: (x * y) % 2 + (x * y) % 3 == 0,
    lambda x, y: ((x * y) % 2 + (x * y) % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + (x * y) % 3) % 2 == 0,
]


def encode(text):
    data_bytes = str(text).encode('utf-8')

    ver = 1
    while ver <= MAX_VERSION:
        cc_bits = 8 if ver <= 9 else 16
        if 4 + cc_bits + len(data_bytes) * 8 <= data_codewords(ver) * 8:
            break
        ver += 1
    if ver > MAX_VERSION:
        raise ValueError('QR: text too long')

    # --- Data bitstream ---
    bits = []

    def push(val, length):
        for i in range(length - 1, -1, -1):
            bits.append((val >> i) & 1)

    push(0b0100, 4)
    push(len(data_bytes), 8 if ver <= 9 else 16)
    for b in data_bytes:
        push(b, 8)
    cap_bits = data_codewords(ver) * 8
    push(0, min(4, cap_bits - len(bits)))
    push(0, (8 - len(bits) % 8) % 8)
    pad = 0xEC
    while len(bits) < cap_bits:
        push(pad, 8)
        pad ^= 0xEC ^ 0x11
    data = []
    for i in range(0, len(bits), 8):
        b = 0
        for j in range(8):
            b = (b << 1) | bits[i + j]
        data.append(b)

    # --- ECC + interleave ---
    num_blocks = NUM_BLOCKS[ver]
    ecc_len = ECC_PER_BLOCK[ver]
    raw_cw = raw_data_modules(ver) // 8
    num_short = num_blocks - raw_cw % num_blocks
    short_len = raw_cw // num_blocks
    div = rs_divisor(ecc_len)
    blocks = []
    k = 0
    for i in range(num_blocks):
        dat = data[k:k + short_len - ecc_len + (0 if i < num_short else 1)]
        k += len(dat)
        ecc = rs_remainder(dat, div)
        if i < num_short:
            dat.append(0)
        blocks.append(dat + ecc)
    codewords = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ecc_len or j >= num_short:
                codewords.append(blk[i])

    # --- Matrix ---
    size = ver * 4 + 17
    mods = [[False] * size for _ in range(size)]
    is_fn = [[False] * size for _ in range(size)]

    def set_fn(x, y, dark):
        mods[y][x] = dark
        is_fn[y][x] = True

    for i in range(size):
        set_fn(6, i, i % 2 == 0)
        set_fn(i, 6, i % 2 == 0)

    def finder(cx, cy):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = max(abs(dx), abs(dy))
                x, y = cx + dx, cy + dy
                if 0 <= x < size and 0 <= y < size:
                    set_fn(x, y, d != 2 and d != 4)

    finder(3, 3)
    finder(size - 4, 3)
    finder(3, size - 4)

    al = alignment_positions(ver, size)
    last = len(al) - 1
    for i in range(len(al)):
        for j in range(len(al)):
            if (i == 0 and j == 0) or (i == 0 and j == last) or (i == last and j == 0):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    set_fn(al[i] + dx, al[j] + dy, max(abs(dx), abs(dy)) != 1)

    def draw_format(mask):
        d = (ECL_M_FORMAT_BITS << 3) | mask
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        f = ((d << 10) | rem) ^ 0x5412
        for i in range(6):
            set_fn(8, i, _bit(f, i))
        set_fn(8, 7, _bit(f, 6))
        set_fn(8, 8, _bit(f, 7))
        set_fn(7, 8, _bit(f, 8))
        for i in range(9, 15):
            set_fn(14 - i, 8, _bit(f, i))
        for i in range(8):
            set_fn(size - 1 - i, 8, _bit(f, i))
        for i in range(8, 15):
            set_fn(8, size - 15 + i, _bit(f, i))
        set_fn(8, size - 8, True)

    draw_format(0)  # reserve area

    if ver >= 7:
        rem = ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        v = (ver << 12) | rem
        for i in range(18):
            a, b = size - 11 + i % 3, i // 3
            set_fn(a, b, _bit(v, i))
            set_fn(b, a, _bit(v, i))

    # Zig-zag codeword placement
    bi = 0
    total_bits = len(codewords) * 8
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not is_fn[y][x] and bi < total_bits:
                    mods[y][x] = _bit(codewords[bi >> 3], 7 - (bi & 7))
                    bi += 1
        right -= 2

    def apply_mask(m):
        fn = MASKS[m]
        for y in range(size):
            row, fn_row = mods[y], is_fn[y]
            for x in range(size):
                if not fn_row[x] and fn(x, y):
                    row[x] = not row[x]

    best, best_penalty = 0, float('inf')
    for m in range(8):
        apply_mask(m)
        draw_format(m)
        p = penalty(mods, size)
        if p < best_penalty:
            best_penalty, best = p, m
        apply_mask(m)  # XOR undo
    apply_mask(best)
    draw_format(best)
    return size, mods


def _line_penalty(rows, size):
    s = 0
    for r in rows:
        run = 1
        for j in range(1, size + 1):
            if j < size and r[j] == r[j - 1]:
                run += 1
            else:
                if run >= 5:
                    s += 3 + run - 5
                run = 1
        # finder-like patterns 1011101 with 4 light modules on either side
        for j in range(size - 6):
            if r[j] and not r[j + 1] and r[j + 2] and r[j + 3] and r[j + 4] and not r[j + 5] and r[j + 6]:
                def light_run(a, b):
                    return not any(r[k] for k in range(a, b) if 0 <= k < size)
                if light_run(j - 4, j) or light_run(j + 7, j + 11):
                    s += 40
    return s


def penalty(m, size):
    p = _line_penalty(m, size)
    p += _line_penalty([list(col) for col in zip(*m)], size)
    dark = 0
    for y in range(size):
        for x in range(size):
            if m[y][x]:
                dark += 1
            if x < size - 1 and y < size - 1:
                c = m[y][x]
                if c == m[y][x + 1] and c == m[y + 1][x] and c == m[y + 1][x + 1]:
                    p += 3
    total = size * size
    p += (-(-abs(dark * 20 - total * 10) // total) - 1) * 10  # ceil division
    return p


# Crisp SVG: one path, integer module coordinates, scales to any size.
def to_svg(text, margin=4):
    size, modules = encode(text)
    dim = size + margin * 2
    d = ''.join(
        f'M{x + margin} {y + margin}h1v1h-1z'
        for y in range(size) for x in range(size) if modules[y][x]
    )
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" shape-rendering="crispEdges">'
            f'<rect width="100%" height="100%" fill="#fff"/><path d="{d}" fill="#000"/></svg>')
