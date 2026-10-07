# ecscan: rancangan

Status: **draft** (belum ada kode). Referensi teknik: `docs/ref/start.py`
dan `docs/ref/start-py-notes.md`.

## 1. Tujuan

Satu aplikasi baru, `ecscan`, di repo yang sama dengan ecloop:

- Memakai ulang mesin ecloop (`lib/`: ecc, sha256, rmd160, addr, bloom).
  `main.c` / binary `ecloop` **tidak diubah**, jadi update dari upstream tetap mudah.
- Semua teknik urutan dipilih lewat satu set argumen yang konsisten.
- Satu proses, inisialisasi **sekali**: filter, gtable, titik precompute, thread pool.
- Siap untuk **pool** (beberapa laptop) tanpa mengubah fondasi.

## 2. Model ruang kunci

```
key = [ prefix (P hex) ][ suffix (S hex) ]
                         [ sub-blok acak ][ isi sub-blok berurutan ]
                           S - B hex        B hex
```

Contoh puzzle 71, `-r 400000000000000000:7fffffffffffffffff -x 7 -sub 4`:

| Bagian | Nilai |
|---|---|
| prefix | 11 hex, indeks `0 .. 2^42-1` (N = 2^42) |
| suffix | 7 hex = 2^28 key |
| sub-blok | 4 hex = 65.536 key, discan berurutan (cepat, group-inversion) |
| sub-blok per suffix | 2^12 = 4096, urutannya diacak per prefix |

- **Indeks prefix** `p = (key >> 4S) - (range_start >> 4S)`. Indeks prefix
  harus muat 64 bit (artinya `S` cukup besar untuk range yang dipakai).
- Prefix di tepi range yang tidak sejajar akan dipotong ke batas `-r`.

## 3. Urutan prefix (`-order`)

Semua mode bersifat **akses acak**: `prefix = order(i)` dihitung langsung dari
counter `i`, tanpa harus menghitung `0..i-1`. Ini kunci untuk resume dan pool
(sebuah unit kerja = rentang counter).

| Mode | Rumus | Cakupan |
|---|---|---|
| `shuffle` (default) | Feistel 4–8 ronde (mixer splitmix64) + cycle-walking ke `[0,N)` | 100%, tanpa duplikat |
| `spread` | bit-reversal atas `2^k ≥ N`; nilai `≥ N` dilewati | 100%, sebaran paling merata di tiap tahap |
| `jump` | `(start + i·jump) mod N`; `-jump-pct` = jump sebagai persen pecahan eksak | 100% jika `gcd(jump, N) = 1` (divalidasi, ditolak atau dikoreksi bila tidak) |
| `lcg` | `x_i = a·x_{i-1} + c mod N`; akses acak lewat komposisi affine (pangkat cepat) | 100% jika syarat Hull–Dobell terpenuhi (divalidasi) |

Opsi bersama: `-start <i|pct%>` (titik mulai, bisa persen), `-seed`.
Tidak dibawa dari `start.py`: `mul`, `bytes`, `seed` (alasannya ada di notes).

## 4. Suffix: acak, dibatasi waktu, multi-pass

- Untuk setiap prefix, urutan sub-blok = Feistel dengan seed `H(seed, prefix)`.
- Satu **kunjungan** menyapu `K` sub-blok (**slice**). Pass `q` menyapu posisi
  `[q·K, (q+1)·K)` dari permutasi itu.
- Setelah semua prefix selesai di pass `q`, lanjut ke pass `q+1`. Setelah
  `ceil(subblok / K)` pass, **seluruh range tercakup**, tanpa duplikat.
- `-T 5` menentukan `K`: saat job dibuat, kecepatan diukur dan
  `K = floor(T · speed / ukuran_subblok)`. `K` lalu **dibekukan di job**.
  Saat jalan, `T` tetap dipakai sebagai batas keras pengaman. Kalau sampai
  terpotong, kunjungan dicatat sebagai parsial dan diulang.
- Setiap sub-blok butuh satu perkalian titik untuk titik awalnya → memakai
  **gtable yang diinisialisasi sekali** (sekitar 30 MB, beberapa µs per lompatan).
  Overhead di bawah 1% per 65 ribu key.

> Untuk pool, `K` harus sama di semua laptop (bagian dari definisi job). Di
> laptop yang lebih lambat, satu kunjungan hanya akan butuh lebih dari T detik.

## 5. Filter pola (`-filter`)

Diterapkan pada hex prefix. Prefix yang tidak lolos dilewati (counter tetap
maju, sehingga tetap deterministik):

`max-letters`, `max-digits`, `max-repeat`, `contains` (OR antar grup, huruf
berulang = hitungan), `max-chi2`.

Saat start, program menampilkan perkiraan persentase ruang yang dilewati, dan
statistik aktualnya per laporan. Catatan: filter tidak menaikkan peluang per
key; filter hanya memilih bagian ruang mana yang **tidak** dicek.

## 6. Job & state: argumen selalu konsisten

Semua parameter yang menentukan **apa** yang discan disimpan di **file job**:

```
range, suffix S, sub B, order + param, seed, start, filter, slice K, target hash
```

- `job_id = hash(isi job)`. Worker menolak state/unit dari job_id lain, jadi
  laptop dengan argumen berbeda tidak bisa diam-diam mencampur hasil.
- Parameter **mesin** (thread, `-T` pengaman, output) tidak masuk job.
- State lokal: `(job_id, pass, counter berikutnya)` + daftar unit parsial.
  Isinya beberapa baris, bukan jutaan marker.

## 7. CLI

```
ecscan new    -r <range> -x 7 -sub 4 -order shuffle -seed 1337 -T 5 [filter] -f <target> -o job.ini
ecscan run    job.ini [-t threads] [-state p71.state] [-out found.txt]
ecscan plan   job.ini [-n 20]            # cetak prefix/sub-blok yang akan dikunjungi
ecscan lookup job.ini <key>              # key -> pass, counter, posisi sub-blok
ecscan bench  job.ini                    # ukur kecepatan, saran K untuk T tertentu
# tahap 2:
ecscan serve  job.ini [-port 7171]       # koordinator pool
ecscan work   <host:port> [-t threads]   # worker pool, job diambil dari server
```

`lookup` untuk `lcg` mungkin hanya tersedia dengan pencarian (tidak ada invers murah).

## 8. Arsitektur kode

```
lib/            (ada)  ecc, addr, sha256, rmd160, bloom, utils
scan/space.c    layout range/prefix/suffix/sub-blok, indeks <-> key
scan/order.c    shuffle, spread, jump, lcg (akses acak + validasi)
scan/filter.c   filter pola prefix
scan/job.c      parse/simpan job, job_id, state, unit kerja
scan/engine.c   thread pool, batch-add dari sub-blok, deadline, cek hash
scan/main.c     CLI
```

Unit kerja = `(pass, counter_awal, counter_akhir)`. Mode lokal menghasilkan
unit sendiri; mode pool menerima unit dari server. Engine tidak tahu bedanya.

## 9. Tahapan

1. **Inti**: space, order (4 mode), filter, job/state, engine, `run/plan/lookup/bench`.
   Diuji pada range kecil yang key-nya diketahui (`data/btc-puzzles-hash`):
   harus ketemu, dan cakupan multi-pass harus tepat 100% tanpa duplikat.
2. **Pool**: `serve` / `work` (TCP sederhana), sewa unit dengan timeout,
   unit yang tidak selesai dikembalikan, key yang ditemukan dilaporkan ke server.

## Riwayat keputusan

- 2026-10-07: prefix 11 hex terstruktur, suffix juga acak dan discan sebatas waktu, lalu ganti prefix.
- 2026-10-07: sisa suffix ditangani dengan **multi-pass** (cakupan akhirnya 100%).
- 2026-10-07: mode prefix versi pertama: `shuffle`, `spread`, `jump`/`jump-pct`, `lcg`.
- 2026-10-07: filter pola dari `start.py` **disertakan**.
- 2026-10-07: dibuat sebagai aplikasi baru (bukan perintah di `ecloop`), dengan rencana pool untuk sekitar 5 laptop.
