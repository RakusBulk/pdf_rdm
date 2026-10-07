# Hướng dẫn sử dụng — pdf-drm

Tài liệu này hướng dẫn sử dụng đầy đủ hệ thống, theo đúng luồng thực tế: **mã
hóa tài liệu → quản lý trên server → mở tài liệu bằng viewer**. Xem
[README.md](README.md) nếu cần chi tiết kỹ thuật (kiến trúc, cài đặt môi
trường phát triển, đóng gói ứng dụng).

## Tổng quan hệ thống

Ba thành phần:

| Thành phần | Vai trò |
|---|---|
| **Server** (`server/`) | Lưu khóa giải mã, quyết định máy nào được mở tài liệu nào, đến bao giờ. Có giao diện web quản trị + API. |
| **CLI** (`cli/`) | Công cụ dòng lệnh cho admin: mã hóa hàng loạt, cấp quyền hàng loạt — dùng khi thao tác trên web quá chậm (vd. 100 tài liệu × 100 người). |
| **Viewer** (`viewer/`) | App cài trên máy người dùng cuối, mở file `.cpdf` sau khi xin phép server. |

Luồng hoạt động: admin mã hóa PDF → thành file `.cpdf` (vô dụng nếu không
có server cấp khóa) → phát tán file cho mọi người → mỗi máy muốn mở phải
được admin duyệt (qua web dashboard hoặc CLI) → viewer xin khóa từ server
mỗi lần mở, server kiểm tra máy + hạn dùng rồi mới cấp.

---

## Phần 1 — Tạo file `.cpdf` (mã hóa tài liệu)

Có 2 cách: **qua web** (nhanh, không cần cài gì, khuyên dùng cho từng file
lẻ) hoặc **qua CLI** (phù hợp mã hóa hàng loạt hoặc chạy tự động/kịch bản).

### Cách A — Qua web dashboard (không cần cài đặt gì)

Vào tab **Tài liệu**, mục "Mã hóa & tải lên tài liệu mới": nhập tên tài
liệu, chọn file PDF, bấm "Mã hóa & tải lên". Server tự mã hóa
(AES-256-GCM) và **lưu file `.cpdf` lại trên server** — tài liệu xuất hiện
ngay trong bảng danh sách, kèm nút:

- **Tải file** — tải file `.cpdf` về máy để phát tán cho người dùng.
- **Xóa** — xóa hẳn tài liệu (và toàn bộ license đã cấp cho nó) khỏi hệ
  thống, không thể hoàn tác.

Cột **Kích thước** cho biết dung lượng file đã lưu trên server — dùng để
theo dõi tổng dung lượng đang chiếm dụng (mục "quản lý/giám sát file").
Giới hạn tối đa 300MB/file.

> Tài liệu tạo qua CLI (Cách B) thì server **không giữ file gốc** (chỉ giữ
> khóa) — nút "Tải file" sẽ bị vô hiệu hóa cho các tài liệu đó, vì file
> `.cpdf` chỉ nằm trên máy đã chạy lệnh mã hóa.

### Cách B — Qua CLI (phù hợp mã hóa hàng loạt)

#### Chuẩn bị môi trường

```bash
cd pdf-drm
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

#### Mã hóa 1 file

```bash
python -m cli.encrypt duong_dan/bao_cao.pdf --title "Báo cáo Q3" \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN \
  --out duong_dan/bao_cao.cpdf
```

- `--out` không bắt buộc — mặc định tạo file cùng tên, đuôi `.cpdf`.
- Lệnh in ra `doc_id` — dùng để cấp quyền sau này.
- **Không chạy lại lệnh này trên file đã cấp quyền rồi** — mỗi lần chạy tạo
  `doc_id` và khóa mới, làm mọi license đã cấp trước đó vô nghĩa.

#### Mã hóa hàng loạt (nhiều file 1 lúc)

```bash
python -m cli.batch_encrypt ./pdfs_can_chia_se --out-dir ./da_ma_hoa \
  --manifest ./manifest.csv \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
```

Quét toàn bộ `*.pdf` trong thư mục (`--recursive` nếu muốn quét cả thư mục
con), mã hóa từng file, ghi ra `manifest.csv` (cột `source_pdf, title,
doc_id, cpdf_path`) — dùng file này ở Phần 2 khi cấp quyền/tạo nhóm hàng
loạt.

Sau bước này: phát tán thư mục `./da_ma_hoa` (cùng 1 bộ file cho tất cả mọi
người, qua email/Google Drive/USB gì cũng được) kèm link tải viewer.

---

## Phần 2 — Chức năng trên Server

### 2.1 Cài đặt & khởi động

```bash
export PDF_DRM_ADMIN_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
./scripts/run_server.sh
```

`PDF_DRM_ADMIN_TOKEN` là khóa "bootstrap" toàn quyền — luôn hoạt động, dùng
để đăng nhập lần đầu và tạo tài khoản admin. **Chạy server sau HTTPS/TLS
thật khi dùng chính thức** — khóa giải mã và mật khẩu đi qua API này.

### 2.2 Đăng nhập dashboard

Mở `http://<địa-chỉ-server>:8443/admin/ui/`. Hai cách đăng nhập:

1. **Tài khoản (username/password)** — dùng hàng ngày. Tạo tài khoản đầu
   tiên ở tab **Quản trị viên** (cần đăng nhập bằng token trước 1 lần).
   Đăng nhập tài khoản trả về phiên (session) hạn 24 giờ.
2. **Admin token** — dùng để bootstrap hoặc khi quên hết mật khẩu tài
   khoản (không có tính năng "quên mật khẩu" qua email).

Ô "Server URL" để **trống** nếu bạn đang mở đúng trang server đó (trường
hợp thường gặp) — chỉ điền khi dashboard được mở từ 1 địa chỉ khác server.
Nếu gõ nhầm token vào ô này, app sẽ báo lỗi ngay thay vì âm thầm gọi sai
API.

### 2.3 Các tab trên dashboard

**Tài liệu** — mã hóa/tải PDF lên trực tiếp (xem Phần 1, Cách A), và danh
sách mọi file đã mã hóa (title, doc_id, kích thước, **nhóm đang thuộc về**,
số license đang có, ngày tạo). Cột "Nhóm" hiện badge tên từng nhóm chứa tài
liệu đó (nếu có) — bấm vào badge để nhảy sang tab Nhóm tài liệu và xem chi
tiết ngay. Mỗi dòng có "Xem license" (nhảy sang tab License đã lọc sẵn),
"Tải file" (tải `.cpdf` về — chỉ khả dụng với tài liệu tạo qua web), và
"Xóa" (xóa hẳn tài liệu + toàn bộ license liên quan, không thể hoàn tác).

**Nhóm tài liệu** — gom nhiều tài liệu thành 1 nhóm đặt tên, để cấp quyền
hàng loạt thay vì chọn từng tài liệu:
- "Tạo nhóm mới": đặt tên + tick chọn tài liệu.
- Bấm "Xem" ở 1 nhóm để xem/thêm/bớt tài liệu, hoặc "Xóa nhóm này" (chỉ
  xóa nhóm, không xóa license đã cấp qua nhóm đó trước đây). Panel chi tiết
  có nút "Ẩn" để đóng lại.
- **Thêm tài liệu mới vào 1 nhóm đã có người được cấp quyền → những người
  đó tự động được cấp quyền mở tài liệu mới luôn**, không cần cấp lại thủ
  công. Hệ thống xác định "đã là thành viên nhóm" bằng cách kiểm tra ai
  đang có license còn hiệu lực với ít nhất 1 tài liệu khác trong nhóm; hạn
  dùng của lần cấp tự động lấy theo hạn dùng hiện có của người đó trong
  nhóm (không tự ý kéo dài hay rút ngắn).
- "Cấp license theo nhóm": chọn nhóm + fingerprint máy + hạn dùng + tên
  hiển thị → cấp toàn bộ tài liệu trong nhóm cho 1 máy trong 1 lần bấm.

**Yêu cầu chờ duyệt** — nơi xử lý các yêu cầu do người dùng gửi tự động từ
viewer (xem Phần 3). Mỗi dòng hiện **Username, Email, Fingerprint, Ghi
chú, Lúc gửi**. Quy trình duyệt:
1. Tick chọn 1 hoặc nhiều yêu cầu (checkbox đầu dòng, hoặc tick-tất-cả ở
   header).
2. Chọn tài liệu cần cấp — tick tay từng ô, hoặc chọn 1 **nhóm** ở dropdown
   "Áp dụng nhóm" rồi bấm "Áp dụng" để tự tick hết tài liệu trong nhóm đó.
3. Nhập **số ngày** (vd. `30`) để tự tính ra ngày hết hạn, hoặc tự chọn
   ngày giờ cụ thể.
4. Bấm "Duyệt đã chọn" (cấp quyền) hoặc "Từ chối đã chọn".

**License** — cấp/thu hồi license trực tiếp theo từng tài liệu:
- "Cấp license trực tiếp": chọn tài liệu + nhập fingerprint tay + hạn dùng
  + tên hiển thị. Cấp thêm cho fingerprint đã có sẵn license sẽ tự gộp vào
  cùng người đó, không tạo dòng rời.
- Bảng danh sách license: khi chọn "Tất cả" tài liệu, bảng **gộp theo từng
  người (machine_fingerprint)** — mỗi người 1 dòng kèm số tài liệu đang có,
  bấm **"Chi tiết"** để xem panel bên dưới liệt kê từng tài liệu người đó
  được cấp (tên tài liệu, hạn dùng, trạng thái, nút Thu hồi riêng), có nút
  "Ẩn" để đóng panel lại. Khi lọc theo 1 tài liệu cụ thể, bảng hiện danh
  sách phẳng như trước (đã rõ ngữ cảnh, không cần gộp).

**Access log** — lịch sử mọi lần viewer xin khóa (kể cả bị từ chối), lọc
theo tài liệu — dùng để kiểm tra ai đã mở, khi nào, hoặc điều tra khi nghi
ngờ rò rỉ.

**Quản trị viên** — tạo tài khoản admin mới (username + mật khẩu tối thiểu
8 ký tự) hoặc xóa tài khoản (tự động vô hiệu hóa phiên đăng nhập của tài
khoản đó).

### 2.4 Làm tương tự qua CLI (khi cần thao tác hàng loạt/tự động hóa)

```bash
# fingerprint máy hiện tại
python -m cli.manage_license fingerprint

# cấp / thu hồi / xem license 1 tài liệu
python -m cli.manage_license grant  --doc-id <id> --machine-fingerprint <fp> --expires-at <ISO> [--label ...] --server ... --admin-token ...
python -m cli.manage_license revoke --doc-id <id> --machine-fingerprint <fp> --server ... --admin-token ...
python -m cli.manage_license list   [--doc-id <id>] --server ... --admin-token ...
python -m cli.manage_license log    [--doc-id <id>] --server ... --admin-token ...

# quản lý nhóm
python -m cli.manage_groups create     --name "..." [--manifest manifest.csv | --doc-id <id> ...] --server ... --admin-token ...
python -m cli.manage_groups list       --server ... --admin-token ...
python -m cli.manage_groups show       --group-id <id> --server ... --admin-token ...
python -m cli.manage_groups add-docs   --group-id <id> [--manifest ... | --doc-id ...] --server ... --admin-token ...
python -m cli.manage_groups remove-docs --group-id <id> --doc-id <id> ... --server ... --admin-token ...
python -m cli.manage_groups delete     --group-id <id> --server ... --admin-token ...
python -m cli.manage_groups grant      --group-id <id> --machine-fingerprint <fp> --expires-at <ISO> [--label ...] --server ... --admin-token ...

# duyệt yêu cầu tự đăng ký
python -m cli.approve_pending list        [--status pending|approved|rejected] --server ... --admin-token ...
python -m cli.approve_pending approve     --request-id <n> [--manifest ... | --doc-id ... | --group-id <id>] --expires-at <ISO> [--label ...] --server ... --admin-token ...
python -m cli.approve_pending approve-all [--manifest ... | --doc-id ... | --group-id <id>] --expires-at <ISO> [--only-domain congty.com] --server ... --admin-token ...
python -m cli.approve_pending reject      --request-id <n> --server ... --admin-token ...

# cấp N người × M tài liệu trong 1 lệnh (users.csv: cột machine_fingerprint,label)
python -m cli.batch_grant [--manifest manifest.csv | --doc-id <id> ... | --group-id <id>] \
  --users users.csv --expires-at <ISO> --server ... --admin-token ...
```

`--expires-at` dùng định dạng ISO-8601, vd. `2026-12-31T23:59:59+00:00`.

---

## Phần 3 — Chức năng Viewer (trình xem)

### 3.1 Cài đặt

Tải file `.zip` tương ứng hệ điều hành (xem [README.md](README.md) mục 6
nếu cần tự build), giải nén, chạy trực tiếp — **không cần cài Python**.

- **macOS**: app chưa ký số nên lần mở đầu tiên phải **chuột phải vào app
  → Open → Open**, thay vì double-click bình thường (double-click sẽ bị
  Gatekeeper chặn với thông báo "không thể xác minh nhà phát triển").
- **Windows**: SmartScreen sẽ cảnh báo "Windows protected your PC" — bấm
  "More info" → "Run anyway".

### 3.2 Lần đầu chạy — đăng ký máy

Mở app lần đầu, một hộp thoại hiện ra hỏi 3 thông tin:

- **Server URL** — đã **điền sẵn** `https://drm.ccie4career.com`, thường chỉ cần giữ nguyên.
  Chỉ sửa khi admin báo địa chỉ khác (hoặc đặt biến môi trường `PDF_DRM_SERVER_URL`
  trước khi mở app, ví dụ khi thử trên server test).
- **Your name** — tên hiển thị (admin sẽ thấy tên này khi duyệt yêu cầu và
  trên watermark mỗi trang tài liệu).
- **Your email** — để admin liên hệ/xác minh.

Sau khi điền và bấm OK, app **tự động gửi đăng ký lên server ngay** —
không cần biết trước sẽ mở tài liệu nào. Một thông báo xác nhận hiện ra
kèm số hiệu yêu cầu. Người dùng chỉ cần báo admin duyệt (hoặc admin theo
dõi tab "Yêu cầu chờ duyệt" và tự duyệt).

Thông tin này lưu tại `~/.pdf_drm_viewer/config.json` trên máy — **chỉ hỏi
1 lần**, các lần mở app sau sẽ dùng lại, không hỏi nữa.

### 3.3 Mở tài liệu

Bấm **Open...** trên toolbar, chọn file `.cpdf`. Nếu máy đã được admin cấp
quyền, tài liệu hiện ra ngay. Nếu chưa (hoặc quyền hết hạn/bị thu hồi), app
báo rõ lý do và hỏi có muốn **gửi yêu cầu xin quyền cho đúng tài liệu này**
không (dùng lại tên/email đã lưu, không cần nhập lại).

Toolbar có:
- **Prev / Next** — chuyển trang.
- **Zoom + / Zoom -** — phóng to/thu nhỏ.
- **Bookmarks** — bật/tắt panel mục lục bên trái (nếu PDF gốc có mục lục),
  bấm vào 1 mục để nhảy thẳng tới trang đó.
- **Page: [ô số]** — nhập số trang rồi Enter để nhảy thẳng tới trang đó,
  không cần bấm Next/Prev nhiều lần.
- **Search** (hoặc Cmd+F/Ctrl+F) — mở thanh tìm kiếm, gõ từ khóa rồi Enter
  để tìm trên **toàn bộ tài liệu** (không giới hạn trang đang xem). Có nút
  "Previous"/"Next" để chuyển giữa các kết quả, hiện số thứ tự kết quả
  (vd. "2/5"), tự nhảy tới đúng trang và **tô sáng màu vàng** vị trí khớp.
  Bấm "Close" để tắt thanh tìm kiếm.

  > Giao diện viewer hiện toàn bộ bằng tiếng Anh (Open, Prev, Next, Zoom,
  > Bookmarks, Search, Page...) — tài liệu này mô tả bằng tiếng Việt để dễ
  > hiểu, tên nút trong ngoặc kép ở trên là tên thật hiển thị trên app.

Ngoài ra có thể **cuộn chuột/trackpad lên xuống** để chuyển trang trực
tiếp — khi cuộn tới hết đáy trang hiện tại, cuộn thêm sẽ tự sang trang kế
tiếp (và ngược lại khi cuộn lên hết đỉnh trang), không cần bấm nút Next/Prev.

> **Về khả năng tìm kiếm:** file `.cpdf` trên đĩa không thể tìm kiếm được
> bằng bất kỳ công cụ nào khác ngoài trình xem này — đó chính là mục đích
> của việc mã hóa. Sau khi trình xem giải mã vào bộ nhớ, tìm kiếm hoạt động
> trên toàn bộ nội dung văn bản gốc của PDF, không giới hạn gì so với PDF
> thường.

**Cố ý không có** nút Print / Save / Export / Save As trong toàn bộ ứng
dụng — đây chính là cách chặn in ấn/xuất file.

Thanh trạng thái dưới cùng hiện: trang hiện tại, thời gian còn hiệu lực
license, nhắc "Printing/export disabled".

### 3.4 Watermark chống chụp lén

Mỗi trang hiển thị có watermark đỏ đậm tại 2 vị trí chéo trang, nội dung
gồm **tên người dùng + mã máy + thời gian thực**, tự đổi vị trí và góc
nghiêng mỗi ~1.6 giây. Mục đích: nếu ai đó chụp màn hình hay chụp ảnh, vẫn
nhận diện được người và thời điểm.

### 3.5 Khi tài liệu tự đóng

App tự kiểm tra lại với server mỗi 5 phút; nếu quyền bị admin thu hồi hoặc
hết hạn, tài liệu đang mở **tự đóng ngay** kèm thông báo lý do.

Nếu server không kết nối được (mất mạng): app cảnh báo "Offline" ngay lần
đầu, sau đó **tự kiểm tra lại mỗi 30 giây** (nhanh hơn nhịp bình thường) để
sớm phát hiện khi có mạng trở lại. Nếu sau **5 phút vẫn không kết nối
được**, tài liệu **tự đóng thật sự** kèm thông báo rõ lý do — không còn
tình trạng cảnh báo suông rồi vẫn mở mãi như trước. Nếu mạng phục hồi
trong lúc đó, app tự quay lại nhịp kiểm tra bình thường, không cần mở lại
tài liệu.

### 3.6 Bắt buộc kết nối mã hóa (HTTPS)

App **bắt buộc** Server URL phải dùng `https://` — nếu nhập `http://` (trừ
`localhost`/`127.0.0.1` dùng để phát triển/thử nghiệm), app từ chối lưu và
yêu cầu nhập lại. Lý do: khóa giải mã tài liệu đi qua kết nối này mỗi lần
mở file — nếu dùng HTTP thường trên mạng LAN/Wi-Fi công cộng, ai đó bắt gói
tin trên cùng mạng có thể đọc được khóa. Khi triển khai server thật (không
phải localhost), cần đặt HTTPS thật (chứng chỉ TLS) — xem README.md phần
"Bảo mật vận hành".

---

## Phần 4 — Ví dụ quy trình đầy đủ

```bash
# 1. Mã hóa toàn bộ báo cáo Q3
python -m cli.batch_encrypt ./bao_cao_q3 --out-dir ./da_ma_hoa \
  --manifest ./manifest.csv --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN

# 2. Gom thành 1 nhóm
python -m cli.manage_groups create --name "Báo cáo Q3" --manifest ./manifest.csv \
  --server http://localhost:8443 --admin-token $PDF_DRM_ADMIN_TOKEN
# -> in ra group_id

# 3. Phát tán thư mục ./da_ma_hoa + link tải viewer cho mọi người

# 4. Người dùng mở viewer lần đầu -> tự động gửi yêu cầu lên server

# 5. Admin vào tab "Yêu cầu chờ duyệt" trên web, chọn nhóm "Báo cáo Q3",
#    nhập số ngày (vd. 30), duyệt hàng loạt các yêu cầu hợp lệ
```

---

## Giới hạn bảo mật cần biết

Không có DRM nào (kể cả sản phẩm thương mại) ngăn được 100% người dùng có
quyền admin trên máy của chính họ. Hệ thống này **không** ngăn được:

- Chụp màn hình / chụp ảnh màn hình bằng điện thoại (watermark giúp *truy
  vết*, không *ngăn chặn*).
- Máy in ảo hoặc công cụ chụp toàn màn hình ở cấp hệ điều hành.
- Người dùng có quyền admin dùng debugger dump bộ nhớ lúc app đang chạy.
- Sửa đồng hồ hệ thống giữa 2 lần server tự kiểm tra lại (mặc định 5 phút).
- Reverse-engineer bản build viewer (chạy trên máy người dùng nên luôn có
  thể bị phân tích).

Nếu yêu cầu là "tuyệt đối không rò rỉ được", không giải pháp phần mềm nào
đáp ứng được — chỉ giảm thiểu rủi ro và tăng khả năng truy vết.

---

## Xử lý sự cố thường gặp

| Hiện tượng | Nguyên nhân thường gặp | Cách xử lý |
|---|---|---|
| Đăng nhập dashboard báo lỗi, log server thấy đường dẫn kỳ lạ như `/admin/ui/<token>/admin/...` | Gõ nhầm admin token vào ô "Server URL" | Để trống ô Server URL nếu dùng chung trang, token chỉ điền vào ô "Admin token" |
| `python -m cli...` báo `SyntaxError` hoặc thiếu module | Chưa `source .venv/bin/activate`, đang dùng Python hệ thống | Activate venv đúng terminal đang gõ lệnh |
| Viewer báo "License denied" dù vừa được cấp quyền | Đang mở nhầm file `.cpdf` đã bị mã hóa lại (đổi `doc_id`) | Cấp lại quyền theo `doc_id` mới nhất, hoặc dùng đúng file `.cpdf` được cấp |
| Quên hết mật khẩu tài khoản admin | Không có tính năng khôi phục qua email | Đăng nhập lại bằng `PDF_DRM_ADMIN_TOKEN`, tạo tài khoản mới ở tab Quản trị viên |
| macOS/Windows chặn không mở được viewer | App chưa ký số (code signing) | macOS: chuột phải → Open → Open. Windows: "More info" → "Run anyway" |
