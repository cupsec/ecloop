# Serah terima ke sesi baru

Sesi baru tidak membawa riwayat chat. Semua konteks ada di branch `docs`.

## Wajib dibaca, berurutan

1. `docs/RULES.md`: aturan branch (kode dari `main`, dokumen hanya di `docs`).
2. `docs/design/ecscan.md`: desain lengkap, default (bagian 11), dan kriteria selesai (bagian 12).
3. `docs/ref/start-py-notes.md` (dan `docs/ref/start.py` bila perlu detail teknik).

## Cara kerja

- Kode dikerjakan di branch kerja sesi tersebut, yang dibuat dari `main`.
  File `docs/` **jangan** ikut ke branch kode.
- Ambil dokumen tanpa pindah branch:
  `git fetch origin docs && git show origin/docs:docs/design/ecscan.md`.
- Perubahan desain atau keputusan baru dicatat di branch `docs`
  (bagian "Riwayat keputusan"), lalu di-push ke `origin/docs`.
- Tahap 1 saja (lihat bagian 10 dan 12). Pool adalah tahap 2.

## Status terakhir

- Desain: disetujui untuk tahap 1.
- Kode: belum dimulai.
