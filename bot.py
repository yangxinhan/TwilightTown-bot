import discord
from discord.ext import commands
from discord import app_commands, ui
import os
from dotenv import load_dotenv
import json
from datetime import datetime, timedelta
from typing import Optional, List
import mysql.connector
from mysql.connector import Error

# Tag 同意度確認按鈕 View
class TagPreferenceView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 永久有效

    @ui.button(label="✅ 接受 Tag", style=discord.ButtonStyle.success, custom_id="accept_tag")
    async def accept_tag(self, interaction: discord.Interaction, button: ui.Button):
        """使用者接受 Tag"""
        user_id = interaction.user.id
        connection = get_db_connection()
        if not connection:
            await interaction.response.send_message("❌ 無法連接資料庫，請稍後重試。", ephemeral=True)
            return

        try:
            cursor = connection.cursor()
            
            # 檢查該使用者是否已有紀錄
            cursor.execute("SELECT id FROM user_tag_preferences WHERE user_id = %s", (user_id,))
            existing = cursor.fetchone()
            
            if existing:
                # 更新現有紀錄
                cursor.execute("""
                    UPDATE user_tag_preferences 
                    SET tag_accepted = TRUE, updated_at = NOW()
                    WHERE user_id = %s
                """, (user_id,))
            else:
                # 插入新紀錄
                cursor.execute("""
                    INSERT INTO user_tag_preferences (user_id, tag_accepted, created_at, updated_at)
                    VALUES (%s, TRUE, NOW(), NOW())
                """, (user_id,))
            
            connection.commit()
            await interaction.response.send_message(
                "✅ 感謝！您已同意接受 Tag。",
                ephemeral=True
            )
            print(f"使用者 {interaction.user.name} (ID: {user_id}) 已接受 Tag。")
            
        except Error as e:
            print(f"❌ 設定 Tag 偏好失敗: {e}")
            await interaction.response.send_message("❌ 設定失敗，請稍後重試。", ephemeral=True)
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

    @ui.button(label="❌ 拒絕 Tag", style=discord.ButtonStyle.danger, custom_id="decline_tag")
    async def decline_tag(self, interaction: discord.Interaction, button: ui.Button):
        """使用者拒絕 Tag"""
        user_id = interaction.user.id
        connection = get_db_connection()
        if not connection:
            await interaction.response.send_message("❌ 無法連接資料庫，請稍後重試。", ephemeral=True)
            return

        try:
            cursor = connection.cursor()
            
            # 檢查該使用者是否已有紀錄
            cursor.execute("SELECT id FROM user_tag_preferences WHERE user_id = %s", (user_id,))
            existing = cursor.fetchone()
            
            if existing:
                # 更新現有紀錄
                cursor.execute("""
                    UPDATE user_tag_preferences 
                    SET tag_accepted = FALSE, updated_at = NOW()
                    WHERE user_id = %s
                """, (user_id,))
            else:
                # 插入新紀錄
                cursor.execute("""
                    INSERT INTO user_tag_preferences (user_id, tag_accepted, created_at, updated_at)
                    VALUES (%s, FALSE, NOW(), NOW())
                """, (user_id,))
            
            connection.commit()
            await interaction.response.send_message(
                "❌ 已記錄。您的 Tag 將被設為不接受。",
                ephemeral=True
            )
            print(f"使用者 {interaction.user.name} (ID: {user_id}) 已拒絕 Tag。")
            
        except Error as e:
            print(f"❌ 設定 Tag 偏好失敗: {e}")
            await interaction.response.send_message("❌ 設定失敗，請稍後重試。", ephemeral=True)
            connection.rollback()
        finally:
            cursor.close()
            connection.close()

# 檢舉文件確認按鈕 View
class ConfirmReportView(ui.View):
    def __init__(self, report_data: dict):
        super().__init__(timeout=600)  # 10 分鐘逾時
        self.report_data = report_data

    @ui.button(label="📎 新增附件並提交", style=discord.ButtonStyle.primary, custom_id="add_attachments_submit")
    async def add_attachments(self, interaction: discord.Interaction, button: ui.Button):
        """開啟檔案上傳對話"""
        await interaction.response.send_modal(AttachmentUploadModal(report_data=self.report_data))
        self.stop()

    @ui.button(label="✅ 直接提交 (無附件)", style=discord.ButtonStyle.success, custom_id="direct_submit")
    async def direct_submit(self, interaction: discord.Interaction, button: ui.Button):
        """直接提交檢舉，不附加檔案"""
        await interaction.response.defer(ephemeral=True, thinking=True)
        await dispatch_anonymous_report(
            interaction=interaction,
            target_user=self.report_data["target_user"],
            reason=self.report_data["reason"],
            additional_context=self.report_data.get("additional_context"),
            attachments=[]
        )
        self.stop()

# --- 環境變數與常數定義 ---
load_dotenv()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID"))
TUTORING_REQUEST_CHANNEL_ID = int(os.getenv("TUTORING_REQUEST_CHANNEL_ID"))
TUTORING_CATEGORY_ID = int(os.getenv("TUTORING_CATEGORY_ID"))
TAG_PREFERENCE_CHANNEL_ID = int(os.getenv("TAG_PREFERENCE_CHANNEL_ID"))  # Tag偏好頻道ID
TUTORING_REQUEST_TIMEOUT = 86400  # 24 小時（秒）

# MariaDB 連接設定
DB_CONFIG = {
    'host': os.getenv("DB_HOST", "100.106.250.36"),
    'user': os.getenv("DB_USER", "admin"),
    'password': os.getenv("DB_PASSWORD", "960404"),
    'database': os.getenv("DB_NAME", "mydb"),
    'charset': 'utf8mb4',
    'use_unicode': True
}

# --- 資料庫連接函數 ---
def get_db_connection():
    """建立並返回資料庫連接"""
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        return connection
    except Error as e:
        print(f"❌ 資料庫連接失敗: {e}")
        return None

# --- Tag 偏好檢查函數 ---
def can_tag_user(user_id: int) -> bool:
    """
    檢查使用者是否可被 Tag
    - True: 可以 Tag（使用者接受或沒有設定過）
    - False: 不可以 Tag（使用者明確拒絕）
    """
    connection = get_db_connection()
    if not connection:
        return True  # 若無法連接資料庫，預設允許 Tag（保險起見）
    
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT tag_accepted FROM user_tag_preferences 
            WHERE user_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        cursor.close()
        
        if result is None:
            # 沒有設定過，視為可以 Tag
            return True
        else:
            # 返回使用者設定的偏好
            return result['tag_accepted']
    
    except Error as e:
        print(f"❌ 檢查 Tag 偏好失敗: {e}")
        return True  # 出錯時預設允許 Tag
    finally:
        connection.close()

def get_user_mention(user: discord.Member) -> str:
    """
    根據使用者的 Tag 偏好，返回 mention 或文本名稱
    - 如果允許 Tag，返回 mention (@user)
    - 如果禁止 Tag，返回文本名稱 (@user_name)
    """
    if can_tag_user(user.id):
        return user.mention  # 返回 mention
    else:
        return f"@{user.display_name}"  # 返回文本名稱

# --- 課表資料處理（改用 MariaDB）---
def load_schedule():
    """從 MariaDB 載入課表資料"""
    connection = get_db_connection()
    if not connection:
        return {}
    
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT date, time, title, description, teacher 
            FROM schedule 
            ORDER BY date, time
        """)
        
        rows = cursor.fetchall()
        schedule = {}
        
        for row in rows:
            # 處理日期格式
            if hasattr(row['date'], 'strftime'):
                date_str = row['date'].strftime("%Y-%m-%d")
            else:
                date_str = str(row['date'])
            
            if date_str not in schedule:
                schedule[date_str] = []
            
            # 處理時間格式（從資料庫 HH:MM:SS 轉為 HH:MM）
            if isinstance(row['time'], str):
                time_str = row['time'][:5]  # 取前 5 個字符 HH:MM
            elif hasattr(row['time'], 'strftime'):
                time_str = row['time'].strftime("%H:%M")
            else:
                time_str = str(row['time'])[:5]
            
            schedule[date_str].append({
                'time': time_str,
                'title': row['title'],
                'description': row['description'],
                'teacher': row['teacher']
            })
        
        cursor.close()
        return schedule
        
    except Error as e:
        print(f"❌ 讀取課表失敗: {e}")
        return {}
    finally:
        connection.close()

def save_schedule(data):
    """將課表資料儲存到 MariaDB（新增或更新）"""
    connection = get_db_connection()
    if not connection:
        print("❌ 無法連接資料庫")
        return False
    
    try:
        cursor = connection.cursor()
        
        for date_str, courses in data.items():
            for course in courses:
                # 檢查該課程是否已存在
                check_query = """
                    SELECT id FROM schedule 
                    WHERE date = %s AND time = %s AND teacher = %s AND title = %s
                """
                cursor.execute(check_query, (
                    date_str,
                    course['time'],
                    course.get('teacher', ''),
                    course['title']
                ))
                
                if cursor.fetchone():
                    # 更新現有課程
                    update_query = """
                        UPDATE schedule 
                        SET description = %s
                        WHERE date = %s AND time = %s AND teacher = %s AND title = %s
                    """
                    cursor.execute(update_query, (
                        course.get('description', ''),
                        date_str,
                        course['time'],
                        course.get('teacher', ''),
                        course['title']
                    ))
                else:
                    # 插入新課程
                    insert_query = """
                        INSERT INTO schedule (date, time, title, description, teacher)
                        VALUES (%s, %s, %s, %s, %s)
                    """
                    cursor.execute(insert_query, (
                        date_str,
                        course['time'],
                        course['title'],
                        course.get('description', ''),
                        course.get('teacher', '')
                    ))
        
        connection.commit()
        cursor.close()
        return True
        
    except Error as e:
        print(f"❌ 儲存課表失敗: {e}")
        connection.rollback()
        return False
    finally:
        connection.close()

# --- Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- UI 元件定義 (Modals & Views) ---

# 1. 一對一輔導申請表單 (Modal)
class TutoringApplyModal(ui.Modal, title="一對一輔導申請"):
    topic = ui.TextInput(label="想請教的主題", style=discord.TextStyle.short, placeholder="例如：Python 函式庫使用", required=True)
    tutor = ui.TextInput(label="希望的鎮民 (選填)", style=discord.TextStyle.short, placeholder="請輸入鎮民的 Discord ID 或留空", required=False)
    schedule = ui.TextInput(label="可配合的時段", style=discord.TextStyle.paragraph, placeholder="例如：平日晚上 8-10 點、週末下午", required=True)
    agreement = ui.TextInput(label="請輸入「同意」以接受錄音/陪同條款", style=discord.TextStyle.short, placeholder="同意", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if self.agreement.value.strip() != "同意":
            await interaction.response.send_message("您必須輸入「同意」才能提交申請。", ephemeral=True)
            return

        request_channel_id = os.getenv("TUTORING_REQUEST_CHANNEL_ID")
        if not request_channel_id:
            await interaction.response.send_message("錯誤：未設定輔導申請頻道ID，請聯絡管理員。", ephemeral=True)
            return

        request_channel = bot.get_channel(int(request_channel_id))
        if not request_channel:
            await interaction.response.send_message("錯誤：找不到輔導申請頻道，請聯絡管理員。", ephemeral=True)
            return

        # 嘗試解析偏好鎮民 (可輸入昵稱或 Discord ID)
        preferred_tutor = None
        tutor_display_text = self.tutor.value or "無指定"
        if self.tutor.value.strip():
            guild = interaction.guild
            # 嘗試用 ID 查詢
            try:
                tutor_id = int(self.tutor.value.strip())
                preferred_tutor = guild.get_member(tutor_id)
            except (ValueError, AttributeError):
                pass
            
            # 若 ID 查詢失敗，嘗試用昵稱查詢
            if not preferred_tutor:
                preferred_tutor = discord.utils.find(
                    lambda m: m.display_name.lower() == self.tutor.value.strip().lower() or m.name.lower() == self.tutor.value.strip().lower(),
                    guild.members
                )
            
            # 若找到該鎮民，更新 display_text 為 mention
            if preferred_tutor:
                tutor_display_text = get_user_mention(preferred_tutor)
            else:
                tutor_display_text = self.tutor.value + " (找不到此使用者)"

        embed = discord.Embed(
            title="新的輔導申請",
            description=f"申請人：{get_user_mention(interaction.user)}",
            color=discord.Color.blue()
        )
        embed.add_field(name="主題", value=self.topic.value, inline=False)
        embed.add_field(name="偏好鎮民", value=tutor_display_text, inline=False)
        embed.add_field(name="可配合時段", value=self.schedule.value, inline=False)
        embed.set_footer(text=f"申請人 ID: {interaction.user.id} | ⏰ 24 小時內回應")

        view = TutoringRequestView(applicant=interaction.user, created_at=datetime.utcnow(), preferred_tutor=preferred_tutor)
        await request_channel.send(embed=embed, view=view)
        
        # 若有指定偏好鎮民，發送一條 tag 訊息
        if preferred_tutor:
            await request_channel.send(f"📢 {get_user_mention(preferred_tutor)}，有一則輔導申請指定了您！")
        
        await interaction.response.send_message("您的輔導申請已成功送出，請靜候回覆。", ephemeral=True)
        print(f"使用者 {interaction.user} 提交了輔導申請，偏好鎮民：{preferred_tutor if preferred_tutor else '無指定'}。")

# 1a. 輔導頻道關閉按鈕 (View)
class CloseTutoringChannelView(ui.View):
    def __init__(self, tutor: discord.Member, applicant: discord.Member):
        super().__init__(timeout=None)
        self.tutor = tutor
        self.applicant = applicant

    @ui.button(label="🔒 關閉輔導", style=discord.ButtonStyle.danger, custom_id="close_tutoring_channel")
    async def close_channel(self, interaction: discord.Interaction, button: ui.Button):
        # 若 tutor 或 applicant 為 None，表示是機器人啟動時的佔位符，直接返回
        if self.tutor is None or self.applicant is None:
            await interaction.response.send_message("❌ 此按鈕已過期，請聯絡管理員。", ephemeral=True)
            return
        
        # 只允許導師或申請人關閉頻道
        if interaction.user not in (self.tutor, self.applicant):
            await interaction.response.send_message("❌ 只有申請人可以關閉此頻道。", ephemeral=True)
            return

        # 取得指定類別（輔導頻道專用）
        channel = interaction.channel
        guild = channel.guild
        
        tutoring_category = guild.get_channel(TUTORING_CATEGORY_ID)
        
        if tutoring_category is None or not isinstance(tutoring_category, discord.CategoryChannel):
            await interaction.response.send_message("❌ 找不到輔導頻道類別，請聯絡管理員。", ephemeral=True)
            return

        # 將頻道移到指定類別（會自動加上「已關閉-」前綴以示區別）
        old_name = channel.name
        new_name = f"已關閉-{old_name}" if not old_name.startswith("已關閉-") else old_name
        await channel.edit(category=tutoring_category, name=new_name)

        # 鎖定頻道：禁止所有人（除管理員）傳送訊息
        await channel.set_permissions(
            guild.default_role,
            read_messages=False,
            send_messages=False
        )
        
        # 保證申請人和導師只能查看但不能發言
        await channel.set_permissions(
            self.applicant,
            read_messages=True,
            send_messages=False
        )
        await channel.set_permissions(
            self.tutor,
            read_messages=True,
            send_messages=False
        )

        # 允許管理員查看和發言
        admin_role = discord.utils.find(lambda r: r.permissions.administrator, guild.roles)
        if admin_role:
            await channel.set_permissions(
                admin_role,
                read_messages=True,
                send_messages=True
            )

        # 停用按鈕
        self.clear_items()
        await interaction.response.defer()
        
        # 發送通知訊息
        embed = discord.Embed(
            title="📋 輔導已結束",
            description=f"此輔導頻道已於 {interaction.created_at.strftime('%Y-%m-%d %H:%M')} 關閉。\n"
                        "頻道將保留作為存證記錄，僅管理員可以查看和編輯。",
            color=discord.Color.greyple()
        )
        await channel.send(embed=embed, view=self)
        print(f"輔導頻道 {channel.name} 已由 {interaction.user.name} 關閉並移至已關閉輔導資料夾。")


# 1b. 輔導申請的按鈕 (View)
class TutoringRequestView(ui.View):
    def __init__(self, applicant: discord.Member, created_at: datetime, preferred_tutor: Optional[discord.Member] = None):
        super().__init__(timeout=None)  # 設為 None 讓 view 永久有效，時間檢查在 interaction_check 中進行
        self.applicant = applicant
        self.created_at = created_at
        self.preferred_tutor = preferred_tutor  # 新增：偏好導師

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """檢查時間限制與按鈕權限"""
        # 若 applicant 為 None（機器人剛啟動時的佔位符），允許交互通過
        if self.applicant is None:
            return True
        
        # 檢查是否超過 24 小時
        if datetime.utcnow() - self.created_at > timedelta(seconds=TUTORING_REQUEST_TIMEOUT):
            await interaction.response.send_message(
                "❌ 此申請已超過 24 小時回應期限，請要求申請人重新提交申請。",
                ephemeral=True
            )
            return False

        # 檢查按鈕權限：如果指定了偏好導師，只有該導師可操作；否則任何導師都可
        if self.preferred_tutor:
            if interaction.user != self.preferred_tutor:
                await interaction.response.send_message(
                    f"❌ 只有 {get_user_mention(self.preferred_tutor)} 可以接受或婉拒此申請。",
                    ephemeral=True
                )
                return False
        
        return True

    @ui.button(label="✅ 接單", style=discord.ButtonStyle.success, custom_id="accept_tutoring")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        # 若 applicant 為 None，表示是機器人啟動時的佔位符
        if self.applicant is None:
            await interaction.response.send_message("❌ 此申請已過期或無效，請聯絡管理員。", ephemeral=True)
            return
        
        tutor = interaction.user
        guild = interaction.guild
        
        # 取得指定類別（輔導頻道專用）
        tutoring_category = guild.get_channel(TUTORING_CATEGORY_ID)
        
        if tutoring_category is None or not isinstance(tutoring_category, discord.CategoryChannel):
            await interaction.response.send_message("❌ 找不到輔導頻道類別，請聯絡管理員。", ephemeral=True)
            return
        
        # 建立臨時私人頻道（在指定類別中）
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            self.applicant: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            tutor: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        channel_name = f"輔導-{self.applicant.name}-{tutor.name}"
        temp_channel = await interaction.guild.create_text_channel(channel_name, overwrites=overwrites, category=tutoring_category)

        # 停用原始訊息的按鈕
        self.clear_items()
        original_message = interaction.message
        edited_embed = original_message.embeds[0]
        edited_embed.description = f"申請人：{get_user_mention(self.applicant)}\n**由 {get_user_mention(tutor)} 已接單**"
        edited_embed.color = discord.Color.green()
        await original_message.edit(embed=edited_embed, view=self)

        # 通知雙方
        await interaction.response.send_message(f"您已接受 {get_user_mention(self.applicant)} 的輔導申請。", ephemeral=True)
        await self.applicant.send(f"好消息！您的輔導申請已被 {get_user_mention(tutor)} 接受。\n請至頻道 {temp_channel.mention} 開始討論。")
        
        # 4. 自動化記錄與提醒
        close_view = CloseTutoringChannelView(tutor=tutor, applicant=self.applicant)
        await temp_channel.send(
            f"歡迎 {get_user_mention(self.applicant)} 與 {get_user_mention(tutor)}！\n"
            f"這裡是你們的專屬討論區。\n\n"
            f"**提醒**：輔導結束後，請記得在此頻道簡要回報紀錄（例如：討論摘要、花費時間），以作為平台存證，感謝！",
            view=close_view
        )
        print(f" {tutor.name} 接受了 {self.applicant.name} 的申請，已建立頻道 {channel_name}。")


    @ui.button(label="❌ 婉拒", style=discord.ButtonStyle.danger, custom_id="decline_tutoring")
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        # 若 applicant 為 None，表示是機器人啟動時的佔位符
        if self.applicant is None:
            await interaction.response.send_message("❌ 此申請已過期或無效，請聯絡管理員。", ephemeral=True)
            return
        
        self.clear_items()
        original_message = interaction.message
        edited_embed = original_message.embeds[0]
        edited_embed.description = f"申請人：{get_user_mention(self.applicant)}\n**由 {get_user_mention(interaction.user)} 已婉拒**"
        edited_embed.color = discord.Color.red()
        await original_message.edit(embed=edited_embed, view=self)

        await interaction.response.send_message(f"您已婉拒此申請。", ephemeral=True)
        await self.applicant.send(f"很遺憾，您的輔導申請已被婉拒。若有疑問請聯繫管理員。")
        print(f" {interaction.user.name} 婉拒了 {self.applicant.name} 的申請。")

# 2. 匿名檢舉流程 (Slash Command)
class ReportModal(ui.Modal):
    def __init__(self):
        super().__init__(title="匿名檢舉")

        self.target_user_input = ui.TextInput(
            label="檢舉對象的使用者名稱或ID",
            placeholder="請輸入對方的暱稱或 Discord ID",
            required=True
        )
        self.add_item(self.target_user_input)

        self.reason_input = ui.TextInput(
            label="事由",
            style=discord.TextStyle.paragraph,
            placeholder="請詳細說明事發經過...",
            required=True,
            max_length=1900
        )
        self.add_item(self.reason_input)

        self.additional_context_input = ui.TextInput(
            label="補充說明 / 證據連結 (選填)",
            style=discord.TextStyle.paragraph,
            placeholder="可貼上圖片連結、訊息連結或補充資訊",
            required=False,
            max_length=1900
        )
        self.add_item(self.additional_context_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        report_data = {
            "target_user": self.target_user_input.value,
            "reason": self.reason_input.value,
            "additional_context": self.additional_context_input.value
        }
        
        await interaction.response.send_message(
            "檢舉資訊已確認。請選擇是否新增檔案證據：",
            view=ConfirmReportView(report_data=report_data),
            ephemeral=True
        )


# 檔案上傳對話 Modal
class AttachmentUploadModal(ui.Modal):
    def __init__(self, report_data: dict):
        super().__init__(title="上傳檢舉證據")
        self.report_data = report_data

        self.note_input = ui.TextInput(
            label="附件說明 (選填)",
            style=discord.TextStyle.paragraph,
            placeholder="請說明這些附件代表的內容\n附件說明的內容可不打，提交後機器人會私訊您，請在私訊框中提交附件",
            required=False,
            max_length=1900
        )
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        
        # 提示使用者開啟 DM 上傳檔案
        try:
            dm_channel = await interaction.user.create_dm()
            await dm_channel.send(
                "👋 請在此 DM 中回覆並附上檔案。\n"
                "點擊訊息框旁的「➕」按鈕，選擇「上傳檔案」來附加圖片或其他檔案。\n"
                "完成後傳送訊息即可，系統會自動將您的檢舉送出。\n\n"
                "⏱️ 請在 10 分鐘內完成上傳。\n"
                "請一次上傳完畢所有附件，並在同一則訊息中傳送。\n"
                "如檔案太多請分兩次檢舉"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 無法傳送私訊給您。請先開啟伺服器中其他成員的 DM，或檢查隱私設定。",
                ephemeral=True
            )
            return

        def check(msg: discord.Message) -> bool:
            return (msg.author == interaction.user and 
                    msg.channel.type == discord.ChannelType.private and 
                    len(msg.attachments) > 0)

        try:
            message = await bot.wait_for("message", check=check, timeout=600)
            attachments = message.attachments
        except:
            await dm_channel.send("⏱️ 上傳超時，檢舉已取消。若需重新檢舉請使用 `/report` 指令。")
            await interaction.followup.send("⏱️ 上傳超時，已取消檢舉。", ephemeral=True)
            return

        additional_context = self.report_data.get("additional_context", "")
        if self.note_input.value:
            if additional_context:
                additional_context += f"\n附件說明：{self.note_input.value}"
            else:
                additional_context = f"附件說明：{self.note_input.value}"

        await dispatch_anonymous_report(
            interaction=interaction,
            target_user=self.report_data["target_user"],
            reason=self.report_data["reason"],
            additional_context=additional_context,
            attachments=list(attachments)
        )
        
        await dm_channel.send("✅ 您的檢舉已送出，感謝您的回報！")



async def dispatch_anonymous_report(
    interaction: discord.Interaction,
    target_user: str,
    reason: str,
    additional_context: Optional[str] = None,
    attachments: Optional[List[discord.Attachment]] = None
) -> None:
    """將匿名檢舉送至管理員頻道，並處理附件下載。"""
    admin_channel_id = os.getenv("ADMIN_CHANNEL_ID")
    if not admin_channel_id:
        await interaction.followup.send("錯誤：未設定管理員頻道ID，請聯絡管理員。", ephemeral=True)
        return

    admin_channel = bot.get_channel(int(admin_channel_id))
    if not admin_channel:
        await interaction.followup.send("錯誤：找不到管理員頻道，請聯絡管理員。", ephemeral=True)
        return

    embed = discord.Embed(
        title="⚠️ 收到新的匿名檢舉",
        color=discord.Color.orange()
    )
    embed.add_field(name="檢舉對象", value=target_user, inline=False)
    embed.add_field(name="事由", value=reason, inline=False)

    evidence_lines = []
    if additional_context:
        evidence_lines.append(additional_context)
    if attachments:
        evidence_lines.append(f"附件數量：{len(attachments)}")

    embed.add_field(name="證據/補充", value="\n".join(evidence_lines) if evidence_lines else "無提供", inline=False)
    embed.set_footer(text="此為匿名檢舉，請管理員謹慎處理。")

    files_to_forward = []
    for attachment in attachments or []:
        try:
            files_to_forward.append(await attachment.to_file())
        except (discord.HTTPException, discord.Forbidden) as fetch_error:
            await interaction.followup.send(
                f"無法取得附件 `{attachment.filename}`，請稍後重試或改以其他方式提供。\n錯誤：{fetch_error}",
                ephemeral=True
            )
            return

    send_kwargs = {"embed": embed}
    if files_to_forward:
        send_kwargs["files"] = files_to_forward

    await admin_channel.send(**send_kwargs)
    await interaction.followup.send("您的檢舉已匿名送出，管理團隊將會進行處理，感謝您的回報。", ephemeral=True)
    print(f"收到一則匿名檢舉，對象：{target_user}，附件數量：{len(files_to_forward)}")

# --- Bot 事件監聽 ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    # 重新註冊 view 以支援機器人重啟後的舊按鈕交互
    bot.add_view(TutoringRequestView(applicant=None, created_at=datetime.utcnow(), preferred_tutor=None))
    bot.add_view(CloseTutoringChannelView(tutor=None, applicant=None))
    bot.add_view(TagPreferenceView())  # 註冊 Tag 偏好視圖
    print(f'Bot is ready to use.')
    
    # 在 Bot 首次啟動時發送 Tag 偏好設置消息
    try:
        tag_channel = bot.get_channel(TAG_PREFERENCE_CHANNEL_ID)
        if tag_channel:
            # 檢查頻道中是否已有機器人發送的消息
            try:
                async for message in tag_channel.history(limit=10):
                    if message.author == bot.user and "機器人 Tag 偏好設置" in message.embeds[0].title if message.embeds else False:
                        print(f"✅ Tag 偏好設置消息已存在於 {tag_channel.name}，跳過重複發送。")
                        break
                else:
                    # 沒有找到既有消息，發送新消息
                    embed = discord.Embed(
                        title="🎯 機器人 Tag 偏好設置",
                        description="請選擇您是否願意被本機器人 Tag。\n\n"
                                    "✅ **接受 Tag**：機器人可以在任何場合提及您。\n"
                                    "❌ **拒絕 Tag**：機器人不會提及您。\n\n"
                                    "**默認狀態**：若您未做選擇，將視為可以被 Tag。",
                        color=discord.Color.blue()
                    )
                    embed.set_footer(text="您的選擇將被記錄在系統中")
                    view = TagPreferenceView()
                    await tag_channel.send(embed=embed, view=view)
                    print(f"✅ Tag 偏好設置消息已發送到 {tag_channel.name}")
            except Exception as e:
                print(f"⚠️ 檢查 Tag 偏好消息時出錯: {e}")
    except Exception as e:
        print(f"⚠️ 無法訪問 Tag 偏好頻道: {e}")
    
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)


# --- 斜線指令定義 ---

# 課表指令群組
schedule_group = app_commands.Group(name="schedule", description="課表相關功能")

@schedule_group.command(name="add", description="新增一堂課至課表")
@app_commands.describe(
    date="課程日期 (格式：YYYY-MM-DD)",
    stime="課程時間 (格式：HH:MM)",
    etime="課程結束時間 (格式：HH:MM)",
    title="課程標題",
    description="課程內容的詳細說明"
)
async def add_class(interaction: discord.Interaction, date: str, stime: str, etime: str, title: str, description: str):
    # 簡易的權限檢查，只允許有 "鎮民" 角色的使用者操作
    if not any(role.name == '鎮民' for role in interaction.user.roles):
        await interaction.response.send_message("抱歉，只有鎮民才能新增課程。", ephemeral=True)
        return

    try:
        # 驗證日期與時間格式
        datetime.strptime(date, "%Y-%m-%d")
        datetime.strptime(stime, "%H:%M")
        datetime.strptime(etime, "%H:%M")
    except ValueError:
        await interaction.response.send_message("日期或時間格式錯誤！請使用 `YYYY-MM-DD` 和 `HH:MM` 格式。", ephemeral=True)
        return

    # 直接插入到 MariaDB
    connection = get_db_connection()
    if not connection:
        await interaction.response.send_message("❌ 無法連接資料庫，請稍後重試。", ephemeral=True)
        return

    try:
        cursor = connection.cursor()
        
        # 檢查是否已存在相同的課程
        check_query = """
            SELECT id FROM schedule 
            WHERE date = %s AND stime = %s AND title = %s AND teacher = %s
        """
        cursor.execute(check_query, (date, stime, etime, title, interaction.user.display_name))
        
        if cursor.fetchone():
            await interaction.response.send_message("❌ 此課程已存在，請勿重複新增。", ephemeral=True)
            cursor.close()
            connection.close()
            return
        
        # 插入新課程
        insert_query = """
            INSERT INTO schedule (date, time, title, description, teacher)
            VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(insert_query, (date, stime, etime, title, description, interaction.user.display_name))
        connection.commit()
        
        await interaction.response.send_message(f"✅ 課程 **{title}** 已成功新增至 **{date}** {stime}-{etime} 的課表。", ephemeral=True)
        print(f"{interaction.user.display_name} 新增了課程：{date} {stime}-{etime} - {title}")
        
        cursor.close()
        
    except Error as e:
        print(f"❌ 新增課程失敗: {e}")
        await interaction.response.send_message(f"❌ 新增課程失敗：{str(e)}", ephemeral=True)
        connection.rollback()
    finally:
        connection.close()

@schedule_group.command(name="today", description="查詢今日課表")
async def today_schedule(interaction: discord.Interaction):
    today_str = datetime.now().strftime("%Y-%m-%d")
    schedule = load_schedule()
    
    embed = discord.Embed(
        title=f"📅 今日課表 ({today_str})",
        color=discord.Color.green()
    )

    if today_str in schedule and schedule[today_str]:
        for course in schedule[today_str]:
            embed.add_field(
                name=f"⏰ {course['time']} - {course['title']}",
                value=f"**知識分享者:** {course['teacher']}\n**內容:** {course['description']}",
                inline=False
            )
    else:
        embed.description = "今天沒有安排任何課程喔！"
        
    await interaction.response.send_message(embed=embed)

@schedule_group.command(name="view", description="查詢指定日期的課表")
@app_commands.describe(date="要查詢的日期 (格式：YYYY-MM-DD)")
async def view_schedule(interaction: discord.Interaction, date: str):
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        await interaction.response.send_message("日期格式錯誤！請使用 `YYYY-MM-DD` 格式。", ephemeral=True)
        return

    schedule = load_schedule()
    
    embed = discord.Embed(
        title=f"📅 {date} 課表",
        color=discord.Color.blue()
    )

    if date in schedule and schedule[date]:
        for course in schedule[date]:
            embed.add_field(
                name=f"⏰ {course['time']} - {course['title']}",
                value=f"**知識分享者:** {course['teacher']}\n**內容:** {course['description']}",
                inline=False
            )
    else:
        embed.description = f"在 {date} 沒有安排任何課程。"
        
    await interaction.response.send_message(embed=embed)

@schedule_group.command(name="cancel", description="取消一堂課")
@app_commands.describe(
    date="課程日期 (格式：YYYY-MM-DD)",
    time="課程時間 (格式：HH:MM)"
)
async def cancel_class(interaction: discord.Interaction, date: str, time: str):
    # 權限檢查
    if not any(role.name == '鎮民' for role in interaction.user.roles):
        await interaction.response.send_message("抱歉，只有鎮民才能取消課程。", ephemeral=True)
        return

    try:
        datetime.strptime(date, "%Y-%m-%d")
        datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.response.send_message("日期或時間格式錯誤！請使用 `YYYY-MM-DD` 和 `HH:MM` 格式。", ephemeral=True)
        return

    # 從 MariaDB 直接刪除課程
    connection = get_db_connection()
    if not connection:
        await interaction.response.send_message("❌ 無法連接資料庫，請稍後重試。", ephemeral=True)
        return

    try:
        cursor = connection.cursor(dictionary=True)
        
        # 先查詢課程是否存在，並確認是本人的課程
        select_query = """
            SELECT id, title FROM schedule 
            WHERE date = %s AND time = %s AND teacher = %s
        """
        cursor.execute(select_query, (date, time, interaction.user.display_name))
        course = cursor.fetchone()
        
        if not course:
            # 沒找到或不是本人的課程
            cursor.execute("""
                SELECT title FROM schedule 
                WHERE date = %s AND time = %s
            """, (date, time))
            
            if cursor.fetchone():
                await interaction.response.send_message("❌ 您只能取消自己的課程。", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ 在 {date} {time} 沒有找到課程。", ephemeral=True)
            
            cursor.close()
            connection.close()
            return
        
        # 刪除課程
        delete_query = """
            DELETE FROM schedule 
            WHERE date = %s AND time = %s AND teacher = %s
        """
        cursor.execute(delete_query, (date, time, interaction.user.display_name))
        connection.commit()
        
        await interaction.response.send_message(
            f"✅ 課程 **{course['title']}** (於 {date} {time}) 已成功取消。",
            ephemeral=True
        )
        print(f"{interaction.user.display_name} 取消了課程：{date} {time} - {course['title']}")
        
        cursor.close()
        
    except Error as e:
        print(f"❌ 取消課程失敗: {e}")
        await interaction.response.send_message(f"❌ 取消課程失敗：{str(e)}", ephemeral=True)
        connection.rollback()
    finally:
        connection.close()

bot.tree.add_command(schedule_group)

# 1. 一對一輔導申請指令
@bot.tree.command(name="apply-tutoring", description="申請一對一線上輔導")
async def apply_tutoring(interaction: discord.Interaction):
    await interaction.response.send_modal(TutoringApplyModal())

# 2. 匿名檢舉指令
@bot.tree.command(name="report", description="匿名檢舉違規行為")
async def report(interaction: discord.Interaction):
    await interaction.response.send_modal(ReportModal())

# 3. 指令使用指南
@bot.tree.command(name="rules", description="顯示微光鎮所有指令的使用方式")
async def rules(interaction: discord.Interaction):
    embed = discord.Embed(
        title="� 微光鎮指令使用指南",
        description="以下是本伺服器提供的所有指令及其使用方式。",
        color=discord.Color.gold()
    )
    
    # 課表相關指令
    embed.add_field(
        name="課表指令群組 (`/schedule`)",
        value=(
            "• **`/schedule add`**\n"
            "  新增一堂課至課表。需要提供課程日期 (YYYY-MM-DD)、時間 (HH:MM)、標題、描述。\n\n"
            "• **`/schedule today`**\n"
            "  查詢今日的課表。\n\n"
            "• **`/schedule view`**\n"
            "  查詢指定日期的課表。需要提供日期 (YYYY-MM-DD)。\n\n"
            "• **`/schedule cancel`**\n"
            "  取消一堂課。只能取消自己的課程，需要提供日期和時間。"
        ),
        inline=False
    )
    
    # 輔導相關指令
    embed.add_field(
        name="🎓 一對一輔導 (`/apply-tutoring`)",
        value=(
            "申請一對一線上輔導。執行後會彈出申請表，填寫內容包括：\n"
            "• 想請教的主題\n"
            "• 希望的鎮民 (選填)\n"
            "• 可配合的時段\n"
            "• 同意錄音/陪同條款\n\n"
            "提交後鎮民可接單或婉拒，接單時會建立臨時私人討論頻道。"
        ),
        inline=False
    )
    
    # 檢舉相關指令
    embed.add_field(
        name="⚠️ 匿名檢舉 (`/report`)",
        value=(
            "匿名檢舉違規行為。執行後會彈出檢舉表，流程如下：\n"
            "1️⃣ 填寫檢舉對象、事由、補充說明\n"
            "2️⃣ 提交後選擇是否上傳附件：\n"
            "   • 選「新增附件並提交」→ 填寫附件說明 → 在 DM 上傳檔案\n"
            "   • 選「直接提交」→ 無附件直接送出\n\n"
            "系統會將您的檢舉匿名轉交給管理員。"
        ),
        inline=False
    )
    
    # 安全規則
    embed.add_field(
        name="核心互動規則",
        value=(
            "• 所有一對一輔導須透過平台申請，禁止私下約談\n"
            "• 不可私下透過非平台渠道聯繫\n"
            "• 建議所有對談錄音或有第三者陪同以保障雙方權益\n"
            "• 與未成年者互動需更加謹慎，禁止單獨私下見面\n"
            "• 若發現違規行為，可使用 `/report` 進行檢舉"
        ),
        inline=False
    )
    
    embed.set_footer(text="微光鎮感謝您的合作 | 如有問題請聯絡管理員")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# 4. Tag 偏好設置指令（管理員用）
@bot.tree.command(name="setup-tag-preference", description="[管理員] 在指定頻道放置 Tag 偏好設置消息")
@app_commands.default_permissions(administrator=True)
async def setup_tag_preference(interaction: discord.Interaction):
    """在 TAG_PREFERENCE_CHANNEL_ID 頻道中放置 Tag 偏好設置消息"""
    
    # 驗證是否為管理員
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 只有管理員可以執行此指令。", ephemeral=True)
        return
    
    try:
        # 取得目標頻道
        target_channel = bot.get_channel(TAG_PREFERENCE_CHANNEL_ID)
        if not target_channel:
            await interaction.response.send_message(
                f"❌ 找不到 ID 為 {TAG_PREFERENCE_CHANNEL_ID} 的頻道。請檢查 TAG_PREFERENCE_CHANNEL_ID 設定。",
                ephemeral=True
            )
            return
        
        # 建立 Embed 消息
        embed = discord.Embed(
            title="🎯 機器人 Tag 偏好設置",
            description="請選擇您是否願意被本機器人 Tag。\n\n"
                        "✅ **接受 Tag**：機器人可以在任何場合提及您。\n"
                        "❌ **拒絕 Tag**：機器人不會提及您。\n\n"
                        "**默認狀態**：若您未做選擇，將視為可以被 Tag。",
            color=discord.Color.blue()
        )
        embed.set_footer(text="您的選擇將被記錄在系統中")
        
        # 發送消息和按鈕
        view = TagPreferenceView()
        await target_channel.send(embed=embed, view=view)
        
        await interaction.response.send_message(
            f"✅ Tag 偏好設置消息已成功放置在 {target_channel.mention}。",
            ephemeral=True
        )
        print(f"管理員 {interaction.user.name} 在頻道 {target_channel.name} 放置了 Tag 偏好設置消息。")
        
    except Exception as e:
        print(f"❌ 放置 Tag 偏好設置消息失敗: {e}")
        await interaction.response.send_message(
            f"❌ 操作失敗：{str(e)}",
            ephemeral=True
        )

# --- 啟動 Bot ---
bot.run(DISCORD_BOT_TOKEN)
