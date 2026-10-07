# Aturan branch `docs`

Branch `docs` menyimpan dokumentasi, desain, dan referensi. `main` tetap
berisi kode saja, kecuali `CLAUDE.md` dan `.claude/` (instruksi dan hook untuk
sesi Claude). Hook `SessionStart` mencetak `RULES.md` dan `HANDOFF.md` dari
branch ini secara otomatis setiap sesi dimulai.

## 1. Arah merge

```
main ──► docs        BOLEH   (docs selalu mengikuti main)
docs ──► main        DILARANG
```

- Setiap kali `main` berubah, `docs` **wajib** di-merge dari `main`.
- Selalu pakai **merge** (`--no-ff`). Jangan rebase, jangan force-push `docs`.
- `docs` tidak pernah di-merge ke `main`, baik lewat PR maupun lokal.

Cara sinkron:

```sh
git fetch origin
git checkout docs
git merge --no-ff origin/main -m "merge: main -> docs"
git push origin docs
```

Kalau ada konflik, menangkan versi `main` untuk file kode. File di `docs/`
hanya ada di branch ini, jadi seharusnya tidak pernah konflik.

## 2. Isi branch

| Folder | Isi | Aturan |
|---|---|---|
| `docs/ref/` | file referensi dari luar (mis. `start.py`) | disimpan **apa adanya**, jangan diedit. Komentar ditulis di file `*-notes.md` terpisah |
| `docs/design/` | rancangan fitur (mis. `ecscan.md`) | setiap file punya baris `Status:` |
| `docs/RULES.md` | aturan ini | diubah hanya atas persetujuan pemilik repo |
| `docs/README.md` | indeks semua dokumen | diperbarui setiap ada file baru |

Hanya file di `docs/` yang boleh ditambah atau diubah di branch ini. Perubahan
kode dibuat di branch kode, lalu masuk ke `docs` lewat sinkron dari `main`.

## 3. Status dokumen desain

`draft` → `disetujui` → `diimplementasi` (sebutkan commit/PR di `main`) → `usang`.

Keputusan penting dicatat di bagian **Riwayat keputusan** pada dokumen desain
yang bersangkutan, dengan tanggal.

## 4. Branch kode

- Branch kode selalu dibuat dari `main`, **bukan** dari `docs`.
- PR kode hanya berisi kode. Tidak boleh ada file `docs/` di dalamnya.

## 5. Commit

- Awalan pesan: `docs:` untuk dokumen, `ref:` untuk file referensi,
  `merge:` untuk sinkron dari `main`.
- Satu topik per commit.

## 6. Jangan pernah di-commit

Di branch mana pun: private key, file hasil temuan (`found*.txt`), seed
dompet, password, token API, atau alamat IP/host laptop pool.
