# TwilightTown Bot - MariaDB 遷移指南

本指南協助將課表資料存儲從 JSON 檔改為樹莓派上的 MariaDB 資料庫。

## 前置需求

- Python 3.9+
- 樹莓派上已安裝 MariaDB/MySQL
- MariaDB admin 帳號 (用戶名: `admin`, 密碼: `960404`)

## 設定步驟

### 1️⃣ 在樹莓派上初始化資料庫

在樹莓派的 MariaDB 執行以下指令：

```bash
# 以 admin 使用者登入
mysql -u admin -p

# 輸入密碼: 960404

# 執行 SQL 初始化指令
source /path/to/init_db.sql
```

或直接執行一行指令：

```bash
mysql -u admin -p960404 < init_db.sql
```

### 2️⃣ 安裝 Python 依賴

```bash
pip install -r requirements.txt
```

### 3️⃣ 配置 .env 檔案

複製 `.env.example` 為 `.env` 並填入你的設定：

```bash
cp .env.example .env
```

編輯 `.env`，將以下項目替換為實際值：

```
DISCORD_BOT_TOKEN=你的_Discord_Bot_Token
ADMIN_CHANNEL_ID=你的管理員頻道ID
TUTORING_REQUEST_CHANNEL_ID=你的輔導申請頻道ID
TUTORING_CATEGORY_ID=你的輔導類別ID

DB_HOST=192.168.1.xxx          # 樹莓派的 IP 地址
DB_USER=admin
DB_PASSWORD=960404
DB_NAME=tutoring_db
```

**重要**：確保你的本地機器可以連接到樹莓派上的 MariaDB！

### 4️⃣ 遷移現有 JSON 資料（可選）

如果你有 `schedule.json` 檔，可以使用遷移腳本：

```bash
# 編輯 migrate_to_db.py，將 <RASPBERRY_PI_IP> 替換為樹莓派 IP
python3 migrate_to_db.py
```

遷移腳本會：
- 連接到樹莓派的 MariaDB
- 讀取 `schedule.json` 中的所有課程
- 將資料插入到資料庫（跳過重複項目）

### 5️⃣ 啟動 Bot

```bash
python3 bot.py
```

## 資料庫架構

### Schedule 表格

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | INT | 主鑰 |
| `date` | DATE | 課程日期 (YYYY-MM-DD) |
| `time` | TIME | 課程時間 (HH:MM:SS) |
| `title` | VARCHAR(255) | 課程標題 |
| `description` | LONGTEXT | 課程描述 |
| `teacher` | VARCHAR(255) | 講師名稱 |
| `created_at` | TIMESTAMP | 建立時間 |
| `updated_at` | TIMESTAMP | 最後更新時間 |

## 常見問題

### ❓ 如何驗證資料庫連接？

```bash
mysql -h 192.168.1.xxx -u admin -p960404 tutoring_db -e "SELECT COUNT(*) FROM schedule;"
```

### ❓ 如何查看所有已儲存的課程？

在樹莓派 MariaDB 執行：

```sql
SELECT date, time, title, teacher FROM schedule ORDER BY date, time;
```

### ❓ 如何備份資料庫？

```bash
mysqldump -h 192.168.1.xxx -u admin -p960404 tutoring_db > backup.sql
```

### ❓ 連接失敗怎麼辦？

1. 確認樹莓派 MariaDB 服務正在運行：
   ```bash
   sudo systemctl status mariadb
   ```

2. 確認防火牆允許連接（預設 MariaDB 使用 port 3306）：
   ```bash
   sudo ufw allow 3306/tcp
   ```

3. 確認 `.env` 中的 `DB_HOST` IP 位址正確

4. 嘗試直接連接測試：
   ```bash
   mysql -h <RASPBERRY_PI_IP> -u admin -p960404 tutoring_db
   ```

## 故障排除

### 無法連接到資料庫

- 檢查樹莓派 IP 位址是否正確
- 檢查是否能 ping 到樹莓派
- 確認 MariaDB 正在樹莓派上運行
- 檢查防火牆設定

### 字元編碼問題

資料庫已設定為 `utf8mb4`，應支援所有中文字元。如遇到亂碼，請確認：
- `.env` 中 `charset` 設定為 `utf8mb4`
- MariaDB 表格 collation 為 `utf8mb4_unicode_ci`

### 資料重複

遷移腳本會檢查重複項目（以 date, time, teacher, title 作為唯一鑰）。若需清空資料庫重新開始：

```bash
mysql -h <RASPBERRY_PI_IP> -u admin -p960404 tutoring_db -e "TRUNCATE TABLE schedule;"
```

## 支援的 Discord 指令

- `/schedule add` - 新增課程
- `/schedule today` - 查詢今日課表
- `/schedule view` - 查詢指定日期課表
- `/schedule cancel` - 取消課程
- `/apply-tutoring` - 申請一對一輔導
- `/report` - 匿名檢舉
- `/rules` - 查看使用指南

## 聯絡與支援

如有問題或建議，請聯絡管理員。

---

**最後更新**: 2025-11-18
