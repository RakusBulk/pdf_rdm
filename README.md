# pdf-drm

Mã hóa PDF và chỉ cho phép mở bằng trình xem riêng, trên các máy được
cấp phép, trong thời hạn quy định. Không dùng mật khẩu/cờ quyền hạn của
chuẩn PDF (những thứ này dễ bị vô hiệu hóa) — thay vào đó, nội dung PDF
được mã hóa AES-256-GCM và khóa giải mã chỉ được cấp phát bởi server sau
khi kiểm tra "fingerprint" phần cứng của máy và thời hạn còn hiệu lực.

## Kiến trúc

```
pdf-drm/
  common/        # mã hóa AES-GCM (.cpdf) + lấy machine fingerprint
  server/        # FastAPI license server (SQLite)
  cli/           # công cụ dòng lệnh: mã hóa PDF, cấp/thu hồi license
  viewer/        # trình xem PySide6 + PyMuPDF (không có Print/Save/Export)
```

Luồng hoạt động:
1. Admin mã hóa `report.pdf` -> `report.cpdf` (nội dung đã mã hóa AES-256-GCM,
   khóa được đăng ký lên server, **không** nằm trong file).
2. Admin lấy "machine fingerprint" từ máy người dùng cuối, rồi cấp license
   (`doc_id` + `machine_fingerprint` + `expires_at`) trên server.
3. Người dùng mở `report.cpdf` bằng trình xem riêng. Trình xem tính
   fingerprint máy hiện tại, gọi server xin khóa. Server chỉ trả khóa nếu
   fingerprint khớp license, license chưa bị thu hồi và chưa hết hạn.
4. Trình xem giải mã trong bộ nhớ, render từng trang thành ảnh raster (không
   có lớp text để copy), không có bất kỳ menu Print/Save/Export nào, và định
   kỳ gọi lại server để phát hiện license bị thu hồi hoặc hết hạn giữa phiên.

## Cài đặt

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 1. Chạy license server

```bash
export PDF_DRM_ADMIN_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
./scripts/run_server.sh
```

Ghi lại `PDF_DRM_ADMIN_TOKEN` — nó là mật khẩu admin cho các API cấp phép.
**Chạy sau một reverse proxy TLS trong môi trường thật** — khóa giải mã đi
qua API này.

Mở `http://<server>:8443/admin/ui/` để vào **giao diện quản trị web**, gồm 6
tab — Tài liệu, **Nhóm tài liệu**, Yêu cầu chờ duyệt (tick chọn + chọn tài
liệu/nhóm + hạn dùng rồi bấm Duyệt/Từ chối, tương đương
`cli.approve_pending`), License (cấp trực tiếp / theo nhóm / thu hồi),
Access log, **Quản trị viên**. Toàn bộ thao tác trên web đều gọi thẳng các
API admin có sẵn — không có logic nghiệp vụ riêng, nên CLI và web luôn nhất
quán với nhau.

### Đăng nhập: token hay tài khoản?

`PDF_DRM_ADMIN_TOKEN` là khóa "bootstrap" — luôn hoạt động, dùng để đăng
nhập lần đầu và tạo tài khoản admin đầu tiên ở tab **Quản trị viên**. Sau
đó, nên dùng tài khoản username/password cho việc đăng nhập hàng ngày (mỗi
admin 1 tài khoản riêng, dễ biết ai làm gì, xóa được từng người mà không
ảnh hưởng người khác) — token vẫn luôn hoạt động song song, giữ lại phòng
khi quên hết mật khẩu tài khoản (không có cách "quên mật khẩu" qua email vì
hệ thống không gửi email).

Đăng nhập bằng tài khoản trả về 1 "session" có hạn dùng 24 giờ (không phải
mật khẩu gốc) — hết hạn phải đăng nhập lại. Xóa 1 tài khoản sẽ tự động vô
hiệu hóa session đang đăng nhập của tài khoản đó.

```bash
# tạo tài khoản admin qua API (hoặc dùng tab Quản trị viên trên web)
curl -X POST http://localhost:8443/admin/users \
  -H "X-Admin-Token: $PDF_DRM_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"username":"phong","password":"mat_khau_du_manh"}'
```

**Nhóm tài liệu (group)** là một tập `doc_id` được đặt tên, thuần túy để
tiện thao tác hàng loạt — không phải một khái niệm cấp quyền riêng. Tạo 1
nhóm chứa 100 tài liệu, sau đó khi duyệt yêu cầu hoặc cấp license chỉ cần
chọn nhóm thay vì tick từng tài liệu; hệ thống tự tạo license cho từng tài
liệu trong nhóm. Thu hồi/hết hạn vẫn xử lý theo từng tài liệu như bình
thường — xóa 1 nhóm không xóa license đã cấp, chỉ xóa "danh sách tiện ích"
đó.

```bash
# Tạo nhóm từ toàn bộ tài liệu trong 1 manifest (xem batch_encrypt ở dưới)
python -m cli.manage_groups create --name "Bao cao Q3" --manifest ./manifest.csv \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN

# Cấp toàn bộ tài liệu trong nhóm cho 1 máy trong 1 lệnh
python -m cli.manage_groups grant --group-id <id> --machine-fingerprint <fp> \
  --expires-at 2026-12-31T23:59:59+00:00 \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN

# batch_grant.py và approve_pending.py cũng nhận --group-id thay cho --manifest/--doc-id
python -m cli.batch_grant --group-id <id> --users ./users.csv \
  --expires-at ... --server ... --admin-token ...
```

## 2. Mã hóa một PDF

```bash
python -m cli.encrypt report.pdf --title "Q3 Report" \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN \
  --out report.cpdf
```

Lệnh in ra `doc_id` — dùng nó ở bước cấp license.

## 3. Lấy fingerprint của máy người dùng cuối

Trên máy sẽ được phép mở file (Windows hoặc macOS):

```bash
python -m cli.manage_license fingerprint
```

Gửi chuỗi hex 64 ký tự này cho admin.

## 4. Cấp license cho máy đó

```bash
python -m cli.manage_license grant \
  --doc-id <doc_id> --machine-fingerprint <fp> \
  --expires-at 2026-12-31T23:59:59+00:00 --label "Nguyen Van A - laptop" \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
```

Thu hồi bất kỳ lúc nào:

```bash
python -m cli.manage_license revoke --doc-id <doc_id> --machine-fingerprint <fp> \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
```

Xem lịch sử truy cập:

```bash
python -m cli.manage_license log --doc-id <doc_id> \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
```

## 5. Mở file bằng trình xem

```bash
python -m viewer.main
```

Lần đầu chạy sẽ hỏi URL server (lưu vào `~/.pdf_drm_viewer/config.json`).
Chọn **Open...** và trỏ tới file `.cpdf`. Nếu máy chưa được cấp license,
license đã hết hạn, hoặc bị thu hồi, server sẽ từ chối và trình xem báo lỗi.

## 6. Đóng gói thành ứng dụng độc lập (không cần cài Python)

Dùng chung 1 file [`viewer.spec`](viewer.spec) cho cả 2 nền tảng, nhưng
**PyInstaller không cross-compile được** — phải build trên đúng hệ điều
hành đích (build trên Mac chỉ ra app Mac, build trên Windows chỉ ra .exe
Windows).

**macOS** (chạy trực tiếp trên máy Mac):
```bash
./scripts/build_macos.sh
```
Ra `dist/SecureViewer.app` và `dist/SecureViewer-macOS.zip`.

**Windows** (chạy trên máy Windows, PowerShell):
```powershell
.\scripts\build_windows.ps1
```
Ra `dist\SecureViewer\SecureViewer.exe` và `dist\SecureViewer-Windows.zip`
(bản portable, giải nén ra chạy thẳng).

Nếu máy có cài sẵn [Inno Setup 6](https://jrsoftware.org/isdl.php)
(`ISCC.exe`), script còn tự build thêm **`dist\SecureViewer-Setup.exe`** —
bộ cài thật sự (Start Menu shortcut, tùy chọn icon Desktop, gỡ cài đặt được
qua "Add or remove programs") thay vì chỉ giải nén zip. Không có Inno Setup
thì script vẫn chạy bình thường, chỉ bỏ qua bước này và in cảnh báo. Script
dùng sẵn [`installer/windows/SecureViewer.iss`](installer/windows/SecureViewer.iss)
— sửa file này nếu cần đổi tên hiển thị, version, hay icon.

Không có máy Windows? Dùng **GitHub Actions** ([`.github/workflows/build-viewer.yml`](.github/workflows/build-viewer.yml))
để build cả 2 bản trên runner cloud của GitHub (runner Windows có sẵn Inno
Setup nên installer luôn được build) — cần đẩy code lên 1 repo GitHub, sau
đó vào tab Actions bấm "Run workflow" (hoặc push tag `viewer-v*`), tải các
file kết quả (`SecureViewer-macOS.zip`, `SecureViewer-Windows.zip`,
`SecureViewer-Setup.exe`) về từ mục Artifacts.

**Cảnh báo Gatekeeper/SmartScreen:** app chưa được ký số (code signing) nên:
- macOS: người nhận phải chuột-phải vào app → Open → Open (1 lần đầu), hoặc
  bạn chạy `xattr -cr SecureViewer.app` trước khi phát tán.
- Windows: SmartScreen sẽ cảnh báo "Windows protected your PC" — người dùng
  bấm "More info" → "Run anyway".

Để hết cảnh báo này cần chứng chỉ ký số thật (Apple Developer ID ~$99/năm,
hoặc chứng chỉ Authenticode cho Windows) — ngoài phạm vi của bộ mã nguồn
này, cân nhắc mua nếu phát hành chính thức cho 100 người dùng cuối.

## Quy trình cho nhiều file × nhiều người (vd. 100 người)

Nguyên tắc: **mỗi file PDF chỉ mã hóa đúng 1 lần**, phát cùng 1 bộ file
`.cpdf` cho tất cả mọi người (qua email, link cloud, USB... không quan trọng
kênh phân phối vì file vô dụng nếu không có license). Việc "ai được mở" xử
lý hoàn toàn ở server, tách rời khỏi bước mã hóa.

### Bước 1 — Mã hóa hàng loạt (1 lần duy nhất)

```bash
python -m cli.batch_encrypt ./pdfs_can_chia_se --out-dir ./da_ma_hoa \
  --manifest ./manifest.csv \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
```

Ra `manifest.csv` (source_pdf, title, doc_id, cpdf_path) — đây là danh sách
tài liệu bạn sẽ cấp quyền. **Không chạy lại lệnh này** trên các file đã cấp
phép rồi — chạy lại sẽ tạo `doc_id` mới, làm license cũ vô nghĩa (đây chính
là lỗi bạn gặp lúc nãy khi chạy `cli.encrypt` hai lần trên cùng 1 file).

Phát tán thư mục `./da_ma_hoa` (cùng 1 bộ file cho tất cả 100 người) + link
tải trình xem, qua bất kỳ kênh nào (email, Google Drive, USB...).

### Bước 2 — Cấp quyền cho 100 người, có 2 cách

**Cách A — Tự đăng ký (khuyên dùng, ít việc tay nhất):**
Không cần thu thập fingerprint thủ công. **Lần đầu mở app**, trình xem hỏi
Server URL + Tên + Email, rồi tự động gửi đăng ký máy này lên server ngay
(endpoint `/request-access`, không cần admin token) — người dùng không cần
biết trước tài liệu nào để xin quyền, cũng không cần thử mở file trước mới
biết bị từ chối. Trên web dashboard (tab **Yêu cầu chờ duyệt**), bạn thấy rõ
tên + email + fingerprint của từng yêu cầu, chọn tài liệu/nhóm + số ngày rồi
duyệt. Có thể duyệt qua CLI hàng loạt:

```bash
# xem ai đang chờ duyệt
python -m cli.approve_pending list --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN

# duyệt hàng loạt tất cả yêu cầu đang chờ, cấp toàn bộ tài liệu trong manifest,
# có thể giới hạn theo domain email công ty để tránh duyệt nhầm người lạ
python -m cli.approve_pending approve-all \
  --manifest ./manifest.csv --expires-at 2026-12-31T23:59:59+00:00 \
  --only-domain congty.com \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
```

Có thể chạy `approve-all` định kỳ qua cron/scheduled task để tự động hoàn
toàn (vd. cứ 15 phút duyệt hết các yêu cầu từ domain công ty).

**Cách B — Đã có sẵn danh sách fingerprint** (vd. IT quản lý máy, đã thu thập
trước): chuẩn bị 1 file `users.csv` (cột `machine_fingerprint`, `label`) rồi
cấp một lần cho toàn bộ người × toàn bộ tài liệu:

```bash
python -m cli.batch_grant --manifest ./manifest.csv --users ./users.csv \
  --expires-at 2026-12-31T23:59:59+00:00 \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
```

100 người × 20 tài liệu = 2000 license được cấp trong 1 lệnh, không cần
lặp tay. Muốn chỉ cấp một vài tài liệu cụ thể thay vì cả manifest, dùng
`--doc-id <id>` (lặp lại) thay cho `--manifest`.

### Tóm tắt: việc gì làm 1 lần, việc gì lặp lại theo người

| Việc | Tần suất |
|---|---|
| Mã hóa PDF (`batch_encrypt`) | 1 lần / tài liệu, không phụ thuộc số người |
| Phát tán file `.cpdf` | 1 lần, cùng file cho tất cả |
| Thu thập fingerprint | 0 lần nếu dùng Cách A (tự động qua app) |
| Cấp license | 1 lệnh hàng loạt (`approve-all` hoặc `batch_grant`), không lặp tay từng người |
| Thu hồi quyền 1 người | `cli.manage_license revoke` — vẫn cần làm riêng lẻ theo người đó |

## Giới hạn thực sự (đọc kỹ trước khi triển khai)

Không có DRM nào — kể cả sản phẩm thương mại (Adobe DRM, Locklizard,
FileOpen...) — ngăn được 100% người dùng có quyền admin trên máy của chính
họ. Hệ thống này nâng rào cản lên đáng kể (không mật khẩu để crack, khóa
không nằm trong file, kiểm tra máy + thời hạn qua server, không có nút
Print/Save trong app), nhưng **không** ngăn được:

- **Chụp màn hình / chụp ảnh màn hình bằng điện thoại.** Watermark (tên +
  fingerprint máy) được in mờ lên mỗi trang để có thể truy vết nếu ảnh chụp
  bị rò rỉ, nhưng không ngăn được hành vi chụp.
- **"Máy in ảo" (virtual/PDF printer) chụp lại khung hình ứng dụng** ở cấp hệ
  điều hành, hoặc công cụ chụp màn hình toàn màn hình.
- **Người dùng có quyền admin dùng debugger để dump vùng nhớ** chứa PDF đã
  giải mã trong lúc ứng dụng đang chạy.
- **Chặn mạng/giả server**: viewer kiểm tra server mỗi 5 giây, ẩn nội dung ngay khi
  mất kết nối và đóng sau 15 giây; hạn license tính theo giờ server nên chỉnh
  đồng hồ máy không kéo dài được. Đánh đổi là cần mạng liên tục để đọc tài liệu.
- **Reverse-engineer trình xem** để tự thêm chức năng export — vì trình xem
  chạy trên máy người dùng, mã nguồn/binary của nó luôn có thể bị phân tích.
  Đóng gói bằng PyInstaller + obfuscation chỉ làm chậm việc này, không chặn
  được hoàn toàn.

Nếu yêu cầu là "tuyệt đối không rò rỉ được", không giải pháp phần mềm nào
(của bất kỳ ai) đáp ứng được — chỉ có thể giảm thiểu rủi ro và tăng khả năng
truy vết. Nếu mức độ nhạy cảm của tài liệu thực sự cao, cân nhắc thêm các
biện pháp ngoài phần mềm (hợp đồng/NDA, giới hạn số người xem, watermark cá
nhân hóa mạnh hơn, xem qua remote desktop có kiểm soát thay vì phát tán file).

## Bảo mật vận hành

- `PDF_DRM_ADMIN_TOKEN` là chìa khóa toàn quyền của hệ thống cấp phép — lưu
  trong secret manager, không commit vào git, xoay định kỳ.
- Chạy server sau HTTPS/TLS thật (không để lộ khóa AES qua HTTP thuần).
- Sao lưu `server/drm.db` — mất file này đồng nghĩa mất toàn bộ khóa giải mã
  và không ai (kể cả admin) mở lại được các file `.cpdf` đã phát hành.
- Mật khẩu tài khoản admin được băm bằng `scrypt` + salt riêng từng người
  (không lưu plaintext); session token là chuỗi ngẫu nhiên 32 byte, hết hạn
  sau 24h. Vẫn nên chạy sau HTTPS vì mật khẩu/session đi qua API dạng
  JSON — HTTP thuần sẽ lộ cả hai.
