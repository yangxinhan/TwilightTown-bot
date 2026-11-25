import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import os
from dotenv import load_dotenv

from datetime import datetime, timedelta, timezone, time as dt_time
from contextlib import contextmanager
from typing import Optional, List

from mysql.connector import Error, pooling
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from weakref import WeakValueDictionary

from tutoring_store import TutoringStore

logger = logging.getLogger('TwilightTown-Bot')
logger.addHandler(logging.NullHandler())


def configure_logging():
    """Create file/console handlers only when starting the bot."""
    if any(isinstance(handler, RotatingFileHandler) for handler in logger.handlers):
        return
    os.makedirs('logs', exist_ok=True)
    file_handler = RotatingFileHandler(
        'logs/bot.log', maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


def safe_db_call(operation):
    """Cleanup on a dropped connection must not hide the original failure."""
    try:
        operation()
    except Error:
        logger.warning("資料庫資源清理失敗", exc_info=True)


def close_db_resources(cursor, connection):
    if cursor is not None:
        safe_db_call(cursor.close)
    if connection is not None:
        safe_db_call(connection.close)


@contextmanager
def database_cursor(*, write=False, dictionary=False):
    connection = get_db_connection()
    if connection is None:
        raise Error("無法連接資料庫")
    cursor = None
    try:
        cursor = connection.cursor(dictionary=dictionary, buffered=True)
        yield cursor
        if write:
            connection.commit()
    except Exception:
        if write:
            safe_db_call(connection.rollback)
        raise
    finally:
        close_db_resources(cursor, connection)


def set_tag_preference(user_id, accepted):
    with database_cursor(write=True) as cursor:
        cursor.execute("SELECT id FROM user_tag_preferences WHERE user_id = %s", (user_id,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE user_tag_preferences SET tag_accepted = %s, updated_at = NOW() WHERE user_id = %s",
                (accepted, user_id),
            )
        else:
            cursor.execute(
                """INSERT INTO user_tag_preferences (user_id, tag_accepted, created_at, updated_at)
                   VALUES (%s, %s, NOW(), NOW())""", (user_id, accepted),
            )


class TagPreferenceView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _set_preference(self, interaction, accepted):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.to_thread(set_tag_preference, interaction.user.id, accepted)
        except Error:
            logger.exception("設定 Tag 偏好失敗 (user_id=%s)", interaction.user.id)
            await interaction.followup.send("❌ 設定失敗，請稍後重試。", ephemeral=True)
            return
        message = "✅ 感謝！您已同意接受 Tag。" if accepted else "❌ 已記錄。您的 Tag 將被設為不接受。"
        await interaction.followup.send(message, ephemeral=True)
        await send_log(f"使用者 {interaction.user.id} 已{'接受' if accepted else '拒絕'} Tag。", "SUCCESS")

    @ui.button(label="✅ 接受 Tag", style=discord.ButtonStyle.success, custom_id="accept_tag")
    async def accept_tag(self, interaction: discord.Interaction, button: ui.Button):
        await self._set_preference(interaction, True)

    @ui.button(label="❌ 拒絕 Tag", style=discord.ButtonStyle.danger, custom_id="decline_tag")
    async def decline_tag(self, interaction: discord.Interaction, button: ui.Button):
        await self._set_preference(interaction, False)

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
CHANNEL_ID_SETTINGS = (
    'ADMIN_CHANNEL_ID', 'TUTORING_REQUEST_CHANNEL_ID', 'TUTORING_CATEGORY_ID',
    'TUTORING_ARCHIVE_CATEGORY_ID', 'TAG_PREFERENCE_CHANNEL_ID',
    'DAILY_SCHEDULE_CHANNEL_ID', 'DISCONNECTION_NOTIFICATION_CHANNEL_ID',
)


def load_configuration(*, required=False):
    """Allow offline imports; validate required settings at process startup."""
    settings = {}
    for name in (*CHANNEL_ID_SETTINGS, 'LOG_CHANNEL_ID'):
        raw = os.getenv(name, '0')
        try:
            value = int(raw)
        except ValueError:
            if required:
                raise ValueError(f"請設定有效的 {name}") from None
            value = 0
        if required and ((name in CHANNEL_ID_SETTINGS and value <= 0)
                         or (name == 'LOG_CHANNEL_ID' and value < 0)):
            raise ValueError(f"請設定有效的 {name}")
        settings[name] = value
    token = os.getenv('DISCORD_BOT_TOKEN')
    if required and not token:
        raise ValueError("請設定 DISCORD_BOT_TOKEN")
    globals().update(settings)
    globals()['DISCORD_BOT_TOKEN'] = token
    globals()['DB_CONFIG'] = {
        'host': os.getenv('DB_HOST'), 'user': os.getenv('DB_USER'),
        'password': os.getenv('DB_PASSWORD'), 'database': os.getenv('DB_NAME'),
        'charset': 'utf8mb4', 'use_unicode': True,
    }


load_configuration()

db_pool = None


def initialize_database():
    global db_pool
    try:
        db_pool = pooling.MySQLConnectionPool(
            pool_name="twilight_pool",
            pool_size=10,
            pool_reset_session=True,
            **DB_CONFIG
        )
        logger.info("✅ 資料庫連接池建立成功")
    except Error as e:
        logger.error(f"❌ 資料庫連接池建立失敗: {e}")
        db_pool = None

# --- 日志發送函數 ---
async def send_log(message: str, level: str = "INFO", embed: discord.Embed = None):
    """
    將日志發送到Discord頻道和文件日志
    :param message: 日志消息
    :param level: 日志級別 (INFO, WARNING, ERROR, SUCCESS)
    :param embed: 可選的Discord Embed對象
    """
    # 記錄到文件和控制台
    if level == "ERROR":
        logger.error(message)
    elif level == "WARNING":
        logger.warning(message)
    elif level == "SUCCESS":
        logger.info(f"✅ {message}")
    else:
        logger.info(message)
    
    # 發送到Discord頻道
    try:
        if LOG_CHANNEL_ID and LOG_CHANNEL_ID != 0:
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                if embed:
                    await log_channel.send(embed=embed)
                else:
                    # 根據日志級別選擇顏色
                    color_map = {
                        "ERROR": discord.Color.red(),
                        "WARNING": discord.Color.orange(),
                        "SUCCESS": discord.Color.green(),
                        "INFO": discord.Color.blue()
                    }
                    
                    embed = discord.Embed(
                        title=f"📝 {level}",
                        description=message,
                        color=color_map.get(level, discord.Color.default()),
                        timestamp=datetime.now(timezone.utc)
                    )
                    await log_channel.send(embed=embed)
    except Exception as e:
        logger.warning(f"無法發送日志到Discord: {e}")

# --- 資料庫連接函數 ---
def get_db_connection():
    """從連接池獲取資料庫連接"""
    if not db_pool:
        return None
    try:
        connection = db_pool.get_connection()
        return connection
    except Error as e:
        error_msg = f"❌ 從連接池獲取資料庫連接失敗: {e}"
        logger.error(error_msg)
        print(error_msg)
        return None

# --- Tag 偏好檢查函數 ---
def can_tag_user(user_id: int) -> bool:
    """
    檢查使用者是否可被 Tag
    - True: 可以 Tag（使用者接受或沒有設定過）
    - False: 不可以 Tag（使用者明確拒絕）
    """
    try:
        with database_cursor(dictionary=True) as cursor:
            cursor.execute(
                "SELECT tag_accepted FROM user_tag_preferences WHERE user_id = %s", (user_id,)
            )
            result = cursor.fetchone()
            return True if result is None else result['tag_accepted']
    except Error:
        logger.warning("檢查 Tag 偏好失敗", exc_info=True)
        return True


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

# --- Bot 設定 ---
class TwilightBot(commands.Bot):
    async def setup_hook(self):
        # Register once per process. Each click reloads durable state from the DB.
        self.add_view(TutoringRequestView())
        self.add_view(CloseTutoringChannelView())
        self.add_view(TagPreferenceView())


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = TwilightBot(command_prefix="!", intents=intents)

tutoring_store = TutoringStore(get_db_connection)
_tutoring_locks = WeakValueDictionary()


def tutoring_lock(kind, record_id):
    key = (kind, record_id)
    lock = _tutoring_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _tutoring_locks[key] = lock
    return lock


async def tutoring_mention(user):
    if await asyncio.to_thread(can_tag_user, user.id):
        return user.mention
    return f"@{user.display_name}"


async def notify_tutoring_user(user_id, content=None, *, embed=None):
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(content, embed=embed)
    except discord.HTTPException:
        logger.warning("無法發送輔導通知給使用者 %s", user_id, exc_info=True)


class TutoringOutcomeUnknown(Exception):
    """The DB cannot confirm whether a Discord channel was recorded."""

# 追蹤斷線狀態的全局變數
bot.disconnection_notified = False

# --- UI 元件定義 (Modals & Views) ---

# 1. 一對一輔導申請表單 (Modal)
class TutoringApplyModal(ui.Modal, title="一對一輔導申請"):
    topic = ui.TextInput(label="想請教的主題", style=discord.TextStyle.short, placeholder="例如：Python 函式庫使用", required=True, max_length=255)
    tutor = ui.TextInput(label="希望的鎮民 (選填)", style=discord.TextStyle.short, placeholder="請輸入鎮民的 Discord ID 或留空", required=False)
    schedule = ui.TextInput(label="可配合的時段", style=discord.TextStyle.paragraph, placeholder="例如：平日晚上 8-10 點、週末下午", required=True, max_length=1024)
    agreement = ui.TextInput(label="請輸入「同意」以接受錄音/陪同條款", style=discord.TextStyle.short, placeholder="同意", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if self.agreement.value.strip() != "同意":
            await interaction.response.send_message("您必須輸入「同意」才能提交申請。", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("請在伺服器內提交輔導申請。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        request_channel = interaction.guild.get_channel(TUTORING_REQUEST_CHANNEL_ID)
        if request_channel is None:
            await interaction.followup.send("❌ 找不到輔導申請頻道，請聯絡管理員。", ephemeral=True)
            return

        preferred_tutor = None
        tutor_value = self.tutor.value.strip()
        if tutor_value:
            if tutor_value.isdigit():
                preferred_tutor = interaction.guild.get_member(int(tutor_value))
                if preferred_tutor is None:
                    try:
                        preferred_tutor = await interaction.guild.fetch_member(int(tutor_value))
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        await interaction.followup.send("❌ 無法查詢指定鎮民，請稍後重試。", ephemeral=True)
                        return
            else:
                matches = [member for member in interaction.guild.members
                           if tutor_value.casefold() in (member.display_name.casefold(), member.name.casefold())]
                if len(matches) == 1:
                    preferred_tutor = matches[0]
            if preferred_tutor is None:
                await interaction.followup.send("❌ 找不到唯一的指定鎮民，請改用 Discord ID，或留空讓其他鎮民接單。", ephemeral=True)
                return
            if preferred_tutor.id == interaction.user.id or not any(role.name == '鎮民' for role in preferred_tutor.roles):
                await interaction.followup.send("❌ 請指定自己以外、具有鎮民身分的導師。", ephemeral=True)
                return

        applicant_mention = await tutoring_mention(interaction.user)
        tutor_display = await tutoring_mention(preferred_tutor) if preferred_tutor else "無指定"
        embed = discord.Embed(title="新的輔導申請", description=f"申請人：{applicant_mention}", color=discord.Color.blue())
        embed.add_field(name="主題", value=self.topic.value, inline=False)
        embed.add_field(name="偏好鎮民", value=tutor_display, inline=False)
        embed.add_field(name="可配合時段", value=self.schedule.value, inline=False)
        embed.set_footer(text=f"申請人 ID: {interaction.user.id} | ⏰ 24 小時內回應")

        request_id = None
        message = None
        published = False
        try:
            request_id = await asyncio.to_thread(tutoring_store.create_request, interaction.user, self.topic.value, preferred_tutor, self.schedule.value)
            # Publish buttons only after the message ID is durable.
            message = await request_channel.send(embed=embed)
            try:
                await asyncio.to_thread(tutoring_store.attach_message, request_id, message.id)
            except Error:
                # A commit may succeed even when its response is lost. Do not
                # delete the message until a fresh read confirms the outcome.
                try:
                    recorded = await asyncio.to_thread(tutoring_store.get_request, message.id)
                except Error:
                    await interaction.followup.send(
                        "⚠️ 無法確認申請結果，請聯絡管理員核對，暫勿重複提交。", ephemeral=True
                    )
                    logger.exception("無法確認申請 %s 的訊息是否已記錄", request_id)
                    return
                if recorded is None:
                    raise
            published = True
            await message.edit(view=TutoringRequestView())
        except (Error, discord.HTTPException):
            logger.exception("發布輔導申請失敗 (request_id=%s)", request_id)
            if published:
                # The request is durable; an administrator can restore the buttons.
                await interaction.followup.send("⚠️ 申請已記錄，但按鈕顯示失敗；請聯絡管理員，無須重複提交。", ephemeral=True)
                return
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.exception("無法移除未發布的輔導訊息 %s", message.id)
            if request_id is not None:
                try:
                    await asyncio.to_thread(tutoring_store.discard_unpublished_request, request_id)
                except Error:
                    logger.exception("無法清理未發布的申請 %s", request_id)
            await interaction.followup.send("❌ 申請未完成，請稍後重試；若頻道留有無按鈕訊息，請通知管理員。", ephemeral=True)
            return

        await interaction.followup.send("您的輔導申請已成功送出，請靜候回覆。", ephemeral=True)
        if preferred_tutor:
            try:
                await request_channel.send(f"📢 {tutor_display}，有一則輔導申請指定了您！")
            except discord.HTTPException:
                logger.warning("輔導申請 %s 的指定導師通知失敗", request_id, exc_info=True)
        await send_log(f"✅ 使用者 {interaction.user.name} (ID: {interaction.user.id}) 提交輔導申請 (ID: {request_id})", "SUCCESS")


class CloseTutoringChannelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🔒 關閉輔導", style=discord.ButtonStyle.danger, custom_id="close_tutoring_channel")
    async def close_channel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("❌ 請在輔導頻道使用此按鈕。", ephemeral=True)
            return
        async with tutoring_lock('channel', interaction.channel_id):
            try:
                record = await asyncio.to_thread(tutoring_store.get_channel, interaction.channel_id)
                if record is None or interaction.user.id not in (record['applicant_id'], record['tutor_id']):
                    await interaction.followup.send("❌ 只有該輔導的申請人或導師可以關閉頻道。", ephemeral=True)
                    return
                if record['status'] != 'active':
                    try:
                        await interaction.message.edit(view=None)
                    except discord.HTTPException:
                        pass
                    await interaction.followup.send("ℹ️ 此輔導已關閉。", ephemeral=True)
                    return
                channel = interaction.channel
                archive_category = interaction.guild.get_channel(TUTORING_ARCHIVE_CATEGORY_ID)
                if not isinstance(archive_category, discord.CategoryChannel):
                    await interaction.followup.send("❌ 找不到頻道存放區類別，請聯絡管理員。", ephemeral=True)
                    return
                # Apply permissions and category in one Discord operation. Keep the
                # bot writable so it can finish posting the archive confirmation.
                overwrites = channel.overwrites
                overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(view_channel=False, send_messages=False)
                for user_id in (record['applicant_id'], record['tutor_id']):
                    member = interaction.guild.get_member(user_id)
                    target = member or discord.Object(id=user_id, type=discord.Member)
                    overwrite = overwrites.get(target, discord.PermissionOverwrite())
                    overwrite.view_channel = True
                    overwrite.send_messages = False
                    overwrites[target] = overwrite
                overwrites[interaction.guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                new_name = channel.name if channel.name.startswith("已關閉-") else f"已關閉-{channel.name}"
                await channel.edit(category=archive_category, name=new_name[:100], overwrites=overwrites, sync_permissions=False)
                try:
                    await asyncio.to_thread(tutoring_store.close_channel, channel.id)
                except Error:
                    # A lost commit response may still have persisted the close.
                    confirmed = await asyncio.to_thread(tutoring_store.get_channel, channel.id)
                    if confirmed is None or confirmed['status'] != 'closed':
                        raise
            except (Error, discord.HTTPException):
                logger.exception("關閉輔導頻道失敗 %s", interaction.channel_id)
                await interaction.followup.send("❌ 關閉未完成，請重試或聯絡管理員。", ephemeral=True)
                return
            try:
                await interaction.message.edit(view=None)
                await channel.send(embed=discord.Embed(title="📋 輔導已結束", description="此頻道已封存。申請人與導師可查看紀錄，無法繼續發言。", color=discord.Color.greyple()))
            except discord.HTTPException:
                logger.warning("輔導已關閉，但封存通知未完成 %s", channel.id, exc_info=True)
            await interaction.followup.send("✅ 輔導已關閉並封存。", ephemeral=True)
            await send_log(f"✅ 輔導頻道 {channel.id} 已由 {interaction.user.id} 關閉。", "SUCCESS")


class TutoringRequestView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _get_actionable_request(self, interaction):
        if interaction.guild is None or interaction.channel_id != TUTORING_REQUEST_CHANNEL_ID:
            await interaction.followup.send("❌ 請在輔導申請頻道操作。", ephemeral=True)
            return None
        request = await asyncio.to_thread(tutoring_store.get_request, interaction.message.id)
        if request is None:
            await interaction.followup.send("❌ 找不到此申請紀錄，請聯絡管理員。", ephemeral=True)
            return None
        if request['status'] != 'pending' or request['expired']:
            await interaction.followup.send("ℹ️ 此申請已處理或超過 24 小時期限。", ephemeral=True)
            return None
        if interaction.user.id == request['applicant_id'] or not any(role.name == '鎮民' for role in interaction.user.roles):
            await interaction.followup.send("❌ 只有申請人以外、具有鎮民身分的導師可以處理申請。", ephemeral=True)
            return None
        if request['preferred_tutor_id'] and interaction.user.id != request['preferred_tutor_id']:
            await interaction.followup.send("❌ 只有指定的鎮民可以接受或婉拒此申請。", ephemeral=True)
            return None
        return request

    async def _mark_message(self, interaction, text, color):
        embed = interaction.message.embeds[0].copy() if interaction.message.embeds else discord.Embed(title="輔導申請")
        embed.description = text
        embed.color = color
        try:
            await interaction.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            # Durable status prevents another action even if Discord cannot edit.
            logger.warning("無法更新輔導訊息 %s", interaction.message.id, exc_info=True)

    @ui.button(label="✅ 接單", style=discord.ButtonStyle.success, custom_id="accept_tutoring")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with tutoring_lock('request', interaction.message.id):
            temp_channel = None
            try:
                request = await self._get_actionable_request(interaction)
                if request is None:
                    return
                guild = interaction.guild
                category = guild.get_channel(TUTORING_CATEGORY_ID)
                if not isinstance(category, discord.CategoryChannel):
                    await interaction.followup.send("❌ 找不到輔導頻道類別，請聯絡管理員。", ephemeral=True)
                    return
                applicant = guild.get_member(request['applicant_id'])
                if applicant is None:
                    try:
                        applicant = await guild.fetch_member(request['applicant_id'])
                    except discord.NotFound:
                        await interaction.followup.send("❌ 申請人已離開伺服器，無法接受此輔導申請。", ephemeral=True)
                        return
                tutor = interaction.user
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    applicant: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                    tutor: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                    guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
                }
                temp_channel = await guild.create_text_channel(f"輔導-{applicant.name}-{tutor.name}"[:100], overwrites=overwrites, category=category)
                # Include the close control before committing success. If posting
                # fails, the channel is removed and the request stays pending.
                applicant_mention, tutor_mention = await asyncio.gather(tutoring_mention(applicant), tutoring_mention(tutor))
                await temp_channel.send(
                    f"歡迎 {applicant_mention} 與 {tutor_mention}！\n這裡是你們的專屬討論區。\n\n"
                    "**提醒**：輔導結束後，請在此頻道簡要回報討論摘要與花費時間，以作為平台存證。",
                    view=CloseTutoringChannelView(),
                )
                completed = await self._record_acceptance(request, temp_channel, applicant, tutor)
                if not completed:
                    await self._remove_uncommitted_channel(temp_channel)
                    await interaction.followup.send("ℹ️ 此申請已處理或過期，未建立新的輔導。", ephemeral=True)
                    return
            except TutoringOutcomeUnknown:
                logger.exception("接單提交結果無法確認，保留頻道 %s 供管理員核對", temp_channel.id)
                await interaction.followup.send("⚠️ 頻道已建立，但無法確認資料庫結果；請聯絡管理員核對，暫勿重複操作。", ephemeral=True)
                return
            except (Error, discord.HTTPException):
                logger.exception("接受輔導申請失敗 (message_id=%s)", interaction.message.id)
                if temp_channel is not None:
                    await self._remove_uncommitted_channel(temp_channel)
                await interaction.followup.send("❌ 接單未完成，請重試；若出現殘留頻道，請通知管理員。", ephemeral=True)
                return
            await self._mark_message(interaction, f"申請人：{applicant_mention}\n**由 {tutor_mention} 已接單**", discord.Color.green())
            await interaction.followup.send(f"✅ 已接受輔導申請，請至 {temp_channel.mention}。", ephemeral=True)
            await notify_tutoring_user(applicant.id, f"好消息！您的輔導申請已被 {tutor_mention} 接受。\n請至頻道 {temp_channel.mention} 開始討論。")
            await send_log(f"✅ 導師 {tutor.id} 接受申請 {request['id']}，已建立頻道 {temp_channel.id}。", "SUCCESS")

    async def _record_acceptance(self, request, channel, applicant, tutor):
        try:
            return await asyncio.to_thread(
                tutoring_store.finish_request, request['id'], 'accepted',
                (channel.id, channel.name, applicant.id, applicant.display_name, tutor.id, tutor.display_name),
            )
        except Error as commit_error:
            # A dropped commit response can still mean success on the server.
            # Confirm on a fresh connection before deleting a recorded channel.
            try:
                recorded = await asyncio.to_thread(tutoring_store.get_channel, channel.id)
            except Error as lookup_error:
                raise TutoringOutcomeUnknown() from lookup_error
            if recorded is not None:
                return True
            raise commit_error

    async def _remove_uncommitted_channel(self, channel):
        try:
            await channel.delete(reason="輔導接單未完成，清理未記錄頻道")
        except discord.HTTPException:
            logger.exception("需要管理員清理未完成的輔導頻道 %s", channel.id)

    @ui.button(label="❌ 婉拒", style=discord.ButtonStyle.danger, custom_id="decline_tutoring")
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with tutoring_lock('request', interaction.message.id):
            try:
                request = await self._get_actionable_request(interaction)
                if request is None:
                    return
                completed = await asyncio.to_thread(tutoring_store.finish_request, request['id'], 'declined')
            except Error:
                logger.exception("婉拒輔導申請失敗 %s", interaction.message.id)
                await interaction.followup.send("❌ 處理失敗，請稍後重試。", ephemeral=True)
                return
            if not completed:
                await interaction.followup.send("ℹ️ 此申請已處理或過期。", ephemeral=True)
                return
            description = interaction.message.embeds[0].description if interaction.message.embeds else None
            applicant_line = next((line for line in (description or '').splitlines() if line.startswith('申請人：')), None)
            if applicant_line is None:
                applicant = interaction.guild.get_member(request['applicant_id'])
                applicant_display = await tutoring_mention(applicant) if applicant else '（已離開伺服器）'
                applicant_line = f"申請人：{applicant_display}"
            await self._mark_message(interaction, f"{applicant_line}\n**此申請已婉拒**", discord.Color.red())
            await interaction.followup.send("✅ 已婉拒此申請。", ephemeral=True)
            await notify_tutoring_user(request['applicant_id'], "很遺憾，您的輔導申請已被婉拒。若有疑問請聯繫管理員。")
            await send_log(f"ℹ️ 導師 {interaction.user.id} 婉拒申請 {request['id']}。", "INFO")

# 2. 站務申請系統流程
# 2.1 申請類型選擇 Select 菜單
class ApplicationTypeSelect(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="身分轉換申請（未成年 → 已滿18歲）", value="identity_change", emoji="🆔"),
            discord.SelectOption(label="權限異動申請", value="permission_change", emoji="🔐"),
            discord.SelectOption(label="技術或制度問題回報", value="tech_issue", emoji="🔧"),
            discord.SelectOption(label="聯繫小志工團隊", value="volunteer_contact", emoji="👥"),
            discord.SelectOption(label="匿名檢舉", value="anonymous_report", emoji="⚠️"),
        ]
        super().__init__(
            placeholder="請選擇申請類型...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="application_type_select"
        )

    async def callback(self, interaction: discord.Interaction):
        app_type = self.values[0]
        
        if app_type == "identity_change":
            await interaction.response.send_modal(IdentityChangeModal())
        elif app_type == "permission_change":
            await interaction.response.send_modal(PermissionChangeModal())
        elif app_type == "tech_issue":
            await interaction.response.send_modal(TechIssueModal())
        elif app_type == "volunteer_contact":
            await interaction.response.send_modal(VolunteerContactModal())
        elif app_type == "anonymous_report":
            await interaction.response.send_modal(AnonymousReportModal())

class ApplicationTypeView(ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(ApplicationTypeSelect())

# 2.2 身分轉換申請 Modal
class IdentityChangeModal(ui.Modal):
    def __init__(self):
        super().__init__(title="🆔 身分轉換申請")

        self.discord_id_input = ui.TextInput(
            label="Discord ID",
            placeholder="輸入您的 Discord ID（右鍵複製個人資料中的ID）",
            required=True,
            max_length=1024
        )
        self.add_item(self.discord_id_input)

        self.age_confirm_input = ui.TextInput(
            label="確認聲明（請輸入「我確認已年滿 18 歲」以表同意）",
            placeholder="我確認已年滿 18 歲，並理解若虛假申報，將永久失去微光鎮參與資格。",
            required=True,
            max_length=1024
        )
        self.add_item(self.age_confirm_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        confirmation = self.age_confirm_input.value
        if "確認" not in confirmation and "同意" not in confirmation and "已滿" not in confirmation:
            await interaction.response.send_message(
                "❌ 您必須同意年齡聲明才能提交申請。",
                ephemeral=True
            )
            return

        app_data = {
            "type": "identity_change",
            "discord_id": self.discord_id_input.value,
            "is_anonymous": False,
            "current_roles": "未來由系統自動抓取"
        }
        
        await dispatch_official_application(interaction, app_data)

# 2.3 權限異動申請 Modal
class PermissionChangeModal(ui.Modal):
    def __init__(self):
        super().__init__(title="🔐 權限異動申請")

        self.discord_id_input = ui.TextInput(
            label="Discord ID",
            placeholder="輸入您的 Discord ID",
            required=True,
            max_length=1024
        )
        self.add_item(self.discord_id_input)

        self.reason_input = ui.TextInput(
            label="申請原因/說明",
            style=discord.TextStyle.paragraph,
            placeholder="請詳細說明需要權限異動的原因...",
            required=True,
            max_length=1024
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        app_data = {
            "type": "permission_change",
            "discord_id": self.discord_id_input.value,
            "reason": self.reason_input.value,
            "is_anonymous": False
        }
        
        await dispatch_official_application(interaction, app_data)

# 2.4 技術或制度問題回報 Modal
class TechIssueModal(ui.Modal):
    def __init__(self):
        super().__init__(title="🔧 技術或制度問題回報")

        self.discord_id_input = ui.TextInput(
            label="Discord ID (選填)",
            placeholder="輸入您的 Discord ID（若想匿名則可不填）",
            required=False,
            max_length=1024
        )
        self.add_item(self.discord_id_input)

        self.issue_input = ui.TextInput(
            label="問題說明",
            style=discord.TextStyle.paragraph,
            placeholder="請詳細描述遇到的技術或制度問題...",
            required=True,
            max_length=1024
        )
        self.add_item(self.issue_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        app_data = {
            "type": "tech_issue",
            "discord_id": self.discord_id_input.value.strip() or "匿名",
            "issue": self.issue_input.value,
            "is_anonymous": not self.discord_id_input.value.strip() or self.discord_id_input.value.strip() == "匿名"
        }
        
        await dispatch_official_application(interaction, app_data)

# 2.5 聯繫小志工團隊 Modal
class VolunteerContactModal(ui.Modal):
    def __init__(self):
        super().__init__(title="👥 聯繫小志工團隊")

        self.discord_id_input = ui.TextInput(
            label="Discord ID (選填)",
            placeholder="輸入您的 Discord ID（若想匿名則可不填）",
            required=False,
            max_length=1024
        )
        self.add_item(self.discord_id_input)

        self.content_input = ui.TextInput(
            label="企畫內容與說明",
            style=discord.TextStyle.paragraph,
            placeholder="請放上企畫內容、推廣理由、想合作的內容、時間長短等詳細資訊...",
            required=True,
            max_length=1024
        )
        self.add_item(self.content_input)

        self.document_link_input = ui.TextInput(
            label="文件超連結 (選填)",
            style=discord.TextStyle.paragraph,
            placeholder="可貼上相關文件的超連結...",
            required=False,
            max_length=1024
        )
        self.add_item(self.document_link_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        app_data = {
            "type": "volunteer_contact",
            "discord_id": self.discord_id_input.value.strip() or "匿名",
            "content": self.content_input.value,
            "document_link": self.document_link_input.value,
            "is_anonymous": not self.discord_id_input.value.strip() or self.discord_id_input.value.strip() == "匿名"
        }
        
        await dispatch_official_application(interaction, app_data)

# 2.6 匿名檢舉 Modal (升級版)
class AnonymousReportModal(ui.Modal):
    def __init__(self):
        super().__init__(title="⚠️ 匿名檢舉")

        self.target_user_input = ui.TextInput(
            label="檢舉對象的使用者名稱或ID",
            placeholder="請輸入對方的暱稱或 Discord ID",
            required=True,
            max_length=1024
        )
        self.add_item(self.target_user_input)

        self.reason_input = ui.TextInput(
            label="事由",
            style=discord.TextStyle.paragraph,
            placeholder="請詳細說明事發經過...",
            required=True,
            max_length=1024
        )
        self.add_item(self.reason_input)

        self.additional_context_input = ui.TextInput(
            label="補充說明 / 證據連結 (選填)",
            style=discord.TextStyle.paragraph,
            placeholder="可貼上圖片連結、訊息連結或補充資訊",
            required=False,
            max_length=1024
        )
        self.add_item(self.additional_context_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        report_data = {
            "type": "anonymous_report",
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
            max_length=1024
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
        except discord.HTTPException:
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
        except asyncio.TimeoutError:
            try:
                await dm_channel.send("⏱️ 上傳超時，檢舉已取消。若需重新檢舉請使用 `/apply` → 匿名檢舉。")
            except discord.HTTPException:
                logger.warning("無法發送附件上傳逾時通知", exc_info=True)
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
        
        try:
            await dm_channel.send("✅ 您的檢舉已送出，感謝您的回報！")
        except discord.HTTPException:
            logger.warning("無法發送檢舉提交私訊確認", exc_info=True)



async def get_user_display_name(bot: discord.Client, user_id_str: str) -> str:
    """將 Discord ID 轉換為用戶名稱。若無法獲取則返回 ID。"""
    if user_id_str == "匿名":
        return "匿名提交"
    
    try:
        user_id = int(user_id_str)
        user = await bot.fetch_user(user_id)
        if user:
            return f"{user.name}（ID: {user_id}）"
        else:
            return f"未知用戶（ID: {user_id}）"
    except (ValueError, discord.NotFound, discord.HTTPException):
        return f"ID: {user_id_str}"


def embed_field_value(value):
    return str(value or "無提供")[:1024]


async def dispatch_official_application(
    interaction: discord.Interaction,
    app_data: dict
) -> None:
    """將站務申請送至管理員頻道。根據申請類型顯示不同的 Embed。"""
    await interaction.response.defer(ephemeral=True, thinking=True)
    admin_channel_id = ADMIN_CHANNEL_ID
    if not admin_channel_id:
        await interaction.followup.send("錯誤：未設定管理員頻道ID，請聯絡管理員。", ephemeral=True)
        return

    admin_channel = bot.get_channel(int(admin_channel_id))
    if not admin_channel:
        await interaction.followup.send("錯誤：找不到管理員頻道，請聯絡管理員。", ephemeral=True)
        return

    app_type = app_data.get("type")
    is_anonymous = app_data.get("is_anonymous", False)
    
    # 獲取用戶顯示名稱
    discord_id_display = await get_user_display_name(bot, app_data.get("discord_id", ""))
    
    # 根據申請類型生成不同的 Embed
    if app_type == "identity_change":
        embed = discord.Embed(
            title="🆔 身分轉換申請",
            color=discord.Color.blurple()
        )
        embed.add_field(name="申請人", value=embed_field_value(discord_id_display), inline=False)
        embed.add_field(name="申請事由", value="年齡身分轉換（未成年 → 已滿 18）", inline=False)
        embed.add_field(name="聲明", value="申請人已確認聲明內容", inline=False)
        
    elif app_type == "permission_change":
        embed = discord.Embed(
            title="🔐 權限異動申請",
            color=discord.Color.green()
        )
        embed.add_field(name="申請人", value=embed_field_value(discord_id_display), inline=False)
        embed.add_field(name="申請原因", value=embed_field_value(app_data.get("reason")), inline=False)
        
    elif app_type == "tech_issue":
        embed = discord.Embed(
            title="🔧 技術或制度問題回報",
            color=discord.Color.gold()
        )
        # 如果有 discord_id 且不是空字符串，顯示用戶名稱
        if app_data.get("discord_id"):
            embed.add_field(name="回報人", value=embed_field_value(discord_id_display), inline=False)
        embed.add_field(name="問題說明", value=embed_field_value(app_data.get("issue")), inline=False)
        
    elif app_type == "volunteer_contact":
        embed = discord.Embed(
            title="👥 聯繫小志工團隊",
            color=discord.Color.purple()
        )
        # 如果有 discord_id 且不是空字符串，顯示用戶名稱
        if app_data.get("discord_id"):
            embed.add_field(name="聯繫人", value=embed_field_value(discord_id_display), inline=False)
        embed.add_field(name="企畫內容與說明", value=embed_field_value(app_data.get("content")), inline=False)
        if app_data.get("document_link"):
            embed.add_field(name="文件超連結", value=embed_field_value(app_data.get("document_link")), inline=False)
    
    else:
        # 不應該執行到這裡，但為了安全起見
        embed = discord.Embed(title="站務申請", color=discord.Color.default())

    # 添加匿名標籤
    if is_anonymous:
        embed.add_field(name="提交方式", value="✅ 匿名提交", inline=False)
    else:
        embed.add_field(name="提交方式", value="❌ 非匿名提交", inline=False)

    embed.set_footer(text="📨 微光鎮｜站務申請與回報系統")
    embed.timestamp = datetime.now(timezone.utc)

    await admin_channel.send(embed=embed)
    await interaction.followup.send("✅ 您的申請已成功提交，感謝您的支持！", ephemeral=True)
    log_msg = f"✅ 收到一則站務申請：{app_type}，提交方式：{'匿名' if is_anonymous else '非匿名'}，提交人：{discord_id_display}"
    await send_log(log_msg, "SUCCESS")
    print(log_msg)


async def dispatch_anonymous_report(
    interaction: discord.Interaction,
    target_user: str,
    reason: str,
    additional_context: Optional[str] = None,
    attachments: Optional[List[discord.Attachment]] = None
) -> None:
    """將匿名檢舉送至管理員頻道，並處理附件下載。"""
    admin_channel_id = ADMIN_CHANNEL_ID
    if not admin_channel_id:
        await interaction.followup.send("錯誤：未設定管理員頻道ID，請聯絡管理員。", ephemeral=True)
        return

    admin_channel = bot.get_channel(int(admin_channel_id))
    if not admin_channel:
        await interaction.followup.send("錯誤：找不到管理員頻道，請聯絡管理員。", ephemeral=True)
        return

    # 嘗試將檢舉對象轉換為用戶名稱
    target_user_display = await get_user_display_name(bot, target_user)

    embed = discord.Embed(
        title="⚠️ 收到新的匿名檢舉",
        color=discord.Color.orange()
    )
    embed.add_field(name="檢舉對象", value=embed_field_value(target_user_display), inline=False)
    embed.add_field(name="事由", value=embed_field_value(reason), inline=False)

    evidence_lines = []
    if additional_context:
        evidence_lines.append(additional_context)
    if attachments:
        evidence_lines.append(f"附件數量：{len(attachments)}")

    embed.add_field(name="證據/補充", value=embed_field_value("\n".join(evidence_lines) if evidence_lines else "無提供"), inline=False)
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
    log_msg = f"⚠️ 收到一則匿名檢舉，對象：{target_user_display}，附件數量：{len(files_to_forward)}"
    await send_log(log_msg, "WARNING")
    print(log_msg)

# --- Bot 事件監聽 ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    activity = discord.Activity(name="烤羊羊", type=discord.ActivityType.playing, start=datetime.now())
    await bot.change_presence(activity=activity)
    await send_log(f"✅ Bot 已啟動並就緒。版本：{bot.user.name}", "SUCCESS")
    print(f'Bot is ready to use.')
    
    # 在 Bot 首次啟動時發送 Tag 偏好設置消息
    try:
        tag_channel = bot.get_channel(TAG_PREFERENCE_CHANNEL_ID)
        if tag_channel:
            # 檢查頻道中是否已有機器人發送的消息
            try:
                async for message in tag_channel.history(limit=10):
                    if message.author == bot.user and message.embeds and "機器人 Tag 偏好設置" in (message.embeds[0].title or ""):
                        await send_log(f"ℹ️ Tag 偏好設置消息已存在於 {tag_channel.name}，跳過重複發送。", "INFO")
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
                    await send_log(f"✅ Tag 偏好設置消息已發送到 {tag_channel.name}", "SUCCESS")
                    print(f"✅ Tag 偏好設置消息已發送到 {tag_channel.name}")
            except Exception as e:
                error_msg = f"⚠️ 檢查 Tag 偏好消息時出錯: {e}"
                await send_log(error_msg, "WARNING")
                print(error_msg)
    except Exception as e:
        error_msg = f"⚠️ 無法訪問 Tag 偏好頻道: {e}"
        await send_log(error_msg, "WARNING")
        print(error_msg)
    
    try:
        synced = await bot.tree.sync()
        await send_log(f"✅ 已同步 {len(synced)} 個指令", "SUCCESS")
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        error_msg = f"❌ 指令同步失敗: {e}"
        await send_log(error_msg, "ERROR")
        print(error_msg)
    
    # 啟動檢查過期申請的後台任務
    if not check_expired_tutoring_requests.is_running():
        check_expired_tutoring_requests.start()
        await send_log("✅ 後台任務「檢查過期申請」已啟動", "SUCCESS")
    
    # 啟動刪除過期頻道的後台任務
    if not delete_archived_tutoring_channels.is_running():
        delete_archived_tutoring_channels.start()
        await send_log("✅ 後台任務「刪除過期頻道」已啟動", "SUCCESS")
    
    # 啟動每日課表傳送任務
    if not send_daily_schedule.is_running():
        send_daily_schedule.start()
        await send_log("✅ 後台任務「每日課表傳送」已啟動", "SUCCESS")


@bot.event
async def on_disconnect():
    """Bot 斷線時發送訊息到聊天室（避免誤報）"""
    await send_log("⚠️ Bot 已斷線", "WARNING")
    print("⚠️ Bot 已斷線，30秒後檢查是否真的持續斷線...")
    bot.disconnection_notified = False
    
    # 等待 30 秒，檢查是否能重新連接
    await asyncio.sleep(30)
    
    # 如果30秒後仍未重新連接且未發送過通知
    if not bot.is_ready() and not bot.disconnection_notified:
        await send_log("❌ Bot仍未重新連接，發送斷線通知", "ERROR")
        print("❌ Bot仍未重新連接，發送斷線通知")
        bot.disconnection_notified = True
        try:
            # 發送訊息到指定頻道
            notification_channel = bot.get_channel(DISCONNECTION_NOTIFICATION_CHANNEL_ID)
            if notification_channel:
                embed = discord.Embed(
                    title="🔴 機器人掛掉了",
                    description="機器人已持續斷線超過 30 秒，請檢查伺服器狀態。",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_footer(text="如果持續無法連接，請聯絡管理員檢查網路或伺服器")
                await notification_channel.send(embed=embed)
                await send_log(f"✅ 斷線通知已發送到 {notification_channel.name}", "SUCCESS")
                print(f"✅ 斷線通知已發送到 {notification_channel.name}")
        except Exception as e:
            error_msg = f"❌ 無法發送斷線訊息: {e}"
            await send_log(error_msg, "ERROR")
            print(error_msg)
    else:
        await send_log("✅ Bot 已成功重新連接，無需發送通知", "SUCCESS")
        print("✅ Bot 已成功重新連接，無需發送通知")


# 定期檢查過期輔導申請
@tasks.loop(hours=1)
async def check_expired_tutoring_requests():
    """先提交過期狀態，再通知；私訊失敗不影響其他申請。"""
    try:
        expired_requests = await asyncio.to_thread(tutoring_store.expire_requests)
    except Error:
        logger.exception("檢查過期輔導申請失敗")
        return
    request_channel = bot.get_channel(TUTORING_REQUEST_CHANNEL_ID)
    for request in expired_requests:
        if request['message_id'] is None:
            continue
        if request_channel is not None and request['message_id']:
            try:
                message = await request_channel.fetch_message(request['message_id'])
                await message.edit(view=None)
            except discord.HTTPException:
                logger.warning("無法移除過期申請按鈕 %s", request['id'], exc_info=True)
        embed = discord.Embed(
            title="⏰ 輔導申請已過期",
            description=f"您的輔導申請「**{request['topic']}**」已超過 24 小時未被回應。若仍需要輔導，請重新提交申請。",
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"申請 ID: {request['id']}")
        await notify_tutoring_user(request['applicant_id'], embed=embed)
    if expired_requests:
        await send_log(f"⏰ 已將 {len(expired_requests)} 個輔導申請標記為過期。", "INFO")


@check_expired_tutoring_requests.before_loop
async def before_check_expired_tutoring_requests():
    """在啟動後台任務前等待 bot 完全就緒"""
    await bot.wait_until_ready()


def get_archived_tutoring_channels():
    with database_cursor(dictionary=True) as cursor:
        cursor.execute(
            """SELECT id, channel_id, channel_name FROM tutoring_channels
               WHERE status = 'closed' AND closed_at < DATE_SUB(NOW(), INTERVAL 30 DAY)"""
        )
        return cursor.fetchall()


def mark_tutoring_channel_deleted(record_id):
    with database_cursor(write=True) as cursor:
        cursor.execute(
            "UPDATE tutoring_channels SET status = %s, deleted_at = NOW() WHERE id = %s",
            ('deleted', record_id),
        )


# 定期刪除 30 天前關閉的輔導頻道
@tasks.loop(hours=6)
async def delete_archived_tutoring_channels():
    """Release DB connections before awaiting any Discord operations."""
    try:
        channels = await asyncio.to_thread(get_archived_tutoring_channels)
    except Error:
        logger.exception("檢查過期頻道失敗")
        return
    for record in channels:
        try:
            channel = bot.get_channel(record['channel_id'])
            if channel is not None:
                try:
                    await channel.delete()
                except discord.NotFound:
                    pass  # Already deleted on Discord; finish updating the DB.
            await asyncio.to_thread(mark_tutoring_channel_deleted, record['id'])
            await send_log(f"✅ 已清理過期頻道 {record['channel_name']} (ID: {record['channel_id']})", "SUCCESS")
        except (Error, discord.HTTPException):
            logger.exception("刪除過期頻道失敗 (ID: %s)", record['channel_id'])


@delete_archived_tutoring_channels.before_loop
async def before_delete_archived_tutoring_channels():
    """在啟動後台任務前等待 bot 完全就緒"""
    await bot.wait_until_ready()


# 每日早上 10 點傳送今日課表
@tasks.loop(time=dt_time(hour=10, minute=0, tzinfo=timezone(timedelta(hours=8))))
async def send_daily_schedule():
    """每天早上 10 點傳送今日課表"""
    try:
        # 使用 UTC+8 日期查詢當日課表
        tz = timezone(timedelta(hours=8))
        now = datetime.now(tz)
        
        # 取得課表頻道
        schedule_channel = bot.get_channel(DAILY_SCHEDULE_CHANNEL_ID)
        if not schedule_channel:
            await send_log(f"⚠️ 找不到課表頻道 (ID: {DAILY_SCHEDULE_CHANNEL_ID})", "WARNING")
            print(f"⚠️ 找不到課表頻道 (ID: {DAILY_SCHEDULE_CHANNEL_ID})")
            return
        
        # 查詢今日課表
        today_str = now.strftime("%Y-%m-%d")
        connection = get_db_connection()
        if not connection:
            await send_log("⚠️ 無法連接資料庫進行課表查詢", "WARNING")
            print("⚠️ 無法連接資料庫進行課表查詢")
            return

        cursor = None
        try:
            cursor = connection.cursor(dictionary=True)
            cursor.execute("""
                SELECT stime, etime, title, description, teacher 
                FROM schedule 
                WHERE date = %s 
                ORDER BY stime
            """, (today_str,))
            
            rows = cursor.fetchall()
            
            embed = discord.Embed(
                title=f"📅 今日課表 ({today_str})",
                color=discord.Color.green()
            )

            if rows:
                for course in rows:
                    # 處理時間格式
                    stime = str(course['stime'])
                    if stime.count(':') >= 2:
                        stime = stime.rsplit(':', 1)[0]
                    
                    etime = str(course['etime'])
                    if etime.count(':') >= 2:
                        etime = etime.rsplit(':', 1)[0]
                    
                    embed.add_field(
                        name=f"⏰ {stime}~{etime} {course['title']}",
                        value=f"**知識分享者:** {course['teacher']}\n**內容:** {course['description']}",
                        inline=False
                    )
            else:
                embed.description = "今天沒有安排任何課程喔！"
            
            embed.set_footer(text=f"生成時間：{now.strftime('%H:%M')}")
            await schedule_channel.send(embed=embed)
            await send_log(f"✅ 已於 {now.strftime('%Y-%m-%d %H:%M')} 傳送今日課表到頻道", "SUCCESS")
            print(f"✅ 已於 {now.strftime('%Y-%m-%d %H:%M')} 傳送今日課表到頻道")
            
        except Error as e:
            error_msg = f"❌ 查詢課表失敗: {e}"
            await send_log(error_msg, "ERROR")
            print(error_msg)
        finally:
            close_db_resources(cursor, connection)
            
    except Exception as e:
        error_msg = f"❌ 傳送每日課表時出錯: {e}"
        await send_log(error_msg, "ERROR")
        print(error_msg)


@send_daily_schedule.before_loop
async def before_send_daily_schedule():
    """在啟動後台任務前等待 bot 完全就緒"""
    await bot.wait_until_ready()


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
    if interaction.guild is None:
        await interaction.response.send_message("❌ 請在伺服器中使用此指令。", ephemeral=True)
        return
    # 簡易的權限檢查，只允許有 "鎮民" 角色的使用者操作
    if not any(role.name == '鎮民' for role in interaction.user.roles):
        await interaction.response.send_message("抱歉，只有鎮民才能新增課程。", ephemeral=True)
        await send_log(f"❌ 使用者 {interaction.user.name} (ID: {interaction.user.id}) 試圖新增課程但權限不足", "WARNING")
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
        await send_log(f"❌ 無法連接資料庫以新增課程 (使用者: {interaction.user.name})", "ERROR")
        return

    cursor = None
    try:
        cursor = connection.cursor()
        
        # 檢查是否已存在相同的課程
        check_query = """
            SELECT id FROM schedule 
            WHERE date = %s AND stime = %s AND etime = %s AND title = %s AND teacher = %s
        """
        cursor.execute(check_query, (date, stime, etime, title, interaction.user.display_name))
        
        if cursor.fetchone():
            await interaction.response.send_message("❌ 此課程已存在，請勿重複新增。", ephemeral=True)
            return
        
        # 插入新課程
        insert_query = """
            INSERT INTO schedule (date, stime, etime, title, description, teacher)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        cursor.execute(insert_query, (date, stime, etime, title, description, interaction.user.display_name))
        connection.commit()
        
        await interaction.response.send_message(f"✅ 課程 **{title}** 已成功新增至 **{date}** {stime}-{etime} 的課表。", ephemeral=True)
        log_msg = f"✅ {interaction.user.display_name} (ID: {interaction.user.id}) 新增了課程：{date} {stime}-{etime} - {title}"
        await send_log(log_msg, "SUCCESS")
        print(log_msg)
        
        
    except Error as e:
        error_msg = f"❌ 新增課程失敗: {e}"
        await send_log(error_msg, "ERROR")
        print(error_msg)
        await interaction.response.send_message("❌ 新增課程失敗，請稍後重試。", ephemeral=True)
        safe_db_call(connection.rollback)
    finally:
        close_db_resources(cursor, connection)

@schedule_group.command(name="today", description="查詢今日課表")
async def today_schedule(interaction: discord.Interaction):
    await interaction.response.defer()
    # 使用 UTC+8 時區
    tz = timezone(timedelta(hours=8))
    today_str = datetime.now(tz).strftime("%Y-%m-%d")
    
    connection = get_db_connection()
    if not connection:
        await interaction.followup.send("❌ 無法連接資料庫，請稍後重試。", ephemeral=True)
        await send_log(f"❌ 無法連接資料庫以查詢今日課表 (使用者: {interaction.user.name})", "ERROR")
        return

    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT stime, etime, title, description, teacher 
            FROM schedule 
            WHERE date = %s 
            ORDER BY stime
        """, (today_str,))
        
        rows = cursor.fetchall()
        
        embed = discord.Embed(
            title=f"📅 今日課表 ({today_str})",
            color=discord.Color.green()
        )

        if rows:
            for course in rows:
                # 處理時間格式
                stime = str(course['stime'])
                if stime.count(':') >= 2:
                    stime = stime.rsplit(':', 1)[0]
                
                etime = str(course['etime'])
                if etime.count(':') >= 2:
                    etime = etime.rsplit(':', 1)[0]
                
                embed.add_field(
                    name=f"⏰ {stime}~{etime} {course['title']}",
                    value=f"**知識分享者:** {course['teacher']}\n**內容:** {course['description']}",
                    inline=False
                )
        else:
            embed.description = "今天沒有安排任何課程喔！"
            
        await interaction.followup.send(embed=embed)
        await send_log(f"✅ {interaction.user.name} (ID: {interaction.user.id}) 查詢了今日課表", "SUCCESS")
        
    except Error as e:
        error_msg = f"❌ 查詢課表失敗: {e}"
        await send_log(error_msg, "ERROR")
        print(error_msg)
        await interaction.followup.send("❌ 查詢課表失敗，請稍後重試。", ephemeral=True)
    finally:
        close_db_resources(cursor, connection)

@schedule_group.command(name="view", description="查詢指定日期的課表")
@app_commands.describe(date="要查詢的日期 (格式：YYYY-MM-DD)")
async def view_schedule(interaction: discord.Interaction, date: str):
    await interaction.response.defer()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        await interaction.followup.send("日期格式錯誤！請使用 `YYYY-MM-DD` 格式。", ephemeral=True)
        return

    connection = get_db_connection()
    if not connection:
        await interaction.followup.send("❌ 無法連接資料庫，請稍後重試。", ephemeral=True)
        await send_log(f"❌ 無法連接資料庫以查詢課表 (使用者: {interaction.user.name})", "ERROR")
        return

    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT stime, etime, title, description, teacher 
            FROM schedule 
            WHERE date = %s 
            ORDER BY stime
        """, (date,))
        
        rows = cursor.fetchall()
        
        embed = discord.Embed(
            title=f"📅 {date} 課表",
            color=discord.Color.blue()
        )

        if rows:
            for course in rows:
                stime = str(course['stime'])
                if stime.count(':') >= 2:
                    stime = stime.rsplit(':', 1)[0]
                
                etime = str(course['etime'])
                if etime.count(':') >= 2:
                    etime = etime.rsplit(':', 1)[0]
                
                embed.add_field(
                    name=f"⏰ {stime}~{etime} {course['title']}",
                    value=f"**知識分享者:** {course['teacher']}\n**內容:** {course['description']}",
                    inline=False
                )
        else:
            embed.description = f"在 {date} 沒有安排任何課程。"
            
        await interaction.followup.send(embed=embed)
        await send_log(f"✅ {interaction.user.name} (ID: {interaction.user.id}) 查詢了 {date} 的課表", "SUCCESS")
        
    except Error as e:
        error_msg = f"❌ 查詢課表失敗: {e}"
        await send_log(error_msg, "ERROR")
        print(error_msg)
        await interaction.followup.send("❌ 查詢課表失敗，請稍後重試。", ephemeral=True)
    finally:
        close_db_resources(cursor, connection)

@schedule_group.command(name="edit", description="修改一堂課的資訊")
@app_commands.describe(
    date="課程日期 (格式：YYYY-MM-DD)",
    stime="課程開始時間 (格式：HH:MM)",
    new_date="新的課程日期 (格式：YYYY-MM-DD，留空則不改)",
    new_stime="新的課程開始時間 (格式：HH:MM，留空則不改)",
    new_etime="新的課程結束時間 (格式：HH:MM，留空則不改)",
    new_title="新的課程標題 (留空則不改)",
    new_description="新的課程內容說明 (留空則不改)"
)
async def edit_class(interaction: discord.Interaction, date: str, stime: str, 
                     new_date: str = None, new_stime: str = None, new_etime: str = None,
                     new_title: str = None, new_description: str = None):
    if interaction.guild is None:
        await interaction.response.send_message("❌ 請在伺服器中使用此指令。", ephemeral=True)
        return
    # 權限檢查
    if not any(role.name == '鎮民' for role in interaction.user.roles):
        await interaction.response.send_message("抱歉，只有鎮民才能修改課程。", ephemeral=True)
        await send_log(f"❌ 使用者 {interaction.user.name} (ID: {interaction.user.id}) 試圖修改課程但權限不足", "WARNING")
        return

    try:
        # 驗證原始日期與時間格式
        datetime.strptime(date, "%Y-%m-%d")
        datetime.strptime(stime, "%H:%M")
        
        # 驗證新的日期與時間格式（如果提供）
        if new_date:
            datetime.strptime(new_date, "%Y-%m-%d")
        if new_stime:
            datetime.strptime(new_stime, "%H:%M")
        if new_etime:
            datetime.strptime(new_etime, "%H:%M")
    except ValueError:
        await interaction.response.send_message("日期或時間格式錯誤！請使用 `YYYY-MM-DD` 和 `HH:MM` 格式。", ephemeral=True)
        return

    connection = get_db_connection()
    if not connection:
        await interaction.response.send_message("❌ 無法連接資料庫，請稍後重試。", ephemeral=True)
        await send_log(f"❌ 無法連接資料庫以修改課程 (使用者: {interaction.user.name})", "ERROR")
        return

    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        
        # 查詢原始課程
        select_query = """
            SELECT id, title, etime, description FROM schedule 
            WHERE date = %s AND stime = %s AND teacher = %s
        """
        cursor.execute(select_query, (date, stime, interaction.user.display_name))
        course = cursor.fetchone()
        
        if not course:
            # 沒找到或不是本人的課程
            cursor.execute("""
                SELECT title FROM schedule 
                WHERE date = %s AND stime = %s
            """, (date, stime))
            
            if cursor.fetchone():
                await interaction.response.send_message("❌ 您只能修改自己的課程。", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ 在 {date} {stime} 沒有找到課程。", ephemeral=True)
            
            return
        
        # 準備更新的值（使用原始值或新值）
        update_date = new_date if new_date else date
        update_stime = new_stime if new_stime else stime
        update_etime = new_etime if new_etime else course['etime']
        update_title = new_title if new_title else course['title']
        update_description = new_description if new_description else course['description']
        
        # 檢查是否有課程與新的時間衝突（除了當前課程）
        conflict_query = """
            SELECT id FROM schedule 
            WHERE date = %s AND stime = %s AND etime = %s AND title = %s 
            AND teacher = %s AND id != %s
        """
        cursor.execute(conflict_query, (update_date, update_stime, update_etime, update_title, 
                                        interaction.user.display_name, course['id']))
        
        if cursor.fetchone():
            await interaction.response.send_message("❌ 修改後的課程與其他課程衝突，請檢查時間。", ephemeral=True)
            return
        
        # 更新課程
        update_query = """
            UPDATE schedule 
            SET date = %s, stime = %s, etime = %s, title = %s, description = %s 
            WHERE id = %s
        """
        cursor.execute(update_query, (update_date, update_stime, update_etime, update_title, 
                                      update_description, course['id']))
        connection.commit()
        
        # 構建修改摘要
        changes = []
        if new_date and new_date != date:
            changes.append(f"日期：{date} → {new_date}")
        if new_stime and new_stime != stime:
            changes.append(f"開始時間：{stime} → {new_stime}")
        if new_etime:
            changes.append(f"結束時間：{course['etime']} → {new_etime}")
        if new_title:
            changes.append(f"標題：{course['title']} → {new_title}")
        if new_description:
            changes.append(f"描述已更新")
        
        change_summary = "\n".join(changes) if changes else "沒有進行任何修改"
        
        await interaction.response.send_message(
            f"✅ 課程 **{course['title']}** 已成功修改。\n\n修改內容：\n{change_summary}",
            ephemeral=True
        )
        log_msg = f"✅ {interaction.user.display_name} (ID: {interaction.user.id}) 修改了課程 (ID: {course['id']})：\n{change_summary}"
        await send_log(log_msg, "SUCCESS")
        print(log_msg)
        
        
    except Error as e:
        error_msg = f"❌ 修改課程失敗: {e}"
        await send_log(error_msg, "ERROR")
        print(error_msg)
        await interaction.response.send_message("❌ 修改課程失敗，請稍後重試。", ephemeral=True)
        safe_db_call(connection.rollback)
    finally:
        close_db_resources(cursor, connection)


@schedule_group.command(name="cancel", description="取消一堂課")
@app_commands.describe(
    date="課程日期 (格式：YYYY-MM-DD)",
    time="課程時間 (格式：HH:MM)"
)
async def cancel_class(interaction: discord.Interaction, date: str, time: str):
    if interaction.guild is None:
        await interaction.response.send_message("❌ 請在伺服器中使用此指令。", ephemeral=True)
        return
    # 權限檢查
    if not any(role.name == '鎮民' for role in interaction.user.roles):
        await interaction.response.send_message("抱歉，只有鎮民才能取消課程。", ephemeral=True)
        await send_log(f"❌ 使用者 {interaction.user.name} (ID: {interaction.user.id}) 試圖取消課程但權限不足", "WARNING")
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
        await send_log(f"❌ 無法連接資料庫以取消課程 (使用者: {interaction.user.name})", "ERROR")
        return

    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        
        # 先查詢課程是否存在，並確認是本人的課程
        select_query = """
            SELECT id, title FROM schedule 
            WHERE date = %s AND stime = %s AND teacher = %s
        """
        cursor.execute(select_query, (date, time, interaction.user.display_name))
        course = cursor.fetchone()
        
        if not course:
            # 沒找到或不是本人的課程
            cursor.execute("""
                SELECT title FROM schedule 
                WHERE date = %s AND stime = %s
            """, (date, time))
            
            if cursor.fetchone():
                await interaction.response.send_message("❌ 您只能取消自己的課程。", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ 在 {date} {time} 沒有找到課程。", ephemeral=True)
            
            return
        
        # 刪除課程
        delete_query = """
            DELETE FROM schedule 
            WHERE date = %s AND stime = %s AND teacher = %s
        """
        cursor.execute(delete_query, (date, time, interaction.user.display_name))
        connection.commit()
        
        await interaction.response.send_message(
            f"✅ 課程 **{course['title']}** (於 {date} {time}) 已成功取消。",
            ephemeral=True
        )
        log_msg = f"✅ {interaction.user.display_name} (ID: {interaction.user.id}) 取消了課程：{date} {time} - {course['title']}"
        await send_log(log_msg, "SUCCESS")
        print(log_msg)
        
        
    except Error as e:
        error_msg = f"❌ 取消課程失敗: {e}"
        await send_log(error_msg, "ERROR")
        print(error_msg)
        await interaction.response.send_message("❌ 取消課程失敗，請稍後重試。", ephemeral=True)
        safe_db_call(connection.rollback)
    finally:
        close_db_resources(cursor, connection)

bot.tree.add_command(schedule_group)

@bot.tree.command(name="apply-tutoring", description="申請一對一線上輔導")
async def apply_tutoring(interaction: discord.Interaction):
    await interaction.response.send_modal(TutoringApplyModal())
    await send_log(f"📝 使用者 {interaction.user.name} (ID: {interaction.user.id}) 開啟了輔導申請表", "INFO")

# 2. 匿名檢舉指令
@bot.tree.command(name="apply", description="📨 微光鎮｜站務申請與回報系統")
async def official_application(interaction: discord.Interaction):
    """開啟站務申請系統，允許使用者選擇申請類型"""
    await send_log(f"📝 使用者 {interaction.user.name} (ID: {interaction.user.id}) 開啟了站務申請系統", "INFO")
    await interaction.response.send_message(
        "👋 歡迎使用微光鎮站務申請與回報系統\n\n"
        "請選擇您的申請類型：",
        view=ApplicationTypeView(),
        ephemeral=True
    )

# 3. 指令使用指南
@bot.tree.command(name="rules", description="顯示微光鎮所有指令的使用方式")
async def rules(interaction: discord.Interaction):
    await send_log(f"📝 使用者 {interaction.user.name} (ID: {interaction.user.id}) 查詢了使用指南", "INFO")
    embed = discord.Embed(
        title="微光鎮指令使用指南",
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
            "  取消一堂課。只能取消自己的課程，需要提供日期和時間。\n\n"
            "• **`/schedule edit`**\n"
            "  編輯一堂課。只能編輯自己的課程，需要提供日期和時間。可選提供新的日期、時間、標題、描述進行修改。"
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
        name="📨 站務申請與回報系統 (`/apply`)",
        value=(
            "微光鎮的統一申請與回報平台。執行後會彈出選單，可選擇以下申請類型：\n\n"
            "🆔 **身分轉換申請**（未成年 → 已滿18歲）\n"
            "   • 需提供 Discord ID 並確認年齡聲明\n\n"
            "🔐 **權限異動申請**\n"
            "   • 提供 Discord ID 和申請原因\n\n"
            "🔧 **技術或制度問題回報**\n"
            "   • 回報系統或制度相關問題\n\n"
            "👥 **聯繫小志工團隊**\n"
            "   • 企畫合作、洽詢其他問題\n\n"
            "⚠️ **匿名檢舉**\n"
            "   • 檢舉違規行為，支援附件上傳\n\n"
            "所有申請均支持選擇是否匿名提交。"
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
            "• 若發現違規行為，可使用 `/apply` → 匿名檢舉進行回報"
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
    
    if interaction.guild is None:
        await interaction.response.send_message("❌ 請在伺服器中使用此指令。", ephemeral=True)
        return

    # 驗證是否為管理員
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 只有管理員可以執行此指令。", ephemeral=True)
        await send_log(f"❌ 非管理員 {interaction.user.name} (ID: {interaction.user.id}) 試圖執行管理員指令", "WARNING")
        return
    
    try:
        await send_log(f"⚙️ 管理員 {interaction.user.name} (ID: {interaction.user.id}) 開始設置 Tag 偏好", "INFO")
        # 取得目標頻道
        target_channel = bot.get_channel(TAG_PREFERENCE_CHANNEL_ID)
        if not target_channel:
            await interaction.response.send_message(
                f"❌ 找不到 ID 為 {TAG_PREFERENCE_CHANNEL_ID} 的頻道。請檢查 TAG_PREFERENCE_CHANNEL_ID 設定。",
                ephemeral=True
            )
            await send_log(f"❌ 找不到 Tag 偏好頻道 (ID: {TAG_PREFERENCE_CHANNEL_ID})", "ERROR")
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
        log_msg = f"✅ 管理員 {interaction.user.name} (ID: {interaction.user.id}) 在頻道 {target_channel.name} 放置了 Tag 偏好設置消息。"
        await send_log(log_msg, "SUCCESS")
        print(log_msg)
        
    except Exception as e:
        error_msg = f"❌ 放置 Tag 偏好設置消息失敗: {e}"
        await send_log(error_msg, "ERROR")
        print(error_msg)
        await interaction.response.send_message(
            "❌ 操作失敗，請稍後重試或聯絡管理員。",
            ephemeral=True
        )

# --- 啟動 Bot ---
if __name__ == '__main__':
    load_dotenv()
    load_configuration(required=True)
    configure_logging()
    initialize_database()
    bot.run(DISCORD_BOT_TOKEN)
