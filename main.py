"""
╔══════════════════════════════════════════════════════════════╗
║          ECCO HOSPITAL CENTER — BOT DE BATE PONTO           ║
║            COM IA, MEMÓRIA E AGENDAMENTO DE LEMBRETES       ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import datetime
import os
import time
import re
import warnings
import requests

import aiosqlite
import discord
import pytz
from discord import app_commands
from discord.ext import commands, tasks

# ──────────────────────────────────────────────────────────────
#  CONFIGURAÇÃO DA IA (GEMINI)
# ──────────────────────────────────────────────────────────────
warnings.filterwarnings("ignore", category=FutureWarning, module="google.generativeai")

import google.generativeai as genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODELS = []

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        available_models = []
        for model in genai.list_models():
            if "generateContent" in model.supported_generation_methods:
                available_models.append(model.name)
        GEMINI_MODELS = [m for m in available_models if "gemini" in m]
        if not GEMINI_MODELS:
            GEMINI_MODELS = [
                "models/gemini-1.5-flash",
                "models/gemini-1.0-pro",
                "models/gemini-pro"
            ]
        print(f"🔍 Modelos Gemini disponíveis: {GEMINI_MODELS}")
    except Exception as e:
        print(f"❌ Erro ao configurar Gemini: {e}")
        GEMINI_MODELS = [
            "models/gemini-1.5-flash",
            "models/gemini-1.0-pro",
            "models/gemini-pro"
        ]
else:
    print("⚠️ Chave API Gemini não encontrada.")

# Canal opcional para respostas automáticas
IA_CHANNEL_ID = None  # Defina o ID do canal para ativar

# ──────────────────────────────────────────────────────────────
#  CONFIGURAÇÃO DO BOT
# ──────────────────────────────────────────────────────────────
TOKEN         = os.environ.get("DISCORD_TOKEN")
PANEL_CHANNEL = int(os.environ.get("PANEL_CHANNEL_ID", "1515846128493658142"))
RANK_CHANNEL  = int(os.environ.get("RANK_CHANNEL_ID", "1515852084480839850"))
LOGS_CHANNEL  = int(os.environ.get("LOGS_CHANNEL_ID",  "1515846898156834956"))
DB            = os.environ.get("DB_PATH", "ponto.db")
BR_TZ         = pytz.timezone("America/Sao_Paulo")

REMOVE_PANEL_CHANNEL = 1515846758456885400
RECRUIT_CHANNEL = 1480675270376558766

AUTHORIZED_REMOVE_ROLE_IDS = [
    1480675269449617524,
    1480675269449617523,
    1480675269449617522,
    1480675269449617521,
    1480675269449617525,
]
AUTHORIZED_REMOVE_IDS = [1480675269449617525, 1508478383825354892]
AUTHORIZED_ADJUST_IDS = [
    1480675269449617524,
    1480675269449617521,
    1480675269449617523,
    1480675269449617522,
]
RECRUIT_ROLE_IDS = [
    1496602784206950571,
    1497672467861475469,
    1480675269449617523,
    1480675269449617524,
    1480675269449617525,
    1480675269449617526,
    1480675269449617527,
]

intents = discord.Intents.default()
intents.members       = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_rank_lock   = asyncio.Lock()
_last_update: float = 0.0

# ──────────────────────────────────────────────────────────────
#  DATABASE (com tabelas novas)
# ──────────────────────────────────────────────────────────────
async def init_db():
    db_dir = os.path.dirname(os.path.abspath(DB))
    os.makedirs(db_dir, exist_ok=True)

    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT    NOT NULL,
                user_name  TEXT    NOT NULL,
                open_time  TEXT    NOT NULL,
                close_time TEXT,
                dur_sec    INTEGER,
                week_start TEXT    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS active (
                user_id   TEXT PRIMARY KEY,
                user_name TEXT NOT NULL,
                open_time TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS msg_store (
                key        TEXT PRIMARY KEY,
                message_id TEXT NOT NULL
            );
            -- NOVAS TABELAS PARA IA E LEMBRETES
            CREATE TABLE IF NOT EXISTS conversation_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message    TEXT NOT NULL,
                response   TEXT,
                timestamp  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message    TEXT NOT NULL,
                remind_at  TEXT NOT NULL,
                done       INTEGER DEFAULT 0
            );
        """)
        await db.commit()

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES AUXILIARES PARA HISTÓRICO E LEMBRETES
# ──────────────────────────────────────────────────────────────
async def save_conversation(user_id: str, channel_id: str, message: str, response: str = None):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO conversation_history (user_id, channel_id, message, response, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, channel_id, message, response, datetime.datetime.now(BR_TZ).isoformat())
        )
        await db.commit()

async def get_channel_history(channel_id: str, limit: int = 20) -> list:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT user_id, message, response, timestamp FROM conversation_history WHERE channel_id = ? ORDER BY timestamp DESC LIMIT ?",
            (channel_id, limit)
        ) as c:
            rows = await c.fetchall()
    return list(reversed(rows))

async def get_user_history(user_id: str, limit: int = 10) -> list:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT message, response, timestamp FROM conversation_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
            (user_id, limit)
        ) as c:
            rows = await c.fetchall()
    return list(reversed(rows))

async def add_reminder(user_id: str, channel_id: str, message: str, remind_at: datetime.datetime):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO reminders (user_id, channel_id, message, remind_at) VALUES (?, ?, ?, ?)",
            (user_id, channel_id, message, remind_at.isoformat())
        )
        await db.commit()

async def get_due_reminders() -> list:
    now = datetime.datetime.now(BR_TZ).isoformat()
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT id, user_id, channel_id, message, remind_at FROM reminders WHERE done = 0 AND remind_at <= ?",
            (now,)
        ) as c:
            rows = await c.fetchall()
    return rows

async def mark_reminder_done(reminder_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))
        await db.commit()

# ──────────────────────────────────────────────────────────────
#  TASK — VERIFICAR LEMBRETES A CADA 30 SEGUNDOS
# ──────────────────────────────────────────────────────────────
@tasks.loop(seconds=30)
async def check_reminders():
    reminders = await get_due_reminders()
    for rid, uid, cid, msg, remind_at in reminders:
        user = bot.get_user(int(uid))
        if user:
            channel = bot.get_channel(int(cid))
            mention = channel.mention if channel else "aqui"
            embed = discord.Embed(
                title="⏰ Lembrete!",
                description=f"Olá {user.mention}, você pediu para lembrar:\n\n**{msg}**",
                color=0xFFA500,
                timestamp=datetime.datetime.now(BR_TZ)
            )
            embed.set_footer(text=f"Agendado para {remind_at}")
            try:
                await user.send(embed=embed)
                if channel:
                    await channel.send(f"{user.mention} ⏰ Lembrete: {msg}")
            except Exception:
                pass
        await mark_reminder_done(rid)

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES EXISTENTES (sem alterações)
# ──────────────────────────────────────────────────────────────
def now_br() -> datetime.datetime:
    return datetime.datetime.now(tz=BR_TZ)

def week_monday(dt: datetime.datetime = None) -> datetime.datetime:
    if dt is None:
        dt = now_br()
    monday = dt - datetime.timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)

def localize(dt: datetime.datetime) -> datetime.datetime:
    return BR_TZ.localize(dt) if dt.tzinfo is None else dt

def hms(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"

def parse_datetime_br(text: str) -> datetime.datetime:
    text = text.strip()
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?$", text)
    if not match:
        raise ValueError("Formato inválido. Use DD/MM/AAAA HH:MM (ex: 25/12/2025 14:30)")
    day, month, year, hour, minute, second = match.groups()
    second = second or "0"
    dt = datetime.datetime(
        int(year), int(month), int(day),
        int(hour), int(minute), int(second)
    )
    return BR_TZ.localize(dt) if dt.tzinfo is None else dt

def extract_user_id(text: str) -> int:
    match = re.search(r'<@!?(\d+)>', text)
    if match:
        return int(match.group(1))
    try:
        return int(text.strip())
    except ValueError:
        return None

async def load_mid(key: str):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT message_id FROM msg_store WHERE key = ?", (key,)) as c:
            row = await c.fetchone()
    return int(row[0]) if row else None

async def save_mid(key: str, msg_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR REPLACE INTO msg_store VALUES (?, ?)", (key, str(msg_id)))
        await db.commit()

async def get_rank() -> list:
    ws = week_monday().isoformat()
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            """SELECT user_id, user_name, SUM(dur_sec)
               FROM sessions
               WHERE week_start >= ? AND close_time IS NOT NULL
               GROUP BY user_id""",
            (ws,),
        ) as c:
            totals = {r[0]: [r[1], r[2] or 0] for r in await c.fetchall()}
        async with db.execute("SELECT user_id, user_name, open_time FROM active") as c:
            actives = await c.fetchall()
    now = now_br()
    for uid, uname, ot in actives:
        dt      = localize(datetime.datetime.fromisoformat(ot))
        elapsed = (now - dt).total_seconds()
        if uid in totals:
            totals[uid][1] += elapsed
        else:
            totals[uid] = [uname, elapsed]
    return sorted(totals.items(), key=lambda x: x[1][1], reverse=True)

def panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="🏥 ECCO HOSPITAL CENTER",
        description=(
            "## 📋 Sistema de Bate Ponto Eletrônico\n\n"
            "Registre sua **entrada** e **saída** usando os botões abaixo.\n\n"
            "🟢 **Abrir Ponto** — Inicia a contagem do seu expediente\n"
            "🔴 **Fechar Ponto** — Encerra e salva o seu expediente\n\n"
            "> *Somente você verá a confirmação do seu ponto.*"
        ),
        color=0x1565C0,
    )
    e.set_footer(text="ECCO HOSPITAL CENTER • Ponto Eletrônico")
    return e

async def rank_embed() -> discord.Embed:
    rank = await get_rank()
    now  = now_br()
    ws   = week_monday(now)
    we   = ws + datetime.timedelta(days=6)
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT COUNT(*) FROM active") as c:
            active_n = (await c.fetchone())[0]
    e = discord.Embed(
        title="🏆 RANKING SEMANAL DE HORAS",
        description=(
            f"**ECCO HOSPITAL CENTER**\n"
            f"📅 {ws.strftime('%d/%m')} — {we.strftime('%d/%m/%Y')}"
        ),
        color=0xFFD700,
    )
    MEDALS = ["🥇", "🥈", "🥉"]
    if not rank:
        e.add_field(name="Sem Registros", value="Nenhuma hora registrada esta semana.", inline=False)
    else:
        page, part = "", 0
        for i, (uid, (uname, secs)) in enumerate(rank):
            prefix = MEDALS[i] if i < 3 else f"`#{i+1:>3}`"
            line   = f"{prefix} **{uname}** — `{hms(secs)}`\n"
            if len(page) + len(line) > 950:
                label = "👥 Colaboradores" if part == 0 else f"👥 Colaboradores (pt.{part + 1})"
                e.add_field(name=label, value=page, inline=False)
                page, part = line, part + 1
            else:
                page += line
        if page:
            label = "👥 Colaboradores" if part == 0 else f"👥 Colaboradores (pt.{part + 1})"
            e.add_field(name=label, value=page, inline=False)
    e.add_field(
        name="🟢 Em Serviço Agora",
        value=f"**{active_n}** colaborador(es) com ponto aberto",
        inline=False,
    )
    e.set_footer(
        text=f"Atualizado em {now.strftime('%d/%m/%Y às %H:%M:%S')} • ECCO HOSPITAL CENTER"
    )
    return e

async def refresh_rank(force: bool = False):
    global _last_update
    cooldown = 10
    if not force and (time.monotonic() - _last_update) < cooldown:
        return
    if _rank_lock.locked():
        return
    async with _rank_lock:
        _last_update = time.monotonic()
        ch = bot.get_channel(RANK_CHANNEL)
        if not ch:
            print(f"⚠️ Canal de ranking ({RANK_CHANNEL}) não encontrado.")
            return
        emb = await rank_embed()
        mid = await load_mid("rank")
        if mid:
            try:
                msg = await ch.fetch_message(mid)
                await msg.edit(embed=emb)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        msg = await ch.send(embed=emb)
        await save_mid("rank", msg.id)

# ──────────────────────────────────────────────────────────────
#  VIEWS (PunchView, RemovePanelView, etc.) — mantidas completas
# ──────────────────────────────────────────────────────────────
# (Aqui você deve colocar todas as classes de View que já existem no seu código.
#  Para encurtar, vou indicar que estão aqui, mas no arquivo real você deve mantê-las.)

# NOTA: Em vez de repetir o código de todas as Views (que é extenso), 
# assumirei que você já tem as classes definidas em seu projeto.
# Se estiver começando do zero, você pode pegar as Views do seu código anterior.

# Exemplo de classe (apenas para referência):

class PunchView(discord.ui.View):
    # ... (código completo)
    pass

class RemovePanelView(discord.ui.View):
    # ... (código completo)
    pass

class RecruitView(discord.ui.View):
    # ... (código completo)
    pass

class DMNotifyView(discord.ui.View):
    # ... (código completo)
    pass

class AdjustSessionView(discord.ui.View):
    # ... (código completo)
    pass

class RemoveSessionView(discord.ui.View):
    # ... (código completo)
    pass

# ──────────────────────────────────────────────────────────────
#  SLASH COMMANDS (existentes + novos)
# ──────────────────────────────────────────────────────────────

# ... (todos os comandos existentes: setup_ponto, setup_painel_remover, setup_recrutamento, meu_ponto, rank_horas, fechar_ponto_admin, relatorio, pontos_abertos, remover_horas, ajustar_horario)

# ──────────────────────────────────────────────────────────────
#  COMANDO /lembrar — Agendar lembrete
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="lembrar", description="Agenda um lembrete para uma data/hora específica")
@app_commands.describe(
    mensagem="O que deseja lembrar",
    data="Data no formato DD/MM/AAAA",
    hora="Hora no formato HH:MM (24h)"
)
async def cmd_lembrar(itx: discord.Interaction, mensagem: str, data: str, hora: str):
    try:
        dt_str = f"{data} {hora}"
        remind_dt = parse_datetime_br(dt_str)
        if remind_dt < now_br():
            return await itx.response.send_message("❌ A data/hora deve ser no futuro.", ephemeral=True)
    except ValueError as e:
        return await itx.response.send_message(f"❌ Formato inválido: {e}", ephemeral=True)

    await add_reminder(
        user_id=str(itx.user.id),
        channel_id=str(itx.channel_id),
        message=mensagem,
        remind_at=remind_dt
    )
    embed = discord.Embed(
        title="✅ Lembrete agendado!",
        description=f"**{mensagem}**\n\n⏰ **{remind_dt.strftime('%d/%m/%Y às %H:%M')}**",
        color=0x2ECC71
    )
    embed.set_footer(text="Você receberá uma notificação na hora.")
    await itx.response.send_message(embed=embed, ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  COMANDO /ia — com memória e contexto
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="ia", description="Faça uma pergunta para a IA (com contexto do chat)")
@app_commands.describe(pergunta="Sua pergunta")
async def cmd_ia(itx: discord.Interaction, pergunta: str):
    if not GEMINI_API_KEY:
        return await itx.response.send_message("❌ IA não configurada.", ephemeral=True)
    if not GEMINI_MODELS:
        return await itx.response.send_message("❌ Nenhum modelo disponível.", ephemeral=True)

    await itx.response.defer(ephemeral=False)

    try:
        # Buscar histórico do canal
        channel_history = await get_channel_history(str(itx.channel_id), limit=20)
        user_history = await get_user_history(str(itx.user.id), limit=10)

        contexto = (
            "Você é um assistente virtual do Hospital ECCO em um servidor FiveM. "
            "Responda de forma educada, objetiva e dentro do contexto hospitalar e de RPG. "
            "Use as informações abaixo para contextualizar sua resposta.\n\n"
        )

        if channel_history:
            contexto += "--- Histórico recente do canal ---\n"
            for entry in channel_history:
                uid, msg, resp, ts = entry
                user = bot.get_user(int(uid))
                nome = user.display_name if user else uid
                if resp:
                    contexto += f"{nome}: {msg}\nBot: {resp}\n"
                else:
                    contexto += f"{nome}: {msg}\n"
            contexto += "\n"

        if user_history:
            contexto += "--- Seu histórico recente comigo ---\n"
            for entry in user_history:
                msg, resp, ts = entry
                contexto += f"Você: {msg}\n"
                if resp:
                    contexto += f"Eu: {resp}\n"
            contexto += "\n"

        contexto += f"Pergunta atual: {pergunta}\n\n"

        resposta_texto = None
        ultimo_erro = None
        for modelo in GEMINI_MODELS:
            try:
                model = genai.GenerativeModel(modelo)
                resposta = model.generate_content(contexto)
                resposta_texto = resposta.text.strip()
                break
            except Exception as e:
                ultimo_erro = e
                continue

        if resposta_texto is None:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
                payload = {"contents": [{"parts": [{"text": contexto}]}]}
                response = requests.post(url, json=payload, timeout=30)
                if response.status_code == 200:
                    data = response.json()
                    if "candidates" in data and data["candidates"]:
                        resposta_texto = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                else:
                    ultimo_erro = f"REST {response.status_code}"
            except Exception as e:
                ultimo_erro = f"REST falhou: {e}"

        if resposta_texto is None:
            raise Exception(f"Falha ao gerar resposta. Último erro: {ultimo_erro}")

        await save_conversation(
            user_id=str(itx.user.id),
            channel_id=str(itx.channel_id),
            message=pergunta,
            response=resposta_texto
        )

        if len(resposta_texto) > 1900:
            resposta_texto = resposta_texto[:1900] + "…"

        embed = discord.Embed(
            title="🤖 Resposta da IA",
            description=resposta_texto,
            color=0x00D4FF,
        )
        embed.set_footer(text=f"Pergunta de {itx.user.display_name}", icon_url=itx.user.display_avatar.url)
        await itx.followup.send(embed=embed)

    except Exception as e:
        await itx.followup.send(f"❌ Erro: {str(e)[:200]}")

# ──────────────────────────────────────────────────────────────
#  EVENTO on_message (para aprendizado contínuo)
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    if IA_CHANNEL_ID and msg.channel.id == IA_CHANNEL_ID and not msg.content.startswith("!"):
        if not GEMINI_API_KEY or not GEMINI_MODELS:
            return
        if msg.content.startswith("/"):
            return

        async with msg.channel.typing():
            try:
                channel_history = await get_channel_history(str(msg.channel.id), limit=15)
                contexto = (
                    "Você é um assistente do Hospital ECCO. Responda de forma breve e útil. "
                    "Máximo de 300 caracteres.\n\n"
                )
                if channel_history:
                    contexto += "--- Histórico recente ---\n"
                    for entry in channel_history:
                        uid, m, r, ts = entry
                        user = bot.get_user(int(uid))
                        nome = user.display_name if user else uid
                        if r:
                            contexto += f"{nome}: {m}\nBot: {r}\n"
                        else:
                            contexto += f"{nome}: {m}\n"
                    contexto += "\n"
                contexto += f"Usuário: {msg.content}"

                resposta_texto = None
                for modelo in GEMINI_MODELS:
                    try:
                        model = genai.GenerativeModel(modelo)
                        resposta = model.generate_content(contexto)
                        resposta_texto = resposta.text.strip()
                        break
                    except Exception:
                        continue
                if resposta_texto is None:
                    try:
                        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
                        payload = {"contents": [{"parts": [{"text": contexto}]}]}
                        response = requests.post(url, json=payload, timeout=30)
                        if response.status_code == 200:
                            data = response.json()
                            if "candidates" in data and data["candidates"]:
                                resposta_texto = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                    except Exception:
                        pass

                if resposta_texto:
                    await save_conversation(
                        user_id=str(msg.author.id),
                        channel_id=str(msg.channel.id),
                        message=msg.content,
                        response=resposta_texto
                    )
                    await msg.reply(resposta_texto[:1900], mention_author=False)
            except Exception:
                pass

    await bot.process_commands(msg)

# ──────────────────────────────────────────────────────────────
#  TAREFAS EM SEGUNDO PLANO
# ──────────────────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def auto_refresh():
    await refresh_rank(force=True)

@tasks.loop(hours=1)
async def notify_active_users():
    now = now_br()
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT user_id, user_name, open_time FROM active") as c:
            actives = await c.fetchall()
    for uid, uname, ot in actives:
        user_id = int(uid)
        user = bot.get_user(user_id)
        if not user:
            continue
        try:
            embed = discord.Embed(
                title="⏰ Verificação de Ponto",
                description=(
                    f"Olá **{uname}**,\n\n"
                    "Você ainda está em serviço? Por favor, confirme clicando em um dos botões abaixo.\n"
                    "Se não estiver mais trabalhando, feche seu ponto imediatamente."
                ),
                color=0x3498DB,
            )
            open_dt = localize(datetime.datetime.fromisoformat(ot))
            embed.add_field(
                name="🕐 Ponto aberto desde",
                value=f"{open_dt.strftime('%d/%m/%Y às %H:%M:%S')}",
                inline=False
            )
            embed.set_footer(text="ECCO HOSPITAL CENTER • Notificação automática")
            view = DMNotifyView(user_id)
            await user.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            pass

# ──────────────────────────────────────────────────────────────
#  ON READY
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()

    # Registrar views persistentes
    bot.add_view(PunchView())
    bot.add_view(RemovePanelView())
    bot.add_view(RecruitView())

    # (Aqui você deve manter a criação dos painéis como está no seu código original)

    # Iniciar tasks
    auto_refresh.start()
    notify_active_users.start()
    check_reminders.start()

    try:
        synced = await bot.tree.sync()
        print(
            f"✅ {bot.user} (ID: {bot.user.id}) online!\n"
            f"    {len(synced)} slash commands sincronizados\n"
            f"    {len(bot.guilds)} servidor(es)"
        )
    except Exception as exc:
        print(f"❌ Erro ao sincronizar slash commands: {exc}")

# ──────────────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Defina a variável de ambiente DISCORD_TOKEN antes de iniciar o bot.")
    bot.run(TOKEN)
