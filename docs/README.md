# Dokumentasi (branch `docs`)

Baca [`RULES.md`](RULES.md) dulu sebelum mengubah apa pun. Sesi baru: mulai dari [`HANDOFF.md`](HANDOFF.md).

## Desain

| Dokumen | Status | Isi |
|---|---|---|
| [`design/ecscan.md`](design/ecscan.md) | disetujui (tahap 1) | aplikasi scan baru (per app di atas `core/`, Linux + Windows): prefix terstruktur, suffix acak berbatas waktu, multi-pass, job file, notifikasi terenkripsi, persiapan pool |

## Referensi

| File | Isi |
|---|---|
| [`ref/start.py`](ref/start.py) | launcher Python puzzle 71 milik pemilik repo (asli, tidak diedit) |
| [`ref/start-py-notes.md`](ref/start-py-notes.md) | analisis teknik di `start.py`: mana yang diambil, dibuang, dan alasannya |
