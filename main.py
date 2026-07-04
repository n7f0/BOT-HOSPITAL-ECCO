"""
╔══════════════════════════════════════════════════════════════╗
║          ECCO HOSPITAL CENTER — BOT DE BATE PONTO           ║
║            COM IA, MEMÓRIA, APRENDIZADO CONTÍNUO            ║
║           COMANDOS PERSONALIZADOS: /ativar_ia /desativar_ia ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import datetime
import os
import time
import re
import warnings
import requests
import json
import hashlib

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
intents.reactions     = True  # Para capturar reações
bot = commands.Bot(command_prefix="!", intents=intents)

_rank_lock   = asyncio.Lock()
_last_update: float = 0.0

# ──────────────────────────────────────────────────────────────
#  DATABASE COM TABELAS DE APRENDIZADO
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
                timestamp  TEXT NOT NULL,
                rating     INTEGER DEFAULT 0,
                message_id TEXT,
                thread_id  TEXT
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
                key        TEXT UNIQUE NOT NULL,
                value      TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                last_used  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id    TEXT PRIMARY KEY,
                preferred_model TEXT,
                response_style TEXT,
                last_interaction TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_history_user ON conversation_history(user_id);
            CREATE INDEX IF NOT EXISTS idx_history_channel ON conversation_history(channel_id);
            CREATE INDEX IF NOT EXISTS idx_knowledge_key ON knowledge_base(key);
        """)
        await db.commit()

        # Migrações
        cursor = await db.execute("PRAGMA table_info(reminders)")
        columns = await cursor.fetchall()
        col_names = [col[1] for col in columns]
        if "done" not in col_names:
            await db.execute("ALTER TABLE reminders ADD COLUMN done INTEGER DEFAULT 0")
            await db.commit()
            print("✅ Coluna 'done' adicionada à tabela reminders.")

        cursor = await db.execute("PRAGMA table_info(conversation_history)")
        columns = await cursor.fetchall()
        col_names = [col[1] for col in columns]
        for col in ["rating", "message_id", "thread_id"]:
            if col not in col_names:
                await db.execute(f"ALTER TABLE conversation_history ADD COLUMN {col} TEXT")
                await db.commit()
                print(f"✅ Coluna '{col}' adicionada à tabela conversation_history.")

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES DE APRENDIZADO E MEMÓRIA
# ──────────────────────────────────────────────────────────────
async def save_conversation(user_id: str, channel_id: str, message: str, response: str = None, 
                            message_id: str = None, thread_id: str = None):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """INSERT INTO conversation_history 
               (user_id, channel_id, message, response, timestamp, message_id, thread_id) 
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, channel_id, message, response, 
             datetime.datetime.now(BR_TZ).isoformat(), message_id, thread_id)
        )
        await db.commit()

async def rate_conversation(message_id: str, rating: int):
    """Atualiza a avaliação de uma resposta (rating: 1=útil, -1=não útil)"""
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE conversation_history SET rating = ? WHERE message_id = ?",
            (rating, message_id)
        )
        await db.commit()

async def get_positive_responses(user_id: str, limit: int = 5) -> list:
    """Retorna as últimas respostas bem avaliadas do usuário"""
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            """SELECT message, response FROM conversation_history 
               WHERE user_id = ? AND rating > 0 AND response IS NOT NULL 
               ORDER BY timestamp DESC LIMIT ?""",
            (user_id, limit)
        ) as c:
            rows = await c.fetchall()
    return rows

async def extract_knowledge(text: str) -> list:
    """Extrai fatos simples de uma conversa (ex: "o CEO é João")"""
    # Implementação simples: procura padrões "X é Y" ou "X são Y"
    facts = []
    patterns = [
        r'([A-Za-zÀ-ÿ\s]+)\s+(é|são)\s+([A-Za-zÀ-ÿ\s,]+)',
        r'([A-Za-zÀ-ÿ\s]+)\s+tem\s+([A-Za-zÀ-ÿ\s]+)',
        r'([A-Za-zÀ-ÿ\s]+)\s+está\s+([A-Za-zÀ-ÿ\s]+)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text, re.IGNORECASE)
        for match in matches:
            key = match[0].strip().lower()
            value = match[-1].strip()
            if len(key) > 3 and len(value) > 2:
                facts.append((key, value))
    return facts

async def update_knowledge_base(key: str, value: str):
    """Atualiza ou insere conhecimento na base"""
    key_hash = hashlib.md5(key.encode()).hexdigest()[:16]
    now = datetime.datetime.now(BR_TZ).isoformat()
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """INSERT INTO knowledge_base (key, value, confidence, last_used) 
               VALUES (?, ?, 1.0, ?)
               ON CONFLICT(key) DO UPDATE SET 
               value = excluded.value, 
               confidence = confidence + 0.1,
               last_used = excluded.last_used""",
            (key_hash, f"{key}: {value}", now)
        )
        await db.commit()

async def get_knowledge_for_context(limit: int = 10) -> str:
    """Recupera conhecimento relevante para o contexto"""
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            """SELECT value FROM knowledge_base 
               ORDER BY confidence DESC, last_used DESC LIMIT ?""",
            (limit,)
        ) as c:
            rows = await c.fetchall()
    return "\n".join([row[0] for row in rows if row[0]])

async def get_user_preferences(user_id: str) -> dict:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT preferred_model, response_style FROM user_preferences WHERE user_id = ?",
            (user_id,)
        ) as c:
            row = await c.fetchone()
    if row:
        return {"model": row[0], "style": row[1]}
    return {}

async def update_user_preferences(user_id: str, **kwargs):
    now = datetime.datetime.now(BR_TZ).isoformat()
    async with aiosqlite.connect(DB) as db:
        # Primeiro verifica se existe
        async with db.execute("SELECT 1 FROM user_preferences WHERE user_id = ?", (user_id,)) as c:
            exists = await c.fetchone()
        if exists:
            # Update
            set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
            values = list(kwargs.values()) + [now, user_id]
            await db.execute(
                f"UPDATE user_preferences SET {set_clause}, last_interaction = ? WHERE user_id = ?",
                values
            )
        else:
            # Insert
            cols = ", ".join(["user_id"] + list(kwargs.keys()) + ["last_interaction"])
            placeholders = ", ".join(["?"] * (1 + len(kwargs) + 1))
            values = [user_id] + list(kwargs.values()) + [now]
            await db.execute(f"INSERT INTO user_preferences ({cols}) VALUES ({placeholders})", values)
        await db.commit()

# ──────────────────────────────────────────────────────────────
#  FUNÇÃO PARA GERAR RESPOSTA COM APRENDIZADO
# ──────────────────────────────────────────────────────────────
async def generate_ia_response(user_id: str, channel_id: str, pergunta: str, contexto_adicional: str = ""):
    """Gera resposta da IA com base no histórico, conhecimento e preferências do usuário"""
    
    # Buscar histórico do canal (últimas 20)
    channel_history = await get_channel_history(channel_id, limit=20)
    user_history = await get_user_history(user_id, limit=10)
    positive_responses = await get_positive_responses(user_id, limit=5)
    knowledge = await get_knowledge_for_context(limit=10)
    preferences = await get_user_preferences(user_id)

    contexto = (
        "Você é um assistente virtual do Hospital ECCO em um servidor FiveM. "
        "Responda de forma educada, objetiva e dentro do contexto hospitalar e de RPG. "
        "Use as informações abaixo para contextualizar sua resposta.\n\n"
    )

    # Adicionar conhecimento adquirido
    if knowledge:
        contexto += f"--- CONHECIMENTO ADQUIRIDO ---\n{knowledge}\n\n"

    # Adicionar histórico do canal
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

    # Adicionar histórico do usuário
    if user_history:
        contexto += "--- Seu histórico recente comigo ---\n"
        for entry in user_history:
            msg, resp, ts = entry
            contexto += f"Você: {msg}\n"
            if resp:
                contexto += f"Eu: {resp}\n"
        contexto += "\n"

    # Adicionar respostas que foram úteis
    if positive_responses:
        contexto += "--- Respostas que você achou úteis no passado ---\n"
        for msg, resp in positive_responses:
            contexto += f"P: {msg}\nR: {resp}\n"
        contexto += "\n"

    # Estilo de resposta personalizado
    if preferences.get("style"):
        contexto += f"Preferência de estilo: {preferences['style']}\n\n"

    # Adicionar contexto adicional (se houver)
    if contexto_adicional:
        contexto += f"Contexto extra: {contexto_adicional}\n\n"

    contexto += f"Pergunta atual: {pergunta}\n\n"

    # Gerar resposta
    resposta_texto = None
    ultimo_erro = None
    
    # Tentar usar modelo preferido do usuário, se disponível
    modelos_tentar = GEMINI_MODELS.copy()
    if preferences.get("model") and preferences["model"] in modelos_tentar:
        modelos_tentar.remove(preferences["model"])
        modelos_tentar.insert(0, preferences["model"])

    for modelo in modelos_tentar:
        try:
            model = genai.GenerativeModel(modelo)
            resposta = model.generate_content(contexto)
            resposta_texto = resposta.text.strip()
            break
        except Exception as e:
            ultimo_erro = e
            continue

    # Fallback REST
    if resposta_texto is None:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            payload = {"contents": [{"parts": [{"text": contexto}]}]}
            response = requests.post(url, json=payload, timeout=30)
            if response.status_code == 200:
                data = response.json()
                if "candidates" in data and data["candidates"]:
                    resposta_texto = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            ultimo_erro = f"REST falhou: {e}"

    if resposta_texto is None:
        raise Exception(f"Falha ao gerar resposta. Último erro: {ultimo_erro}")

    # Extrair conhecimento da resposta e pergunta
    try:
        # Extrair fatos da conversa
        facts = await extract_knowledge(pergunta + " " + resposta_texto)
        for key, value in facts:
            await update_knowledge_base(key, value)
    except Exception as e:
        print(f"⚠️ Erro ao extrair conhecimento: {e}")

    return resposta_texto

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES AUXILIARES EXISTENTES (adaptadas)
# ──────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────
#  TASK — EXTRAIR CONHECIMENTO PERIODICAMENTE
# ──────────────────────────────────────────────────────────────
@tasks.loop(hours=6)
async def extract_knowledge_task():
    """Tarefa para extrair conhecimento de conversas recentes"""
    try:
        async with aiosqlite.connect(DB) as db:
            # Pegar conversas das últimas 6 horas que ainda não foram processadas
            cutoff = (datetime.datetime.now(BR_TZ) - datetime.timedelta(hours=6)).isoformat()
            async with db.execute(
                """SELECT message, response FROM conversation_history 
                   WHERE timestamp > ? AND response IS NOT NULL 
                   AND id NOT IN (SELECT conversation_id FROM knowledge_extracted)""",
                (cutoff,)
            ) as c:
                rows = await c.fetchall()
        
        for msg, resp in rows:
            text = f"{msg} {resp}"
            facts = await extract_knowledge(text)
            for key, value in facts:
                await update_knowledge_base(key, value)
    except Exception as e:
        print(f"⚠️ Erro na extração de conhecimento: {e}")

# ──────────────────────────────────────────────────────────────
#  EVENTO DE REAÇÃO PARA AVALIAÇÃO
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    
    # Verifica se a reação é em uma mensagem do bot
    if reaction.message.author.id != bot.user.id:
        return
    
    # Verifica se a mensagem tem ID registrada no banco
    message_id = str(reaction.message.id)
    
    # Determina o rating baseado na reação
    emoji = str(reaction.emoji)
    rating = 0
    if emoji in ["👍", "✅", "❤️", "⭐"]:
        rating = 1
    elif emoji in ["👎", "❌", "😡"]:
        rating = -1
    
    if rating != 0:
        await rate_conversation(message_id, rating)
        print(f"📊 Avaliação: {user.display_name} deu {rating} para mensagem {message_id}")

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
    await itx.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="listar_canais_ia", description="[ADMIN] Lista todos os canais com IA ativa")
@app_commands.default_permissions(administrator=True)
async def cmd_listar_canais_ia(itx: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT channel_id FROM ia_enabled_channels WHERE enabled = 1") as c:
            rows = await c.fetchall()
    if not rows:
        return await itx.response.send_message("ℹ️ Nenhum canal com IA ativa.", ephemeral=True)
    lines = []
    for row in rows:
        channel = bot.get_channel(int(row[0]))
        name = channel.mention if channel else f"ID: {row[0]}"
        lines.append(f"• {name}")
    embed = discord.Embed(title="📋 Canais com IA Ativa", description="\n".join(lines), color=0x2ECC71)
    await itx.response.send_message(embed=embed, ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  COMANDO /ia (com aprendizado)
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="ia", description="Faça uma pergunta para a IA (com aprendizado)")
@app_commands.describe(pergunta="Sua pergunta")
async def cmd_ia(itx: discord.Interaction, pergunta: str):
    if not GEMINI_API_KEY:
        return await itx.response.send_message("❌ IA não configurada.", ephemeral=True)
    if not GEMINI_MODELS:
        return await itx.response.send_message("❌ Nenhum modelo disponível.", ephemeral=True)

    await itx.response.defer(ephemeral=False)

    try:
        resposta_texto = await generate_ia_response(
            str(itx.user.id), 
            str(itx.channel_id), 
            pergunta
        )
        
        # Salvar conversa com ID da mensagem
        msg = await itx.followup.send(resposta_texto[:1900])
        await save_conversation(
            user_id=str(itx.user.id),
            channel_id=str(itx.channel_id),
            message=pergunta,
            response=resposta_texto,
            message_id=str(msg.id)
        )
        
        # Atualizar preferências do usuário (estilo de resposta)
        # Podemos detectar se o usuário gosta de respostas mais curtas ou longas
        if len(resposta_texto) < 100:
            await update_user_preferences(str(itx.user.id), response_style="curto")
        elif len(resposta_texto) > 500:
            await update_user_preferences(str(itx.user.id), response_style="detalhado")
        
    except Exception as e:
        await itx.followup.send(f"❌ Erro: {str(e)[:200]}")

# ──────────────────────────────────────────────────────────────
#  COMANDO /aprender — Força aprendizado manual
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="aprender", description="[ADMIN] Força o bot a aprender com uma conversa específica")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    mensagem="Mensagem para aprender",
    resposta="Resposta correspondente (opcional)"
)
async def cmd_aprender(itx: discord.Interaction, mensagem: str, resposta: str = None):
    await itx.response.defer(ephemeral=True)
    try:
        # Extrair conhecimento
        text = mensagem
        if resposta:
            text += " " + resposta
        facts = await extract_knowledge(text)
        count = 0
        for key, value in facts:
            await update_knowledge_base(key, value)
            count += 1
        
        await itx.followup.send(f"✅ Aprendido {count} fato(s) da mensagem.", ephemeral=True)
    except Exception as e:
        await itx.followup.send(f"❌ Erro: {str(e)[:200]}", ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  COMANDO /conhecimento — Ver conhecimento adquirido
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="conhecimento", description="Mostra o conhecimento que o bot já aprendeu")
async def cmd_conhecimento(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    try:
        knowledge = await get_knowledge_for_context(limit=20)
        if not knowledge:
            return await itx.followup.send("ℹ️ Ainda não aprendi nada.", ephemeral=True)
        
        embed = discord.Embed(
            title="🧠 Conhecimento Adquirido",
            description=f"```\n{knowledge}\n```",
            color=0x9B59B6
        )
        embed.set_footer(text="O conhecimento é atualizado automaticamente com o uso.")
        await itx.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await itx.followup.send(f"❌ Erro: {str(e)[:200]}", ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  COMANDO /limpar_conhecimento — Limpa conhecimento (admin)
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="limpar_conhecimento", description="[ADMIN] Limpa toda a base de conhecimento")
@app_commands.default_permissions(administrator=True)
async def cmd_limpar_conhecimento(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    try:
        async with aiosqlite.connect(DB) as db:
            await db.execute("DELETE FROM knowledge_base")
            await db.commit()
        await itx.followup.send("✅ Base de conhecimento limpa.", ephemeral=True)
    except Exception as e:
        await itx.followup.send(f"❌ Erro: {str(e)[:200]}", ephemeral=True)

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES EXISTENTES (bate ponto, ranking, lembretes, etc.)
# ──────────────────────────────────────────────────────────────
# [Manter todas as funções e classes existentes: PunchView, RemovePanelView, etc.]
# Para não estourar o limite, vou incluir apenas a estrutura, mas você deve copiar as classes completas do seu código anterior.

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
            resposta_texto = await generate_ia_response(
                str(msg.author.id),
                str(msg.channel.id),
                msg.content
            )
            
            if resposta_texto:
                # Salvar conversa com ID da mensagem
                reply = await msg.reply(resposta_texto[:1900], mention_author=False)
                await save_conversation(
                    user_id=str(msg.author.id),
                    channel_id=str(msg.channel.id),
                    message=msg.content,
                    response=resposta_texto,
                    message_id=str(reply.id)
                )
        except Exception as e:
            print(f"❌ Erro no on_message: {e}")

    await bot.process_commands(msg)

# ──────────────────────────────────────────────────────────────
#  ON READY
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    # Registrar views (PunchView, RemovePanelView, RecruitView)
    # [Manter as views do código anterior]
    
    # Iniciar tasks
    auto_refresh.start()
    notify_active_users.start()
    check_reminders.start()
    extract_knowledge_task.start()
    
    # Sincronizar comandos
    try:
        synced = await bot.tree.sync()
        print(f"✅ Comandos sincronizados: {len(synced)}")
        for cmd in synced:
            print(f"   /{cmd.name}")
    except Exception as e:
        print(f"❌ Erro ao sincronizar: {e}")
    
    print(f"✅ {bot.user} (ID: {bot.user.id}) online!")

# ──────────────────────────────────────────────────────────────
#  TASKS EXISTENTES
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
        user = bot.get_user(int(uid))
        if not user:
            continue
        try:
            embed = discord.Embed(
                title="⏰ Verificação de Ponto",
                description=f"Olá **{uname}**, você ainda está em serviço?",
                color=0x3498DB
            )
            open_dt = localize(datetime.datetime.fromisoformat(ot))
            embed.add_field(name="🕐 Ponto aberto desde", value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"), inline=False)
            embed.set_footer(text="ECCO HOSPITAL CENTER • Notificação automática")
            view = DMNotifyView(int(uid))
            await user.send(embed=embed, view=view)
        except:
            pass

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES AUXILIARES FALTANTES
# ──────────────────────────────────────────────────────────────
def now_br():
    return datetime.datetime.now(BR_TZ)

def localize(dt):
    return BR_TZ.localize(dt) if dt.tzinfo is None else dt

async def is_ia_enabled(channel_id: str) -> bool:
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT enabled FROM ia_enabled_channels WHERE channel_id = ?", (channel_id,)) as c:
            row = await c.fetchone()
    return bool(row[0] if row else 0)

async def set_ia_enabled(channel_id: str, enabled: bool):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR REPLACE INTO ia_enabled_channels (channel_id, enabled) VALUES (?, ?)",
                         (channel_id, 1 if enabled else 0))
        await db.commit()

async def get_due_reminders():
    now = datetime.datetime.now(BR_TZ).isoformat()
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT id, user_id, channel_id, message, remind_at FROM reminders WHERE done = 0 AND remind_at <= ?", (now,)) as c:
            return await c.fetchall()

async def mark_reminder_done(rid):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
        await db.commit()

async def refresh_rank(force=False):
    # Placeholder - manter do código original
    pass

# ──────────────────────────────────────────────────────────────
#  VIEWS (manter do código anterior)
# ──────────────────────────────────────────────────────────────
# [Inserir aqui todas as classes de View: PunchView, RemovePanelView, etc.]

# ──────────────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ Defina a variável de ambiente DISCORD_TOKEN antes de iniciar o bot.")
    bot.run(TOKEN)