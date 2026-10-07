# Catatan referensi: `start.py`

`docs/ref/start.py` adalah launcher Python milik user (puzzle 71). File ini
disimpan apa adanya sebagai **referensi teknik**, bukan untuk dijalankan dari
repo ini. Fitur `scan` di ecloop mengambil idenya, bukan kodenya.

## Cara kerja `start.py` (ringkas)

- Key 18 hex = `[prefix 2 hex][middle M hex][suffix S hex]`, `M + S = 16`.
- Generator memilih daftar `(prefix, middle)`; tiap pasangan = satu range
  `prefix+middle+000..0 : prefix+middle+fff..f`.
- Untuk **setiap range**, launcher menjalankan **satu proses** scanner
  (`KeyHunt-Cuda` atau `./ec add`), lalu menulis "done marker" ke file teks.

## Teknik pemilihan urutan (prefix/middle) dan penilaiannya

| Teknik di start.py | Isi | Penilaian untuk ecloop `scan` |
|---|---|---|
| `index` + `--middle-jump` (add) | `v = start + k*jump mod N`; jump ganjil = siklus penuh (N = 2^n) | **Ambil**. Generalisasi: jump harus coprime dengan N (N tidak selalu 2^n) |
| `--jump-percent` | jump sebagai persen dari N, pecahan eksak | **Ambil** (parser pecahan desimal eksak) |
| `spread` (bit-reversal / van der Corput) | 2^k langkah pertama = grid seragam | **Ambil**. Untuk N bukan 2^n pakai cycle-walking |
| `lcg` (Hull–Dobell) | `v = a*v + c mod N` | **Ambil**, validasi syarat periode penuh |
| `--shuffle` (Feistel 6 ronde, blake2b) | permutasi bijektif, tanpa duplikat | **Ambil** sebagai mode `rand` utama. Ganti blake2b dengan mixer 64-bit cepat (splitmix64), tambah cycle-walking untuk N bukan 2^n |
| `percent` | mendarat di x% ruang, lalu berjalan | **Ambil** sebagai titik awal (`start`) untuk mode apa pun |
| `--near-plus/--near-minus` | kunjungi tetangga ±d | Opsional, prioritas rendah |
| `--lookup` (unpermute) | key → urutan ke-berapa | **Ambil**: berguna untuk verifikasi & resume |
| `--dry-run` / `--output` | cetak rencana tanpa scan | **Ambil** (cetak N prefix pertama) |
| `mul` (`v*f mod 2^n`) | orbit maksimal 25% ruang (dicatat sendiri di kodenya) | **Buang**: tidak bisa cakup semua range |
| `bytes` (per-byte mod 256) | maksimal 256 nilai unik | **Buang**: cakupan sangat kecil |
| `seed` (`random.Random` bytes) | acak murni, bisa duplikat | **Buang**: digantikan Feistel (acak tanpa duplikat) |
| `--prefix` 2 hex × middle, `--shuffle-prefix(-each)`, `--prefix-slot`, `--interleave` | kombinasi prefix 2-hex dengan middle | **Tidak perlu**: di ecloop seluruh 11 hex diperlakukan satu indeks; Feistel atas indeks penuh sudah mengacak prefix+middle sekaligus. Subset prefix → cukup lewat `-r` |
| Filter pola (`--max-letters/digits/repeat`, `--*-contains`, `--max-chi2`) | buang range berpola "tidak wajar" | **Ditunda**: private key seragam acak, filter ini tidak menaikkan peluang, hanya melubangi cakupan |
| Done log multi-level (`dones-*-{level}suffix.txt`) | jutaan baris hex per chunk | **Ganti**: urutan deterministik → state cukup `(mode, param, seed, counter)` + log batch parsial |
| Engine/subprocess per range | 1 proses per range | **Ini yang dihilangkan**: satu proses, thread & tabel diinisialisasi sekali |

## Catatan kecil dari kode

- `round_key` memakai blake2b 4 byte, jadi lebar setengah > 32 bit hanya
  dapat 32 bit kunci ronde. Tetap bijektif, tapi pencampuran lebih lemah.
- `BASE_P71`, `TOTAL_HEX_LEN = 18` hard-coded untuk puzzle 71. Di ecloop
  semuanya diturunkan dari `-r`.
- `EcloopEngine` mengirim `end+1` karena `ecloop add` memperlakukan akhir
  range sebagai eksklusif. Itu benar.
