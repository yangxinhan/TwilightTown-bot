# Twilight Town Bot - 日志系統實裝總結

## 📝 實裝內容

已為 TwilightTown-bot 完整實裝日志系統，涵蓋所有主要操作和事件。

## 🎯 主要功能

### 1. 三層日志記錄
- **文件日志**：持久化存儲到 `logs/bot.log`，支持日志輪轉
- **控制台日志**：實時輸出到終端/控制台
- **Discord 日志**：可選地發送到指定 Discord 頻道

### 2. 日志架構
```
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger('TwilightTown-Bot')
```

### 3. 核心函數
```python
async def send_log(message: str, level: str = "INFO", embed: discord.Embed = None)
```
- 參數：
  - `message`: 日志消息
  - `level`: 日志級別 ("INFO", "WARNING", "ERROR", "SUCCESS")
  - `embed`: 可選的 Discord Embed 對象

## 📊 記錄的操作

### Tag 系統
- ✅ 用戶接受 Tag
- ❌ 用戶拒絕 Tag

### 輔導系統
- 📝 提交輔導申請
- ✅ 導師接受申請
- ❌ 導師婉拒申請
- 🔒 輔導頻道關閉
- ⏰ 過期申請檢查和通知
- 🗑️ 過期頻道刪除

### 課表系統
- ➕ 新增課程
- ✏️ 修改課程信息
- ❌ 取消課程
- 📅 查詢課表（今日/指定日期）
- 📨 每日課表自動發送

### 申請系統
- 📋 站務申請提交（各類型）
- ⚠️ 匿名檢舉提交

### 系統事件
- 🚀 Bot 啟動
- 📡 指令同步
- 🔌 Bot 連接/斷線
- 🔄 後台任務運行

## 🔧 環境變數配置

在 `.env` 文件中添加：
```
LOG_CHANNEL_ID=你的日志頻道ID
```

- 若不設置或設置為 0，日志仍會記錄到文件和控制台
- 若設置有效的頻道 ID，日志會同時發送到 Discord 頻道

## 📁 日志文件位置

```
project_root/
└── logs/
    ├── bot.log        # 當前日志（最大 10MB）
    ├── bot.log.1      # 備份 1
    ├── bot.log.2      # 備份 2
    ├── bot.log.3      # 備份 3
    ├── bot.log.4      # 備份 4
    └── bot.log.5      # 備份 5
```

## 🎨 Discord 日志格式

日志在 Discord 中顯示為 Embed，包含：
- 標題：日志級別（INFO/WARNING/ERROR/SUCCESS）
- 描述：日志消息內容
- 顏色：根據級別自動選擇
  - 藍色：INFO
  - 綠色：SUCCESS
  - 橙色：WARNING
  - 紅色：ERROR
- 時間戳：操作發生的確切時間

## 🔐 安全建議

1. **日志頻道權限**：設置為僅管理員可見
2. **敏感信息**：日志中不包含用戶密碼或敏感數據
3. **定期檢查**：定期查看日志以檢測異常行為
4. **備份日志**：定期備份 logs 文件夾以防數據丟失

## 📋 修改清單

### 導入和配置（第 1-38 行）
- 添加 `logging` 和 `RotatingFileHandler` 導入
- 配置文件日志處理器（每個文件 10MB，保留 5 個備份）
- 配置控制台日志處理器

### 環境變數（第 139-147 行 附近）
- 添加 `LOG_CHANNEL_ID` 環境變數

### 日志發送函數（新增，大約第 150-193 行）
- 實現 `send_log()` 函數，支持文件、控制台和 Discord 發送

### 全面修改的函數和方法（共計 20+ 處）

1. **UI Components**
   - `TagPreferenceView.accept_tag()`
   - `TagPreferenceView.decline_tag()`

2. **輔導系統**
   - `TutoringApplyModal.on_submit()`
   - `CloseTutoringChannelView.close_channel()`
   - `TutoringRequestView.accept()`
   - `TutoringRequestView.decline()`

3. **申請系統**
   - `dispatch_official_application()`
   - `dispatch_anonymous_report()`

4. **Bot 事件**
   - `on_ready()`
   - `on_disconnect()`

5. **後台任務**
   - `check_expired_tutoring_requests()`
   - `delete_archived_tutoring_channels()`
   - `send_daily_schedule()`

6. **Slash Commands**
   - `add_class()`
   - `today_schedule()`
   - `view_schedule()`
   - `edit_class()`
   - `cancel_class()`
   - `apply_tutoring()`
   - `official_application()`
   - `rules()`
   - `setup_tag_preference()`

7. **其他函數**
   - `get_db_connection()`

## ✨ 使用示例

日志會自動記錄，例如：

### 控制台輸出
```
2026-03-02 10:30:45 - INFO - 📝 使用者 YangXinhan (ID: 123456) 開啟了輔導申請表
2026-03-02 10:35:22 - SUCCESS - ✅ 使用者 YangXinhan (ID: 123456) 已接受 Tag。
```

### 文件日志
```
2026-03-02 10:30:45,123 - TwilightTown-Bot - INFO - 📝 使用者 YangXinhan (ID: 123456) 開啟了輔導申請表
2026-03-02 10:35:22,456 - TwilightTown-Bot - SUCCESS - ✅ 使用者 YangXinhan (ID: 123456) 已接受 Tag。
```

### Discord 日志頻道
如果設置了 `LOG_CHANNEL_ID`，會顯示精美的 Embed 格式

## 🚀 下一步

1. 在 `.env` 中設置 `LOG_CHANNEL_ID`
2. 創建一個專用的日志頻道（可選但推薦）
3. 重啟 bot
4. 查看文件日志和/或 Discord 日志頻道驗證功能

## 📚 相關文檔

- [LOG_SETUP.md](LOG_SETUP.md) - 詳細的日志系統設置指南
- [.env.example.log](.env.example.log) - 環境變數示例

---

**實裝日期**：2026-03-02
**狀態**：✅ 完成
