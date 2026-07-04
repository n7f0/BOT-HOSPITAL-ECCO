"""
╔══════════════════════════════════════════════════════════════╗
║          ECCO HOSPITAL CENTER — BOT DE BATE PONTO           ║
║        COM IA, MEMÓRIA, APRENDIZADO CONTÍNUO E AVALIAÇÃO    ║
║           COMANDOS: /ativar_ia /desativar_ia /sync          ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import datetime
import os
import time
import re
import warnings
import json
import logging
from collections import defaultdict

import aiohttp
import aiosqlite
import discord
import pytz
from discord import app_commands
from discord.ext import commands, tasks

# ──────────────────────────────────────────────────────────────
#  CONFIGURAÇÃO DE LOGGING (Melhor Prática)
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%d/%m/%Y %H:%M:%S'
)
logger = logging.getLogger('EccoBot')

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
            GEMINI_MODELS = ["models/gemini-1.5-flash", "models/gemini-1.0-pro", "models/gemini-pro"]
        logger.info(f"Modelos Gemini disponíveis: {GEMINI_MODELS}")
    except Exception as e:
        logger.error(f"Erro ao configurar Gemini: {e}")
        GEMINI_MODELS = ["models/gemini-1.5-flash", "models/gemini-1.0-pro", "models/gemini-pro"]
else:
    logger.warning("Chave API Gemini não encontrada.")

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

AUTHORIZED_REMOVE_ROLE_IDS = [1480675269449617524, 1480675269449617523, 1480675269449617522, 1480675269449617521, 1480675269449617525]
AUTHORIZED_REMOVE_IDS = [1480675269449617525, 1508478383825354892]
AUTHORIZED_ADJUST_IDS = [1480675269449617524, 1480675269449617521, 1480675269449617523, 1480675269449617522]
RECRUIT_ROLE_IDS = [1496602784206950571, 1497672467861475469, 1480675269449617523, 1480675269449617524, 1480675269449617525, 1480675269449617526, 1480675269449617527]

STOPWORDS_PTBR = {
    'para', 'como', 'por', 'com', 'uma', 'sobre', 'quando', 'onde', 'qual', 'mais', 'muito', 'pode', 'isso',
    'você', 'aqui', 'este', 'esta', 'está', 'fazer', 'também', 'pelo', 'pela', 'dos', 'das', 'nas', 'nos', 
    'mas', 'que', 'não', 'sim', 'quem', 'seja', 'isso', 'esse', 'essa', 'qualquer', 'mesmo', 'porque', 'quais'
}

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_rank_lock = asyncio.Lock()
_last_update: float = 0.0

# ──────────────────────────────────────────────────────────────
#  GERENCIADOR DE BANCO DE DADOS (Conexão Persistente)
# ──────────────────────────────────────────────────────────────
class DatabaseManager:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None

    async def connect(self):
        if not self.conn:
            db_dir = os.path.dirname(os.path.abspath(self.db_path))
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            self.conn = await aiosqlite.connect(self.db_path)
            logger.info("Conexão com o banco de dados estabelecida.")

    async def execute(self, query, params=()):
        await self.connect()
        return await self.conn.execute(query, params)

    async def executescript(self, script):
        await self.connect()
        await self.conn.executescript(script)

    async def commit(self):
        if self.conn:
            await self.conn.commit()

    async def fetchone(self, query, params=()):
        await self.connect()
        async with self.conn.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, query, params=()):
        await self.connect()
        async with self.conn.execute(query, params) as cursor:
            return await cursor.fetchall()

db = DatabaseManager(DB)

async def init_db():
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
        CREATE TABLE IF NOT EXISTS conversation_history (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message    TEXT NOT NULL,
            response   TEXT,
            rating     INTEGER DEFAULT 0,
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
        CREATE TABLE IF NOT EXISTS ia_enabled_channels (
            channel_id TEXT PRIMARY KEY,
            enabled    INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS knowledge_base (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            question   TEXT NOT NULL,
            answer     TEXT NOT NULL,
            upvotes    INTEGER DEFAULT 0,
            downvotes  INTEGER DEFAULT 0,
            score      INTEGER DEFAULT 0,
            usage_count INTEGER DEFAULT 0,
            last_used  TEXT
        );
        CREATE TABLE IF NOT EXISTS user_patterns (
            user_id    TEXT PRIMARY KEY,
            topics     TEXT,
            avg_rating REAL DEFAULT 0,
            total_interactions INTEGER DEFAULT 0
        );
    """)
    await db.commit()

    # Migrações
    col_names = [col[1] for col in await db.fetchall("PRAGMA table_info(conversation_history)")]
    if "rating" not in col_names:
        await db.execute("ALTER TABLE conversation_history ADD COLUMN rating INTEGER DEFAULT 0")
        logger.info("Migração: 'rating' adicionado a conversation_history.")

    col_names = [col[1] for col in await db.fetchall("PRAGMA table_info(knowledge_base)")]
    if "upvotes" not in col_names:
        await db.execute("ALTER TABLE knowledge_base ADD COLUMN upvotes INTEGER DEFAULT 0")
        logger.info("Migração: colunas de avaliação adicionadas à knowledge_base.")

    col_names = [col[1] for col in await db.fetchall("PRAGMA table_info(reminders)")]
    if "done" not in col_names:
        await db.execute("ALTER TABLE reminders ADD COLUMN done INTEGER DEFAULT 0")
        logger.info("Migração: 'done' adicionado a reminders.")
    await db.commit()

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES AUXILIARES PARA APRENDIZADO & DB
# ──────────────────────────────────────────────────────────────
async def save_conversation(user_id: str, channel_id: str, message: str, response: str = None, rating: int = 0):
    await db.execute(
        "INSERT INTO conversation_history (user_id, channel_id, message, response, rating, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, channel_id, message, response, rating, datetime.datetime.now(BR_TZ).isoformat())
    )
    await db.commit()
    # Retorna o ID da última inserção
    row = await db.fetchone("SELECT last_insert_rowid()")
    return row[0] if row else 0

async def rate_response(conversation_id: int, rating: int):
    await db.execute("UPDATE conversation_history SET rating = ? WHERE id = ?", (rating, conversation_id))
    await db.commit()

async def save_knowledge(question: str, answer: str):
    row = await db.fetchone("SELECT id, upvotes, downvotes FROM knowledge_base WHERE question = ?", (question,))
    if row:
        await db.execute("UPDATE knowledge_base SET usage_count = usage_count + 1, last_used = ? WHERE id = ?",
                        (datetime.datetime.now(BR_TZ).isoformat(), row[0]))
    else:
        await db.execute(
            "INSERT INTO knowledge_base (question, answer, last_used) VALUES (?, ?, ?)",
            (question, answer, datetime.datetime.now(BR_TZ).isoformat())
        )
    await db.commit()

async def get_knowledge(question: str) -> list:
    return await db.fetchall(
        "SELECT id, question, answer, upvotes, downvotes, usage_count FROM knowledge_base WHERE question LIKE ? ORDER BY (upvotes - downvotes) DESC, usage_count DESC LIMIT 3",
        (f"%{question}%",)
    )

async def update_user_pattern(user_id: str, topic: str):
    row = await db.fetchone("SELECT topics, total_interactions FROM user_patterns WHERE user_id = ?", (user_id,))
    if row:
        topics = json.loads(row[0]) if row[0] else []
        topics.append(topic)
        topics = topics[-20:]
        await db.execute("UPDATE user_patterns SET topics = ?, total_interactions = ? WHERE user_id = ?",
                        (json.dumps(topics), row[1] + 1, user_id))
    else:
        await db.execute("INSERT INTO user_patterns (user_id, topics, total_interactions) VALUES (?, ?, ?)",
                        (user_id, json.dumps([topic]), 1))
    await db.commit()

async def get_user_patterns(user_id: str) -> dict:
    row = await db.fetchone("SELECT topics, total_interactions, avg_rating FROM user_patterns WHERE user_id = ?", (user_id,))
    if row:
        return {"topics": json.loads(row[0]) if row[0] else [], "total_interactions": row[1] or 0, "avg_rating": row[2] or 0.0}
    return {"topics": [], "total_interactions": 0, "avg_rating": 0.0}

async def get_channel_history(channel_id: str, limit: int = 20) -> list:
    rows = await db.fetchall(
        "SELECT user_id, message, response, rating, timestamp FROM conversation_history WHERE channel_id = ? ORDER BY timestamp DESC LIMIT ?",
        (channel_id, limit)
    )
    return list(reversed(rows))

async def get_user_history(user_id: str, limit: int = 10) -> list:
    rows = await db.fetchall(
        "SELECT message, response, rating, timestamp FROM conversation_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit)
    )
    return list(reversed(rows))

async def add_reminder(user_id: str, channel_id: str, message: str, remind_at: datetime.datetime):
    await db.execute("INSERT INTO reminders (user_id, channel_id, message, remind_at) VALUES (?, ?, ?, ?)",
                    (user_id, channel_id, message, remind_at.isoformat()))
    await db.commit()

async def mark_reminder_done(reminder_id: int):
    await db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))
    await db.commit()

async def is_ia_enabled(channel_id: str) -> bool:
    row = await db.fetchone("SELECT enabled FROM ia_enabled_channels WHERE channel_id = ?", (channel_id,))
    return bool(row[0]) if row else False

async def set_ia_enabled(channel_id: str, enabled: bool):
    await db.execute("INSERT OR REPLACE INTO ia_enabled_channels (channel_id, enabled) VALUES (?, ?)",
                    (channel_id, 1 if enabled else 0))
    await db.commit()

# ──────────────────────────────────────────────────────────────
#  TASKS — OTIMIZADAS
# ──────────────────────────────────────────────────────────────
@tasks.loop(seconds=30)
async def check_reminders():
    now = datetime.datetime.now(BR_TZ).isoformat()
    reminders = await db.fetchall("SELECT id, user_id, channel_id, message, remind_at FROM reminders WHERE done = 0 AND remind_at <= ?", (now,))
    
    for rid, uid, cid, msg, remind_at in reminders:
        user = bot.get_user(int(uid))
        if user:
            embed = discord.Embed(
                title="⏰ Lembrete!",
                description=f"Olá {user.mention}, você pediu para lembrar:\n\n**{msg}**",
                color=0xFFA500,
                timestamp=datetime.datetime.now(BR_TZ)
            )
            embed.set_footer(text=f"Agendado para {remind_at}")
            try:
                await user.send(embed=embed)
                channel = bot.get_channel(int(cid))
                if channel:
                    await channel.send(f"{user.mention} ⏰ Lembrete: {msg}")
            except discord.Forbidden:
                logger.warning(f"Não foi possível enviar DM de lembrete para {user.id}.")
            except Exception as e:
                logger.error(f"Erro ao enviar lembrete para {user.id}: {e}")
        await mark_reminder_done(rid)

@tasks.loop(hours=24)
async def cleanup_database():
    """Limpa o histórico de mensagens da IA antigas para o banco não inchar"""
    thirty_days_ago = (datetime.datetime.now(BR_TZ) - datetime.timedelta(days=30)).isoformat()
    try:
        await db.execute("DELETE FROM conversation_history WHERE timestamp < ?", (thirty_days_ago,))
        await db.commit()
        logger.info("Limpeza do banco de dados concluída: Mensagens antigas removidas.")
    except Exception as e:
        logger.error(f"Erro na limpeza do banco: {e}")

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES AUXILIARES (Ponto e Utilidades)
# ──────────────────────────────────────────────────────────────
def now_br() -> datetime.datetime: return datetime.datetime.now(tz=BR_TZ)

def week_monday(dt: datetime.datetime = None) -> datetime.datetime:
    if dt is None: dt = now_br()
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
    if not match: raise ValueError("Formato inválido. Use DD/MM/AAAA HH:MM")
    day, month, year, hour, minute, second = match.groups()
    dt = datetime.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or "0"))
    return BR_TZ.localize(dt) if dt.tzinfo is None else dt

def extract_user_id(text: str) -> int:
    match = re.search(r'<@!?(\d+)>', text)
    if match: return int(match.group(1))
    try: return int(text.strip())
    except ValueError: return None

async def load_mid(key: str):
    row = await db.fetchone("SELECT message_id FROM msg_store WHERE key = ?", (key,))
    return int(row[0]) if row else None

async def save_mid(key: str, msg_id: int):
    await db.execute("INSERT OR REPLACE INTO msg_store VALUES (?, ?)", (key, str(msg_id)))
    await db.commit()

async def get_rank() -> list:
    ws = week_monday().isoformat()
    totals_raw = await db.fetchall(
        "SELECT user_id, user_name, SUM(dur_sec) FROM sessions WHERE week_start >= ? AND close_time IS NOT NULL GROUP BY user_id",
        (ws,)
    )
    totals = {r[0]: [r[1], r[2] or 0] for r in totals_raw}
    actives = await db.fetchall("SELECT user_id, user_name, open_time FROM active")
    now = now_br()
    
    for uid, uname, ot in actives:
        dt = localize(datetime.datetime.fromisoformat(ot))
        elapsed = (now - dt).total_seconds()
        if uid in totals: totals[uid][1] += elapsed
        else: totals[uid] = [uname, elapsed]
        
    return sorted(totals.items(), key=lambda x: x[1][1], reverse=True)

def panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="🏥 ECCO HOSPITAL CENTER",
        description="## 📋 Sistema de Bate Ponto Eletrônico\n\nRegistre sua **entrada** e **saída** usando os botões abaixo.\n\n🟢 **Abrir Ponto** — Inicia a contagem do seu expediente\n🔴 **Fechar Ponto** — Encerra e salva o seu expediente\n\n> *Somente você verá a confirmação do seu ponto.*",
        color=0x1565C0,
    )
    e.set_footer(text="ECCO HOSPITAL CENTER • Ponto Eletrônico")
    return e

async def rank_embed() -> discord.Embed:
    rank = await get_rank()
    now = now_br()
    ws = week_monday(now)
    we = ws + datetime.timedelta(days=6)
    
    active_row = await db.fetchone("SELECT COUNT(*) FROM active")
    active_n = active_row[0] if active_row else 0
    
    e = discord.Embed(title="🏆 RANKING SEMANAL DE HORAS", description=f"**ECCO HOSPITAL CENTER**\n📅 {ws.strftime('%d/%m')} — {we.strftime('%d/%m/%Y')}", color=0xFFD700)
    MEDALS = ["🥇", "🥈", "🥉"]
    
    if not rank:
        e.add_field(name="Sem Registros", value="Nenhuma hora registrada esta semana.", inline=False)
    else:
        page, part = "", 0
        for i, (uid, (uname, secs)) in enumerate(rank):
            prefix = MEDALS[i] if i < 3 else f"`#{i+1:>3}`"
            line = f"{prefix} **{uname}** — `{hms(secs)}`\n"
            if len(page) + len(line) > 950:
                e.add_field(name="👥 Colaboradores" if part == 0 else f"👥 Colaboradores (pt.{part + 1})", value=page, inline=False)
                page, part = line, part + 1
            else: page += line
        if page:
            e.add_field(name="👥 Colaboradores" if part == 0 else f"👥 Colaboradores (pt.{part + 1})", value=page, inline=False)
            
    e.add_field(name="🟢 Em Serviço Agora", value=f"**{active_n}** colaborador(es) com ponto aberto", inline=False)
    e.set_footer(text=f"Atualizado em {now.strftime('%d/%m/%Y às %H:%M:%S')} • ECCO HOSPITAL CENTER")
    return e

async def refresh_rank(force: bool = False):
    global _last_update
    cooldown = 10
    if not force and (time.monotonic() - _last_update) < cooldown: return
    if _rank_lock.locked(): return
    
    async with _rank_lock:
        _last_update = time.monotonic()
        ch = bot.get_channel(RANK_CHANNEL)
        if not ch: return logger.warning(f"Canal de ranking ({RANK_CHANNEL}) não encontrado.")
        
        emb = await rank_embed()
        mid = await load_mid("rank")
        if mid:
            try:
                msg = await ch.fetch_message(mid)
                return await msg.edit(embed=emb)
            except discord.NotFound: pass
            except discord.HTTPException as e: return logger.error(f"Erro ao editar rank: {e}")
            
        try:
            msg = await ch.send(embed=emb)
            await save_mid("rank", msg.id)
        except Exception as e: logger.error(f"Erro ao enviar painel de rank: {e}")

# ──────────────────────────────────────────────────────────────
#  VIEWS (Manter a mesma interface para o usuário)
# ──────────────────────────────────────────────────────────────
class RatingView(discord.ui.View):
    def __init__(self, conversation_id: int, message_id: int):
        super().__init__(timeout=3600)
        self.conversation_id = conversation_id
        self.message_id = message_id

    @discord.ui.button(label="👍", style=discord.ButtonStyle.success, custom_id="rate_up")
    async def rate_up(self, itx: discord.Interaction, _: discord.ui.Button):
        await rate_response(self.conversation_id, 1)
        await itx.response.send_message("✅ Obrigado pelo feedback positivo!", ephemeral=True)
        for child in self.children: child.disabled = True
        await itx.message.edit(view=self)

    @discord.ui.button(label="👎", style=discord.ButtonStyle.danger, custom_id="rate_down")
    async def rate_down(self, itx: discord.Interaction, _: discord.ui.Button):
        await rate_response(self.conversation_id, -1)
        await itx.response.send_message("✅ Feedback registrado! Vou melhorar.", ephemeral=True)
        for child in self.children: child.disabled = True
        await itx.message.edit(view=self)

class PunchView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="✅  Abrir Ponto", style=discord.ButtonStyle.success, custom_id="ecco:open")
    async def open_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        uid, name, now = str(itx.user.id), itx.user.display_name, now_br()
        row = await db.fetchone("SELECT open_time FROM active WHERE user_id = ?", (uid,))
        if row:
            dt = localize(datetime.datetime.fromisoformat(row[0]))
            e = discord.Embed(title="⚠️ Ponto Já Aberto!", description=f"Você já tem um ponto aberto desde **{dt.strftime('%d/%m/%Y às %H:%M:%S')}**.\nTempo decorrido: **{hms((now - dt).total_seconds())}**\n\nPara encerrar, clique em 🔴 **Fechar Ponto**.", color=0xFFA500)
            return await itx.response.send_message(embed=e, ephemeral=True)
            
        await db.execute("INSERT OR REPLACE INTO active (user_id, user_name, open_time) VALUES (?, ?, ?)", (uid, name, now.isoformat()))
        await db.commit()
        
        e = discord.Embed(title="✅ Ponto Aberto com Sucesso!", color=0x2ECC71)
        e.add_field(name="👤 Colaborador", value=f"**{name}**", inline=True)
        e.add_field(name="🕐 Horário de Entrada", value=now.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.set_thumbnail(url=str(itx.user.display_avatar.url))
        await itx.response.send_message(embed=e, ephemeral=True)
        
        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="📥 Entrada Registrada", color=0x2ECC71, timestamp=now)
            le.add_field(name="Colaborador", value=f"{itx.user.mention}\n`{name}`", inline=True)
            await lch.send(embed=le)
        asyncio.create_task(refresh_rank())

    @discord.ui.button(label="🔴  Fechar Ponto", style=discord.ButtonStyle.danger, custom_id="ecco:close")
    async def close_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        uid, name, now = str(itx.user.id), itx.user.display_name, now_br()
        row = await db.fetchone("SELECT open_time FROM active WHERE user_id = ?", (uid,))
        if not row:
            return await itx.response.send_message(embed=discord.Embed(title="⚠️ Sem Ponto Aberto!", description="Você não tem ponto aberto.", color=0xFFA500), ephemeral=True)
            
        open_dt = localize(datetime.datetime.fromisoformat(row[0]))
        dur_sec = int((now - open_dt).total_seconds())
        ws = week_monday(open_dt).isoformat()
        
        await db.execute("INSERT INTO sessions (user_id, user_name, open_time, close_time, dur_sec, week_start) VALUES (?, ?, ?, ?, ?, ?)", (uid, name, row[0], now.isoformat(), dur_sec, ws))
        await db.execute("DELETE FROM active WHERE user_id = ?", (uid,))
        await db.commit()
        
        e = discord.Embed(title="🔴 Ponto Fechado com Sucesso!", color=0xE74C3C)
        e.add_field(name="👤 Colaborador", value=f"**{name}**", inline=False)
        e.add_field(name="⏱️ Duração da Sessão", value=f"**{hms(dur_sec)}**", inline=False)
        e.set_thumbnail(url=str(itx.user.display_avatar.url))
        await itx.response.send_message(embed=e, ephemeral=True)
        
        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="📤 Saída Registrada", color=0xE74C3C, timestamp=now)
            le.add_field(name="Colaborador", value=f"{itx.user.mention}", inline=True)
            le.add_field(name="Duração", value=f"**{hms(dur_sec)}**", inline=True)
            await lch.send(embed=le)
        asyncio.create_task(refresh_rank())

# (Para economizar espaço, as classes RemovePanelView, RecruitView, etc mantêm o padrão, mas substituindo as chamadas de banco e prints)
class RemovePanelView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="⏱️ Remover Horas", style=discord.ButtonStyle.danger, custom_id="remove_hours_panel")
    async def remove_hours_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_modal(RemoveHoursFromMemberModal())

class RemoveHoursFromMemberModal(discord.ui.Modal, title="Remover Horas"):
    membro = discord.ui.TextInput(label="Membro (ID ou menção)", required=True)
    horas = discord.ui.TextInput(label="Horas a remover (Ex: 1.5)", required=True)
    async def on_submit(self, itx: discord.Interaction):
        if not any(r.id in AUTHORIZED_REMOVE_ROLE_IDS for r in itx.user.roles):
            return await itx.response.send_message("❌ Sem permissão.", ephemeral=True)
        uid = extract_user_id(self.membro.value)
        if not uid: return await itx.response.send_message("❌ ID inválido.", ephemeral=True)
        
        try: hrs = float(self.horas.value.replace(',', '.'))
        except ValueError: return await itx.response.send_message("❌ Valor inválido.", ephemeral=True)
        
        row = await db.fetchone("SELECT id, open_time, close_time, dur_sec FROM sessions WHERE user_id = ? AND close_time IS NOT NULL ORDER BY close_time DESC LIMIT 1", (str(uid),))
        if not row: return await itx.response.send_message("ℹ️ Nenhuma sessão fechada encontrada.", ephemeral=True)
        
        session_id, ot, ct, dur = row
        rem_sec = int(hrs * 3600)
        
        if dur < rem_sec: return await itx.response.send_message(f"❌ Sessão muito curta ({hms(dur)}).", ephemeral=True)
        nova_dur = dur - rem_sec
        
        if nova_dur <= 0:
            await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            msg = "Sessão removida."
        else:
            nova_saida = datetime.datetime.fromisoformat(ot) + datetime.timedelta(seconds=nova_dur)
            await db.execute("UPDATE sessions SET dur_sec = ?, close_time = ? WHERE id = ?", (nova_dur, nova_saida.isoformat(), session_id))
            msg = f"Sessão ajustada para {hms(nova_dur)}."
        await db.commit()
        await itx.response.send_message(f"✅ {msg}", ephemeral=True)
        asyncio.create_task(refresh_rank(force=True))

class RecruitView(discord.ui.View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="📢 Recrutamento", style=discord.ButtonStyle.primary, custom_id="recruit_button")
    async def recruit_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        await itx.response.send_modal(RecruitModal())

class RecruitModal(discord.ui.Modal, title="📢 Novo Recrutamento"):
    mensagem = discord.ui.TextInput(label="Mensagem", style=discord.TextStyle.paragraph, required=True)
    async def on_submit(self, itx: discord.Interaction):
        role_mentions = " ".join(f"<@&{r}>" for r in RECRUIT_ROLE_IDS)
        embed = discord.Embed(title="📢 Recrutamento", description=self.mensagem.value, color=0x00BFFF, timestamp=now_br())
        embed.set_footer(text=f"Por {itx.user.display_name}")
        await itx.channel.send(content=role_mentions, embed=embed)
        await itx.response.send_message("✅ Enviado!", ephemeral=True)

class DMNotifyView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=3600)
        self.user_id = user_id
    @discord.ui.button(label="✅ Ainda em serviço", style=discord.ButtonStyle.success)
    async def confirm_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        if itx.user.id != self.user_id: return await itx.response.send_message("❌ Não é para você.", ephemeral=True)
        await itx.response.send_message("👍 Confirmado!", ephemeral=True)
    @discord.ui.button(label="🔴 Fechar Ponto", style=discord.ButtonStyle.danger)
    async def close_from_dm_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        if itx.user.id != self.user_id: return
        uid, name, now = str(itx.user.id), itx.user.display_name, now_br()
        row = await db.fetchone("SELECT open_time FROM active WHERE user_id = ?", (uid,))
        if not row: return await itx.response.send_message("⚠️ Sem ponto aberto.", ephemeral=True)
        
        open_dt = localize(datetime.datetime.fromisoformat(row[0]))
        dur = int((now - open_dt).total_seconds())
        ws = week_monday(open_dt).isoformat()
        
        await db.execute("INSERT INTO sessions (user_id, user_name, open_time, close_time, dur_sec, week_start) VALUES (?,?,?,?,?,?)", (uid, name, row[0], now.isoformat(), dur, ws))
        await db.execute("DELETE FROM active WHERE user_id = ?", (uid,))
        await db.commit()
        await itx.response.send_message(f"🔴 Ponto Fechado! Duração: **{hms(dur)}**")
        asyncio.create_task(refresh_rank())

# ──────────────────────────────────────────────────────────────
#  COMANDOS DA IA OTIMIZADOS COM ASYNC
# ──────────────────────────────────────────────────────────────
async def fetch_gemini_fallback(contexto: str) -> str:
    """Fallback 100% assíncrono para gerar a resposta se o SDK falhar"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": contexto}]}]}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=30) as response:
            if response.status == 200:
                data = await response.json()
                if "candidates" in data and data["candidates"]:
                    return data["candidates"][0]["content"]["parts"][0]["text"].strip()
            logger.error(f"Erro no fallback REST da IA: Status {response.status}")
    return None

def extract_topics(text: str) -> list:
    """Extrai tópicos relevantes usando a lista de stopwords em português"""
    palavras = set(re.findall(r'\b[a-záéíóúâêôãõç]{4,}\b', text.lower()))
    return [p for p in palavras if p not in STOPWORDS_PTBR]

@bot.tree.command(name="ia", description="Faça uma pergunta para a IA (com contexto do chat)")
@app_commands.describe(pergunta="Sua pergunta")
async def cmd_ia(itx: discord.Interaction, pergunta: str):
    if not GEMINI_API_KEY or not GEMINI_MODELS:
        return await itx.response.send_message("❌ IA não configurada.", ephemeral=True)

    await itx.response.defer(ephemeral=False)
    
    try:
        channel_history = await get_channel_history(str(itx.channel_id), limit=20)
        user_history = await get_user_history(str(itx.user.id), limit=10)
        user_patterns = await get_user_patterns(str(itx.user.id))
        knowledge = await get_knowledge(pergunta)

        contexto = "Você é um assistente virtual do Hospital ECCO em um servidor FiveM. Responda de forma educada e objetiva.\n\n"
        if user_patterns["total_interactions"] > 0 and user_patterns["topics"]:
            contexto += f"Tópicos frequentes do usuário: {', '.join(user_patterns['topics'][-5:])}\n\n"
        if channel_history:
            contexto += "--- Histórico do canal ---\n" + "".join([f"{entry[1]}\nBot:{entry[2]}\n" for entry in channel_history if entry[2]]) + "\n"
        if knowledge:
            contexto += "--- Conhecimento Relevante ---\n" + "".join([f"Q: {k[1]}\nA: {k[2]}\n" for k in knowledge if k[3]-k[4] >= 0]) + "\n"
            
        contexto += f"Pergunta: {pergunta}"

        resposta_texto = None
        for modelo in GEMINI_MODELS:
            try:
                model = genai.GenerativeModel(modelo)
                # OTIMIZAÇÃO: Chamada Assíncrona para não travar o Event Loop
                resposta = await model.generate_content_async(contexto)
                resposta_texto = resposta.text.strip()
                break
            except Exception as e:
                logger.warning(f"Falha ao gerar com modelo {modelo}: {e}")
                continue

        if not resposta_texto:
            resposta_texto = await fetch_gemini_fallback(contexto)

        if not resposta_texto:
            return await itx.followup.send("❌ Falha ao conectar com a IA no momento.")

        # Salva a conversa
        conv_id = await save_conversation(str(itx.user.id), str(itx.channel_id), pergunta, resposta_texto)
        
        # Padrões e Conhecimento
        for topico in extract_topics(pergunta)[:3]:
            await update_user_pattern(str(itx.user.id), topico)
        await save_knowledge(pergunta, resposta_texto)

        embed = discord.Embed(description=resposta_texto[:4000], color=0x00D4FF)
        await itx.followup.send(embed=embed, view=RatingView(conv_id, None))
        
    except Exception as e:
        logger.error(f"Erro no comando /ia: {e}", exc_info=True)
        await itx.followup.send("❌ Ocorreu um erro interno ao processar a requisição.")

@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or msg.content.startswith(("/", "!")):
        return await bot.process_commands(msg)
        
    if not await is_ia_enabled(str(msg.channel.id)) or not GEMINI_API_KEY:
        return await bot.process_commands(msg)

    async with msg.channel.typing():
        try:
            channel_history = await get_channel_history(str(msg.channel.id), limit=15)
            knowledge = await get_knowledge(msg.content)
            
            contexto = "Você é um assistente virtual do Hospital ECCO em um servidor FiveM. Responda brevemente e objetivamente. Máx 400 caracteres.\n\n"
            if channel_history:
                contexto += "\n".join([f"Msg: {entry[1]}\nBot: {entry[2]}\n" for entry in channel_history if entry[2]])
            if knowledge:
                contexto += "".join([f"Contexto: {k[2]}\n" for k in knowledge if k[3]-k[4] >= 0])
                
            contexto += f"\nUsuário {msg.author.display_name}: {msg.content}"

            resposta_texto = None
            for modelo in GEMINI_MODELS:
                try:
                    model = genai.GenerativeModel(modelo)
                    # OTIMIZAÇÃO: Chamada Assíncrona
                    resposta = await model.generate_content_async(contexto)
                    resposta_texto = resposta.text.strip()
                    break
                except Exception: continue

            if not resposta_texto:
                resposta_texto = await fetch_gemini_fallback(contexto)

            if resposta_texto:
                conv_id = await save_conversation(str(msg.author.id), str(msg.channel.id), msg.content, resposta_texto)
                for topico in extract_topics(msg.content)[:3]:
                    await update_user_pattern(str(msg.author.id), topico)
                await save_knowledge(msg.content, resposta_texto)

                embed = discord.Embed(description=resposta_texto[:1900], color=0x00D4FF)
                await msg.reply(embed=embed, view=RatingView(conv_id, None), mention_author=False)

        except Exception as e:
            logger.error(f"Erro on_message IA: {e}")

    await bot.process_commands(msg)

# ──────────────────────────────────────────────────────────────
#  OUTROS COMANDOS MANTIDOS (Resumo)
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="ativar_ia", description="[ADMIN] Ativa IA no canal")
@app_commands.default_permissions(administrator=True)
async def cmd_ativar_ia(itx: discord.Interaction):
    await set_ia_enabled(str(itx.channel_id), True)
    await itx.response.send_message("✅ IA Ativada.", ephemeral=True)

@bot.tree.command(name="desativar_ia", description="[ADMIN] Desativa IA no canal")
@app_commands.default_permissions(administrator=True)
async def cmd_desativar_ia(itx: discord.Interaction):
    await set_ia_enabled(str(itx.channel_id), False)
    await itx.response.send_message("❌ IA Desativada.", ephemeral=True)

@bot.tree.command(name="meu_ponto", description="Consulte suas horas desta semana")
async def cmd_meu_ponto(itx: discord.Interaction):
    uid, ws = str(itx.user.id), week_monday(now_br()).isoformat()
    sessions = await db.fetchall("SELECT open_time, close_time, dur_sec FROM sessions WHERE user_id = ? AND week_start >= ? ORDER BY open_time DESC", (uid, ws))
    active = await db.fetchone("SELECT open_time FROM active WHERE user_id = ?", (uid,))
    
    total = sum(s[2] for s in sessions if s[2])
    desc = ""
    if active:
        dt = localize(datetime.datetime.fromisoformat(active[0]))
        total += (now_br() - dt).total_seconds()
        desc = f"🟢 **Em Serviço** desde `{dt.strftime('%H:%M:%S')}`\n\n"
        
    e = discord.Embed(title=f"📊 Meu Ponto", description=desc, color=0x1565C0)
    e.add_field(name="⏱️ Total", value=f"**{hms(total)}**")
    await itx.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="sync", description="[ADMIN] Sincroniza comandos")
@app_commands.default_permissions(administrator=True)
async def cmd_sync(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    synced = await bot.tree.sync()
    await itx.followup.send(f"✅ Sincronizados: {len(synced)}", ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  TASKS RECORRENTES
# ──────────────────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def auto_refresh():
    await refresh_rank(force=True)

@tasks.loop(hours=1)
async def notify_active_users():
    actives = await db.fetchall("SELECT user_id, user_name, open_time FROM active")
    for uid, uname, ot in actives:
        user = bot.get_user(int(uid))
        if not user: continue
        try:
            embed = discord.Embed(title="⏰ Verificação de Ponto", description=f"Olá {uname}, você ainda está em serviço?", color=0x3498DB)
            await user.send(embed=embed, view=DMNotifyView(int(uid)))
        except discord.Forbidden:
            logger.info(f"DM bloqueada para o usuário {uid}")
        except Exception as e:
            logger.error(f"Erro ao notificar usuário {uid}: {e}")

# ──────────────────────────────────────────────────────────────
#  ON READY
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()

    bot.add_view(PunchView())
    bot.add_view(RemovePanelView())
    bot.add_view(RecruitView())

    # Garantir recriação dos painéis, se necessário
    for ch_id, key, view_cls, emb_func in [
        (PANEL_CHANNEL, "panel", PunchView, panel_embed),
        (REMOVE_PANEL_CHANNEL, "remove_panel", RemovePanelView, lambda: discord.Embed(title="⏱️ Painel de Remoção de Horas")),
        (RECRUIT_CHANNEL, "recruit_panel", RecruitView, lambda: discord.Embed(title="📢 Painel de Recrutamento"))
    ]:
        ch = bot.get_channel(ch_id)
        if ch:
            mid = await load_mid(key)
            if not mid:
                msg = await ch.send(embed=emb_func(), view=view_cls())
                await save_mid(key, msg.id)
                logger.info(f"Painel {key} criado.")

    await refresh_rank(force=True)
    auto_refresh.start()
    notify_active_users.start()
    check_reminders.start()
    cleanup_database.start()  # Nova Task para limpar DB!

    try:
        synced = await bot.tree.sync()
        logger.info(f"Comandos sincronizados: {len(synced)}")
    except Exception as exc:
        logger.error(f"Erro ao sincronizar comandos: {exc}")

    logger.info(f"✅ {bot.user} online!")

if __name__ == "__main__":
    if not TOKEN: raise SystemExit("❌ DISCORD_TOKEN ausente.")
    bot.run(TOKEN)
