"""Minimal QR Code generator for terminal display.

Generates QR codes using only stdlib — no external dependencies.
Implements QR Code Model 2, Version 1-10, Error Correction Level L.
Output as Unicode block characters for terminal display.
"""

# This is a minimal implementation sufficient for encoding octo:// tokens.
# For tokens up to ~300 chars, QR version 6-10 with ECC-L is enough.

import struct


# GF(256) arithmetic for Reed-Solomon
_EXP = [1] * 256
_LOG = [0] * 256
_v = 1
for _i in range(255):
    _EXP[_i] = _v
    _LOG[_v] = _i
    _v <<= 1
    if _v >= 256:
        _v ^= 0x11D
_EXP[255] = _EXP[0]


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[(_LOG[a] + _LOG[b]) % 255]


def _rs_generator(n):
    g = [1]
    for i in range(n):
        ng = [0] * (len(g) + 1)
        for j, coeff in enumerate(g):
            ng[j] ^= coeff
            ng[j + 1] ^= _gf_mul(coeff, _EXP[i])
        g = ng
    return g


def _rs_encode(data, ecc_len):
    gen = _rs_generator(ecc_len)
    msg = list(data) + [0] * ecc_len
    for i in range(len(data)):
        coeff = msg[i]
        if coeff != 0:
            for j, g in enumerate(gen):
                msg[i + j] ^= _gf_mul(g, coeff)
    return msg[len(data):]


# QR version parameters (version, size, ecc_L_total, ecc_L_per_block, num_blocks)
_VERSIONS = {
    1: (21, 7, 1),
    2: (25, 10, 1),
    3: (29, 15, 1),
    4: (33, 20, 1),
    5: (37, 26, 1),
    6: (41, 18, 2),
    7: (45, 20, 2),
    8: (49, 24, 2),
    9: (53, 30, 2),
    10: (57, 18, 4),
    11: (61, 20, 4),
    12: (65, 24, 4),
    13: (69, 26, 4),
    14: (73, 30, 4),
    15: (77, 22, 5),  # Increased for longer tokens
    16: (81, 24, 5),
    17: (85, 28, 6),
    18: (89, 30, 6),
    19: (93, 28, 7),
    20: (97, 28, 7),
}

# Data capacity in bytes for each version at ECC level L (byte mode)
_CAPACITY_L = {
    1: 17, 2: 32, 3: 53, 4: 78, 5: 106,
    6: 134, 7: 154, 8: 192, 9: 230, 10: 271,
    11: 321, 12: 367, 13: 425, 14: 458, 15: 520,
    16: 586, 17: 644, 18: 718, 19: 792, 20: 858,
}

# Total codewords per version
_TOTAL_CODEWORDS = {
    1: 26, 2: 44, 3: 70, 4: 100, 5: 134,
    6: 172, 7: 196, 8: 242, 9: 292, 10: 346,
    11: 404, 12: 466, 13: 532, 14: 581, 15: 655,
    16: 733, 17: 815, 18: 901, 19: 991, 20: 1085,
}

# ECC codewords per block for level L
_ECC_PER_BLOCK = {
    1: 7, 2: 10, 3: 15, 4: 20, 5: 26,
    6: 18, 7: 20, 8: 24, 9: 30, 10: 18,
    11: 20, 12: 24, 13: 26, 14: 30, 15: 22,
    16: 24, 17: 28, 18: 30, 19: 28, 20: 28,
}

# Number of error correction blocks
_NUM_BLOCKS = {
    1: 1, 2: 1, 3: 1, 4: 1, 5: 1,
    6: 2, 7: 2, 8: 2, 9: 2, 10: 4,
    11: 4, 12: 4, 13: 4, 14: 4, 15: 5,  # Corrected
    16: 5, 17: 6, 18: 6, 19: 7, 20: 7,
}

# Alignment pattern positions
_ALIGN = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
    11: [6, 30, 54],
    12: [6, 32, 58],
    13: [6, 34, 62],
    14: [6, 26, 46, 66],
    15: [6, 26, 48, 70],
    16: [6, 26, 50, 74],
    17: [6, 30, 54, 78],
    18: [6, 30, 56, 82],
    19: [6, 30, 58, 86],
    20: [6, 34, 62, 90],
}

# Format info bits for ECC level L, masks 0-7
_FORMAT_BITS = [
    0x77C4, 0x72F3, 0x7DAA, 0x789D, 0x662F, 0x6318, 0x6C41, 0x6976,
]

# Version info bits for versions 7+
_VERSION_BITS = {
    7: 0x07C94, 8: 0x085BC, 9: 0x09A99, 10: 0x0A4D3,
    11: 0x0BBF6, 12: 0x0C762, 13: 0x0D847, 14: 0x0E60D,
    15: 0x0F928, 16: 0x10B78, 17: 0x1145D, 18: 0x12A17,
    19: 0x13532, 20: 0x149A6,
}


def _select_version(data_len):
    for v in range(1, 21):
        if data_len <= _CAPACITY_L[v]:
            return v
    raise ValueError(f"Data too long ({data_len} bytes, max {_CAPACITY_L[20]})")


def _encode_data(data_bytes, version):
    """Encode data into QR codewords (byte mode, ECC level L)."""
    bits = []

    # Mode indicator: byte mode = 0100
    bits.extend([0, 1, 0, 0])

    # Character count (8 bits for v1-9, 16 bits for v10+)
    count_bits = 8 if version <= 9 else 16
    n = len(data_bytes)
    for i in range(count_bits - 1, -1, -1):
        bits.append((n >> i) & 1)

    # Data
    for b in data_bytes:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)

    # Terminator (up to 4 zeros)
    total_data_codewords = _TOTAL_CODEWORDS[version] - _ECC_PER_BLOCK[version] * _NUM_BLOCKS[version]
    total_bits = total_data_codewords * 8
    term = min(4, total_bits - len(bits))
    bits.extend([0] * term)

    # Pad to byte boundary
    while len(bits) % 8 != 0:
        bits.append(0)

    # Convert to bytes
    codewords = []
    for i in range(0, len(bits), 8):
        b = 0
        for j in range(8):
            if i + j < len(bits):
                b = (b << 1) | bits[i + j]
            else:
                b <<= 1
        codewords.append(b)

    # Pad with alternating 236, 17
    pad = [236, 17]
    pi = 0
    while len(codewords) < total_data_codewords:
        codewords.append(pad[pi])
        pi ^= 1

    return codewords[:total_data_codewords]


def _add_ecc(data_codewords, version):
    """Add error correction and interleave."""
    num_blocks = _NUM_BLOCKS[version]
    ecc_per = _ECC_PER_BLOCK[version]
    total_data = len(data_codewords)
    base_per_block = total_data // num_blocks
    extra = total_data % num_blocks

    blocks_data = []
    blocks_ecc = []
    idx = 0
    for i in range(num_blocks):
        blen = base_per_block + (1 if i >= num_blocks - extra else 0)
        block = data_codewords[idx:idx + blen]
        idx += blen
        blocks_data.append(block)
        blocks_ecc.append(_rs_encode(block, ecc_per))

    # Interleave data
    result = []
    max_data = max(len(b) for b in blocks_data)
    for i in range(max_data):
        for block in blocks_data:
            if i < len(block):
                result.append(block[i])

    # Interleave ECC
    for i in range(ecc_per):
        for block in blocks_ecc:
            if i < len(block):
                result.append(block[i])

    return result


def _make_matrix(version):
    size = _VERSIONS[version][0]
    # None = not yet filled, True = black, False = white
    matrix = [[None] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def _set(r, c, val, res=True):
        if 0 <= r < size and 0 <= c < size:
            matrix[r][c] = val
            if res:
                reserved[r][c] = True

    # Finder patterns
    for (cr, cc) in [(0, 0), (0, size - 7), (size - 7, 0)]:
        for r in range(7):
            for c in range(7):
                if (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4)):
                    _set(cr + r, cc + c, True)
                else:
                    _set(cr + r, cc + c, False)

    # Separators
    for i in range(8):
        for (cr, cc) in [(7, i), (i, 7), (7, size - 8 + i), (i, size - 8),
                         (size - 8, i), (size - 8 + i, 7)]:
            _set(cr, cc, False)

    # Timing patterns
    for i in range(8, size - 8):
        _set(6, i, i % 2 == 0)
        _set(i, 6, i % 2 == 0)

    # Alignment patterns
    positions = _ALIGN[version]
    for r in positions:
        for c in positions:
            # Skip if overlapping with finder patterns
            if reserved[r][c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    val = (abs(dr) == 2 or abs(dc) == 2 or (dr == 0 and dc == 0))
                    _set(r + dr, c + dc, val)

    # Dark module
    _set(size - 8, 8, True)

    # Reserve format info areas
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):
        reserved[8][size - 1 - i] = True
        reserved[size - 1 - i][8] = True

    # Reserve version info areas (version 7+)
    if version >= 7:
        for i in range(6):
            for j in range(3):
                reserved[i][size - 11 + j] = True
                reserved[size - 11 + j][i] = True

    return matrix, reserved


def _place_data(matrix, reserved, codewords):
    size = len(matrix)
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)

    idx = 0
    col = size - 1
    upward = True

    while col >= 0:
        if col == 6:
            col -= 1
            continue

        for row_offset in range(size):
            row = (size - 1 - row_offset) if upward else row_offset
            for dc in [0, -1]:
                c = col + dc
                if 0 <= c < size and not reserved[row][c]:
                    if idx < len(bits):
                        matrix[row][c] = bits[idx] == 1
                        idx += 1
                    else:
                        matrix[row][c] = False

        col -= 2
        upward = not upward


def _apply_mask(matrix, reserved, mask_id):
    size = len(matrix)
    masks = [
        lambda r, c: (r + c) % 2 == 0,
        lambda r, c: r % 2 == 0,
        lambda r, c: c % 3 == 0,
        lambda r, c: (r + c) % 3 == 0,
        lambda r, c: (r // 2 + c // 3) % 2 == 0,
        lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
        lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
        lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
    ]
    fn = masks[mask_id]
    for r in range(size):
        for c in range(size):
            if not reserved[r][c] and fn(r, c):
                matrix[r][c] = not matrix[r][c]


def _penalty(matrix):
    """Calculate penalty score for mask selection."""
    size = len(matrix)
    score = 0

    # Rule 1: runs of same color
    for r in range(size):
        run = 1
        for c in range(1, size):
            if matrix[r][c] == matrix[r][c - 1]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2

    for c in range(size):
        run = 1
        for r in range(1, size):
            if matrix[r][c] == matrix[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
        if run >= 5:
            score += run - 2

    # Rule 2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            v = matrix[r][c]
            if matrix[r][c + 1] == v and matrix[r + 1][c] == v and matrix[r + 1][c + 1] == v:
                score += 3

    return score


def _place_format(matrix, version, mask_id):
    size = len(matrix)
    fmt = _FORMAT_BITS[mask_id]

    # Around top-left finder
    bits_positions_h = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5),
                        (8, 7), (8, 8), (7, 8), (5, 8), (4, 8), (3, 8),
                        (2, 8), (1, 8), (0, 8)]
    bits_positions_v = [(size - 1, 8), (size - 2, 8), (size - 3, 8),
                        (size - 4, 8), (size - 5, 8), (size - 6, 8),
                        (size - 7, 8), (8, size - 8), (8, size - 7),
                        (8, size - 6), (8, size - 5), (8, size - 4),
                        (8, size - 3), (8, size - 2), (8, size - 1)]

    for i in range(15):
        bit = (fmt >> (14 - i)) & 1 == 1
        r, c = bits_positions_h[i]
        matrix[r][c] = bit
        r, c = bits_positions_v[i]
        matrix[r][c] = bit

    # Version info (version 7+)
    if version >= 7:
        vbits = _VERSION_BITS[version]
        for i in range(18):
            bit = (vbits >> i) & 1 == 1
            r, c = i // 3, size - 11 + i % 3
            matrix[r][c] = bit
            matrix[c][r] = bit


def generate(text):
    """Generate QR code matrix from text. Returns 2D list of booleans."""
    data = text.encode("utf-8")
    version = _select_version(len(data))
    codewords = _encode_data(data, version)
    all_codewords = _add_ecc(codewords, version)

    matrix, reserved = _make_matrix(version)
    _place_data(matrix, reserved, all_codewords)

    # Try all masks, pick best
    best_mask = 0
    best_score = float("inf")
    for m in range(8):
        trial = [row[:] for row in matrix]
        _apply_mask(trial, reserved, m)
        s = _penalty(trial)
        if s < best_score:
            best_score = s
            best_mask = m

    _apply_mask(matrix, reserved, best_mask)
    _place_format(matrix, version, best_mask)

    return matrix


def to_terminal(matrix, quiet_zone=2):
    """Render QR matrix as terminal string using Unicode half-blocks."""
    size = len(matrix)
    # Add quiet zone
    total = size + quiet_zone * 2
    lines = []

    # Use ▀ (upper half block), ▄ (lower half block), █ (full block), ' ' (empty)
    # Two rows per line using half blocks
    for r in range(0, total, 2):
        line = []
        for c in range(total):
            # Map to matrix coordinates (with quiet zone offset)
            mr1 = r - quiet_zone
            mr2 = r + 1 - quiet_zone
            mc = c - quiet_zone

            top = matrix[mr1][mc] if (0 <= mr1 < size and 0 <= mc < size) else False
            bot = matrix[mr2][mc] if (0 <= mr2 < size and 0 <= mc < size) else False

            if top and bot:
                line.append("█")
            elif top and not bot:
                line.append("▀")
            elif not top and bot:
                line.append("▄")
            else:
                line.append(" ")
        lines.append("".join(line))

    return "\n".join(lines)
