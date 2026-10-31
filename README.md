# Food Rescue
## Sustainable Food & Diet / Waste Management

## Setup Backend Lokal

Gunakan Windows PowerShell dengan Python minimal 3.10, Git, serta Docker Desktop yang sudah berjalan.

### 1. Clone repository

```powershell
git clone https://github.com/pbp-kelompok-b7/Food-Rescue-Be.git be-food-rescue
cd be-food-rescue
```

Jika sudah clone, buka terminal di folder backend yang berisi `manage.py` dan `requirements.txt`.

### 2. Install dependency

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Command menggunakan Python dari virtual environment secara langsung, sehingga aktivasi `.venv` tidak diperlukan.

### 3. Konfigurasi environment

Salin template hanya jika `.env` belum ada:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Salin secret key yang dihasilkan ke `DJANGO_SECRET_KEY` dalam `.env`. Sesuaikan konfigurasi lokal:

```dotenv
DJANGO_SECRET_KEY=<secret-key-yang-dihasilkan>
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
POSTGRES_DB=food_rescue
POSTGRES_USER=food_rescue
POSTGRES_PASSWORD=<password-database-lokal>
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
```

Ganti placeholder sebelum melanjutkan. Docker Compose dan Django membaca konfigurasi dari `.env` yang sama. Jika `DATABASE_URL` diatur, Django memprioritaskannya dibanding `POSTGRES_*`. Jangan commit `.env`.

### 4. Jalankan PostgreSQL

```powershell
docker compose up -d db
docker compose exec db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Lanjutkan setelah muncul `accepting connections`. Pastikan port lokal `5432` tidak digunakan service lain. Kredensial dalam `.env` digunakan saat volume database pertama kali dibuat; perubahan `.env` tidak otomatis mengubah password database yang sudah tersimpan.

### 5. Migration dan server Django

Model dasar dan migration awal sudah tersedia. Jalankan `migrate` untuk database pengembangan baru.

```powershell
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py runserver
```

Backend menyediakan API JSON di `/api/v1/`. Untuk membuat akun admin, gunakan terminal kedua di folder backend:

```powershell
.\.venv\Scripts\python.exe manage.py createsuperuser
```

Halaman admin tersedia di http://127.0.0.1:8000/admin/.

### Pemeriksaan sebelum push

Dengan PostgreSQL berjalan:

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test
```

Hentikan server dengan `Ctrl+C`. Untuk menghentikan database tanpa menghapus datanya:

```powershell
docker compose stop db
```

Backend menggunakan Django REST Framework. Template halaman pengguna sudah dihapus; konfigurasi Django Templates tetap dipakai Django Admin. Frontend React dikelola terpisah.

Scaffold memuat model dasar, migration, shared API utilities, dan 62 operasi kontrak di `/api/v1/`. Endpoint bisnis masih merespons `501 not_implemented`; login dan transaksi belum berjalan. Lihat [panduan scaffold](docs/scaffolding.md) untuk struktur dan batas implementasi.

## Kolaborator
1. 2506534245 - FIQHI DESKI ISMAIL
2. 2506593613 - NABILA OKTAVIA RAMADHANI
3. 2506539082 - EVAN ANDRIAN
4. 2506625470 - KAYSAN NAVID MUSYAFFA

## Deskripsi Aplikasi
Platform untuk meredistribusikan dua jenis _resource_ pangan sebelum menjadi _waste_: 
(1) Packaged Food Surplus, yaitu makanan siap konsumsi yang masih layak dan belum dikonsumsi seperti _rice box_, _snack box_, _bakery packs_, _catering portions_, dan _unopened event food_
(2) Compostable Food Waste, yaitu sisa pangan organik yang masih dapat dimanfaatkan untuk _composting_, BSF/_maggot_, _animal feed_ tertentu, atau _upcycling_. User dapat membuat _supply listing_ untuk DONATE atau SELL, user lain dapat menemukan _supply_ atau membuat _request resource_ yang dibutuhkan, melakukan _reservation_, lalu mengatur _pickup_. Komunikasi lanjutan diarahkan ke platform pihak ke-3. Pembayaran dan negosiasi final dilakukan di luar platform.

## Problem & Solution
Problem: _Packaged food surplus_ dan _compostable food waste_ sering tidak tersalurkan karena pemilik tidak mengetahui pihak yang dapat memanfaatkannya. Akibatnya, makanan layak konsumsi maupun sisa pangan yang masih berguna berakhir menjadi _waste_.

Solution: _Platform matching_ yang mempertemukan pihak yang memiliki _resource_ dengan pihak yang dapat memanfaatkannya melalui Supply Listing, Request Board, Reservation, dan Pickup. Listing dapat berupa DONATE atau SELL. Untuk SELL, harga ditentukan owner dan pembayaran dilakukan di luar platform.

## Daftar Modul
### Module 1 â€” Supply Listing (ResourceListing)
User membuat dan mengelola resource yang dimiliki. Registered User dapat melakukan CRUD, sedangkan public hanya dapat browse/search/filter listing.
Data utama:
- resource_type: PACKAGED_FOOD_SURPLUS / COMPOSTABLE_FOOD_WASTE
- title, category, description
- quantity, unit
- mode: DONATE / SELL
- price (jika SELL)
- condition
- suitable_for / intended_use: HUMAN_CONSUMPTION / COMPOSTING / BSF / ANIMAL_FEED / UPCYCLING sesuai resource
- pickup_location
- available_until/deadline
- storage notes, allergen notes, packaging status bila relevan
- status

### Module 2 â€” Request Board (ResourceRequest)
User membuat dan mengelola request resource yang sedang dicari/dibutuhkan. Modul ini memungkinkan user untuk browse active requests, search/filter request, dan provider dapat menawarkan resource terhadap request tertentu.
Data utama:
- resource_type/category
- quantity_needed, unit
- location/area
- donate_only/max_price
- intended_use
- valid_until
- status

### Module 3 â€” Match/Reservation (Reservation)
Mengelola kesepakatan antara provider dan receiver. Reservation dapat dibuat ketika receiver claim Supply Listing, atau ketika provider menawarkan supply terhadap Resource Request.
Fokus modul: siapa mengambil resource apa, berapa quantity, harga yang disepakati, serta approval.
Data utama:
- provider
- receiver
- listing/request
- quantity
- agreed_price (jika ada)
- status: PENDING / ACCEPTED / REJECTED / CANCELLED
Setelah reservation diterima, stock pada listing dapat diperbarui.

### Module 4 â€” Pickup & Fulfillment (Pickup)
Mengelola pelaksanaan pengambilan setelah reservation diterima.
Fokus modul: kapan dan di mana handoff dilakukan serta apakah fulfillment berhasil.
Data utama:
- reservation
- pickup_date
- pickup_time
- final pickup_location
- pickup instructions / notes
- status: SCHEDULED / COMPLETED / NO_SHOW / CANCELLED

Fitur: reschedule pickup, update fulfillment status, dan opsi contact redirect ke pihak ke-3. Payment dan negosiasi final dilakukan di luar platform.

### Shared Foundation â€” Account & Authentication
Modul ini bukan business module utama. Modul ini bertanggung jawab untuk menangani autentikasi dan otorisasi pengguna.
Mencakup:
- register, login, logout
- basic profile dan contact/WhatsApp number
- authentication
- ownership/authorization rules agar user hanya dapat mengelola data miliknya

### Core flow:
Supply Listing / Resource Request â†’ Reservation â†’ Pickup â†’ Completed

## Pembagian Modul
[Module 1](#module-1): [Fiqhi](#kolaborator)

[Module 2](#module-2): [Kaysan](#kolaborator)

[Module 3](#module-3): [Evan](#kolaborator)

[Module 4](#module-4): [Nabila](#kolaborator)

[Shared Foundation](#shared-foundation): [Fiqhi](#kolaborator)

## Public API
OpenStreetMap/Nominatim digunakan untuk pencarian dan geocoding lokasi pada Supply Listing, Resource Request, dan Pickup.
Penggunaan:
- User mencari alamat/lokasi ketika membuat supply atau request;
- Nominatim mengubah pencarian lokasi menjadi data terstruktur seperti display name dan koordinat;
- Lokasi final pickup dapat dicari/dikonfirmasi dengan data lokasi tersebut;
- Data lokasi dari API dapat di filter berdasarkan area/kota sebelum dipilih atau ditampilkan.
Untuk scope saat ini, platform tidak menjanjikan nearest-listing search, radius search, route optimization, ETA, atau distance calculation.

## Peran Pengguna
### Potential Users:
Packaged Food Surplus providers: event organizer, catering, bakery, restoran/kafe, hotel, kantin, komunitas/organisasi yang mengadakan acara.
Compostable Food Waste providers: kafe, restoran, juice shop, bakery, pasar, food-processing business, dan rumah tangga.
Receivers: individu, komunitas/NGO, composter/composting community, urban farmer/community garden, BSF/maggot farm, peternak untuk jenis waste yang sesuai, dan upcycling business.
### User Roles:
Guest: browse Supply Listing dan Request Board yang tersedia.
Registered User: dapat bertindak sebagai provider maupun receiver tergantung aktivitasnya; membuat supply/request, melakukan reservation, dan mengatur pickup.
Admin: moderasi user, listing, request, reservation, dan pickup.

Provider/receiver adalah behavioral role, bukan account role terpisah.

## Scope & Safety Notes
### Scope:
- Packaged Food Surplus yang masih layak konsumsi dan belum dikonsumsi, misalnya rice box, snack box, bakery packs, catering portions, dan unopened event food.
- Compostable Food Waste yang masih dapat dimanfaatkan, misalnya coffee grounds, fruit/vegetable scraps, eggshells, dan kategori organik lain yang sesuai.
- Donate/sell, request, reservation, local pickup, dan contact via WhatsApp.

### Safety/Platform Limitation:
- Provider wajib mencantumkan condition, deadline/available-until, dan catatan penyimpanan bila relevan.
- Platform hanya memfasilitasi listing dan matching; platform tidak melakukan automatic food safety verification.
- Food waste untuk animal feed hanya boleh ditandai jika jenis resource memang sesuai; platform tidak menganggap semua food waste aman untuk pakan.

### Out of Scope:
- payment gateway
- delivery/logistics
- AI/CV identification
- in-app chat/call
- route optimization
- pengolahan sampah
- automatic safety verification

## AI Disclosure

Penggunaan AI dalam proyek ini dijelaskan per anggota, sesuai bantuan yang digunakan dan hasil yang diperiksa oleh masing-masing individu.

### Fiqhi Deski Ismail — 2506534245

#### Overview

Aku menggunakan AI (Codex) untuk membantu proses perancangan, implementasi, review, dan debugging backend Food Rescue. Bantuan AI yang digunakan meliputi:

1. Brainstorming ide, penyusunan awal Product Requirements Document (PRD), user flow, dan pembagian modul sesuai requirement tugas.
2. Penyusunan struktur Django REST Framework, model, API contract, serta penjelasan alur kode seperti serializer, queryset, dan service.
3. Penulisan dan perbaikan kode pada bagian yang aku kerjakan, termasuk Supply Listing, shared foundation, dan autentikasi.
4. Penulisan dan pelaksanaan tes, review pull request, serta pemeriksaan kesesuaian respons API dengan kontrak.
5. Konfigurasi deployment dan debugging integrasi backend PWS dengan frontend Vercel, termasuk CSRF, session cookie, dan Google Sign-In.

#### Strategi Prompting

Aku membagi permintaan berdasarkan fitur atau masalah yang sedang dikerjakan. Untuk implementasi, aku memberikan requirement, API contract, atau referensi desain sebagai acuan. Untuk review, aku meminta AI memeriksa perubahan pada branch atau pull request tertentu. Ketika menemukan error, aku memberikan pesan error, screenshot, atau hasil pengujian, lalu meminta AI menelusuri penyebabnya dari kode dan perilaku aplikasi.

#### Keterbatasan AI

Hasil AI belum tentu sesuai dengan kondisi aplikasi yang sedang berjalan. Tes yang menggunakan mock dapat membantu memeriksa logika kode, tetapi belum membuktikan integrasi dengan layanan Google atau deployment berhasil. Implementasi juga tetap perlu dibandingkan dengan requirement dan API contract agar tidak hanya mengikuti asumsi AI.

Contohnya, saat debugging Google Sign-In, pesan `invalid_credentials` sempat dikaitkan dengan Client ID dan spasi pada konfigurasi. Setelah konfigurasi diperiksa dan login masih gagal, pemeriksaan dilanjutkan ke runtime PWS. Dari pemeriksaan tersebut ditemukan selisih waktu sekitar dua detik antara PWS dan Google, sementara verifikasi token belum memberikan toleransi waktu. Proses ini menunjukkan bahwa dugaan AI perlu diuji dengan bukti runtime sebelum dianggap sebagai penyebab pasti.

#### Perbaikan Manual

Aku mencoba ulang alur aplikasi melalui browser, memeriksa request dan respons melalui DevTools, serta membandingkan konfigurasi Client ID pada layanan terkait. Ketika saran awal belum menyelesaikan error, aku menyampaikan hasil percobaan tersebut agar pemeriksaan dilanjutkan. Perubahan kode untuk debugging ini ditulis dengan bantuan AI; pengecekan melalui browser dan keputusan untuk menerapkan perubahan tetap melibatkan pemeriksaanku.

Referensi log chat:

```text
Perancangan requirement, design figma, user flow, dan API contract:
https://chatgpt.com/s/cx_6ae4cc18edb08191bd1f42c682d4716e

Implementasi backend dan penjelasan kode:
https://chatgpt.com/s/cx_6ae4cc23a61081919f16db482e3a5b76

Review PR dan diagnosis error API:
https://chatgpt.com/s/cx_6ae4cc36e0f48191b2f6a3111eb44966
```

### Nabila Oktavia Ramadhani — 2506593613

#### Overview

[Isi alat/model AI yang digunakan dan bantuan yang diterima dalam pengerjaan Pickup & Fulfillment. Jika tidak menggunakan AI, nyatakan di sini.]

#### Strategi Prompting

[Isi cara menyusun prompt dan konteks yang diberikan kepada AI.]

#### Keterbatasan AI

[Isi keterbatasan atau kesalahan hasil AI yang ditemukan selama pengerjaan.]

#### Perbaikan Manual

[Isi pemeriksaan dan perbaikan yang dilakukan sendiri terhadap hasil AI.]

Referensi log chat:

```text
[LINK_CHAT_NABILA_1]
[LINK_CHAT_NABILA_2]
```

### Evan Andrian — 2506539082

#### Overview

Dalam pengerjaan modul Match/Reservation (backend dan frontend), saya menggunakan bantuan AI (Kiro, model Claude) sebagai alat pendukung.

Backend (Django REST Framework) — app `reservations`:
- Serializer dan view untuk alur reservation: pembuatan claim dari listing dan offer dari request, revise quantity oleh submitter, accept atomik dengan snapshot terms dan alokasi stok, reject dan cancel dengan pelepasan alokasi, serta moderasi admin.
- Aturan sesuai API contract Revisi 2 (§5, §7.4): penolakan unknown/read-only field (400), kode `page_not_found` untuk halaman di luar rentang, 403 untuk non-approver, dan pembekuan `resource_deadline` dari listing yang terkunci saat acceptance.
- Pembuatan dan penyempurnaan test case untuk kasus-kasus di atas.

Frontend (React + TypeScript + React Router):
- Halaman ClaimForm (claim listing), OfferForm (offer request), ReservationInbox (daftar reservation), dan ReservationDetail (detail + kontak pihak lawan).
- Service `reservations.ts` bertipe (model Reservation/Terms/Contact) dengan bootstrap CSRF untuk request mutasi, hook data (`useReservation`, `useRequest`), validasi form sisi klien, dan pemetaan pesan error sesuai §7.4 dan §9.
- Penggantian halaman placeholder `ModulePage` menjadi halaman nyata pada route terkait.

#### Strategi Prompting

Prompt disusun iteratif dan berbasis konteks nyata, bukan permintaan generik:
- Memberi konteks konkret dengan membuka file terkait (serializer, view, komponen) agar AI membaca kode yang sebenarnya sebelum mengusulkan perubahan, serta menempelkan output error dan log terminal apa adanya.
- Meminta diagnosis sebab-akibat lebih dulu sebelum meminta perbaikan, agar solusi mengarah ke akar masalah, bukan tambalan.
- Meminta AI mengikuti pola yang sudah ada di proyek agar konsisten dengan konvensi kode, struktur service, dan token design system.
- Membatasi scope tiap langkah (satu fitur atau satu bug per interaksi) dan meminta verifikasi (test, typecheck, build) sebelum menyatakan selesai.
- Untuk operasi berisiko (revert commit, push, menghapus volume Docker), meminta opsi dan konfirmasi terlebih dahulu sebelum dieksekusi.

#### Keterbatasan AI

Keterbatasan dan kesalahan hasil AI yang ditemukan selama pengerjaan:
- Saat verifikasi, AI sempat menebak endpoint yang salah (`/api/v1/session/`) padahal yang benar adalah `/api/v1/auth/session/`, sehingga sempat menyimpulkan 404 sebelum memeriksa `urls.py`.
- Keluaran kode AI tetap perlu diperiksa terhadap API contract; AI dapat mengusulkan perubahan yang perlu dikoreksi agar sesuai spesifikasi (misalnya perilaku unknown-field dan kode error).

#### Perbaikan Manual

Pemeriksaan dan perbaikan yang dilakukan sendiri terhadap hasil AI:
- Meninjau setiap perubahan serializer/view terhadap API contract Revisi 2 dan menjalankan `manage.py check`, `makemigrations --check --dry-run`, serta `manage.py test` sebelum menerima hasil.
- Memverifikasi frontend dengan `npm run lint`, `npm run typecheck`, `npm test`, dan `npm run build`; memastikan route placeholder diganti dengan halaman yang benar.
- Mengonfirmasi secara manual bahwa commit tidak menyertakan file sensitif (`.env`) dan hanya berisi file fitur sebelum push.

Referensi log chat:
https://drive.google.com/drive/folders/1U2TPVhRuXjYb0w1HNA5fqi_GmIZB9Tnd?usp=drive_link



### Kaysan Navid Musyaffa — 2506625470

#### Overview

[Isi alat/model AI yang digunakan dan bantuan yang diterima dalam pengerjaan Request Board. Jika tidak menggunakan AI, nyatakan di sini.]

#### Strategi Prompting

[Isi cara menyusun prompt dan konteks yang diberikan kepada AI.]

#### Keterbatasan AI

[Isi keterbatasan atau kesalahan hasil AI yang ditemukan selama pengerjaan.]

#### Perbaikan Manual

[Isi pemeriksaan dan perbaikan yang dilakukan sendiri terhadap hasil AI.]

Referensi log chat:

```text
[LINK_CHAT_KAYSAN_1]
[LINK_CHAT_KAYSAN_2]
```
