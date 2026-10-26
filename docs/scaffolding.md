# Scaffold Backend

Scaffold mengikuti Technical Design Revisi 2. Ada 62 operasi HTTP di `/api/v1/`, ditulis langsung dalam `urls.py` dan `views.py` setiap app. View memakai function-based view DRF dengan `@api_view` dan `@permission_classes`; satu URL dapat menangani beberapa HTTP method dalam fungsi yang sama.

## Struktur

- `accounts`: custom User dengan login email dan field profil.
- `listings`, `requests_app`, `reservations`, `pickups`: model, migration, serializer kosong, URL, dan view placeholder.
- `shared`: enum domain, AuditEvent usulan, endpoint reference-data dan lokasi.
- `common`: hanya response/error helpers, pagination, session authentication, dan abstract timestamp/version models yang dipakai bersama.

Tidak ada generator class/route, manifest runtime, service factory backend, atau file selector/permission kosong. Business logic bisa ditulis langsung di app masing-masing. Pisahkan ke service hanya ketika kompleksitas transaksi memang membutuhkannya.

## Status implementasi

GET `/api/v1/requests/` dan GET `/api/v1/requests/{id}/` sudah diimplementasikan untuk issue #13 (RB-BE-01). Endpoint lain masih mengembalikan 501. Login/register dilindungi CSRF. Operasi tulis bisnis, audit writes, Nominatim, dan expiry belum diimplementasikan.

### Request Board: browse, detail, dan histori

- Publik hanya melihat request OPEN, belum melewati `valid_until`, tidak diarsipkan, dan memiliki sisa kebutuhan positif. Kondisi ini juga berlaku pada akses detail publik.
- `scope=mine` memerlukan akun aktif dan hanya menampilkan request milik sendiri, termasuk semua status dan arsip. `status` hanya boleh dipakai pada scope ini.
- Detail nonpublik tersedia bagi pemilik, peserta reservation terkait (termasuk histori rejected/cancelled), dan admin aktif. Akun lain mendapat 404. Owner pada response hanya berisi id dan display_name; kontak tidak disertakan.
- Filter: `q` (2–100 karakter; judul, deskripsi, kategori, label lokasi, atau area), `resource_type`, `category`, `area` (exact, case-insensitive), `intended_use`, dan `donate_only=true|false`.
- Ordering: `created_at`, `-created_at`, `valid_until`, `-valid_until`. Default `-created_at`, dengan `-id` untuk urutan stabil pada nilai waktu yang sama.
- Pagination: `page` default 1, `page_size` default 20 (1–100). Halaman di luar rentang mendapat 404 `page_not_found`; tipe/parameter invalid mendapat 400. Parameter kosong, berulang, dan tidak dikenal ditolak.
- Quantity dihitung dari reservation ACCEPTED: pickup selesai menjadi fulfilled, pickup yang belum selesai menjadi allocated; cancelled/no-show tidak dialokasikan. Remaining = needed − allocated − fulfilled. Pending/rejected/cancelled tidak mengurangi remaining.
- GET tidak menulis status, membatalkan pengajuan, atau mengubah version. Request kedaluwarsa disembunyikan dari publik meskipun proses expiry belum memperbarui status tersimpan; accepted tetap dihitung dan histori dapat dibaca.
- Vocabulary kategori sementara memakai enam kode seed usulan Technical Design §7.6.1 di `shared/reference_data.py`. Implementasi reference-data (dependensi SF) perlu memakai vocabulary yang sama.

Contoh: `/api/v1/requests/?area=Depok&donate_only=true&page_size=20` dan `/api/v1/requests/?scope=mine&status=CLOSED`.

Tes terfokus: `.\.venv\Scripts\python.exe manage.py test requests_app --settings=config.test_settings`. Untuk PostgreSQL, hilangkan `--settings=config.test_settings`; kredensial lokal harus benar dan user database harus dapat membuat database test.

Model dan migration tersedia sebagai dasar. Beberapa detail schema masih perlu ditinjau saat implementasi. Database lokal belum dimigrasi dalam pekerjaan ini. Bila database pernah memakai User bawaan Django, jangan menganggap migrate otomatis mengonversinya ke custom User.

## Pemeriksaan

Dengan PostgreSQL development berjalan:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test
```

Untuk smoke test terisolasi dengan SQLite sementara:

```powershell
.\.venv\Scripts\python.exe manage.py test --settings=config.test_settings
```

SQLite tidak menggantikan pengujian PostgreSQL dan concurrency. Frontend tetap memakai route guards dan API client /api/v1; semua halaman bisnis masih placeholder.
