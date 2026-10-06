# Triển khai license server lên CentOS / Ubuntu

Hướng dẫn này dựng `server/` ([README.md](README.md)) chạy như 1 service hệ
thống (systemd), sau Nginx làm reverse proxy + TLS thật (Let's Encrypt) —
đúng yêu cầu bắt buộc của viewer (xem [README.md § Bảo mật vận hành](README.md#bảo-mật-vận-hành)
và [USER_GUIDE.md § 3.6](USER_GUIDE.md#36-mandatory-encrypted-connection-https)):
viewer từ chối kết nối server không phải `https://` (trừ localhost).

Áp dụng cho:
- **Ubuntu 22.04 / 24.04**
- **CentOS Stream 9 / RHEL 9 / Rocky Linux 9 / AlmaLinux 9** (lệnh `dnf`)

Có ghi chú riêng chỗ nào 2 hệ khác nhau (chủ yếu: quản lý gói, firewall,
SELinux, đường dẫn cấu hình Nginx).

---

## 0. Yêu cầu trước khi bắt đầu

- 1 VPS/máy chủ Linux, có quyền `sudo`/root.
- 1 domain đã trỏ bản ghi A về IP server (bắt buộc nếu muốn Let's Encrypt
  cấp chứng chỉ TLS thật — nếu chỉ chạy nội bộ trong LAN không có domain
  public, xem ghi chú ở [§6](#6-cấu-hình-nginx--tls-lets-encrypt) về
  chứng chỉ tự ký/CA nội bộ).
- Python **3.10 trở lên** (khuyến nghị 3.11+; mã có xử lý riêng cho việc
  `datetime.fromisoformat()` ở Python < 3.11 không parse được hậu tố `Z`,
  nên 3.10 vẫn chạy đúng, chỉ 3.11+ là gọn hơn).

---

## 1. Cài gói hệ thống

### Ubuntu
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git nginx
```

### CentOS / RHEL / Rocky / Alma
```bash
sudo dnf install -y python3 python3-pip git nginx
```
(Nếu bản Python mặc định của distro quá cũ — vd. CentOS 9 có Python
3.9 — cân nhắc cài thêm `python3.11` từ EPEL/module riêng và dùng nó thay
`python3` ở các bước dưới.)

---

## 2. Tạo user hệ thống riêng & lấy code

Không chạy service bằng root — tạo 1 user riêng không có quyền đăng nhập
shell:

```bash
sudo useradd --system --create-home --home-dir /opt/pdf-drm --shell /usr/sbin/nologin pdfdrm
sudo -u pdfdrm git clone <URL-repo-cua-ban> /opt/pdf-drm
```

Không dùng git? Copy thư mục code hiện có lên bằng `rsync`/`scp` vào
`/opt/pdf-drm` rồi `sudo chown -R pdfdrm:pdfdrm /opt/pdf-drm`.

## 3. Cài Python dependencies

```bash
cd /opt/pdf-drm
sudo -u pdfdrm python3 -m venv .venv
sudo -u pdfdrm .venv/bin/pip install -r requirements.txt
```

## 4. Sinh admin token, lưu vào file env riêng (không commit vào git)

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

```bash
sudo tee /etc/pdf-drm.env > /dev/null <<'EOF'
PDF_DRM_ADMIN_TOKEN=dan-token-vua-sinh-vao-day
EOF
sudo chmod 640 /etc/pdf-drm.env
sudo chown root:pdfdrm /etc/pdf-drm.env
```

`PDF_DRM_ADMIN_TOKEN` là chìa khóa toàn quyền của toàn hệ thống cấp phép —
xem README.md phần "Bảo mật vận hành": lưu ở nơi an toàn (secret manager
nếu có), xoay định kỳ, không commit vào git.

## 5. Tạo systemd service

`/etc/systemd/system/pdf-drm.service`:

```ini
[Unit]
Description=pdf-drm license server
After=network.target

[Service]
Type=simple
User=pdfdrm
Group=pdfdrm
WorkingDirectory=/opt/pdf-drm
EnvironmentFile=/etc/pdf-drm.env
ExecStart=/opt/pdf-drm/.venv/bin/uvicorn server.app:app --host 127.0.0.1 --port 8443
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/pdf-drm/server
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Điểm quan trọng:
- `--host 127.0.0.1`: app **không** expose trực tiếp ra ngoài — chỉ Nginx
  (chạy trên cùng máy) gọi vào được. TLS + cổng public do Nginx lo ở bước 6.
- **Không thêm `--workers N` > 1.** Server dùng SQLite
  (`server/drm.db`); nhiều process cùng ghi vào 1 file SQLite dễ gây khóa
  (`database is locked`) nếu chưa bật WAL mode. Nếu cần scale cao hơn, xem
  ghi chú "Khi nào cần hơn 1 worker" ở cuối file.
- `ReadWritePaths=/opt/pdf-drm/server` cho phép service ghi
  `server/drm.db` và `server/storage/` dù `ProtectSystem=strict` khóa phần
  còn lại của filesystem ở chế độ read-only — tăng cường an toàn nếu service
  bị khai thác.

Kích hoạt:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pdf-drm
sudo systemctl status pdf-drm
sudo journalctl -u pdf-drm -f   # xem log realtime, Ctrl+C để thoát
```

Nếu thấy service không start được vì thiếu `PDF_DRM_ADMIN_TOKEN` — kiểm tra
lại `/etc/pdf-drm.env` có đúng định dạng `KEY=value`, không có dấu ngoặc
kép thừa.

## 6. Cấu hình Nginx + TLS (Let's Encrypt)

### Cài certbot

Ubuntu:
```bash
sudo apt install -y certbot python3-certbot-nginx
```

CentOS/RHEL/Rocky/Alma (cần bật EPEL trước nếu chưa có):
```bash
sudo dnf install -y epel-release
sudo dnf install -y certbot python3-certbot-nginx
```

### File cấu hình Nginx

Ubuntu — tạo `/etc/nginx/sites-available/pdf-drm`, rồi symlink vào
`sites-enabled`:
```bash
sudo tee /etc/nginx/sites-available/pdf-drm > /dev/null <<'EOF'
server {
    listen 80;
    server_name drm.example.com;

    client_max_body_size 300M;  # khớp MAX_UPLOAD_BYTES trong server/app.py

    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/pdf-drm /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

CentOS/RHEL (không có `sites-available`, dùng thẳng `conf.d`):
```bash
sudo tee /etc/nginx/conf.d/pdf-drm.conf > /dev/null <<'EOF'
server {
    listen 80;
    server_name drm.example.com;

    client_max_body_size 300M;

    location / {
        proxy_pass http://127.0.0.1:8443;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
sudo nginx -t && sudo systemctl reload nginx
```

Nhớ đổi `drm.example.com` thành domain thật.

`client_max_body_size 300M` khớp với giới hạn `MAX_UPLOAD_BYTES = 300 *
1024 * 1024` trong [server/app.py](server/app.py) (tính năng upload PDF qua
web dashboard) — nếu bỏ dòng này, Nginx sẽ trả lỗi 413 với file lớn trước
khi request kịp tới app.

### Lấy chứng chỉ TLS

```bash
sudo certbot --nginx -d drm.example.com
```

Certbot tự sửa file Nginx ở trên để thêm `listen 443 ssl`, redirect
80→443, và cấu hình gia hạn tự động (systemd timer `certbot.timer`, kiểm
tra bằng `sudo certbot renew --dry-run`).

> **Chỉ chạy nội bộ trong LAN, không có domain public?** Viewer chấp nhận
> `http://` cho `localhost`/`127.0.0.1`/`::1` để dev, nhưng với máy khác
> trong mạng thì **bắt buộc HTTPS thật** (xem USER_GUIDE.md §3.6) — dùng
> chứng chỉ tự ký hoặc CA nội bộ của công ty (`openssl req -x509 ...` hoặc
> `step-ca`), rồi cấu hình `ssl_certificate`/`ssl_certificate_key` thủ công
> trong block Nginx thay vì chạy certbot.

## 7. Mở firewall

Chỉ mở **80/443** ra internet — **không** mở 8443 (app chỉ nghe
`127.0.0.1`, không cần và không nên public).

Ubuntu (`ufw`):
```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'   # mở cả 80 và 443
sudo ufw enable
sudo ufw status
```

CentOS/RHEL (`firewalld`):
```bash
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload
sudo firewall-cmd --list-all
```

### SELinux (CentOS/RHEL, mặc định enforcing)

Nếu Nginx báo lỗi `502 Bad Gateway` dù service `pdf-drm` đang chạy tốt —
SELinux thường là nguyên nhân đầu tiên cần nghi ngờ (Nginx bị policy chặn
kết nối ra ngoài qua network, kể cả tới `127.0.0.1`):

```bash
sudo setsebool -P httpd_can_network_connect 1
```

Kiểm tra `sudo ausearch -m avc -ts recent` nếu vẫn còn lỗi lạ liên quan
SELinux.

## 8. Kiểm tra

```bash
curl -I https://drm.example.com/admin/ui/
```

Kỳ vọng `HTTP/2 200` (hoặc `301`/`302` nếu domain redirect thêm). Mở domain
đó bằng trình duyệt sẽ thấy màn hình đăng nhập dashboard — đăng nhập bằng
`PDF_DRM_ADMIN_TOKEN` để tạo tài khoản admin đầu tiên (xem README.md
"Đăng nhập: token hay tài khoản?").

## 9. Backup

Hai thứ **bắt buộc** phải backup định kỳ:

- **`/opt/pdf-drm/server/drm.db`** — chứa toàn bộ khóa giải mã (`key_hex`)
  và license. **Mất file này = mất vĩnh viễn khả năng mở mọi file `.cpdf`
  đã phát hành cho người dùng** (kể cả admin cũng không cứu được).
- **`/opt/pdf-drm/server/storage/`** — các file `.cpdf` được lưu khi upload
  qua web dashboard (không có với tài liệu tạo qua CLI, vì CLI không gửi
  file gốc lên server).

```bash
# ví dụ cron hàng ngày, đẩy sang máy khác qua rsync
0 2 * * * rsync -a /opt/pdf-drm/server/drm.db /opt/pdf-drm/server/storage/ backup-host:/backups/pdf-drm/
```

`drm.db` chứa khóa AES ở dạng **plaintext** trong cột `key_hex` — bản
backup cần được mã hóa hoặc lưu ở nơi kiểm soát truy cập chặt như dữ liệu
gốc, không copy tùy tiện ra ổ đĩa rời/cloud không mã hóa.

## 10. Cập nhật code sau này

```bash
cd /opt/pdf-drm
sudo -u pdfdrm git pull
sudo -u pdfdrm .venv/bin/pip install -r requirements.txt
sudo systemctl restart pdf-drm
sudo systemctl status pdf-drm
```

---

## Khi nào cần hơn 1 worker / vượt quá SQLite

Cấu hình 1 worker ở trên đủ dùng tới hàng nghìn viewer đang mở tài liệu
đồng thời (xem phân tích chi tiết đã trao đổi trước đó về giới hạn ghi của
SQLite). Nếu thật sự cần scale cao hơn nữa:

1. Bật WAL trước khi tính tới nhiều worker: thêm
   `conn.execute("PRAGMA journal_mode=WAL")` trong `get_conn()`
   ([server/database.py](server/database.py)) — cho phép đọc song song
   không bị writer chặn, tăng concurrency đáng kể chỉ với 1 dòng.
2. Nếu vẫn chưa đủ, cân nhắc chuyển sang Postgres thay vì chạy nhiều
   `uvicorn` worker cùng ghi 1 file SQLite (rủi ro khóa cao, không được
   SQLite khuyến nghị cho multi-process ghi đồng thời qua nhiều worker).

---

## Thông báo và duyệt yêu cầu qua Telegram (tùy chọn)

Khi có người gửi yêu cầu truy cập, bot nhắn riêng cho quản trị viên, kèm tên tài
liệu người dùng muốn mở và danh sách nhóm tài liệu. Chọn nhóm rồi bấm Duyệt hoặc
Từ chối ngay trong Telegram. Bot dùng long polling nên không cần mở thêm port.

1. Tạo bot qua @BotFather, lấy token. Cần `pip install requests` (đã có trong `requirements.txt`).
2. Gửi `/start` cho bot để biết Telegram user ID của bạn.
3. Thêm vào `/etc/pdf-drm.env` (quyền 640, không commit token):

   ```
   TELEGRAM_BOT_TOKEN=token-tu-botfather
   TELEGRAM_ALLOWED_USER_IDS=123456789
   TELEGRAM_APPROVE_DAYS=180
   ```

   `TELEGRAM_ALLOWED_USER_IDS` là danh sách ID cách nhau bằng dấu phẩy. Chỉ những
   ID này nhận thông báo và bấm duyệt được; người khác bị bỏ qua. Để trống thì bot
   chỉ trả lời `/start` bằng ID của người gửi.
4. `sudo systemctl restart pdf-drm`.

Trong tin nhắn, nếu người dùng mở một tài liệu cụ thể thì có nút `Chỉ tài liệu: ...`
(chọn sẵn) để cấp đúng tài liệu đó, kể cả khi nó chưa thuộc nhóm nào. Hạn dùng chọn
bằng nút 30 / 60 / 180 ngày hoặc `Tự nhập số ngày` rồi gõ số (1-3650) gửi cho bot.
`TELEGRAM_APPROVE_DAYS` là giá trị mặc định.

Không đặt `TELEGRAM_BOT_TOKEN` thì tính năng này tắt hoàn toàn. Gửi thông báo bị
giới hạn 8 tin/phút và không lặp lại cho cùng một máy trong 10 phút, nên yêu cầu
spam không làm ngập Telegram (mọi yêu cầu vẫn nằm trên dashboard).
