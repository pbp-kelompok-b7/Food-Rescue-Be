# Scaffold Backend

Scaffold mengikuti Technical Design Revisi 2. Ada 62 operasi HTTP di `/api/v1/`, ditulis langsung dalam `urls.py` dan `views.py` setiap app. View memakai function-based view DRF dengan `@api_view` dan `@permission_classes`; satu URL dapat menangani beberapa HTTP method dalam fungsi yang sama.

## Struktur

- `accounts`: custom User dengan login email dan field profil.
- `listings`, `requests_app`, `reservations`, `pickups`: model, migration, serializer kosong, URL, dan view placeholder.
- `shared`: enum domain, AuditEvent usulan, endpoint reference-data dan lokasi.
- `common`: hanya response/error helpers, pagination, session authentication, dan abstract timestamp/version models yang dipakai bersama.

Tidak ada generator class/route, manifest runtime, service factory backend, atau file selector/permission kosong. Business logic bisa ditulis langsung di app masing-masing. Pisahkan ke service hanya ketika kompleksitas transaksi memang membutuhkannya.

## Pembaruan SF-BE-05

Dokumen ini mencatat kondisi scaffold awal, bukan status akhir seluruh modul. Validator bersama, audit atomik, cache policy privat dan throttle lintas worker kini tersedia di berkas `common`/`shared` yang sudah ada; detail konfigurasi dan migrasi ada di [README](../README.md). Implementasi lifecycle yang sudah tersedia diuji bersama helper tersebut. Endpoint placeholder tetap tidak dianggap selesai; tidak ada expiry command yang ditambahkan oleh SF-BE-05.

## Status implementasi awal

Endpoint masih mengembalikan 501. Akses menggunakan permission DRF standar; login/register dilindungi CSRF. Serializer, filter, per-object authorization, operasi quantity/state, audit writes, Nominatim, dan expiry belum diimplementasikan. Tidak ada expiry command atau rate limit custom yang berpura-pura siap.

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
