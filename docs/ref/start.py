#!/usr/bin/env python
"""Unified puzzle-71 scan launcher.

One range generator, several scanner engines. Add a selection method by
subclassing MiddleMethod or Method. Add a scanner by subclassing Engine.
Both register themselves. Ranges are inclusive on both ends; an engine whose
CLI treats the end as exclusive adjusts that inside command().

Key layout, 18 hex digits, space [2^70, 2^71):

    [prefix][middle][suffix]
      40..7f   M hex    S hex
      2 hex           M + S = 16

--suffix-len (default 8) sets the split. One range is one process call.
"""

import argparse
import hashlib
import math
import os
import random
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from fractions import Fraction

FOUND_FILE = "ALLAHUAKBAR.txt"
INPUT_FILE = "p71.bin"

BIT_P71 = 71
BASE_P71 = 1 << (BIT_P71 - 1)  # 2^70
TOTAL_HEX_LEN = 18
PREFIX_HEX_LEN = 2
DEFAULT_SUFFIX_HEX_LEN = 8
DEFAULT_DONE_LEVELS = "8,7"
DEFAULT_PREFIX_SPEC = "40-7f"
DEFAULT_MIDDLE_TOTAL = 16 ** (TOTAL_HEX_LEN - PREFIX_HEX_LEN - DEFAULT_SUFFIX_HEX_LEN)

FEISTEL_ROUNDS = 6
MAX_MARKERS_PER_CHUNK = 1_000_000
CROSS_LEVEL_MAX = 65_536
HASH160_BYTES = 20
HEX_LETTERS = set("abcdef")
HEX_DIGIT_BYTES = set(b"0123456789abcdef")

METHODS = {}
ENGINES = {}


class SplitLayout:
    """Prefix / middle / suffix split derived from --suffix-len."""

    def __init__(self, suffix_len):
        max_suffix = TOTAL_HEX_LEN - PREFIX_HEX_LEN - 1
        if not 1 <= suffix_len <= max_suffix:
            raise SystemExit(f"--suffix-len must be 1..{max_suffix}, got {suffix_len}")
        self.suffix_len = suffix_len
        self.middle_hex_len = TOTAL_HEX_LEN - PREFIX_HEX_LEN - suffix_len
        self.middle_total = 16 ** self.middle_hex_len
        self.chunk_size = 16 ** suffix_len
        self.half_bits = self.middle_hex_len * 4 // 2
        self.half_mask = (1 << self.half_bits) - 1

    def middle_hex(self, middle_int):
        return f"{middle_int:0{self.middle_hex_len}x}"


class FullLayout:
    """Whole puzzle-71 space as equal chunks, numbered 1..total.

    Chunk count matches the (prefix x middle) pairs of SplitLayout. half_* is
    named like SplitLayout so permute_middle() can walk either space.
    """

    def __init__(self, suffix_len):
        max_suffix = TOTAL_HEX_LEN - PREFIX_HEX_LEN - 1
        if not 1 <= suffix_len <= max_suffix:
            raise SystemExit(f"--suffix-len must be 1..{max_suffix}, got {suffix_len}")
        self.suffix_len = suffix_len
        self.chunk_size = 16 ** suffix_len
        self.chunk_bits = (BIT_P71 - 1) - 4 * suffix_len
        if self.chunk_bits < 2:
            raise SystemExit(f"--suffix-len {suffix_len} is too large for method full")
        self.middle_total = 1 << self.chunk_bits
        self.half_bits = self.chunk_bits // 2
        self.half_mask = (1 << self.half_bits) - 1

    def range_hex(self, index):
        """1-based index -> inclusive (start, end), 18 hex digits."""
        start = BASE_P71 + (index - 1) * self.chunk_size
        return f"{start:0{TOTAL_HEX_LEN}x}", f"{start + self.chunk_size - 1:0{TOTAL_HEX_LEN}x}"

    def index_from(self, key_hex):
        """Key or chunk-start hex -> 1-based index."""
        return (int(key_hex, 16) - BASE_P71) // self.chunk_size + 1


def round_key(seed, rnd, value, half_mask):
    payload = f"{seed}:{rnd}:{value}".encode("ascii")
    return int.from_bytes(hashlib.blake2b(payload, digest_size=4).digest(), "big") & half_mask


def permute_middle(index0, seed, layout):
    """0-based row -> middle. Balanced Feistel, so the map is a bijection."""
    left = (index0 >> layout.half_bits) & layout.half_mask
    right = index0 & layout.half_mask
    for rnd in range(FEISTEL_ROUNDS):
        left, right = right, left ^ round_key(seed, rnd, right, layout.half_mask)
    return (left << layout.half_bits) | right


def unpermute_middle(middle_int, seed, layout):
    """Inverse of permute_middle. middle -> 0-based row."""
    left = (middle_int >> layout.half_bits) & layout.half_mask
    right = middle_int & layout.half_mask
    for rnd in reversed(range(FEISTEL_ROUNDS)):
        left, right = right ^ round_key(seed, rnd, left, layout.half_mask), left
    return (left << layout.half_bits) | right


def parse_prefixes(spec):
    """'40-7f' / '40,7f,7d' / '40-45,7f' -> 2-digit hex, order kept, dups dropped."""
    out = []
    for item in spec.lower().split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            lo_s, hi_s = (part.strip() for part in item.split("-", 1))
            lo, hi = int(lo_s, 16), int(hi_s, 16)
            if lo > hi:
                lo, hi = hi, lo
            values = range(lo, hi + 1)
        else:
            values = [int(item, 16)]
        for value in values:
            if not 0 <= value <= 0xFF:
                raise SystemExit(f"prefix outside 00-ff: {value:x}")
            hex_value = f"{value:02x}"
            if hex_value not in out:
                out.append(hex_value)
    if not out:
        raise SystemExit("--prefix is empty")
    return tuple(out)


def split_range(item):
    """'1-100' -> (1, 100). '-5' -> None (a signed number, not a range).

    A dash at position 0 is a sign, so '-5' and '-9--3' stay intact.
    """
    start = 1 if item.startswith("-") else 0
    pos = item.find("-", start + 1)
    if pos < 0:
        return None
    return int(item[:pos]), int(item[pos + 1:])


def wrap_index(value, total):
    """1-based index modulo total. 0, negatives, and values past total are valid."""
    return ((value - 1) % total) + 1


def parse_numbers(spec, total, flag):
    """1-based numbers modulo total. User order is kept. Used by --index and --pick-rows."""
    if total <= 0:
        raise SystemExit(f"{flag} has nothing to choose from (0 rows)")
    out, seen, wrapped = [], set(), 0
    for item in spec.split(","):
        item = item.strip().replace("_", "")
        if not item:
            continue
        span = split_range(item)
        if span is None:
            values = [int(item)]
        else:
            lo, hi = span
            if lo > hi:
                lo, hi = hi, lo
            values = range(lo, hi + 1)
        for value in values:
            folded = wrap_index(value, total)
            if folded != value:
                wrapped += 1
            if folded not in seen:
                seen.add(folded)
                out.append(folded)
    if not out:
        raise SystemExit(f"{flag} is empty")
    if wrapped:
        print(
            f"[INFO] {wrapped} numbers in {flag} were outside 1..{total:,} and wrapped.",
            file=sys.stderr,
        )
    return out


def multiplicative_order(factor, modulus):
    """Multiplicative order of factor modulo a power of two. Result is a power of two."""
    factor %= modulus
    if modulus == 1 or factor % 2 == 0:
        return None
    order = max(1, modulus // 4)
    while order > 1 and pow(factor, order // 2, modulus) == 1:
        order //= 2
    return order if pow(factor, order, modulus) == 1 else None


def series_mul(args, total):
    """v, v*f, v*f^2, ... mod total. --middle-jump is the multiplier.

    Odd powers stay odd, so even indexes are never hit. The unit group mod 2^n
    is Z/2 x Z/2^(n-2), so an orbit covers at most a quarter of the space.
    """
    factor = args.middle_jump % total
    value = wrap_index(args.start_index, total)
    if factor % 2 == 0:
        print(
            f"[WARN] --jump-mode mul needs an odd multiplier; {args.middle_jump} is even.",
            file=sys.stderr,
        )
        print(
            "       Each step adds a factor of 2, so the series collapses to 0 and stays there.",
            file=sys.stderr,
        )
    if factor in (0, 1):
        print(f"[WARN] multiplier {factor} stays on one value.", file=sys.stderr)
    orbit = multiplicative_order(factor, total // math.gcd(value, total)) if factor % 2 else None
    if orbit:
        print(
            f"[INFO] multiplicative orbit: {orbit:,} values of {total:,} "
            f"({orbit / total * 100:.4f}% of the space). "
            f"jump-mode add with an odd jump covers 100%.",
            file=sys.stderr,
        )
    indexes, seen = [], set()
    while len(indexes) < args.count:
        if value in seen:
            break
        seen.add(value)
        indexes.append(value)
        value = (value * factor) % total or total
    if len(indexes) < args.count:
        print(
            f"[WARN] series repeated, got {len(indexes):,} unique of {args.count:,} requested.",
            file=sys.stderr,
        )
    return indexes


def unique_prime_factors(number):
    out, rest, divisor = set(), number, 2
    while divisor * divisor <= rest:
        while rest % divisor == 0:
            out.add(divisor)
            rest //= divisor
        divisor += 1
    if rest > 1:
        out.add(rest)
    return out


def bit_reverse(value, bits):
    """Reverse the low `bits` bits. 0b0001 -> 0b1000 when bits=4."""
    result = 0
    for _ in range(bits):
        result = (result << 1) | (value & 1)
        value >>= 1
    return result


def series_spread(args, total):
    """Van der Corput / bit-reversal order. The most even coverage at every prefix of the run.

    The first m = 2^k terms are exactly the uniform grid of spacing total/m.
    The map is a bijection: a full run visits every chunk once.
    """
    bits = total.bit_length() - 1
    if (1 << bits) != total:
        raise SystemExit(f"--jump-mode spread needs a power-of-two total, got {total:,}")
    warn_ignored(
        [
            ("--middle-jump", args.middle_jump != 1),
            ("--jump-c", args.jump_c != 1),
        ],
        "--jump-mode spread (bit-reversal already fixes the order)",
    )
    print(
        f"[INFO] spread (bit-reversal, {bits} bits): the first 2^k chunks form a uniform grid. "
        f"Full coverage {total:,}, no repeats.",
        file=sys.stderr,
    )
    start = (args.start_index - 1) % total
    count = min(args.count, total)
    if count < args.count:
        print(
            f"[WARN] space has {total:,} chunks, got {count:,} unique of {args.count:,} "
            f"requested (the series wrapped).",
            file=sys.stderr,
        )
    return [bit_reverse((start + step) % total, bits) + 1 for step in range(count)]


def series_lcg(args, total):
    """v -> (a*v + c) mod total. Multiplicative step that can still cover 100%.

    Hull-Dobell for a full period when total = 2^n: c odd, and a % 4 == 1.
    """
    multiplier = args.middle_jump % total
    addend = args.jump_c % total
    value = (wrap_index(args.start_index, total) - 1) % total
    primes = unique_prime_factors(total)
    rules = [
        (math.gcd(addend, total) == 1, f"c={addend} must be coprime to {total:,}"),
        (
            all((multiplier - 1) % prime == 0 for prime in primes),
            f"a-1={multiplier - 1} must be divisible by every prime factor of the total "
            f"({sorted(primes)})",
        ),
        (
            total % 4 != 0 or (multiplier - 1) % 4 == 0,
            f"total is divisible by 4, so a-1={multiplier - 1} must be divisible by 4",
        ),
    ]
    full = all(ok for ok, _ in rules)
    if full:
        print(f"[INFO] LCG a={multiplier} c={addend}: full period {total:,} (100% of the space).",
              file=sys.stderr)
    else:
        print(f"[WARN] LCG a={multiplier} c={addend}: period is not full. Failed checks:",
              file=sys.stderr)
        for ok, message in rules:
            if not ok:
                print(f"       - {message}", file=sys.stderr)
        suggested = multiplier - (multiplier - 1) % 4
        if suggested % 4 != 1:
            suggested = multiplier + (1 - multiplier % 4) % 4
        odd_c = addend if addend % 2 else addend + 1
        print(
            f"       A full-period example: --middle-jump {suggested} --jump-c {odd_c}",
            file=sys.stderr,
        )
    indexes, seen = [], set()
    while len(indexes) < args.count:
        if value in seen:
            break
        seen.add(value)
        indexes.append(value + 1)
        value = (multiplier * value + addend) % total
    if len(indexes) < args.count:
        print(
            f"[WARN] series repeated, got {len(indexes):,} unique of {args.count:,} requested.",
            file=sys.stderr,
        )
    return indexes


def jump_from_percent(percent, total):
    """Percent of the space -> jump. Rounded to odd so the cycle is full.

    The percent is an exact fraction. A float would lose digits once the space
    exceeds about 2^53, and an even jump cuts the cycle down.
    """
    try:
        frac = Fraction(str(percent).strip())
    except (ValueError, ZeroDivisionError):
        raise SystemExit(f"--jump-percent is not a number: {percent!r}")
    if not 0 < frac <= 100:
        raise SystemExit("--jump-percent must be greater than 0 and at most 100")
    jump = max(1, round(frac * total / 100))
    if jump % 2 == 0:
        jump += 1
    return jump


def expand_neighbors(indexes, plus, minus, total):
    """After each point, also visit +d and -d. Order kept, duplicates dropped, modulo total."""
    out, seen = [], set()
    for base in indexes:
        candidates = [base]
        candidates += [((base - 1 + delta) % total) + 1 for delta in plus]
        candidates += [((base - 1 - delta) % total) + 1 for delta in minus]
        for item in candidates:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def parse_offsets(spec, flag):
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        number = int(item)
        if number <= 0:
            raise SystemExit(f"{flag} must be > 0, got {number}")
        if number not in out:
            out.append(number)
    if not out:
        raise SystemExit(f"{flag} is empty")
    return out


def apply_neighbors(args, indexes, total):
    """Attach --near-plus / --near-minus and report how much the list grew."""
    if not (args.near_plus or args.near_minus):
        return indexes
    plus = parse_offsets(args.near_plus, "--near-plus") if args.near_plus else []
    minus = parse_offsets(args.near_minus, "--near-minus") if args.near_minus else []
    expanded = expand_neighbors(indexes, plus, minus, total)
    growth = len(expanded) / len(indexes) if indexes else 0
    parts = []
    if plus:
        parts.append("+" + ",".join(str(item) for item in plus))
    if minus:
        parts.append("-" + ",".join(str(item) for item in minus))
    print(
        f"[INFO] neighbors {' '.join(parts)}: {len(indexes):,} points -> "
        f"{len(expanded):,} ({growth:.1f}x).",
        file=sys.stderr,
    )
    if growth < 1.5 and len(plus) + len(minus) > 1:
        print(
            "[INFO] little growth: the points were already close, so many neighbors collided.",
            file=sys.stderr,
        )
    if args.shuffle:
        print("[WARN] --near-plus / --near-minus act on ROW NUMBERS, then --shuffle", file=sys.stderr)
        print("       scatters those rows across the keyspace. The neighbors are then", file=sys.stderr)
        print("       not adjacent keys. Drop --shuffle to sweep a real neighborhood.", file=sys.stderr)
    return expanded


def cycle_length(jump, total):
    """Unique rows visited before the additive walk repeats.

    total = 16**n = 2^(4n). An odd jump has gcd 1 and a full cycle.
    Each factor of 2 in the jump cuts the cycle in half.
    """
    jump %= total
    if jump == 0:
        return 1
    return total // math.gcd(jump, total)


def index_series(args, total):
    """Rows from --start-index, --count, and --middle-jump. Wraps modulo total.

    Shared by method index (middle rows) and method full (chunk numbers).
    """
    if args.count <= 0:
        raise SystemExit("--count must be > 0")
    if args.jump_percent:
        args.middle_jump = jump_from_percent(args.jump_percent, total)
        print(
            f"[INFO] --jump-percent {args.jump_percent}% of {total:,} "
            f"-> --middle-jump {args.middle_jump:,} (rounded to odd, exact fraction).",
            file=sys.stderr,
        )
    if args.jump_mode == "mul":
        return apply_neighbors(args, series_mul(args, total), total)
    if args.jump_mode == "lcg":
        return apply_neighbors(args, series_lcg(args, total), total)
    if args.jump_mode == "spread":
        return apply_neighbors(args, series_spread(args, total), total)

    cycle = cycle_length(args.middle_jump, total)
    start0 = (args.start_index - 1) % total
    jump = args.middle_jump % total
    indexes, seen = [], set()
    for step in range(args.count):
        idx = ((start0 + step * jump) % total) + 1
        if idx in seen:
            continue
        seen.add(idx)
        indexes.append(idx)
    if args.start_index != wrap_index(args.start_index, total):
        print(
            f"[INFO] --start-index {args.start_index} wrapped (mod {total:,}) to row {indexes[0]:,}.",
            file=sys.stderr,
        )
    if len(indexes) < args.count:
        print(
            f"[WARN] jump cycle is only {cycle:,} rows, got {len(indexes):,} unique "
            f"of {args.count:,} requested.",
            file=sys.stderr,
        )
        if jump == 0:
            print("       --middle-jump is a multiple of the total, so the walk stays put.",
                  file=sys.stderr)
        else:
            print("       Use an ODD --middle-jump for a full cycle (no repeats).",
                  file=sys.stderr)
    return apply_neighbors(args, indexes, total)


def parse_slots(spec, prefix_count):
    """'1,7,21,55' / '1-10' -> 1-based positions in the prefix order. User order kept."""
    out, seen = [], set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            lo_s, hi_s = (part.strip() for part in item.split("-", 1))
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                lo, hi = hi, lo
            values = range(lo, hi + 1)
        else:
            values = [int(item)]
        for value in values:
            if not 1 <= value <= prefix_count:
                raise SystemExit(
                    f"--prefix-slot must be 1..{prefix_count} (active prefixes), got {value}"
                )
            if value not in seen:
                seen.add(value)
                out.append(value)
    if not out:
        raise SystemExit("--prefix-slot is empty")
    return out


def parse_done_levels(spec, layout):
    """Marker levels to write. The scan level is always included, for resume. Descending."""
    levels = set()
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        level = int(item)
        if not 1 <= level <= layout.suffix_len:
            raise SystemExit(
                f"--done-levels {level} is invalid: must be 1..{layout.suffix_len} "
                f"(a level above --suffix-len would claim a wider scan than the one that ran)"
            )
        levels.add(level)
    levels.add(layout.suffix_len)
    for level in levels:
        count = 16 ** (layout.suffix_len - level)
        if count > MAX_MARKERS_PER_CHUNK:
            raise SystemExit(
                f"level {level} writes {count:,} markers per chunk "
                f"(limit {MAX_MARKERS_PER_CHUNK:,}). "
                f"Drop that level from --done-levels or lower --suffix-len."
            )
    return sorted(levels, reverse=True)


def chunk_markers(chunk_start_int, level, layout):
    """One suffix-S chunk -> 16**(S-level) markers at that level, all fully covered."""
    step = 16 ** level
    count = 16 ** (layout.suffix_len - level)
    return [f"{chunk_start_int + i * step:0{TOTAL_HEX_LEN}x}" for i in range(count)]


def middles_from_index(indexes, layout):
    """1-based index -> middle hex. Row 1 is the smallest middle."""
    for index in indexes:
        yield f"#{index}", layout.middle_hex(index - 1), ""


def apply_shuffle(middles, seed, layout):
    """Feistel-permute every middle. Bijective on the middle space, so uniques stay unique."""
    out = []
    for label, middle, detail in middles:
        shuffled = permute_middle(int(middle, 16), seed, layout)
        out.append((f"{label}~shuffle{seed}", layout.middle_hex(shuffled), detail))
    return out


def parse_byte_list(spec, count, flag):
    """'34,219,106,44' -> [34, 219, 106, 44]. A single number is reused for every byte."""
    values = [int(item) for item in spec.split(",") if item.strip()]
    if len(values) == 1:
        values = values * count
    if len(values) != count:
        raise SystemExit(
            f"--{flag} needs {count} numbers (middle is {count} bytes at this --suffix-len), "
            f"got {len(values)}"
        )
    for value in values:
        if not 0 <= value <= 255:
            raise SystemExit(f"--{flag} values must be 0..255, got {value}")
    return values


def middles_from_bytes(start_bytes, step, op, count, layout):
    """Walk each middle byte mod 256.

    add: b <- (b + step) mod 256. Odd step cycles through all 256 values.
    mul: b <- (b * step) mod 256. Only an odd multiplier is bijective.
         An even multiplier collapses every byte to 0 within 8 steps.
    Stops when a middle repeats.
    """
    current = list(start_bytes)
    seen = set()
    produced = 0
    while produced < count:
        middle = "".join(f"{value:02x}" for value in current)[:layout.middle_hex_len]
        if middle in seen:
            break
        seen.add(middle)
        produced += 1
        yield f"bytes#{produced}", middle, ", ".join(str(value) for value in current)
        if op == "mul":
            current = [(value * delta) % 256 for value, delta in zip(current, step)]
        else:
            current = [(value + delta) % 256 for value, delta in zip(current, step)]


def middles_from_seed(seed, count, layout):
    """Same draw as gen_rand_255.py: Random(seed).randrange(256) per middle byte."""
    rng = random.Random(seed)
    nbytes = (layout.middle_hex_len + 1) // 2
    for number in range(1, count + 1):
        values = [rng.randrange(256) for _ in range(nbytes)]
        middle = "".join(f"{value:02x}" for value in values)[:layout.middle_hex_len]
        yield f"seed{seed}#{number}", middle, ", ".join(str(value) for value in values)


def shuffle_prefixes(prefixes, seed, nonce=""):
    """Shuffle prefix order. The seed stream is separate from the middle permutation."""
    return tuple(random.Random(f"{seed}:prefix:{nonce}").sample(list(prefixes), len(prefixes)))


def build_ranges(middles, prefixes, layout, reshuffle_seed=None, slots=None, interleave=False):
    """One middle -> one suffix-S range per selected prefix.

    slots are 1-based positions in the prefix order. After a prefix shuffle the
    same slot hits a different prefix, so a long run samples all 64.
    """
    base = tuple(prefixes)
    per_middle = []
    for label, middle in middles:
        order = shuffle_prefixes(base, reshuffle_seed, middle) if reshuffle_seed is not None else base
        chosen = [(slot, order[slot - 1]) for slot in slots] if slots else list(enumerate(order, 1))
        per_middle.append((label, middle, chosen))

    def one(label, middle, slot, prefix):
        start = f"{prefix}{middle}{'0' * layout.suffix_len}"
        end = f"{prefix}{middle}{'f' * layout.suffix_len}"
        return (f"{label} slot#{slot}", start, end)

    if not interleave:
        return [
            one(label, middle, slot, prefix)
            for label, middle, chosen in per_middle
            for slot, prefix in chosen
        ]

    # Latin-square walk: step (r, mi) takes prefix (r*a + mi) mod P.
    # gcd(a, P) = 1 makes each middle visit every prefix once as r runs.
    # a == M-1 (mod P) would repeat a prefix on the round boundary, so that a is skipped.
    ranges = []
    middle_count = len(per_middle)
    rounds = max((len(chosen) for _, _, chosen in per_middle), default=0)
    stride = 1
    while rounds > 1 and (
        math.gcd(stride, rounds) != 1 or stride % rounds == (middle_count - 1) % rounds
    ):
        stride += 1
    for round_index in range(rounds):
        for middle_index in range(middle_count):
            label, middle, chosen = per_middle[middle_index]
            slot, prefix = chosen[(round_index * stride + middle_index) % len(chosen)]
            ranges.append(one(label, middle, slot, prefix))
    return ranges


def build_full_ranges(indexes, layout, shuffle_seed=None):
    """Method full: one index -> one range. No prefix, no middle."""
    out = []
    for index in indexes:
        if shuffle_seed is None:
            chunk, label = index, f"#{index}"
        else:
            chunk = permute_middle(index - 1, shuffle_seed, layout) + 1
            label = f"#{index}~shuffle{shuffle_seed}"
        start, end = layout.range_hex(chunk)
        out.append((label, start, end))
    return out


def max_run(text, pred):
    best = run = 0
    for char in text:
        run = run + 1 if pred(char) else 0
        best = max(best, run)
    return best


def max_repeat_run(text):
    best = run = 1
    for index in range(1, len(text)):
        run = run + 1 if text[index] == text[index - 1] else 1
        best = max(best, run)
    return best if text else 0


def parse_contains(spec, width, flag):
    """'a4bd,a4c8' -> [Counter('a4bd'), Counter('a4c8')]. Commas are OR.

    Counts, not a set: 'aa' requires at least two 'a' characters, in any position.
    """
    groups = []
    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            continue
        bad = [char for char in part if char not in "0123456789abcdef"]
        if bad:
            raise SystemExit(f"{flag}: not hex: {''.join(sorted(set(bad)))}")
        if len(part) > width:
            raise SystemExit(
                f"{flag}: '{part}' needs {len(part)} chars but the field is {width} chars"
            )
        groups.append(Counter(part))
    if not groups:
        raise SystemExit(f"{flag} is empty")
    return groups


def matches_contains(text, groups):
    """True when the text meets at least one group."""
    counts = Counter(text)
    return any(all(counts[char] >= need for char, need in group.items()) for group in groups)


def expected_pass_rate(groups, width, samples=40000, seed=9):
    """Rough fraction of random fields that pass the groups. A comparison only."""
    if not groups:
        return None
    rng = random.Random(seed)
    hits = 0
    alphabet = "0123456789abcdef"
    for _ in range(samples):
        text = "".join(rng.choice(alphabet) for _ in range(width))
        if matches_contains(text, groups):
            hits += 1
    return hits / samples


def chi_square_hex(text):
    """Chi-square of the hex composition. Small means the characters are even."""
    counts = Counter(text)
    expected = len(text) / 16
    return sum((counts.get(f"{i:x}", 0) - expected) ** 2 / expected for i in range(16))


def filter_contains(ranges, layout, prefix_groups, middle_groups, chi_max):
    """Keep ranges by prefix contents, middle contents, and composition balance.

    Prefix is the first 2 hex of the fixed part. Middle is the rest of the fixed
    part. The suffix is the wildcard the scanner walks, so it is not tested.
    """
    if not (prefix_groups or middle_groups or chi_max):
        return ranges, 0
    fixed = TOTAL_HEX_LEN - layout.suffix_len
    kept = []
    for label, start, end in ranges:
        prefix, middle = start[:PREFIX_HEX_LEN], start[PREFIX_HEX_LEN:fixed]
        if prefix_groups and not matches_contains(prefix, prefix_groups):
            continue
        if middle_groups and not matches_contains(middle, middle_groups):
            continue
        if chi_max and chi_square_hex(start[:fixed]) > chi_max:
            continue
        kept.append((label, start, end))
    return kept, len(ranges) - len(kept)


def filter_runs(ranges, layout, max_letters, max_digits, max_same):
    """Drop ranges whose fixed part has a long letter run, digit run, or repeat run.

    Only prefix+middle is measured. The suffix is the wildcard (00000000..ffffffff).
    """
    if not any((max_letters, max_digits, max_same)):
        return ranges, 0
    fixed = TOTAL_HEX_LEN - layout.suffix_len
    kept = []
    for label, start, end in ranges:
        text = start[:fixed]
        if max_letters and max_run(text, lambda char: char in HEX_LETTERS) > max_letters:
            continue
        if max_digits and max_run(text, lambda char: char not in HEX_LETTERS) > max_digits:
            continue
        if max_same and max_repeat_run(text) > max_same:
            continue
        kept.append((label, start, end))
    return kept, len(ranges) - len(kept)


def warn_ignored(pairs, context):
    """Report flags that do nothing on this path, so a silent no-op is visible."""
    for name, active in pairs:
        if active:
            print(f"[WARN] {name} does not apply to {context}; ignored.", file=sys.stderr)


def resolve_seed(args, reason):
    """Seed shared by --shuffle, method seed, and --shuffle-prefix. Printed once if random."""
    if args.seed is None:
        args.seed = random.randrange(2 ** 32)
        print(
            f"[INFO] seed was not set ({reason}); using --seed {args.seed} to repeat this run.",
            file=sys.stderr,
        )
    return args.seed


class DoneLog:
    """Per-engine done markers. KeyHunt and ecloop files stay separate."""

    def __init__(self, template):
        self.template = template

    def path(self, level):
        return self.template.format(level=level)

    def load(self, levels):
        dones = {}
        for level in levels:
            path = self.path(level)
            if os.path.exists(path):
                with open(path, "r") as handle:
                    dones[level] = set(handle.read().splitlines())
            else:
                dones[level] = set()
        return dones

    def already(self, start_hex, layout, dones):
        """Done when the scan-level marker exists, or every finer child marker exists.

        The cross-level check is what keeps a finished --suffix-len 8 run valid
        after switching to --suffix-len 10.
        """
        if start_hex in dones.get(layout.suffix_len, ()):
            return True
        start_int = int(start_hex, 16)
        for level, marks in dones.items():
            if level >= layout.suffix_len or not marks:
                continue
            count = 16 ** (layout.suffix_len - level)
            if count > CROSS_LEVEL_MAX:
                continue
            step = 16 ** level
            if all(f"{start_int + i * step:0{TOTAL_HEX_LEN}x}" in marks for i in range(count)):
                return True
        return False

    def mark(self, start_hex, layout, levels, dones):
        start_int = int(start_hex, 16)
        for level in levels:
            markers = chunk_markers(start_int, level, layout)
            with open(self.path(level), "a") as handle:
                handle.write("\n".join(markers) + "\n")
            if level in dones:
                dones[level].update(markers)


def found():
    return os.path.exists(FOUND_FILE) and os.path.getsize(FOUND_FILE) > 0


class Method:
    """A way to choose ranges. Set `name` and the class registers itself.

    layout_class is SplitLayout (prefix/middle) or FullLayout (flat chunks).
    Middle methods yield middles; the shared pipeline expands prefixes.
    A flat method implements ranges() and is not a MiddleMethod.
    """

    name = ""
    layout_class = SplitLayout

    def layout_for(self, args):
        return self.layout_class(args.suffix_len)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            return
        if cls.name in METHODS:
            raise RuntimeError(f"duplicate method name: {cls.name}")
        METHODS[cls.name] = cls()


class MiddleMethod(Method):
    """Yield (label, middle_hex, detail). Prefix expansion happens afterwards.

    uses_index_series: generate() consumes --index / --start-index / --middle-jump.
    uses_byte_walk: generate() consumes --bytes / --byte-op / --byte-step.
    Flags the method does not read are reported and ignored.
    """

    uses_index_series = False
    uses_byte_walk = False

    def generate(self, args, layout):
        raise NotImplementedError(self.name)


class IndexMethod(MiddleMethod):
    name = "index"
    uses_index_series = True

    def generate(self, args, layout):
        if args.index:
            indexes = parse_numbers(args.index, layout.middle_total, "--index")
        else:
            indexes = index_series(args, layout.middle_total)
        return list(middles_from_index(indexes, layout))


class SeedMethod(MiddleMethod):
    name = "seed"

    def generate(self, args, layout):
        return list(middles_from_seed(resolve_seed(args, "method seed"), args.count, layout))


class BytesMethod(MiddleMethod):
    name = "bytes"
    uses_byte_walk = True

    def generate(self, args, layout):
        count_bytes = (layout.middle_hex_len + 1) // 2
        start = parse_byte_list(args.bytes, count_bytes, "bytes")
        step = parse_byte_list(args.byte_step, count_bytes, "byte-step")
        if args.byte_op == "mul":
            even = [item for item in step if item % 2 == 0]
            if even:
                print(
                    f"[WARN] --byte-op mul with even multiplier {even}: each byte collapses "
                    f"to 0 within 8 steps and then stays there (2**8 = 0 mod 256).",
                    file=sys.stderr,
                )
                print("       Use an odd multiplier (3, 5, 7, ...) to actually cycle.",
                      file=sys.stderr)
        out = list(middles_from_bytes(start, step, args.byte_op, args.count, layout))
        if len(out) < args.count:
            print(
                f"[WARN] middles started repeating, got {len(out)} unique of {args.count} "
                f"requested. The cycle ended.",
                file=sys.stderr,
            )
            if args.byte_op == "add":
                print(
                    "       Per-byte arithmetic mod 256 tops out at 256 unique middles "
                    "(no carry between bytes). Use method index or full with --middle-jump "
                    "for a wider walk.",
                    file=sys.stderr,
                )
        return out


KEYSPACE_P71 = 1 << (BIT_P71 - 1)  # 2^70 keys, [2^70, 2^71)


def parse_percents(spec):
    """Comma-separated exact fractions, each from 0 to 100 inclusive.

    Kept as Fraction so a long decimal such as 0.328746234343432 is not rounded
    to a float before it is applied to the 2^70 keyspace.
    """
    if not str(spec).strip():
        raise SystemExit(
            "method percent needs --percent, for example --percent 0.328746234343432"
        )
    out = []
    for item in str(spec).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            frac = Fraction(item)
        except (ValueError, ZeroDivisionError):
            raise SystemExit(f"--percent is not a number: {item!r}")
        if not 0 <= frac <= 100:
            raise SystemExit(f"--percent must be from 0 to 100, got {item}")
        out.append((item, frac))
    if not out:
        raise SystemExit("--percent is empty")
    return out


def percent_index(frac, total):
    """1-based index of the unit that starts at this percent of `total` units.

    0% is the first unit. 100% is the last unit. An exact midpoint lands on the
    first unit of the upper half (floor), which is the key at that fraction.
    """
    if total <= 0:
        raise SystemExit("percent space is empty")
    if frac <= 0:
        return 1
    if frac >= 100:
        return total
    index0 = int(frac * total / 100)
    if index0 >= total:
        index0 = total - 1
    return index0 + 1


def percent_key(frac):
    """Absolute key at this percent of the puzzle-71 keyspace [2^70, 2^71)."""
    if frac <= 0:
        return BASE_P71
    if frac >= 100:
        return BASE_P71 + KEYSPACE_P71 - 1
    offset = int(frac * KEYSPACE_P71 / 100)
    if offset >= KEYSPACE_P71:
        offset = KEYSPACE_P71 - 1
    return BASE_P71 + offset


def walk_indexes(starts, count, jump, total):
    """From each 1-based start, take `count` steps of `jump`, wrapping modulo total."""
    if count <= 0:
        raise SystemExit("--count must be > 0")
    stride = jump % total
    out, seen = [], set()
    for start in starts:
        for step in range(count):
            idx = ((start - 1 + step * stride) % total) + 1
            if idx not in seen:
                seen.add(idx)
                out.append(idx)
    if stride == 0 and count > 1:
        print(
            "[WARN] --middle-jump is a multiple of the percent space, "
            "so extra steps stay on the percent point.",
            file=sys.stderr,
        )
    elif len(out) < len(starts) * count:
        print(
            f"[WARN] walk repeated, got {len(out):,} unique of {len(starts) * count:,} requested.",
            file=sys.stderr,
        )
    return out


class FullMethod(Method):
    """Flat numbering of [2^70, 2^71). No prefix split."""

    name = "full"
    layout_class = FullLayout

    def ignored(self, args):
        return [
            ("--prefix", args.prefix != DEFAULT_PREFIX_SPEC),
            ("--prefix-slot", bool(args.prefix_slot)),
            ("--shuffle-prefix", args.shuffle_prefix),
            ("--shuffle-prefix-each", args.shuffle_prefix_each),
            ("--interleave", args.interleave),
            ("--bytes", args.bytes != "0,0,0,0"),
            ("--byte-op", args.byte_op != "add"),
            ("--byte-step", args.byte_step != "1"),
        ]

    def generate(self, args, layout):
        if args.index:
            indexes = parse_numbers(args.index, layout.middle_total, "--index")
        else:
            indexes = index_series(args, layout.middle_total)
        shuffle_seed = resolve_seed(args, "--shuffle") if args.shuffle else None
        return build_full_ranges(indexes, layout, shuffle_seed)


class PercentMethod(Method):
    """Land on a percent of a chosen space, then walk --count steps from there.

    --percent-of selects the denominator:
      full           all 2^70 keys. 50% is the numerical midpoint, key 0x60...
      prefix+middle  one (middle, prefix) pair in scan order. The denominator is
                     the active prefix list times the middle count, not 2^70.
      middle         middle rows only. The hit middle is then expanded to prefixes.
    Default is full, so a percent is a fraction of every key.

    --shuffle permutes each landed unit after the percent is taken, the same
    Feistel map as method index and method full. The percent itself is read in
    natural order. On prefix+middle the middle is permuted and the prefix slot
    stays put. --count steps in that natural numbering, then each step is mapped.
    """

    name = "percent"

    def layout_for(self, args):
        if getattr(args, "percent_of", "full") == "full":
            return FullLayout(args.suffix_len)
        return SplitLayout(args.suffix_len)

    def generate(self, args, layout):
        percents = parse_percents(args.percent)
        base = args.percent_of
        self._warn_unused(args, base)
        shuffle_seed = resolve_seed(args, "--shuffle") if args.shuffle else None
        if base == "full":
            return self._ranges_full(args, layout, percents, shuffle_seed)
        prefixes, reshuffle = self._prefixes(args, pair_space=(base == "prefix+middle"))
        if base == "middle":
            return self._ranges_middle(args, layout, percents, prefixes, reshuffle, shuffle_seed)
        return self._ranges_pairs(args, layout, percents, prefixes, shuffle_seed)

    def _warn_unused(self, args, base):
        common = [
            ("--index", bool(args.index)),
            ("--start-index", args.start_index != 1),
            ("--jump-percent", bool(args.jump_percent)),
            ("--jump-mode", args.jump_mode != "add"),
            ("--jump-c", args.jump_c != 1),
            ("--bytes", args.bytes != "0,0,0,0"),
            ("--byte-op", args.byte_op != "add"),
            ("--byte-step", args.byte_step != "1"),
        ]
        if base == "full":
            common += [
                ("--prefix", args.prefix != DEFAULT_PREFIX_SPEC),
                ("--prefix-slot", bool(args.prefix_slot)),
                ("--shuffle-prefix", args.shuffle_prefix),
                ("--shuffle-prefix-each", args.shuffle_prefix_each),
                ("--interleave", args.interleave),
            ]
        else:
            common.append(("--interleave", args.interleave))
        warn_ignored(common, f"method percent --percent-of {base}")

    def _prefixes(self, args, pair_space):
        prefixes = parse_prefixes(args.prefix)
        if args.shuffle_prefix_each and not args.shuffle_prefix:
            raise SystemExit("--shuffle-prefix-each requires --shuffle-prefix")
        if pair_space and args.shuffle_prefix_each:
            raise SystemExit(
                "--shuffle-prefix-each does not apply to --percent-of prefix+middle "
                "(the percent space needs one fixed prefix list)"
            )
        prefix_seed = resolve_seed(args, "--shuffle-prefix") if args.shuffle_prefix else None
        if args.shuffle_prefix and not args.shuffle_prefix_each:
            prefixes = shuffle_prefixes(prefixes, prefix_seed)
        if args.prefix_slot:
            slots = parse_slots(args.prefix_slot, len(prefixes))
            prefixes = tuple(prefixes[slot - 1] for slot in slots)
        reshuffle = prefix_seed if (args.shuffle_prefix_each and not pair_space) else None
        return prefixes, reshuffle

    def _ranges_full(self, args, layout, percents, shuffle_seed):
        starts = []
        for text, frac in percents:
            key = percent_key(frac)
            chunk = layout.index_from(f"{key:0{TOTAL_HEX_LEN}x}")
            mapped = chunk
            note = ""
            if shuffle_seed is not None:
                mapped = permute_middle(chunk - 1, shuffle_seed, layout) + 1
                note = f" -> shuffle {shuffle_seed} chunk #{mapped:,}"
            start_hex, end_hex = layout.range_hex(mapped)
            print(
                f"[INFO] {text}% of full keyspace ({KEYSPACE_P71:,} keys, 2^{BIT_P71 - 1}) "
                f"-> key {key:0{TOTAL_HEX_LEN}x} -> chunk #{chunk:,}"
                f"{note} range {start_hex}:{end_hex}",
                file=sys.stderr,
            )
            starts.append(chunk)
        indexes = apply_neighbors(
            args, walk_indexes(starts, args.count, args.middle_jump, layout.middle_total),
            layout.middle_total,
        )
        return build_full_ranges(indexes, layout, shuffle_seed)

    def _ranges_middle(self, args, layout, percents, prefixes, reshuffle, shuffle_seed):
        starts = []
        for text, frac in percents:
            row = percent_index(frac, layout.middle_total)
            note = ""
            if shuffle_seed is not None:
                mapped = layout.middle_hex(permute_middle(row - 1, shuffle_seed, layout))
                note = f" -> shuffle {shuffle_seed} middle {mapped}"
            print(
                f"[INFO] {text}% of middle ({layout.middle_total:,} rows) -> middle row #{row:,} "
                f"({layout.middle_hex(row - 1)}){note}, then {len(prefixes)} prefix(es)",
                file=sys.stderr,
            )
            starts.append(row)
        indexes = apply_neighbors(
            args, walk_indexes(starts, args.count, args.middle_jump, layout.middle_total),
            layout.middle_total,
        )
        middles = list(middles_from_index(indexes, layout))
        if shuffle_seed is not None:
            middles = apply_shuffle(middles, shuffle_seed, layout)
        middles = [(label, middle) for label, middle, _ in middles]
        return build_ranges(middles, prefixes, layout, reshuffle)

    def _ranges_pairs(self, args, layout, percents, prefixes, shuffle_seed):
        width = len(prefixes)
        space = width * layout.middle_total
        starts = []
        for text, frac in percents:
            pair = percent_index(frac, space)
            middle0, slot0 = divmod(pair - 1, width)
            note = ""
            if shuffle_seed is not None:
                mapped = layout.middle_hex(permute_middle(middle0, shuffle_seed, layout))
                note = f" -> shuffle {shuffle_seed} middle {mapped}"
            print(
                f"[INFO] {text}% of prefix+middle ({space:,} pairs = {width} prefix × "
                f"{layout.middle_total:,} middle) -> pair #{pair:,} "
                f"prefix {prefixes[slot0]} middle {layout.middle_hex(middle0)}{note}",
                file=sys.stderr,
            )
            starts.append(pair)
        indexes = apply_neighbors(
            args, walk_indexes(starts, args.count, args.middle_jump, space), space,
        )
        ranges = []
        for pair in indexes:
            middle0, slot0 = divmod(pair - 1, width)
            prefix = prefixes[slot0]
            middle_int = middle0
            tag = f"#{pair}"
            if shuffle_seed is not None:
                middle_int = permute_middle(middle0, shuffle_seed, layout)
                tag = f"#{pair}~shuffle{shuffle_seed}"
            middle = layout.middle_hex(middle_int)
            start = f"{prefix}{middle}{'0' * layout.suffix_len}"
            end = f"{prefix}{middle}{'f' * layout.suffix_len}"
            ranges.append((f"{tag} {prefix}+{middle}", start, end))
        return ranges


def resolve_middles(args, layout, method):
    if not method.uses_byte_walk:
        warn_ignored(
            [
                ("--bytes", args.bytes != "0,0,0,0"),
                ("--byte-op", args.byte_op != "add"),
                ("--byte-step", args.byte_step != "1"),
            ],
            f"method {args.method}",
        )
    if not method.uses_index_series:
        if args.count <= 0:
            raise SystemExit("--count must be > 0")
        warn_ignored(
            [
                ("--index", bool(args.index)),
                ("--start-index", args.start_index != 1),
                ("--middle-jump", args.middle_jump != 1),
                ("--jump-mode", args.jump_mode != "add"),
                ("--jump-c", args.jump_c != 1),
                ("--jump-percent", bool(args.jump_percent)),
                ("--near-plus", bool(args.near_plus)),
                ("--near-minus", bool(args.near_minus)),
            ],
            f"method {args.method} (middles are not row numbers, so there is nothing to jump)",
        )
    middles = method.generate(args, layout)
    if args.shuffle:
        seed = resolve_seed(args, "--shuffle")
        if args.method == "seed":
            print(
                "[INFO] method seed already draws random middles; --shuffle only remaps them. "
                "The result stays random (harmless, and it adds nothing).",
                file=sys.stderr,
            )
        raw = middles
        middles = apply_shuffle(middles, seed, layout)
        for (_, source, detail), (_, shuffled, _) in zip(raw, middles):
            if args.method != "index":
                print(f"[{args.method}] {detail} -> {source} -> shuffle {shuffled}", file=sys.stderr)
    elif args.method != "index":
        for _, middle, detail in middles:
            print(f"[{args.method}] {detail} -> {middle}", file=sys.stderr)
    return [(label, middle) for label, middle, _ in middles]


def build_middle_ranges(args, layout, method):
    prefixes = parse_prefixes(args.prefix)
    if args.shuffle_prefix_each and not args.shuffle_prefix:
        raise SystemExit("--shuffle-prefix-each requires --shuffle-prefix")
    prefix_seed = resolve_seed(args, "--shuffle-prefix") if args.shuffle_prefix else None
    if args.shuffle_prefix and not args.shuffle_prefix_each:
        prefixes = shuffle_prefixes(prefixes, prefix_seed)
    slots = parse_slots(args.prefix_slot, len(prefixes)) if args.prefix_slot else None
    middles = resolve_middles(args, layout, method)
    ranges = build_ranges(
        middles,
        prefixes,
        layout,
        prefix_seed if args.shuffle_prefix_each else None,
        slots,
        args.interleave,
    )
    return ranges, middles, prefixes, slots


def finalize_ranges(args, ranges, layout):
    """Content filters, run-length filters, then --pick-rows. Prints what was dropped."""
    middle_width = TOTAL_HEX_LEN - layout.suffix_len - PREFIX_HEX_LEN
    prefix_groups = (
        parse_contains(args.prefix_contains, PREFIX_HEX_LEN, "--prefix-contains")
        if args.prefix_contains else None
    )
    middle_groups = (
        parse_contains(args.middle_contains, middle_width, "--middle-contains")
        if args.middle_contains else None
    )
    before = len(ranges)
    ranges, removed_contents = filter_contains(
        ranges, layout, prefix_groups, middle_groups, args.max_chi2,
    )
    if prefix_groups or middle_groups:
        prefix_rate = expected_pass_rate(prefix_groups, PREFIX_HEX_LEN)
        middle_rate = expected_pass_rate(middle_groups, middle_width)
        expected = (prefix_rate if prefix_rate is not None else 1) * (
            middle_rate if middle_rate is not None else 1
        )
        actual = len(ranges) / before if before else 0
        print(
            f"[INFO] content filter: kept {actual * 100:.1f}% "
            f"(a random field would keep about {expected * 100:.1f}%).",
            file=sys.stderr,
        )
        if expected and actual < expected * 0.5:
            print("[WARN] pass rate is far below that estimate. The scan order makes the",
                  file=sys.stderr)
            print("       fixed field uniform: --jump-mode spread starts at middle 00000000,",
                  file=sys.stderr)
            print("       and a small --start-index without --shuffle does too. Add --shuffle",
                  file=sys.stderr)
            print("       or advance --start-index so the fixed field varies.",
                  file=sys.stderr)
    if removed_contents:
        print(
            f"[INFO] content/composition filter dropped {removed_contents:,} of {before:,} "
            f"ranges ({len(ranges):,} left).",
            file=sys.stderr,
        )
    ranges, removed_runs = filter_runs(
        ranges, layout, args.max_letters, args.max_digits, args.max_repeat,
    )
    if removed_runs:
        print(
            f"[INFO] run filter dropped {removed_runs:,} ranges ({len(ranges):,} left).",
            file=sys.stderr,
        )
    if (removed_runs or removed_contents) and not ranges:
        print(
            "[WARN] filters removed EVERY range. Relax --max-* or change --start-index; "
            "natural order starts at 00000000, which is a long run.",
            file=sys.stderr,
        )
    generated = len(ranges)
    if args.pick_rows:
        chosen = parse_numbers(args.pick_rows, generated, "--pick-rows")
        ranges = [ranges[index - 1] for index in chosen]
    return ranges, generated


def jump_info(args, layout, method_name):
    if args.jump_mode == "spread":
        return "spread (bit-reversal)"
    if args.jump_mode == "lcg":
        return f"x{args.middle_jump}+{args.jump_c} (lcg)"
    if args.jump_mode == "mul":
        return f"x{args.middle_jump} (mul)"
    if method_name == "full" or (
        method_name == "index" and not args.index and args.middle_jump != 1
    ):
        cycle = cycle_length(args.middle_jump, layout.middle_total)
        if method_name == "full":
            note = " (full cycle)" if cycle == layout.middle_total else f" (cycle {cycle:,})"
            return f"{args.middle_jump}{note}"
        note = " full" if cycle == layout.middle_total else ""
        return f"{args.middle_jump} (cycle {cycle:,}{note})"
    return str(args.middle_jump)


def print_middle_plan(args, layout, levels, ranges, generated, middles, prefixes, slots):
    total_keys = len(ranges) * layout.chunk_size
    order = f"shuffle(seed {args.seed})" if args.shuffle else "natural"
    if args.interleave:
        order = f"{order} +interleave"
    if args.shuffle_prefix_each:
        prefix_order = f"shuffle-each-middle(seed {args.seed})"
    elif args.shuffle_prefix:
        shown = ",".join(prefixes[:6])
        if len(prefixes) > 6:
            shown += "..."
        prefix_order = f"shuffle(seed {args.seed}): {shown}"
    else:
        prefix_order = "natural"
    if slots:
        used = f"{len(slots)}/{len(prefixes)} slots {','.join(str(item) for item in slots[:8])}"
        if len(slots) > 8:
            used += "..."
    else:
        used = f"{len(prefixes)} (all)"
    picked = f" (picked from {generated:,} generated)" if args.pick_rows else ""
    print(
        f"[INFO] engine={args.engine} suffix-len={layout.suffix_len} "
        f"middle-hex={layout.middle_hex_len} total-middle={layout.middle_total:,} "
        f"key/range={layout.chunk_size:,}",
        file=sys.stderr,
    )
    print(
        f"[INFO] method={args.method} order={order} prefix-order={prefix_order} "
        f"middles={len(middles)} jump={jump_info(args, layout, args.method)} "
        f"prefixes={used} ranges={len(ranges)}{picked} "
        f"keys={total_keys:,} (2^{total_keys.bit_length() - 1})",
        file=sys.stderr,
    )
    markers = ", ".join(
        f"L{level}={16 ** (layout.suffix_len - level):,}" for level in levels
    )
    print(f"[INFO] done-levels: {markers} markers per chunk", file=sys.stderr)


def print_full_plan(args, layout, levels, ranges, generated):
    total_keys = len(ranges) * layout.chunk_size
    order = f"shuffle(seed {args.seed})" if args.shuffle else "natural"
    picked = f" (picked from {generated:,})" if args.pick_rows else ""
    print(
        f"[INFO] engine={args.engine} FULL (no prefix split) suffix-len={layout.suffix_len} "
        f"total-chunks={layout.middle_total:,} (2^{layout.chunk_bits}) "
        f"key/range={layout.chunk_size:,}",
        file=sys.stderr,
    )
    print(
        f"[INFO] order={order} jump={jump_info(args, layout, 'full')} "
        f"ranges={len(ranges)}{picked} keys={total_keys:,} (2^{total_keys.bit_length() - 1})",
        file=sys.stderr,
    )
    markers = ", ".join(
        f"L{level}={16 ** (layout.suffix_len - level):,}" for level in levels
    )
    print(f"[INFO] done-levels: {markers} markers per chunk", file=sys.stderr)


def check_exe(exe):
    if os.path.isfile(exe) and os.access(exe, os.X_OK):
        return
    if shutil.which(exe):
        return
    raise SystemExit(
        f"scanner binary not found or not executable: {exe!r}. "
        f"Put it next to start.py or pass --exe PATH."
    )


def do_lookup(args, layout):
    if isinstance(layout, FullLayout):
        text = args.lookup.strip().lower()
        if len(text) != TOTAL_HEX_LEN:
            raise SystemExit(
                f"method full: --lookup needs a {TOTAL_HEX_LEN}-digit hex key, "
                f"got {text!r} ({len(text)} digits)"
            )
        value = int(text, 16)
        if not BASE_P71 <= value < 2 * BASE_P71:
            raise SystemExit(f"key {text} is outside the puzzle 71 keyspace [2^70, 2^71)")
        chunk = layout.index_from(text)
        if args.shuffle:
            origin = unpermute_middle(chunk - 1, resolve_seed(args, "--shuffle"), layout) + 1
            print(f"key {text} = chunk #{chunk:,} = row #{origin:,} (shuffle seed {args.seed})")
        else:
            print(f"key {text} = row #{chunk:,} (natural order)")
        return 0
    middle_hex = args.lookup.strip().lower()
    if len(middle_hex) != layout.middle_hex_len:
        raise SystemExit(
            f"--lookup must be {layout.middle_hex_len} hex digits for --suffix-len "
            f"{layout.suffix_len}, got {middle_hex!r}"
        )
    middle_int = int(middle_hex, 16)
    if args.shuffle:
        index0 = unpermute_middle(middle_int, resolve_seed(args, "--shuffle"), layout)
        print(f"middle {middle_hex} = row #{index0 + 1} (shuffle seed {args.seed})")
    else:
        print(f"middle {middle_hex} = row #{middle_int + 1} (natural order)")
    return 0


class Engine:
    """One scanner process per range.

    command() receives the inclusive end from the generator. If the tool treats
    the end as exclusive, add one inside command(). Subclass this, set name,
    default_exe, and done_template. The class registers itself.
    """

    name = ""
    default_exe = ""
    done_template = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.name:
            return
        if cls.name in ENGINES:
            raise RuntimeError(f"duplicate engine name: {cls.name}")
        ENGINES[cls.name] = cls()

    def prepare(self, args, dry_run):
        """Check the binary and the target. Return context for command()."""
        return {}

    def command(self, args, ctx, start_hex, end_hex):
        raise NotImplementedError(self.name)


class KeyHuntEngine(Engine):
    """KeyHunt-Cuda. Inclusive --range. Done log matches scan_keyhunt_p71_gen.py."""

    name = "keyhunt"
    default_exe = "KeyHunt-Cuda.exe"
    done_template = "dones-p71-keyhunt-{level}suffix.txt"

    def prepare(self, args, dry_run):
        warn_ignored(
            [
                ("--threads", args.threads is not None),
                ("--addr", args.addr is not None),
            ],
            "engine keyhunt",
        )
        if args.gpu_id is None:
            args.gpu_id = 0
        if args.gpu_grid is None:
            args.gpu_grid = "256,256"
        if not dry_run:
            check_exe(args.exe)
        return {}

    def command(self, args, ctx, start_hex, end_hex):
        return [
            args.exe,
            "-t", "0",
            "-g",
            "--gpui", str(args.gpu_id),
            "--gpux", args.gpu_grid,
            "-m", "addresses",
            "--coin", "BTC",
            "-o", FOUND_FILE,
            "--range", f"{start_hex}:{end_hex}",
            "-i", args.input,
            *shlex.split(args.extra_args),
        ]


class EcloopEngine(Engine):
    """ecloop (CPU). -r end is exclusive, so the inclusive end is sent as end+1.

    Binary target files (20-byte hash160 records, like p71.bin) are rewritten to
    <name>.hash160.txt. .blf and hex-text targets are passed through.
    Done log matches p71_ecloop.py and stays separate from the KeyHunt log.
    """

    name = "ecloop"
    default_exe = "./ec"
    done_template = "dones-p71-ecloop-{level}suffix.txt"

    def prepare(self, args, dry_run):
        warn_ignored(
            [
                ("--gpu-id", args.gpu_id is not None),
                ("--gpu-grid", args.gpu_grid is not None),
            ],
            "engine ecloop",
        )
        if args.threads is None:
            args.threads = os.cpu_count() or 1
        if args.threads < 1:
            raise SystemExit(f"--threads must be >= 1, got {args.threads}")
        if args.addr is None:
            args.addr = "c"
        if args.addr not in ("c", "u", "cu"):
            raise SystemExit(f"--addr must be c, u, or cu, got {args.addr!r}")
        if dry_run:
            return {"filter": args.input}
        check_exe(args.exe)
        return {"filter": self.prepare_filter(args.input)}

    def prepare_filter(self, path):
        if not os.path.isfile(path):
            raise SystemExit(f"--input not found: {path!r}")
        if path.lower().endswith(".blf"):
            return path
        with open(path, "rb") as handle:
            data = handle.read()
        rows = data.split()
        if rows and all(
            len(row) == 2 * HASH160_BYTES and set(row.lower()) <= HEX_DIGIT_BYTES
            for row in rows
        ):
            return path
        if not data or len(data) % HASH160_BYTES:
            raise SystemExit(
                f"--input {path!r} is not a .blf, not hex hash160 text, and its size "
                f"{len(data)} is not a multiple of {HASH160_BYTES} (raw hash160)."
            )
        out_path = os.path.splitext(path)[0] + ".hash160.txt"
        with open(out_path, "w", encoding="ascii") as handle:
            for offset in range(0, len(data), HASH160_BYTES):
                handle.write(data[offset:offset + HASH160_BYTES].hex() + "\n")
        print(
            f"[INFO] {path} binary -> {out_path} ({len(data) // HASH160_BYTES} hash160)",
            file=sys.stderr,
        )
        return out_path

    def command(self, args, ctx, start_hex, end_hex):
        # ecloop stops when start >= end, so the inclusive last key must be end+1.
        exclusive = f"{int(end_hex, 16) + 1:0{TOTAL_HEX_LEN}x}"
        return [
            args.exe, "add",
            "-f", ctx["filter"],
            "-t", str(args.threads),
            "-a", args.addr,
            "-r", f"{start_hex}:{exclusive}",
            "-o", FOUND_FILE,
            *shlex.split(args.extra_args),
        ]


def run_scan(args, engine, ranges, layout, levels):
    """Write ranges to a file, or call the engine once per range with resume."""
    if args.output:
        with open(args.output, "w", encoding="ascii") as handle:
            for _, start_hex, end_hex in ranges:
                handle.write(f"{start_hex}:{end_hex}\n")
        print(f"Generated {len(ranges)} ranges to {args.output}", file=sys.stderr)
        return 0

    ctx = engine.prepare(args, dry_run=args.dry_run)
    done_log = DoneLog(engine.done_template)
    dones = (
        {level: set() for level in levels}
        if (args.no_resume or args.dry_run) else done_log.load(levels)
    )
    skipped = 0
    worked = 0
    total = len(ranges)

    for position, (label, start_hex, end_hex) in enumerate(ranges, 1):
        if done_log.already(start_hex, layout, dones):
            skipped += 1
            continue
        worked += 1
        print("=" * 70)
        print(f"[{position}/{total}] {label} range {start_hex}:{end_hex}")
        cmd = engine.command(args, ctx, start_hex, end_hex)
        print(" ".join(cmd))
        if args.dry_run:
            continue
        proc = subprocess.run(cmd, check=False)
        if found():
            print("\n" + "!" * 70)
            print(f"KEY FOUND. see {FOUND_FILE}")
            print("!" * 70)
            done_log.mark(start_hex, layout, levels, dones)
            sys.exit(0)
        if proc.returncode != 0:
            print(
                f"[WARN] chunk {start_hex} exit code {proc.returncode}, stopped, not marked done."
            )
            sys.exit(proc.returncode)
        done_log.mark(start_hex, layout, levels, dones)

    if skipped:
        print(
            f"[INFO] skipped {skipped:,} of {total:,} ranges (already in the done log).",
            file=sys.stderr,
        )
    if skipped == total:
        print("[INFO] nothing new to scan. Every requested range is already done.",
              file=sys.stderr)
        print("       Advance --start-index, raise --count, change --seed, or pass",
              file=sys.stderr)
        print("       --no-resume to scan them again.", file=sys.stderr)
    else:
        tail = f", skipped {skipped:,}." if skipped else "."
        print(f"Finished: scanned {worked:,} new ranges{tail}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Puzzle 71 scan launcher. One range generator, pluggable scanner engines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""examples:
  python start.py --engine keyhunt --method index --index 1,71,33,543,32746
  python start.py --engine ecloop --method index --index 1-100 --prefix 40,7f
  python start.py --engine ecloop --method index --start-index 1 --count 50 --suffix-len 9
  python start.py --engine keyhunt --method index --index 1,71,33 --shuffle --seed 1337
  python start.py --engine ecloop --method bytes --bytes 34,219,106,44 --byte-step 1,3,5,7 --count 50
  python start.py --engine ecloop --method bytes --bytes 34,219,106,44 --byte-op mul --byte-step 3 --count 20
  python start.py --engine keyhunt --method full --start-index 1 --count 100 --middle-jump 1000003
  python start.py --engine ecloop --method full --jump-mode spread --start-index 1 --count 1000
  python start.py --engine ecloop --method full --jump-mode lcg --middle-jump 1337 --jump-c 1 --count 100
  python start.py --engine keyhunt --method seed --seed 1337 --count 10 --shuffle-prefix
  python start.py --method percent --percent 0.328746234343432 --percent-of full
  python start.py --method percent --percent 0.328746234343432 --shuffle --seed 1337
  python start.py --method percent --percent 12.5 --percent-of middle --prefix 40,7f
  python start.py --method percent --percent 50 --percent-of prefix+middle --count 4
  python start.py --lookup 2f91e9bb --shuffle --seed 1337
  python start.py --engine ecloop --method index --index 1,71 --dry-run
  python start.py --method seed --seed 7 --count 5 --output ranges.txt

engines: {", ".join(ENGINES)}
methods: {", ".join(METHODS)}
default suffix-len {DEFAULT_SUFFIX_HEX_LEN} -> {DEFAULT_MIDDLE_TOTAL:,} middle rows.
""",
    )
    engine = parser.add_argument_group("engine")
    engine.add_argument(
        "--engine",
        choices=tuple(ENGINES),
        default="keyhunt",
        help="Scanner to launch. keyhunt = KeyHunt-Cuda (GPU). ecloop = ./ec (CPU). "
        "Default: keyhunt. Add another engine by subclassing Engine in this file.",
    )
    engine.add_argument(
        "--exe",
        default="",
        help="Path to the scanner binary. Default: KeyHunt-Cuda.exe or ./ec, from --engine.",
    )
    engine.add_argument(
        "--input",
        default=INPUT_FILE,
        help=f"Target file. KeyHunt reads it with -i. ecloop reads .blf, hex hash160 "
        f"text, or raw 20-byte records (converted automatically). Default: {INPUT_FILE}.",
    )
    engine.add_argument(
        "--threads",
        type=int,
        default=None,
        help="ecloop -t. Default: CPU count. Ignored by keyhunt (KeyHunt is started with -t 0).",
    )
    engine.add_argument(
        "--addr",
        default=None,
        help="ecloop -a address type: c (compressed), u (uncompressed), cu (both). Default: c.",
    )
    engine.add_argument(
        "--gpu-id",
        dest="gpu_id",
        type=int,
        default=None,
        help="KeyHunt --gpui. Default: 0. Ignored by ecloop.",
    )
    engine.add_argument(
        "--gpu-grid",
        dest="gpu_grid",
        default=None,
        help='KeyHunt --gpux. Default: "256,256". Ignored by ecloop.',
    )
    engine.add_argument(
        "--extra-args",
        dest="extra_args",
        default="",
        help='Extra arguments appended to the scanner command, for example --extra-args "-endo".',
    )

    select = parser.add_argument_group("range selection")
    select.add_argument(
        "--method",
        choices=tuple(METHODS),
        default="index",
        help="How middles/chunks are chosen. index = row numbers. seed = Random(seed) bytes. "
        "bytes = per-byte walk mod 256. full = flat chunks across the whole keyspace, no prefix. "
        "percent = land on --percent of --percent-of (full keyspace, prefix+middle, or middle). "
        "Default: index. Add another method by subclassing MiddleMethod or Method in this file.",
    )
    select.add_argument(
        "--percent",
        default="",
        help="method percent: where to land, as a percent. Exact fraction, any number of digits, "
        'for example "0.328746234343432". Comma separates several landing points. '
        "0 = the first unit, 100 = the last. Combined with --count and --middle-jump "
        "to walk onward from each landing.",
    )
    select.add_argument(
        "--percent-of",
        dest="percent_of",
        choices=("full", "prefix+middle", "middle"),
        default="full",
        help="Denominator for --percent. full = all 2^70 keys (default). "
        "prefix+middle = one pair in scan order; the space is the active prefix list "
        "times the middle count, not the whole keyspace. "
        "middle = middle rows only, then each hit middle is expanded across --prefix.",
    )
    select.add_argument(
        "--index",
        default="",
        help='method index or full: which rows. Commas and ranges work: "1,71,33" or "1-100,5000".',
    )
    select.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="method index or full, when --index is omitted: first row (default 1), with --count. "
        "Any integer is accepted and wrapped modulo the row count.",
    )
    select.add_argument(
        "--count",
        type=int,
        default=1,
        help="How many middles or chunks to take (method seed/bytes, or index/full without --index). "
        "Default: 1.",
    )
    select.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed for --shuffle, --shuffle-prefix, and method seed. Random, and printed, when omitted.",
    )
    select.add_argument(
        "--shuffle",
        action="store_true",
        help="Permute middles with a seeded Feistel bijection. Works on every method: "
        "index row 1 is no longer the smallest middle, and a byte walk is spread out. "
        "method percent lands on the natural row, then permutes that row. "
        "No duplicate middles.",
    )
    select.add_argument(
        "--lookup",
        default="",
        help="Middle hex -> row number, then exit. With method full, an 18-digit key -> chunk row. "
        "Honors --shuffle, --seed, and --suffix-len.",
    )
    select.add_argument(
        "--suffix-len",
        dest="suffix_len",
        type=int,
        default=DEFAULT_SUFFIX_HEX_LEN,
        help=f"Hex digits of the suffix walked by one process call (default {DEFAULT_SUFFIX_HEX_LEN}). "
        f"Longer = fewer process starts and a coarser resume. Middle width = 16 - suffix-len.",
    )
    select.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX_SPEC,
        help=f'Prefixes to scan: "{DEFAULT_PREFIX_SPEC}" or a hex list "40,7f,7d". '
        f"Default: {DEFAULT_PREFIX_SPEC}. Ignored by method full.",
    )
    select.add_argument(
        "--shuffle-prefix",
        dest="shuffle_prefix",
        action="store_true",
        help="Shuffle prefix order once, from --seed, and reuse it for every middle.",
    )
    select.add_argument(
        "--shuffle-prefix-each",
        dest="shuffle_prefix_each",
        action="store_true",
        help="Reshuffle prefix order for every middle. Requires --shuffle-prefix.",
    )
    select.add_argument(
        "--prefix-slot",
        dest="prefix_slot",
        default="",
        help='Which prefix positions to scan for each middle, for example "1,7,21,55" or "1-10". '
        "Empty = all prefixes. After a prefix shuffle, a slot hits a different prefix.",
    )
    select.add_argument(
        "--interleave",
        action="store_true",
        help="Walk middles and prefixes together, so each step changes both. "
        "Without this, one middle stays until all of its prefixes are done. "
        "Deterministic: every pair is still scanned once.",
    )
    select.add_argument(
        "--bytes",
        default="0,0,0,0",
        help='method bytes: starting byte values, 0-255, for example "34,219,106,44". '
        "One number is reused for every byte.",
    )
    select.add_argument(
        "--byte-op",
        dest="byte_op",
        choices=("add", "mul"),
        default="add",
        help="method bytes: add or multiply each byte (mod 256) per step. Default: add.",
    )
    select.add_argument(
        "--byte-step",
        dest="byte_step",
        default="1",
        help='method bytes: per-byte addend or multiplier, for example "1" or "1,3,5,7". '
        "add: an odd step cycles through 256. mul: must be odd; even collapses to 0.",
    )

    order = parser.add_argument_group("jump and neighbors (method index and method full)")
    order.add_argument(
        "--middle-jump",
        dest="middle_jump",
        type=int,
        default=1,
        help="Stride between rows when using --start-index/--count (default 1 = sequential). "
        "Wraps modulo the row count, so any integer, including negative, is accepted. "
        "An odd jump has a full cycle.",
    )
    order.add_argument(
        "--jump-mode",
        dest="jump_mode",
        choices=("add", "mul", "lcg", "spread"),
        default="add",
        help="How the row order is built. add: v+jump (odd jump = 100%% of the space). "
        "mul: v*jump, so 1, f, f^2, ... (odd indexes only, orbit <= 25%%). "
        "lcg: v*jump + --jump-c, multiplicative but can cover 100%%. "
        "spread: bit-reversal, 100%% coverage and the most even prefix of any length. "
        "spread does not use the jump value.",
    )
    order.add_argument(
        "--jump-c",
        dest="jump_c",
        type=int,
        default=1,
        help="Additive term for --jump-mode lcg (default 1). Must be odd for a full period.",
    )
    order.add_argument(
        "--jump-percent",
        dest="jump_percent",
        default="",
        help="Set --middle-jump as a PERCENT of the row count (0.1 = 0.1%% of the space). "
        "Any number of decimal digits is kept: the value is an exact fraction, not a float. "
        "Rounded to odd so the cycle is full. Overrides --middle-jump.",
    )
    order.add_argument(
        "--near-plus",
        dest="near_plus",
        default="",
        help='Also scan neighbors at these PLUS distances, for example "3,7,9". '
        "Neighbors are placed right after their point. Duplicates are dropped.",
    )
    order.add_argument(
        "--near-minus",
        dest="near_minus",
        default="",
        help="Same as --near-plus, in the minus direction. The two lists can differ.",
    )
    order.add_argument(
        "--pick-rows",
        dest="pick_rows",
        default="",
        help='From the FINAL range list (after middle x prefix and filters), scan only these rows. '
        'Example: "71,72,1075,22" or "1-50". Written order is kept. Numbers wrap.',
    )

    filters = parser.add_argument_group("filters")
    filters.add_argument(
        "--max-letters",
        dest="max_letters",
        type=int,
        default=0,
        help="Drop a range whose fixed part (prefix+middle) has more than N hex letters "
        "(a-f) in a row. 0 = off.",
    )
    filters.add_argument(
        "--max-digits",
        dest="max_digits",
        type=int,
        default=0,
        help="Same, for decimal digits (0-9) in a row. 0 = off.",
    )
    filters.add_argument(
        "--max-repeat",
        dest="max_repeat",
        type=int,
        default=0,
        help="Same, for identical characters in a row (for example 'fff'). 0 = off.",
    )
    filters.add_argument(
        "--middle-contains",
        dest="middle_contains",
        default="",
        help='Middle must contain these hex characters, in any position. Example: "a4bd". '
        'Comma is OR: "a4bd,a4c8". A repeated character is a count: "aa" means at least two a.',
    )
    filters.add_argument(
        "--prefix-contains",
        dest="prefix_contains",
        default="",
        help='Same as --middle-contains, for the 2-character prefix. Example: "a".',
    )
    filters.add_argument(
        "--max-chi2",
        dest="max_chi2",
        type=float,
        default=0,
        help="Drop a range whose fixed-part hex composition is uneven (chi-square > N). "
        "Smaller is stricter. 0 = off.",
    )

    run = parser.add_argument_group("run")
    run.add_argument(
        "--done-levels",
        dest="done_levels",
        default=DEFAULT_DONE_LEVELS,
        help=f'Comma-separated done-marker levels (default "{DEFAULT_DONE_LEVELS}"). '
        f"The scan level is always included. Levels above --suffix-len are rejected. "
        f"Each engine writes its own dones-p71-<engine>-<level>suffix.txt.",
    )
    run.add_argument(
        "--output",
        default="",
        help="Write inclusive start:end ranges to this file and exit. The scanner is not launched.",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Print each range and the scanner command. Do not launch it and do not write the done log.",
    )
    run.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore the done log and scan every range again.",
    )
    return parser


def main():
    args = build_parser().parse_args()
    try:
        engine = ENGINES[args.engine]
        method = METHODS[args.method]
    except KeyError as exc:
        raise SystemExit(f"unknown name: {exc}") from exc
    if not args.exe:
        args.exe = engine.default_exe
    layout = method.layout_for(args)
    if args.lookup:
        return do_lookup(args, layout)

    levels = parse_done_levels(args.done_levels, layout)
    if isinstance(method, PercentMethod):
        ranges = method.generate(args, layout)
        ranges, generated = finalize_ranges(args, ranges, layout)
        if isinstance(layout, FullLayout):
            print_full_plan(args, layout, levels, ranges, generated)
        else:
            order = f"shuffle(seed {args.seed})" if args.shuffle else "natural"
            print(
                f"[INFO] engine={args.engine} method=percent percent-of={args.percent_of} "
                f"suffix-len={layout.suffix_len} order={order} ranges={len(ranges)}"
                + (f" (picked from {generated:,})" if args.pick_rows else ""),
                file=sys.stderr,
            )
            markers = ", ".join(
                f"L{level}={16 ** (layout.suffix_len - level):,}" for level in levels
            )
            print(f"[INFO] done-levels: {markers} markers per chunk", file=sys.stderr)
    elif isinstance(method, MiddleMethod):
        ranges, middles, prefixes, slots = build_middle_ranges(args, layout, method)
        ranges, generated = finalize_ranges(args, ranges, layout)
        print_middle_plan(args, layout, levels, ranges, generated, middles, prefixes, slots)
    else:
        warn_ignored(
            method.ignored(args),
            "method full (the keyspace is numbered flat, with no prefix/middle split)",
        )
        ranges = method.generate(args, layout)
        ranges, generated = finalize_ranges(args, ranges, layout)
        print_full_plan(args, layout, levels, ranges, generated)
    return run_scan(args, engine, ranges, layout, levels)


if __name__ == "__main__":
    sys.exit(main() or 0)
