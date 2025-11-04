import discord
from discord.ext import commands
from discord import app_commands, ui
import os
from dotenv import load_dotenv
import json
from datetime import datetime, timedelta
from typing import Optional, List

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
SCHEDULE_FILE = "schedule.json"
TUTORING_REQUEST_TIMEOUT = 86400  # 24 小時（秒）

# --- 課表資料處理 ---
def load_schedule():
    """從 JSON 檔案載入課表資料"""
    try:
        with open(SCHEDULE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_schedule(data):
    """將課表資料儲存到 JSON 檔案"""
    with open(SCHEDULE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# --- Bot 設定 ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- UI 元件定義 (Modals & Views) ---

# 1. 一對一輔導申請表單 (Modal)
class TutoringApplyModal(ui.Modal, title="一對一輔導申請"):
    topic = ui.TextInput(label="想請教的主題", style=discord.TextStyle.short, placeholder="例如：Python 函式庫使用", required=True)
    tutor = ui.TextInput(label="希望的導師 (選填)", style=discord.TextStyle.short, placeholder="請輸入導師的 Discord ID 或留空", required=False)
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

        embed = discord.Embed(
            title="新的輔導申請",
            description=f"申請人：{interaction.user.mention}",
            color=discord.Color.blue()
        )
        embed.add_field(name="主題", value=self.topic.value, inline=False)
        embed.add_field(name="偏好導師", value=self.tutor.value or "無指定", inline=False)
        embed.add_field(name="可配合時段", value=self.schedule.value, inline=False)
        embed.set_footer(text=f"申請人 ID: {interaction.user.id} | ⏰ 24 小時內回應")

        view = TutoringRequestView(applicant=interaction.user, created_at=datetime.utcnow())
        await request_channel.send(embed=embed, view=view)
        await interaction.response.send_message("您的輔導申請已成功送出，請靜候導師回覆。", ephemeral=True)
        print(f"使用者 {interaction.user} 提交了輔導申請。")

# 1b. 輔導申請的按鈕 (View)
class TutoringRequestView(ui.View):
    def __init__(self, applicant: discord.Member, created_at: datetime):
        super().__init__(timeout=TUTORING_REQUEST_TIMEOUT)  # 24 小時逾時
        self.applicant = applicant
        self.created_at = created_at

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """檢查時間限制與按鈕權限"""
        # 檢查是否超過 24 小時
        if datetime.utcnow() - self.created_at > timedelta(seconds=TUTORING_REQUEST_TIMEOUT):
            await interaction.response.send_message(
                "❌ 此申請已超過 24 小時回應期限，請要求申請人重新提交申請。",
                ephemeral=True
            )
            return False

        # 檢查按鈕權限：只有指定的導師或無指定時任何導師都可操作
        # 這裡使用簡單邏輯，實際可根據申請時的偏好導師進行檢查
        return True

    @ui.button(label="✅ 接單", style=discord.ButtonStyle.success, custom_id="accept_tutoring")
    async def accept(self, interaction: discord.Interaction, button: ui.Button):
        tutor = interaction.user
        category = interaction.channel.category
        
        # 建立臨時私人頻道
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            self.applicant: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            tutor: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        channel_name = f"輔導-{self.applicant.name}-{tutor.name}"
        temp_channel = await interaction.guild.create_text_channel(channel_name, overwrites=overwrites, category=category)

        # 停用原始訊息的按鈕
        self.clear_items()
        original_message = interaction.message
        edited_embed = original_message.embeds[0]
        edited_embed.description = f"申請人：{self.applicant.mention}\n**由 {tutor.mention} 已接單**"
        edited_embed.color = discord.Color.green()
        await original_message.edit(embed=edited_embed, view=self)

        # 通知雙方
        await interaction.response.send_message(f"您已接受 {self.applicant.mention} 的輔導申請。", ephemeral=True)
        await self.applicant.send(f"好消息！您的輔導申請已被導師 {tutor.mention} 接受。\n請至頻道 {temp_channel.mention} 開始討論。")
        
        # 4. 自動化記錄與提醒
        await temp_channel.send(
            f"歡迎 {self.applicant.mention} 與導師 {tutor.mention}！\n"
            f"這裡是你們的專屬討論區。\n\n"
            f"**給導師的提醒**：輔導結束後，請記得在此頻道簡要回報紀錄（例如：討論摘要、花費時間），以作為平台存證，感謝！"
        )
        print(f"導師 {tutor.name} 接受了 {self.applicant.name} 的申請，已建立頻道 {channel_name}。")


    @ui.button(label="❌ 婉拒", style=discord.ButtonStyle.danger, custom_id="decline_tutoring")
    async def decline(self, interaction: discord.Interaction, button: ui.Button):
        self.clear_items()
        original_message = interaction.message
        edited_embed = original_message.embeds[0]
        edited_embed.description = f"申請人：{self.applicant.mention}\n**由 {interaction.user.mention} 已婉拒**"
        edited_embed.color = discord.Color.red()
        await original_message.edit(embed=edited_embed, view=self)

        await interaction.response.send_message(f"您已婉拒此申請。", ephemeral=True)
        await self.applicant.send(f"很遺憾，您的輔導申請已被婉拒。若有疑問請聯繫管理員。")
        print(f"導師 {interaction.user.name} 婉拒了 {self.applicant.name} 的申請。")

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
    # 如果View是設計成永久有效的，需要這段來重載
    bot.add_view(TutoringRequestView(applicant=None, created_at=datetime.utcnow()))
    print(f'Bot is ready to use.')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(e)


# --- 斜線指令定義 ---

# 課表指令群組
schedule_group = app_commands.Group(name="schedule", description="課表相關功能")

@schedule_group.command(name="add", description="[教師用] 新增一堂課至課表")
@app_commands.describe(
    date="課程日期 (格式：YYYY-MM-DD)",
    time="課程時間 (格式：HH:MM)",
    title="課程標題",
    description="課程內容的詳細說明"
)
async def add_class(interaction: discord.Interaction, date: str, time: str, title: str, description: str):
    # 簡易的權限檢查，只允許有 "老師" 角色的使用者操作
    # 在您的伺服器中，請確保教師們都有一個名為 "老師" 的角色
    if not any(role.name == '鎮民' for role in interaction.user.roles):
        await interaction.response.send_message("抱歉，只有教師才能新增課程。", ephemeral=True)
        return

    try:
        # 驗證日期與時間格式
        datetime.strptime(date, "%Y-%m-%d")
        datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.response.send_message("日期或時間格式錯誤！請使用 `YYYY-MM-DD` 和 `HH:MM` 格式。", ephemeral=True)
        return

    schedule = load_schedule()
    if date not in schedule:
        schedule[date] = []

    schedule[date].append({
        "time": time,
        "title": title,
        "description": description,
        "teacher": interaction.user.display_name
    })
    
    # 排序當天的課程
    schedule[date].sort(key=lambda x: x['time'])
    
    save_schedule(schedule)
    
    await interaction.response.send_message(f"✅ 課程 **{title}** 已成功新增至 **{date}** 的課表。", ephemeral=True)
    print(f"教師 {interaction.user.display_name} 新增了課程：{date} {time} - {title}")

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
                value=f"**授課老師:** {course['teacher']}\n**內容:** {course['description']}",
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
                value=f"**授課老師:** {course['teacher']}\n**內容:** {course['description']}",
                inline=False
            )
    else:
        embed.description = f"在 {date} 沒有安排任何課程。"
        
    await interaction.response.send_message(embed=embed)

@schedule_group.command(name="cancel", description="[教師用] 取消一堂課")
@app_commands.describe(
    date="課程日期 (格式：YYYY-MM-DD)",
    time="課程時間 (格式：HH:MM)"
)
async def cancel_class(interaction: discord.Interaction, date: str, time: str):
    # 權限檢查
    if not any(role.name == '鎮民' for role in interaction.user.roles):
        await interaction.response.send_message("抱歉，只有教師才能取消課程。", ephemeral=True)
        return

    try:
        datetime.strptime(date, "%Y-%m-%d")
        datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.response.send_message("日期或時間格式錯誤！請使用 `YYYY-MM-DD` 和 `HH:MM` 格式。", ephemeral=True)
        return

    schedule = load_schedule()
    
    if date not in schedule or not schedule[date]:
        await interaction.response.send_message(f"❌ 在 {date} 沒有任何課程。", ephemeral=True)
        return

    # 尋找要取消的課程
    course_to_cancel = None
    for course in schedule[date]:
        if course['time'] == time:
            course_to_cancel = course
            break

    if course_to_cancel is None:
        await interaction.response.send_message(f"❌ 在 {date} {time} 沒有找到課程。", ephemeral=True)
        return

    # 檢查是否為本人的課程
    if course_to_cancel['teacher'] != interaction.user.display_name:
        await interaction.response.send_message("❌ 您只能取消自己的課程。", ephemeral=True)
        return

    # 移除課程
    schedule[date].remove(course_to_cancel)
    
    # 若該日期已無課程，刪除該日期
    if not schedule[date]:
        del schedule[date]

    save_schedule(schedule)

    await interaction.response.send_message(
        f"✅ 課程 **{course_to_cancel['title']}** (於 {date} {time}) 已成功取消。",
        ephemeral=True
    )
    print(f"教師 {interaction.user.display_name} 取消了課程：{date} {time} - {course_to_cancel['title']}")

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
        name="� 課表指令群組 (`/schedule`)",
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
            "• 希望的導師 (選填)\n"
            "• 可配合的時段\n"
            "• 同意錄音/陪同條款\n\n"
            "提交後導師可接單或婉拒，接單時會建立臨時私人討論頻道。"
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
        name="� 核心互動規則",
        value=(
            "• 所有一對一輔導須透過平台申請，禁止私下約談\n"
            "• 導師與學生不可私下透過非平台渠道聯繫\n"
            "• 建議所有對談錄音或有第三者陪同以保障雙方權益\n"
            "• 與未成年者互動需更加謹慎，禁止單獨私下見面\n"
            "• 若發現違規行為，可使用 `/report` 進行檢舉"
        ),
        inline=False
    )
    
    embed.set_footer(text="微光鎮感謝您的合作 | 如有問題請聯絡管理員")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- 啟動 Bot ---
bot.run(DISCORD_BOT_TOKEN)
