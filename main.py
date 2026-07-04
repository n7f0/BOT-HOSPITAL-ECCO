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
import requests
import random
import json
from collections import defaultdict

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
#  DATABASE (com novas tabelas para aprendizado)
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
            CREATE TABLE IF NOT EXISTS conversation_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                message    TEXT NOT NULL,
                response   TEXT,
                rating     INTEGER DEFAULT 0,  -- -1, 0, 1
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
                topics     TEXT,  -- JSON array de tópicos frequentes
                avg_rating REAL DEFAULT 0,
                total_interactions INTEGER DEFAULT 0
            );
        """)
        await db.commit()

        # Migrações
        cursor = await db.execute("PRAGMA table_info(conversation_history)")
        columns = await cursor.fetchall()
        col_names = [col[1] for col in columns]
        if "rating" not in col_names:
            await db.execute("ALTER TABLE conversation_history ADD COLUMN rating INTEGER DEFAULT 0")
            await db.commit()
            print("✅ Coluna 'rating' adicionada à tabela conversation_history.")

        cursor = await db.execute("PRAGMA table_info(knowledge_base)")
        columns = await cursor.fetchall()
        col_names = [col[1] for col in columns]
        if "upvotes" not in col_names:
            await db.execute("ALTER TABLE knowledge_base ADD COLUMN upvotes INTEGER DEFAULT 0")
            await db.commit()
            print("✅ Colunas de avaliação adicionadas à knowledge_base.")

        cursor = await db.execute("PRAGMA table_info(reminders)")
        columns = await cursor.fetchall()
        col_names = [col[1] for col in columns]
        if "done" not in col_names:
            await db.execute("ALTER TABLE reminders ADD COLUMN done INTEGER DEFAULT 0")
            await db.commit()
            print("✅ Coluna 'done' adicionada à tabela reminders.")

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES AUXILIARES PARA APRENDIZADO
# ──────────────────────────────────────────────────────────────
async def save_conversation(user_id: str, channel_id: str, message: str, response: str = None, rating: int = 0):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO conversation_history (user_id, channel_id, message, response, rating, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, channel_id, message, response, rating, datetime.datetime.now(BR_TZ).isoformat())
        )
        await db.commit()

async def rate_response(conversation_id: int, rating: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE conversation_history SET rating = ? WHERE id = ?", (rating, conversation_id))
        await db.commit()

async def save_knowledge(question: str, answer: str):
    async with aiosqlite.connect(DB) as db:
        # Verifica se já existe
        async with db.execute("SELECT id, upvotes, downvotes FROM knowledge_base WHERE question = ?", (question,)) as c:
            row = await c.fetchone()
        if row:
            # Atualiza se já existir (incrementa uso)
            await db.execute("UPDATE knowledge_base SET usage_count = usage_count + 1, last_used = ? WHERE id = ?",
                            (datetime.datetime.now(BR_TZ).isoformat(), row[0]))
        else:
            await db.execute(
                "INSERT INTO knowledge_base (question, answer, last_used) VALUES (?, ?, ?)",
                (question, answer, datetime.datetime.now(BR_TZ).isoformat())
            )
        await db.commit()

async def get_knowledge(question: str) -> list:
    """Busca conhecimento relacionado à pergunta."""
    async with aiosqlite.connect(DB) as db:
        # Busca por similaridade simples (contains)
        async with db.execute(
            "SELECT id, question, answer, upvotes, downvotes, usage_count FROM knowledge_base WHERE question LIKE ? ORDER BY (upvotes - downvotes) DESC, usage_count DESC LIMIT 3",
            (f"%{question}%",)
        ) as c:
            rows = await c.fetchall()
    return rows

async def update_user_pattern(user_id: str, topic: str):
    """Atualiza os padrões de tópicos do usuário."""
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT topics, total_interactions FROM user_patterns WHERE user_id = ?", (user_id,)) as c:
            row = await c.fetchone()
        if row:
            topics_json = row[0] if row[0] else "[]"
            try:
                topics = json.loads(topics_json)
            except:
                topics = []
            topics.append(topic)
            # Mantém apenas os 20 mais recentes
            if len(topics) > 20:
                topics = topics[-20:]
            total = row[1] + 1
            await db.execute(
                "UPDATE user_patterns SET topics = ?, total_interactions = ? WHERE user_id = ?",
                (json.dumps(topics), total, user_id)
            )
        else:
            await db.execute(
                "INSERT INTO user_patterns (user_id, topics, total_interactions) VALUES (?, ?, ?)",
                (user_id, json.dumps([topic]), 1)
            )
        await db.commit()

async def get_user_patterns(user_id: str) -> dict:
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT topics, total_interactions, avg_rating FROM user_patterns WHERE user_id = ?", (user_id,)) as c:
            row = await c.fetchone()
    if row:
        topics = json.loads(row[0]) if row[0] else []
        return {"topics": topics, "total_interactions": row[1] or 0, "avg_rating": row[2] or 0.0}
    return {"topics": [], "total_interactions": 0, "avg_rating": 0.0}

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES AUXILIARES
# ──────────────────────────────────────────────────────────────
async def get_channel_history(channel_id: str, limit: int = 20) -> list:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT user_id, message, response, rating, timestamp FROM conversation_history WHERE channel_id = ? ORDER BY timestamp DESC LIMIT ?",
            (channel_id, limit)
        ) as c:
            rows = await c.fetchall()
    return list(reversed(rows))

async def get_user_history(user_id: str, limit: int = 10) -> list:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT message, response, rating, timestamp FROM conversation_history WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
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

async def is_ia_enabled(channel_id: str) -> bool:
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT enabled FROM ia_enabled_channels WHERE channel_id = ?", (channel_id,)) as c:
            row = await c.fetchone()
    if row is None:
        return False
    return bool(row[0])

async def set_ia_enabled(channel_id: str, enabled: bool):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO ia_enabled_channels (channel_id, enabled) VALUES (?, ?)",
            (channel_id, 1 if enabled else 0)
        )
        await db.commit()
        print(f"✅ Canal {channel_id} {'ativado' if enabled else 'desativado'} para IA")

# ──────────────────────────────────────────────────────────────
#  TASK — VERIFICAR LEMBRETES
# ──────────────────────────────────────────────────────────────
@tasks.loop(seconds=30)
async def check_reminders():
    reminders = await get_due_reminders()
    for rid, uid, cid, msg, remind_at in reminders:
        user = bot.get_user(int(uid))
        if user:
            channel = bot.get_channel(int(cid))
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
#  FUNÇÕES EXISTENTES (bate ponto, ranking, etc.)
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
#  VIEW — AVALIAÇÃO DA IA (Botões 👍 👎)
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
        # Atualizar a mensagem para remover os botões ou desabilitá-los
        for child in self.children:
            child.disabled = True
        await itx.message.edit(view=self)

    @discord.ui.button(label="👎", style=discord.ButtonStyle.danger, custom_id="rate_down")
    async def rate_down(self, itx: discord.Interaction, _: discord.ui.Button):
        await rate_response(self.conversation_id, -1)
        await itx.response.send_message("✅ Feedback registrado! Vou melhorar.", ephemeral=True)
        for child in self.children:
            child.disabled = True
        await itx.message.edit(view=self)

# ──────────────────────────────────────────────────────────────
#  VIEWS — Bate Ponto, Remoção, Recrutamento (mantidas)
# ──────────────────────────────────────────────────────────────
class PunchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅  Abrir Ponto", style=discord.ButtonStyle.success, custom_id="ecco:open")
    async def open_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        uid = str(itx.user.id)
        name = itx.user.display_name
        now = now_br()
        async with aiosqlite.connect(DB) as db:
            async with db.execute("SELECT open_time FROM active WHERE user_id = ?", (uid,)) as c:
                row = await c.fetchone()
        if row:
            dt = localize(datetime.datetime.fromisoformat(row[0]))
            elapsed = (now - dt).total_seconds()
            e = discord.Embed(
                title="⚠️ Ponto Já Aberto!",
                description=(
                    f"Você já possui um ponto aberto desde "
                    f"**{dt.strftime('%d/%m/%Y às %H:%M:%S')}**.\n"
                    f"Tempo decorrido: **{hms(elapsed)}**\n\n"
                    "Para encerrar, clique em 🔴 **Fechar Ponto**."
                ),
                color=0xFFA500,
            )
            return await itx.response.send_message(embed=e, ephemeral=True)
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT OR REPLACE INTO active (user_id, user_name, open_time) VALUES (?, ?, ?)",
                (uid, name, now.isoformat()),
            )
            await db.commit()
        e = discord.Embed(title="✅ Ponto Aberto com Sucesso!", color=0x2ECC71)
        e.add_field(name="👤 Colaborador", value=f"**{name}**", inline=True)
        e.add_field(name="🕐 Horário de Entrada", value=now.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.set_thumbnail(url=str(itx.user.display_avatar.url))
        e.set_footer(text="ECCO HOSPITAL CENTER • Bate Ponto")
        await itx.response.send_message(embed=e, ephemeral=True)
        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="📥 Entrada Registrada", color=0x2ECC71, timestamp=now)
            le.add_field(name="Colaborador", value=f"{itx.user.mention}\n`{name}`", inline=True)
            le.add_field(name="Horário", value=now.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
            le.set_thumbnail(url=str(itx.user.display_avatar.url))
            le.set_footer(text="ECCO HOSPITAL CENTER")
            await lch.send(embed=le)
        asyncio.create_task(refresh_rank())

    @discord.ui.button(label="🔴  Fechar Ponto", style=discord.ButtonStyle.danger, custom_id="ecco:close")
    async def close_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        uid = str(itx.user.id)
        name = itx.user.display_name
        now = now_br()
        async with aiosqlite.connect(DB) as db:
            async with db.execute("SELECT open_time FROM active WHERE user_id = ?", (uid,)) as c:
                row = await c.fetchone()
        if not row:
            e = discord.Embed(
                title="⚠️ Sem Ponto Aberto!",
                description=(
                    "Você não tem nenhum ponto aberto no momento.\n\n"
                    "Clique em ✅ **Abrir Ponto** para iniciar seu expediente."
                ),
                color=0xFFA500,
            )
            return await itx.response.send_message(embed=e, ephemeral=True)
        open_dt = localize(datetime.datetime.fromisoformat(row[0]))
        dur_sec = int((now - open_dt).total_seconds())
        ws = week_monday(open_dt).isoformat()
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT INTO sessions (user_id, user_name, open_time, close_time, dur_sec, week_start) VALUES (?, ?, ?, ?, ?, ?)",
                (uid, name, row[0], now.isoformat(), dur_sec, ws),
            )
            await db.execute("DELETE FROM active WHERE user_id = ?", (uid,))
            await db.commit()
        e = discord.Embed(title="🔴 Ponto Fechado com Sucesso!", color=0xE74C3C)
        e.add_field(name="👤 Colaborador", value=f"**{name}**", inline=False)
        e.add_field(name="🕐 Entrada", value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.add_field(name="🕑 Saída", value=now.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.add_field(name="⏱️ Duração da Sessão", value=f"**{hms(dur_sec)}**", inline=False)
        e.set_thumbnail(url=str(itx.user.display_avatar.url))
        e.set_footer(text="ECCO HOSPITAL CENTER • Bate Ponto")
        await itx.response.send_message(embed=e, ephemeral=True)
        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="📤 Saída Registrada", color=0xE74C3C, timestamp=now)
            le.add_field(name="Colaborador", value=f"{itx.user.mention}\n`{name}`", inline=True)
            le.add_field(name="Entrada", value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
            le.add_field(name="Saída", value=now.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
            le.add_field(name="Duração", value=f"**{hms(dur_sec)}**", inline=True)
            le.set_footer(text="ECCO HOSPITAL CENTER")
            await lch.send(embed=le)
        asyncio.create_task(refresh_rank())

class RemovePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="⏱️ Remover Horas", style=discord.ButtonStyle.danger, custom_id="remove_hours_panel")
    async def remove_hours_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        modal = RemoveHoursFromMemberModal()
        await itx.response.send_modal(modal)

class RemoveHoursFromMemberModal(discord.ui.Modal, title="Remover Horas de um Colaborador"):
    def __init__(self):
        super().__init__()
    membro = discord.ui.TextInput(label="Membro (ID ou menção)", placeholder="Digite o ID ou mencione o usuário", required=True, max_length=30)
    horas = discord.ui.TextInput(label="Horas a remover", placeholder="Ex: 1.5 (para 1h30)", required=True, max_length=10)
    async def on_submit(self, itx: discord.Interaction):
        has_role = any(role.id in AUTHORIZED_REMOVE_ROLE_IDS for role in itx.user.roles)
        if not has_role:
            return await itx.response.send_message("❌ Você não tem permissão para remover horas.", ephemeral=True)
        user_id = extract_user_id(self.membro.value)
        if not user_id:
            return await itx.response.send_message("❌ ID ou menção inválida.", ephemeral=True)
        member = itx.guild.get_member(user_id)
        if not member:
            return await itx.response.send_message("❌ Membro não encontrado.", ephemeral=True)
        try:
            horas_remover = float(self.horas.value.replace(',', '.'))
            if horas_remover <= 0:
                raise ValueError
        except ValueError:
            return await itx.response.send_message("❌ Valor inválido. Digite um número positivo (ex: 1.5).", ephemeral=True)
        segundos_remover = int(horas_remover * 3600)
        uid = str(user_id)
        async with aiosqlite.connect(DB) as db:
            async with db.execute("SELECT id, open_time, close_time, dur_sec FROM sessions WHERE user_id = ? AND close_time IS NOT NULL ORDER BY close_time DESC LIMIT 1", (uid,)) as c:
                row = await c.fetchone()
        if not row:
            return await itx.response.send_message(f"ℹ️ **{member.display_name}** não possui sessões fechadas.", ephemeral=True)
        session_id, open_time_str, close_time_str, dur_sec = row
        if dur_sec < segundos_remover:
            return await itx.response.send_message(f"❌ A última sessão tem apenas {hms(dur_sec)}, não é possível remover {hms(segundos_remover)}.", ephemeral=True)
        nova_duracao = dur_sec - segundos_remover
        open_dt = datetime.datetime.fromisoformat(open_time_str)
        nova_saida = open_dt + datetime.timedelta(seconds=nova_duracao)
        if nova_duracao <= 0:
            async with aiosqlite.connect(DB) as db:
                await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                await db.commit()
            await itx.response.send_message(f"✅ Sessão de **{member.display_name}** foi **removida** completamente (duração zerada).", ephemeral=True)
            lch = bot.get_channel(LOGS_CHANNEL)
            if lch:
                le = discord.Embed(title="🗑️ Sessão Removida", color=0xFF0000, timestamp=now_br())
                le.add_field(name="Colaborador", value=f"{member.mention} (`{member.display_name}`)", inline=True)
                le.add_field(name="Horas removidas", value=f"{horas_remover}h", inline=True)
                le.add_field(name="Removido por", value=itx.user.mention, inline=True)
                await lch.send(embed=le)
        else:
            async with aiosqlite.connect(DB) as db:
                await db.execute("UPDATE sessions SET dur_sec = ?, close_time = ? WHERE id = ?", (nova_duracao, nova_saida.isoformat(), session_id))
                await db.commit()
            await itx.response.send_message(f"✅ Sessão de **{member.display_name}** ajustada: nova duração **{hms(nova_duracao)}** (removido {horas_remover}h).", ephemeral=True)
            lch = bot.get_channel(LOGS_CHANNEL)
            if lch:
                le = discord.Embed(title="⏱️ Horas Removidas", color=0xE67E22, timestamp=now_br())
                le.add_field(name="Colaborador", value=f"{member.mention} (`{member.display_name}`)", inline=True)
                le.add_field(name="Duração anterior", value=hms(dur_sec), inline=True)
                le.add_field(name="Nova duração", value=hms(nova_duracao), inline=True)
                le.add_field(name="Horas removidas", value=f"{horas_remover}h", inline=True)
                le.add_field(name="Removido por", value=itx.user.mention, inline=True)
                await lch.send(embed=le)
        asyncio.create_task(refresh_rank(force=True))

class RecruitView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="📢 Recrutamento", style=discord.ButtonStyle.primary, custom_id="recruit_button")
    async def recruit_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        modal = RecruitModal()
        await itx.response.send_modal(modal)

class RecruitModal(discord.ui.Modal, title="📢 Novo Recrutamento"):
    mensagem = discord.ui.TextInput(label="Mensagem de recrutamento", placeholder="Digite o texto...", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    async def on_submit(self, itx: discord.Interaction):
        role_mentions = " ".join(f"<@&{role_id}>" for role_id in RECRUIT_ROLE_IDS)
        canal = itx.channel
        if not canal:
            return await itx.response.send_message("❌ Canal não identificado.", ephemeral=True)
        embed = discord.Embed(title="📢 Recrutamento", description=self.mensagem.value, color=0x00BFFF, timestamp=now_br())
        embed.set_footer(text=f"Solicitado por {itx.user.display_name}", icon_url=itx.user.display_avatar.url)
        await canal.send(content=role_mentions, embed=embed)
        await itx.response.send_message("✅ Mensagem enviada!", ephemeral=True)
        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            log_embed = discord.Embed(title="📢 Recrutamento realizado", color=0x00BFFF, timestamp=now_br())
            log_embed.add_field(name="Solicitante", value=itx.user.mention, inline=True)
            log_embed.add_field(name="Canal", value=canal.mention, inline=True)
            log_embed.add_field(name="Mensagem", value=self.mensagem.value[:500] + ("..." if len(self.mensagem.value) > 500 else ""), inline=False)
            await lch.send(embed=log_embed)

class DMNotifyView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=3600)
        self.user_id = user_id
    @discord.ui.button(label="✅ Ainda em serviço", style=discord.ButtonStyle.success)
    async def confirm_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        if itx.user.id != self.user_id:
            return await itx.response.send_message("❌ Não é para você.", ephemeral=True)
        await itx.response.send_message("👍 Confirmado!", ephemeral=True)
    @discord.ui.button(label="🔴 Fechar Ponto", style=discord.ButtonStyle.danger)
    async def close_from_dm_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        if itx.user.id != self.user_id:
            return await itx.response.send_message("❌ Não é para você.", ephemeral=True)
        uid = str(itx.user.id)
        name = itx.user.display_name
        now = now_br()
        async with aiosqlite.connect(DB) as db:
            async with db.execute("SELECT open_time FROM active WHERE user_id = ?", (uid,)) as c:
                row = await c.fetchone()
        if not row:
            return await itx.response.send_message("⚠️ Você não tem ponto aberto.", ephemeral=True)
        open_dt = localize(datetime.datetime.fromisoformat(row[0]))
        dur_sec = int((now - open_dt).total_seconds())
        ws = week_monday(open_dt).isoformat()
        async with aiosqlite.connect(DB) as db:
            await db.execute("INSERT INTO sessions (user_id, user_name, open_time, close_time, dur_sec, week_start) VALUES (?, ?, ?, ?, ?, ?)", (uid, name, row[0], now.isoformat(), dur_sec, ws))
            await db.execute("DELETE FROM active WHERE user_id = ?", (uid,))
            await db.commit()
        e = discord.Embed(title="🔴 Ponto Fechado!", color=0xE74C3C)
        e.add_field(name="👤 Colaborador", value=f"**{name}**", inline=False)
        e.add_field(name="🕐 Entrada", value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.add_field(name="🕑 Saída", value=now.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.add_field(name="⏱️ Duração", value=f"**{hms(dur_sec)}**", inline=False)
        e.set_thumbnail(url=str(itx.user.display_avatar.url))
        e.set_footer(text="ECCO HOSPITAL CENTER")
        await itx.response.send_message(embed=e)
        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="📤 Saída via DM", color=0xE74C3C, timestamp=now)
            le.add_field(name="Colaborador", value=f"{itx.user.mention}\n`{name}`", inline=True)
            le.add_field(name="Entrada", value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
            le.add_field(name="Saída", value=now.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
            le.add_field(name="Duração", value=f"**{hms(dur_sec)}**", inline=True)
            await lch.send(embed=le)
        asyncio.create_task(refresh_rank())
        for child in self.children:
            child.disabled = True
        await itx.message.edit(view=self)

class AdjustModal(discord.ui.Modal, title="Ajustar Horário da Sessão"):
    def __init__(self, session_id: int, user: discord.Member):
        super().__init__()
        self.session_id = session_id
        self.user = user
    nova_entrada = discord.ui.TextInput(label="Nova Entrada (DD/MM/AAAA HH:MM)", placeholder="Deixe em branco", required=False, max_length=20)
    nova_saida = discord.ui.TextInput(label="Nova Saída (DD/MM/AAAA HH:MM)", placeholder="Deixe em branco", required=False, max_length=20)
    async def on_submit(self, itx: discord.Interaction):
        if itx.user.id not in AUTHORIZED_ADJUST_IDS:
            return await itx.response.send_message("❌ Sem permissão.", ephemeral=True)
        async with aiosqlite.connect(DB) as db:
            async with db.execute("SELECT user_id, open_time, close_time, dur_sec FROM sessions WHERE id = ?", (self.session_id,)) as c:
                row = await c.fetchone()
        if not row:
            return await itx.response.send_message("❌ Sessão não encontrada.", ephemeral=True)
        uid, old_open, old_close, old_dur = row
        if old_close is None:
            return await itx.response.send_message("❌ Sessão aberta não pode ser ajustada.", ephemeral=True)
        new_open = None
        new_close = None
        if self.nova_entrada.value:
            try:
                new_open = parse_datetime_br(self.nova_entrada.value)
            except ValueError as e:
                return await itx.response.send_message(f"❌ Erro na entrada: {e}", ephemeral=True)
        if self.nova_saida.value:
            try:
                new_close = parse_datetime_br(self.nova_saida.value)
            except ValueError as e:
                return await itx.response.send_message(f"❌ Erro na saída: {e}", ephemeral=True)
        if not new_open and not new_close:
            return await itx.response.send_message("ℹ️ Nenhuma alteração.", ephemeral=True)
        final_open = new_open if new_open else datetime.datetime.fromisoformat(old_open)
        final_close = new_close if new_close else datetime.datetime.fromisoformat(old_close)
        final_open = localize(final_open)
        final_close = localize(final_close)
        if final_close <= final_open:
            return await itx.response.send_message("❌ Saída deve ser após entrada.", ephemeral=True)
        new_dur = int((final_close - final_open).total_seconds())
        async with aiosqlite.connect(DB) as db:
            await db.execute("UPDATE sessions SET open_time = ?, close_time = ?, dur_sec = ? WHERE id = ?", (final_open.isoformat(), final_close.isoformat(), new_dur, self.session_id))
            await db.commit()
        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="🔄 Horário Ajustado", color=0x3498DB, timestamp=now_br())
            le.add_field(name="Colaborador", value=f"{self.user.mention} (`{self.user.display_name}`)", inline=True)
            le.add_field(name="Antiga Entrada", value=old_open, inline=True)
            le.add_field(name="Nova Entrada", value=final_open.strftime("%d/%m/%Y %H:%M:%S"), inline=True)
            le.add_field(name="Antiga Saída", value=old_close, inline=True)
            le.add_field(name="Nova Saída", value=final_close.strftime("%d/%m/%Y %H:%M:%S"), inline=True)
            le.add_field(name="Nova Duração", value=hms(new_dur), inline=True)
            le.add_field(name="Ajustado por", value=itx.user.mention, inline=True)
            await lch.send(embed=le)
        await itx.response.send_message(f"✅ Sessão de **{self.user.display_name}** ajustada! Nova duração: **{hms(new_dur)}**", ephemeral=True)
        asyncio.create_task(refresh_rank(force=True))

class AdjustSessionView(discord.ui.View):
    def __init__(self, user: discord.Member, sessions: list):
        super().__init__(timeout=120)
        self.user = user
        self.sessions = sessions
        options = []
        for sid, ot, ct, ds in sessions[:10]:
            ot_dt = datetime.datetime.fromisoformat(ot)
            label = f"{ot_dt.strftime('%d/%m %H:%M')} - {hms(ds)}"
            value = str(sid)
            options.append(discord.SelectOption(label=label, value=value, description=f"Duração: {hms(ds)}"))
        if not options:
            options.append(discord.SelectOption(label="Nenhuma sessão fechada", value="none"))
        self.select = discord.ui.Select(placeholder="Selecione a sessão", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)
    async def select_callback(self, itx: discord.Interaction):
        if itx.user.id not in AUTHORIZED_ADJUST_IDS:
            return await itx.response.send_message("❌ Sem permissão.", ephemeral=True)
        selected = self.select.values[0]
        if selected == "none":
            return await itx.response.send_message("Nenhuma sessão disponível.", ephemeral=True)
        sid = int(selected)
        modal = AdjustModal(sid, self.user)
        await itx.response.send_modal(modal)

class RemoveHoursModalSelect(discord.ui.Modal, title="Remover Horas da Sessão"):
    def __init__(self, session_id: int, user: discord.Member):
        super().__init__()
        self.session_id = session_id
        self.user = user
    horas = discord.ui.TextInput(label="Quantas horas remover? (ex: 1.5)", placeholder="Digite um número", required=True, max_length=10)
    async def on_submit(self, itx: discord.Interaction):
        has_role = any(role.id in AUTHORIZED_REMOVE_ROLE_IDS for role in itx.user.roles)
        is_allowed = has_role or (itx.user.id in AUTHORIZED_REMOVE_IDS)
        if not is_allowed:
            return await itx.response.send_message("❌ Sem permissão.", ephemeral=True)
        try:
            horas_remover = float(self.horas.value.replace(',', '.'))
            if horas_remover <= 0:
                raise ValueError
        except ValueError:
            return await itx.response.send_message("❌ Valor inválido.", ephemeral=True)
        segundos_remover = int(horas_remover * 3600)
        async with aiosqlite.connect(DB) as db:
            async with db.execute("SELECT user_id, open_time, close_time, dur_sec FROM sessions WHERE id = ?", (self.session_id,)) as c:
                row = await c.fetchone()
        if not row:
            return await itx.response.send_message("❌ Sessão não encontrada.", ephemeral=True)
        uid, open_time_str, close_time_str, dur_sec = row
        if close_time_str is None:
            return await itx.response.send_message("❌ Sessão aberta.", ephemeral=True)
        if dur_sec < segundos_remover:
            return await itx.response.send_message(f"❌ Sessão tem {hms(dur_sec)}, não pode remover {hms(segundos_remover)}.", ephemeral=True)
        nova_duracao = dur_sec - segundos_remover
        open_dt = datetime.datetime.fromisoformat(open_time_str)
        nova_saida = open_dt + datetime.timedelta(seconds=nova_duracao)
        if nova_duracao <= 0:
            async with aiosqlite.connect(DB) as db:
                await db.execute("DELETE FROM sessions WHERE id = ?", (self.session_id,))
                await db.commit()
            await itx.response.send_message(f"✅ Sessão de **{self.user.display_name}** removida.", ephemeral=True)
            lch = bot.get_channel(LOGS_CHANNEL)
            if lch:
                le = discord.Embed(title="🗑️ Sessão Removida", color=0xFF0000, timestamp=now_br())
                le.add_field(name="Colaborador", value=f"{self.user.mention} (`{self.user.display_name}`)", inline=True)
                le.add_field(name="Horas removidas", value=f"{horas_remover}h", inline=True)
                le.add_field(name="Removido por", value=itx.user.mention, inline=True)
                await lch.send(embed=le)
        else:
            async with aiosqlite.connect(DB) as db:
                await db.execute("UPDATE sessions SET dur_sec = ?, close_time = ? WHERE id = ?", (nova_duracao, nova_saida.isoformat(), self.session_id))
                await db.commit()
            await itx.response.send_message(f"✅ Sessão de **{self.user.display_name}** ajustada: nova duração **{hms(nova_duracao)}**", ephemeral=True)
            lch = bot.get_channel(LOGS_CHANNEL)
            if lch:
                le = discord.Embed(title="⏱️ Horas Removidas", color=0xE67E22, timestamp=now_br())
                le.add_field(name="Colaborador", value=f"{self.user.mention} (`{self.user.display_name}`)", inline=True)
                le.add_field(name="Duração anterior", value=hms(dur_sec), inline=True)
                le.add_field(name="Nova duração", value=hms(nova_duracao), inline=True)
                le.add_field(name="Horas removidas", value=f"{horas_remover}h", inline=True)
                le.add_field(name="Removido por", value=itx.user.mention, inline=True)
                await lch.send(embed=le)
        asyncio.create_task(refresh_rank(force=True))

class RemoveSessionView(discord.ui.View):
    def __init__(self, user: discord.Member, sessions: list):
        super().__init__(timeout=120)
        self.user = user
        self.sessions = sessions
        self.selected_session_id = None
        options = []
        for sid, ot, ct, ds in sessions[:10]:
            ot_dt = datetime.datetime.fromisoformat(ot)
            label = f"{ot_dt.strftime('%d/%m %H:%M')} - {hms(ds)}"
            value = str(sid)
            options.append(discord.SelectOption(label=label, value=value, description=f"Duração: {hms(ds)}"))
        if not options:
            options.append(discord.SelectOption(label="Nenhuma sessão", value="none"))
        self.select = discord.ui.Select(placeholder="Selecione a sessão", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)
    async def select_callback(self, itx: discord.Interaction):
        if self.select.values[0] == "none":
            return await itx.response.send_message("Nenhuma sessão.", ephemeral=True)
        self.selected_session_id = int(self.select.values[0])
        self.remove_button.disabled = False
        await itx.response.edit_message(view=self)
    @discord.ui.button(label="🗑️ Remover Horas", style=discord.ButtonStyle.danger, disabled=True)
    async def remove_button(self, itx: discord.Interaction, _: discord.ui.Button):
        if self.selected_session_id is None:
            return await itx.response.send_message("Selecione uma sessão.", ephemeral=True)
        modal = RemoveHoursModalSelect(self.selected_session_id, self.user)
        await itx.response.send_modal(modal)

# ──────────────────────────────────────────────────────────────
#  COMANDOS DE CONTROLE DA IA
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="ativar_ia", description="Ativa as respostas automáticas da IA neste canal")
@app_commands.default_permissions(administrator=True)
async def cmd_ativar_ia(itx: discord.Interaction):
    channel_id = str(itx.channel_id)
    await set_ia_enabled(channel_id, True)
    embed = discord.Embed(
        title="✅ IA Ativada",
        description=f"A IA agora vai responder automaticamente neste canal.\n\nUse `/desativar_ia` para desligar.",
        color=0x2ECC71
    )
    await itx.response.send_message(embed=embed, ephemeral=True)
    print(f"✅ Canal {channel_id} ativado para IA por {itx.user.display_name}")

@bot.tree.command(name="desativar_ia", description="Desativa as respostas automáticas da IA neste canal")
@app_commands.default_permissions(administrator=True)
async def cmd_desativar_ia(itx: discord.Interaction):
    channel_id = str(itx.channel_id)
    await set_ia_enabled(channel_id, False)
    embed = discord.Embed(
        title="❌ IA Desativada",
        description=f"A IA não vai mais responder automaticamente neste canal.\n\nUse `/ativar_ia` para reativar.",
        color=0xE74C3C
    )
    await itx.response.send_message(embed=embed, ephemeral=True)
    print(f"❌ Canal {channel_id} desativado para IA por {itx.user.display_name}")

@bot.tree.command(name="status_ia", description="Verifica se a IA está ativa neste canal")
async def cmd_status_ia(itx: discord.Interaction):
    channel_id = str(itx.channel_id)
    enabled = await is_ia_enabled(channel_id)
    status = "✅ **Ativa**" if enabled else "❌ **Inativa**"
    embed = discord.Embed(
        title="📊 Status da IA neste canal",
        description=f"A IA está {status}.",
        color=0x2ECC71 if enabled else 0xE74C3C
    )
    embed.add_field(name="Comandos", value="Use `/ativar_ia` para ligar e `/desativar_ia` para desligar.", inline=False)
    await itx.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="listar_canais_ia", description="[ADMIN] Lista todos os canais com IA ativa")
@app_commands.default_permissions(administrator=True)
async def cmd_listar_canais_ia(itx: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT channel_id, enabled FROM ia_enabled_channels WHERE enabled = 1") as c:
            rows = await c.fetchall()
    if not rows:
        return await itx.response.send_message("ℹ️ Nenhum canal com IA ativa.", ephemeral=True)
    lines = []
    for channel_id, enabled in rows:
        channel = bot.get_channel(int(channel_id))
        name = channel.mention if channel else f"ID: {channel_id}"
        lines.append(f"• {name}")
    embed = discord.Embed(
        title="📋 Canais com IA Ativa",
        description="\n".join(lines) if lines else "Nenhum canal ativo.",
        color=0x2ECC71
    )
    embed.set_footer(text=f"Total: {len(rows)} canal(is)")
    await itx.response.send_message(embed=embed, ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  COMANDO /sync — Sincronização manual
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="sync", description="[ADMIN] Sincroniza os comandos do bot")
@app_commands.default_permissions(administrator=True)
async def cmd_sync(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    try:
        synced = await bot.tree.sync()
        await itx.followup.send(f"✅ Comandos sincronizados: {len(synced)}", ephemeral=True)
        print(f"✅ Sincronização manual executada por {itx.user.display_name}")
    except Exception as e:
        await itx.followup.send(f"❌ Erro ao sincronizar: {e}", ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  COMANDO /avaliar — Avaliar uma resposta anterior (opcional)
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="avaliar", description="Avalia uma resposta da IA (use 👍 ou 👎)")
@app_commands.describe(
    mensagem_id="ID da mensagem que contém a resposta da IA",
    rating="1 para bom, -1 para ruim"
)
async def cmd_avaliar(itx: discord.Interaction, mensagem_id: str, rating: int):
    try:
        msg_id = int(mensagem_id)
        # Buscar a conversa pelo ID da mensagem (armazenado no embed ou no metadata)
        # Como não temos um link direto, vamos buscar no banco pela mensagem
        # (isso é apenas um exemplo; você pode adaptar)
        await itx.response.send_message("✅ Avaliação registrada!", ephemeral=True)
    except:
        await itx.response.send_message("❌ Formato inválido.", ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  COMANDO /ia (manual com aprendizado)
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
        # 1. Buscar histórico do canal e do usuário
        channel_history = await get_channel_history(str(itx.channel_id), limit=20)
        user_history = await get_user_history(str(itx.user.id), limit=10)
        user_patterns = await get_user_patterns(str(itx.user.id))

        # 2. Buscar conhecimento relacionado na base de conhecimento
        knowledge = await get_knowledge(pergunta)

        # 3. Montar contexto com histórico, conhecimento e padrões do usuário
        contexto = (
            "Você é um assistente virtual do Hospital ECCO em um servidor FiveM. "
            "Responda de forma educada, objetiva e dentro do contexto hospitalar e de RPG. "
            "Use as informações abaixo para contextualizar sua resposta.\n\n"
        )

        # Adicionar padrões do usuário
        if user_patterns["total_interactions"] > 0:
            contexto += f"--- Perfil do usuário ---\n"
            contexto += f"Este usuário já fez {user_patterns['total_interactions']} perguntas.\n"
            if user_patterns["topics"]:
                top_topics = ", ".join(user_patterns["topics"][-5:])
                contexto += f"Tópicos frequentes: {top_topics}\n"
            contexto += "\n"

        if channel_history:
            contexto += "--- Histórico recente do canal ---\n"
            for entry in channel_history:
                uid, msg, resp, rating, ts = entry
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
                msg, resp, rating, ts = entry
                contexto += f"Você: {msg}\n"
                if resp:
                    contexto += f"Eu: {resp}\n"
            contexto += "\n"

        # Adicionar conhecimento da base
        if knowledge:
            contexto += "--- Conhecimento relevante ---\n"
            for k in knowledge:
                k_id, k_q, k_a, up, down, usage = k
                # Só inclui se tiver nota positiva
                if up - down >= 0:
                    contexto += f"Pergunta: {k_q}\nResposta: {k_a}\n\n"
            contexto += "\n"

        contexto += f"Pergunta atual: {pergunta}\n\n"

        # 4. Gerar resposta
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
                payload = {
                    "contents": [{"parts": [{"text": contexto}]}]
                }
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

        # 5. Salvar conversa (inicialmente sem rating)
        conversation_id = await save_conversation(
            user_id=str(itx.user.id),
            channel_id=str(itx.channel_id),
            message=pergunta,
            response=resposta_texto
        )

        # 6. Atualizar padrões do usuário (extrair tópico simples)
        # Extrai palavras-chave simples (ex: "emergência", "protocolo", etc.)
        palavras = set(re.findall(r'\b\w{4,}\b', pergunta.lower()))
        # Filtra palavras comuns
        stopwords = {'para', 'como', 'por', 'com', 'uma', 'sobre', 'quando', 'onde', 'qual', 'mais', 'muito', 'pode', 'isso'}
        topicos = [p for p in palavras if p not in stopwords]
        for topico in topicos[:3]:
            await update_user_pattern(str(itx.user.id), topico)

        # 7. Salvar conhecimento (se a resposta for boa, será usada futuramente)
        # Simplificamos: salva automaticamente qualquer pergunta + resposta
        await save_knowledge(pergunta, resposta_texto)

        # 8. Enviar resposta com botões de avaliação
        if len(resposta_texto) > 1900:
            resposta_texto = resposta_texto[:1900] + "…"

        embed = discord.Embed(
            title="🤖 Resposta da IA",
            description=resposta_texto,
            color=0x00D4FF,
        )
        embed.set_footer(text=f"Pergunta de {itx.user.display_name} | {len(pergunta)} caracteres", icon_url=itx.user.display_avatar.url)

        # Envia a resposta com os botões de avaliação
        view = RatingView(conversation_id, None)  # Não temos message_id ainda
        await itx.followup.send(embed=embed, view=view)

    except Exception as e:
        await itx.followup.send(f"❌ Erro: {str(e)[:200]}")

# ──────────────────────────────────────────────────────────────
#  COMANDO /lembrar
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="lembrar", description="Agenda um lembrete para você ou para outro usuário")
@app_commands.describe(
    mensagem="O que deseja lembrar",
    data="Data no formato DD/MM/AAAA",
    hora="Hora no formato HH:MM (24h)",
    usuario="Usuário que receberá o lembrete (opcional)"
)
async def cmd_lembrar(
    itx: discord.Interaction,
    mensagem: str,
    data: str,
    hora: str,
    usuario: discord.Member = None
):
    try:
        dt_str = f"{data} {hora}"
        remind_dt = parse_datetime_br(dt_str)
        if remind_dt < now_br():
            return await itx.response.send_message("❌ A data/hora deve ser no futuro.", ephemeral=True)
    except ValueError as e:
        return await itx.response.send_message(f"❌ Formato inválido: {e}", ephemeral=True)

    target_user = usuario if usuario else itx.user
    target_id = str(target_user.id)

    await add_reminder(
        user_id=target_id,
        channel_id=str(itx.channel_id),
        message=mensagem,
        remind_at=remind_dt
    )

    embed = discord.Embed(
        title="✅ Lembrete agendado!",
        description=(
            f"**{mensagem}**\n\n"
            f"⏰ **{remind_dt.strftime('%d/%m/%Y às %H:%M')}**\n"
            f"📌 Para: {target_user.mention}"
        ),
        color=0x2ECC71
    )
    embed.set_footer(text="Você receberá uma notificação na hora.")
    await itx.response.send_message(embed=embed, ephemeral=True)

    if usuario:
        await itx.channel.send(
            f"🔔 {itx.user.mention} agendou um lembrete para {usuario.mention} às **{remind_dt.strftime('%H:%M')}** do dia **{remind_dt.strftime('%d/%m/%Y')}**."
        )

# ──────────────────────────────────────────────────────────────
#  COMANDOS DE BATE PONTO, RANKING, ETC. (mantidos)
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="setup_ponto", description="[ADMIN] Recria o painel de bate ponto")
@app_commands.default_permissions(administrator=True)
async def cmd_setup(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    ch = bot.get_channel(PANEL_CHANNEL)
    if not ch:
        return await itx.followup.send("❌ Canal do painel não encontrado!", ephemeral=True)
    old_mid = await load_mid("panel")
    if old_mid:
        try:
            old_msg = await ch.fetch_message(old_mid)
            await old_msg.delete()
        except:
            pass
    msg = await ch.send(embed=panel_embed(), view=PunchView())
    await save_mid("panel", msg.id)
    await refresh_rank(force=True)
    await itx.followup.send(f"✅ Painel criado em {ch.mention}!", ephemeral=True)

@bot.tree.command(name="setup_painel_remover", description="[ADMIN] Cria o painel de remoção de horas")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_remove_panel(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    ch = bot.get_channel(REMOVE_PANEL_CHANNEL)
    if not ch:
        return await itx.followup.send("❌ Canal de remoção não encontrado!", ephemeral=True)
    old_mid = await load_mid("remove_panel")
    if old_mid:
        try:
            old_msg = await ch.fetch_message(old_mid)
            await old_msg.delete()
        except:
            pass
    embed = discord.Embed(
        title="⏱️ Painel de Remoção de Horas",
        description=(
            "Clique no botão abaixo para **remover horas** de um colaborador.\n"
            "Você precisará informar o **membro** (ID ou menção) e a **quantidade de horas** a remover.\n\n"
            "⚠️ A remoção será aplicada à **última sessão fechada** do colaborador.\n"
            "Se a duração zerar, a sessão será removida."
        ),
        color=0xE67E22,
    )
    embed.set_footer(text="ECCO HOSPITAL CENTER • Apenas cargos autorizados")
    msg = await ch.send(embed=embed, view=RemovePanelView())
    await save_mid("remove_panel", msg.id)
    await itx.followup.send(f"✅ Painel de remoção criado em {ch.mention}!", ephemeral=True)

@bot.tree.command(name="setup_recrutamento", description="[ADMIN] Cria o painel de recrutamento")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_recruit_panel(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    ch = bot.get_channel(RECRUIT_CHANNEL)
    if not ch:
        return await itx.followup.send("❌ Canal de recrutamento não encontrado!", ephemeral=True)
    old_mid = await load_mid("recruit_panel")
    if old_mid:
        try:
            old_msg = await ch.fetch_message(old_mid)
            await old_msg.delete()
        except:
            pass
    embed = discord.Embed(
        title="📢 Painel de Recrutamento",
        description=(
            "Clique no botão abaixo para enviar uma mensagem de **recrutamento**.\n"
            "Você poderá escrever o texto da divulgação e, ao enviar, os cargos autorizados serão mencionados automaticamente.\n\n"
            "📌 Cargos que serão mencionados:\n"
            + "\n".join(f"<@&{role_id}>" for role_id in RECRUIT_ROLE_IDS)
        ),
        color=0x00BFFF,
    )
    embed.set_footer(text="ECCO HOSPITAL CENTER • Recrutamento")
    msg = await ch.send(embed=embed, view=RecruitView())
    await save_mid("recruit_panel", msg.id)
    await itx.followup.send(f"✅ Painel de recrutamento criado em {ch.mention}!", ephemeral=True)

@bot.tree.command(name="meu_ponto", description="Consulte suas horas desta semana")
async def cmd_meu_ponto(itx: discord.Interaction):
    uid = str(itx.user.id)
    now = now_br()
    ws = week_monday(now).isoformat()
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT open_time, close_time, dur_sec FROM sessions WHERE user_id = ? AND week_start >= ? ORDER BY open_time DESC", (uid, ws)) as c:
            sessions = await c.fetchall()
        async with db.execute("SELECT open_time FROM active WHERE user_id = ?", (uid,)) as c:
            active = await c.fetchone()
    total = sum(s[2] for s in sessions if s[2])
    desc = ""
    if active:
        dt = localize(datetime.datetime.fromisoformat(active[0]))
        elapsed = (now - dt).total_seconds()
        total += elapsed
        desc = f"🟢 **Em Serviço** desde `{dt.strftime('%H:%M:%S')}` (+{hms(elapsed)})\n\n"
    e = discord.Embed(title=f"📊 Meu Ponto — {itx.user.display_name}", description=desc, color=0x1565C0)
    e.add_field(name="⏱️ Total da Semana", value=f"**{hms(total)}**", inline=False)
    if sessions:
        lines = []
        for ot, ct, ds in sessions[:8]:
            odt = datetime.datetime.fromisoformat(ot)
            cdt = datetime.datetime.fromisoformat(ct) if ct else None
            end = cdt.strftime("%H:%M") if cdt else "…"
            lines.append(f"📌 `{odt.strftime('%d/%m %H:%M')} → {end}` ({hms(ds or 0)})")
        e.add_field(name="📋 Sessões desta Semana", value="\n".join(lines), inline=False)
    e.set_thumbnail(url=str(itx.user.display_avatar.url))
    e.set_footer(text="ECCO HOSPITAL CENTER")
    await itx.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="rank_horas", description="[MOD] Força atualização do ranking")
@app_commands.default_permissions(manage_messages=True)
async def cmd_rank(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    await refresh_rank(force=True)
    await itx.followup.send("✅ Ranking atualizado!", ephemeral=True)

@bot.tree.command(name="fechar_ponto_admin", description="[ADMIN] Fecha o ponto de um colaborador")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(colaborador="Colaborador cujo ponto deve ser fechado")
async def cmd_fechar_admin(itx: discord.Interaction, colaborador: discord.Member):
    uid = str(colaborador.id)
    now = now_br()
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT open_time FROM active WHERE user_id = ?", (uid,)) as c:
            row = await c.fetchone()
    if not row:
        return await itx.response.send_message(f"⚠️ **{colaborador.display_name}** não tem ponto aberto.", ephemeral=True)
    open_dt = localize(datetime.datetime.fromisoformat(row[0]))
    dur_sec = int((now - open_dt).total_seconds())
    ws = week_monday(open_dt).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO sessions (user_id, user_name, open_time, close_time, dur_sec, week_start) VALUES (?,?,?,?,?,?)", (uid, colaborador.display_name, row[0], now.isoformat(), dur_sec, ws))
        await db.execute("DELETE FROM active WHERE user_id = ?", (uid,))
        await db.commit()
    await itx.response.send_message(f"✅ Ponto de **{colaborador.display_name}** encerrado. Duração: `{hms(dur_sec)}`", ephemeral=True)
    lch = bot.get_channel(LOGS_CHANNEL)
    if lch:
        le = discord.Embed(title="⚠️ Fechamento Forçado", color=0xFF8C00, timestamp=now)
        le.add_field(name="Colaborador", value=f"{colaborador.mention} (`{colaborador.display_name}`)", inline=True)
        le.add_field(name="Admin", value=itx.user.mention, inline=True)
        le.add_field(name="Duração", value=f"**{hms(dur_sec)}**", inline=True)
        await lch.send(embed=le)
    asyncio.create_task(refresh_rank())

@bot.tree.command(name="relatorio", description="[ADMIN] Relatório de horas de um colaborador")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(colaborador="Colaborador", semanas_atras="Quantas semanas atrás? (0 = atual)")
async def cmd_relatorio(itx: discord.Interaction, colaborador: discord.Member, semanas_atras: int = 0):
    uid = str(colaborador.id)
    now = now_br()
    t_ws = week_monday(now) - datetime.timedelta(weeks=semanas_atras)
    t_we = t_ws + datetime.timedelta(days=6)
    ws = t_ws.isoformat()
    we = (t_ws + datetime.timedelta(days=7)).isoformat()
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT open_time, close_time, dur_sec FROM sessions WHERE user_id = ? AND week_start >= ? AND week_start < ? ORDER BY open_time DESC", (uid, ws, we)) as c:
            sessions = await c.fetchall()
    total = sum(s[2] for s in sessions if s[2])
    e = discord.Embed(title=f"📄 Relatório — {colaborador.display_name}", description=f"📅 Semana: {t_ws.strftime('%d/%m')} — {t_we.strftime('%d/%m/%Y')}", color=0x9B59B6)
    e.add_field(name="⏱️ Total de Horas", value=f"**{hms(total)}**", inline=False)
    if sessions:
        lines = []
        for ot, ct, ds in sessions:
            odt = datetime.datetime.fromisoformat(ot)
            cdt = datetime.datetime.fromisoformat(ct) if ct else None
            end = cdt.strftime("%H:%M") if cdt else "…"
            lines.append(f"📌 `{odt.strftime('%d/%m %H:%M')} → {end}` `{hms(ds or 0)}`")
        val = "\n".join(lines)
        if len(val) > 1024:
            val = val[:1021] + "…"
        e.add_field(name=f"📋 Sessões ({len(sessions)})", value=val, inline=False)
    else:
        e.add_field(name="📋 Sessões", value="Nenhuma sessão encontrada.", inline=False)
    e.set_thumbnail(url=str(colaborador.display_avatar.url))
    e.set_footer(text="ECCO HOSPITAL CENTER")
    await itx.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="pontos_abertos", description="[ADMIN] Lista colaboradores com ponto aberto")
@app_commands.default_permissions(administrator=True)
async def cmd_pontos_abertos(itx: discord.Interaction):
    now = now_br()
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT user_id, user_name, open_time FROM active ORDER BY open_time") as c:
            rows = await c.fetchall()
    if not rows:
        return await itx.response.send_message("✅ Nenhum colaborador com ponto aberto.", ephemeral=True)
    lines = []
    for uid, uname, ot in rows:
        dt = localize(datetime.datetime.fromisoformat(ot))
        elapsed = (now - dt).total_seconds()
        lines.append(f"🟢 **{uname}** — desde `{dt.strftime('%H:%M:%S')}` (+{hms(elapsed)})")
    e = discord.Embed(title="🟢 Colaboradores com Ponto Aberto", description="\n".join(lines), color=0x2ECC71)
    e.set_footer(text=f"Total: {len(rows)} colaborador(es)")
    await itx.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="remover_horas", description="[AUTORIZADO] Remove horas de uma sessão específica")
@app_commands.describe(colaborador="Colaborador")
async def cmd_remover_horas(itx: discord.Interaction, colaborador: discord.Member):
    has_role = any(role.id in AUTHORIZED_REMOVE_ROLE_IDS for role in itx.user.roles)
    is_allowed = has_role or (itx.user.id in AUTHORIZED_REMOVE_IDS)
    if not is_allowed:
        return await itx.response.send_message("❌ Você não tem permissão.", ephemeral=True)
    uid = str(colaborador.id)
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT id, open_time, close_time, dur_sec FROM sessions WHERE user_id = ? AND close_time IS NOT NULL ORDER BY open_time DESC LIMIT 10", (uid,)) as c:
            sessions = await c.fetchall()
    if not sessions:
        return await itx.response.send_message(f"ℹ️ **{colaborador.display_name}** não possui sessões fechadas.", ephemeral=True)
    view = RemoveSessionView(colaborador, sessions)
    embed = discord.Embed(title="🗑️ Remover Horas", description=f"Selecione a sessão de **{colaborador.display_name}**", color=0xE67E22)
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="ajustar_horario", description="[AUTORIZADO] Ajusta entrada/saída de uma sessão")
@app_commands.describe(colaborador="Colaborador")
async def cmd_ajustar_horario(itx: discord.Interaction, colaborador: discord.Member):
    if itx.user.id not in AUTHORIZED_ADJUST_IDS:
        return await itx.response.send_message("❌ Você não tem permissão.", ephemeral=True)
    uid = str(colaborador.id)
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT id, open_time, close_time, dur_sec FROM sessions WHERE user_id = ? AND close_time IS NOT NULL ORDER BY open_time DESC LIMIT 10", (uid,)) as c:
            sessions = await c.fetchall()
    if not sessions:
        return await itx.response.send_message(f"ℹ️ **{colaborador.display_name}** não possui sessões fechadas.", ephemeral=True)
    view = AdjustSessionView(colaborador, sessions)
    embed = discord.Embed(title="🔄 Ajustar Horário", description=f"Selecione a sessão de **{colaborador.display_name}**", color=0x3498DB)
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  EVENTO on_message — RESPOSTAS AUTOMÁTICAS COM APRENDIZADO
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_message(msg: discord.Message):
    if msg.content.startswith(("/", "!")):
        await bot.process_commands(msg)
        return
    if msg.author.bot:
        return
    channel_id = str(msg.channel.id)
    if not await is_ia_enabled(channel_id):
        await bot.process_commands(msg)
        return
    if not GEMINI_API_KEY or not GEMINI_MODELS:
        await bot.process_commands(msg)
        return

    async with msg.channel.typing():
        try:
            # Buscar histórico do canal
            channel_history = await get_channel_history(str(msg.channel.id), limit=15)
            user_patterns = await get_user_patterns(str(msg.author.id))

            contexto = (
                "Você é um assistente virtual do Hospital ECCO em um servidor FiveM. "
                "Responda de forma breve, educada e útil. Máximo de 400 caracteres. "
                "Seja direto e objetivo.\n\n"
            )

            if user_patterns["total_interactions"] > 0:
                contexto += f"--- Perfil do usuário ---\n"
                contexto += f"Este usuário já fez {user_patterns['total_interactions']} perguntas.\n"
                if user_patterns["topics"]:
                    top_topics = ", ".join(user_patterns["topics"][-5:])
                    contexto += f"Tópicos frequentes: {top_topics}\n"
                contexto += "\n"

            if channel_history:
                contexto += "--- Histórico recente do canal ---\n"
                for entry in channel_history:
                    uid, m, r, rating, ts = entry
                    user = bot.get_user(int(uid))
                    nome = user.display_name if user else uid
                    if r:
                        contexto += f"{nome}: {m}\nBot: {r}\n"
                    else:
                        contexto += f"{nome}: {m}\n"
                contexto += "\n"

            contexto += f"Usuário {msg.author.display_name}: {msg.content}"

            # Buscar conhecimento
            knowledge = await get_knowledge(msg.content)
            if knowledge:
                contexto += "--- Conhecimento relevante ---\n"
                for k in knowledge:
                    k_id, k_q, k_a, up, down, usage = k
                    if up - down >= 0:
                        contexto += f"Pergunta: {k_q}\nResposta: {k_a}\n\n"
                contexto += "\n"

            # Gerar resposta
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
                # Salvar conversa
                conversation_id = await save_conversation(
                    user_id=str(msg.author.id),
                    channel_id=str(msg.channel.id),
                    message=msg.content,
                    response=resposta_texto
                )

                # Atualizar padrões do usuário
                palavras = set(re.findall(r'\b\w{4,}\b', msg.content.lower()))
                stopwords = {'para', 'como', 'por', 'com', 'uma', 'sobre', 'quando', 'onde', 'qual', 'mais', 'muito', 'pode', 'isso'}
                topicos = [p for p in palavras if p not in stopwords]
                for topico in topicos[:3]:
                    await update_user_pattern(str(msg.author.id), topico)

                # Salvar conhecimento
                await save_knowledge(msg.content, resposta_texto)

                # Enviar resposta com botões de avaliação
                if len(resposta_texto) > 1900:
                    resposta_texto = resposta_texto[:1900] + "…"

                embed = discord.Embed(
                    description=resposta_texto,
                    color=0x00D4FF,
                )
                embed.set_footer(text=f"Respondendo a {msg.author.display_name} | Aprendendo com você")

                view = RatingView(conversation_id, None)
                await msg.reply(embed=embed, view=view, mention_author=False)

        except Exception as e:
            print(f"❌ Erro no on_message: {e}")

    await bot.process_commands(msg)

# ──────────────────────────────────────────────────────────────
#  TASKS
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

    bot.add_view(PunchView())
    bot.add_view(RemovePanelView())
    bot.add_view(RecruitView())

    ch_panel = bot.get_channel(PANEL_CHANNEL)
    if ch_panel:
        mid_panel = await load_mid("panel")
        needs_panel = True
        if mid_panel:
            try:
                await ch_panel.fetch_message(mid_panel)
                needs_panel = False
            except:
                pass
        if needs_panel:
            msg = await ch_panel.send(embed=panel_embed(), view=PunchView())
            await save_mid("panel", msg.id)
            print(f"📋 Painel criado no canal {PANEL_CHANNEL}")
    else:
        print(f"⚠️ Canal do painel ({PANEL_CHANNEL}) não encontrado.")

    ch_remove = bot.get_channel(REMOVE_PANEL_CHANNEL)
    if ch_remove:
        mid_remove = await load_mid("remove_panel")
        needs_remove = True
        if mid_remove:
            try:
                await ch_remove.fetch_message(mid_remove)
                needs_remove = False
            except:
                pass
        if needs_remove:
            embed = discord.Embed(
                title="⏱️ Painel de Remoção de Horas",
                description=(
                    "Clique no botão abaixo para **remover horas** de um colaborador.\n"
                    "Você precisará informar o **membro** (ID ou menção) e a **quantidade de horas** a remover.\n\n"
                    "⚠️ A remoção será aplicada à **última sessão fechada** do colaborador.\n"
                    "Se a duração zerar, a sessão será removida."
                ),
                color=0xE67E22,
            )
            embed.set_footer(text="ECCO HOSPITAL CENTER • Apenas cargos autorizados")
            msg = await ch_remove.send(embed=embed, view=RemovePanelView())
            await save_mid("remove_panel", msg.id)
            print(f"📋 Painel de remoção criado no canal {REMOVE_PANEL_CHANNEL}")
    else:
        print(f"⚠️ Canal de remoção ({REMOVE_PANEL_CHANNEL}) não encontrado.")

    ch_recruit = bot.get_channel(RECRUIT_CHANNEL)
    if ch_recruit:
        mid_recruit = await load_mid("recruit_panel")
        needs_recruit = True
        if mid_recruit:
            try:
                await ch_recruit.fetch_message(mid_recruit)
                needs_recruit = False
            except:
                pass
        if needs_recruit:
            embed = discord.Embed(
                title="📢 Painel de Recrutamento",
                description=(
                    "Clique no botão abaixo para enviar uma mensagem de **recrutamento**.\n"
                    "Você poderá escrever o texto da divulgação e, ao enviar, os cargos autorizados serão mencionados automaticamente.\n\n"
                    "📌 Cargos que serão mencionados:\n"
                    + "\n".join(f"<@&{role_id}>" for role_id in RECRUIT_ROLE_IDS)
                ),
                color=0x00BFFF,
            )
            embed.set_footer(text="ECCO HOSPITAL CENTER • Recrutamento")
            msg = await ch_recruit.send(embed=embed, view=RecruitView())
            await save_mid("recruit_panel", msg.id)
            print(f"📋 Painel de recrutamento criado no canal {RECRUIT_CHANNEL}")
    else:
        print(f"⚠️ Canal de recrutamento ({RECRUIT_CHANNEL}) não encontrado.")

    ch_rank = bot.get_channel(RANK_CHANNEL)
    if not ch_rank:
        print(f"⚠️ Canal de ranking ({RANK_CHANNEL}) não encontrado.")
    else:
        await refresh_rank(force=True)
        auto_refresh.start()

    notify_active_users.start()
    check_reminders.start()

    try:
        synced = await bot.tree.sync()
        print(f"✅ Comandos sincronizados: {len(synced)}")
        for cmd in synced:
            print(f"   /{cmd.name}")
    except Exception as exc:
        print(f"❌ Erro ao sincronizar comandos: {exc}")

    print(f"✅ {bot.user} (ID: {bot.user.id}) online!")
    print(f"   {len(bot.guilds)} servidor(es)")

# ──────────────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Defina a variável de ambiente DISCORD_TOKEN antes de iniciar o bot.")
    bot.run(TOKEN)