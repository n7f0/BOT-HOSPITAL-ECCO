"""
╔══════════════════════════════════════════════════════════════╗
║          ECCO HOSPITAL CENTER — BOT DE BATE PONTO           ║
║                     + IA GEMINI (google.genai)              ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import datetime
import os
import time
import re

import aiosqlite
import discord
import pytz
from discord import app_commands
from discord.ext import commands, tasks

# ──────────────────────────────────────────────────────────────
#  CONFIGURAÇÃO DA IA (GEMINI - novo pacote google.genai)
# ──────────────────────────────────────────────────────────────
from google import genai

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if GEMINI_API_KEY:
    GEMINI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    # Lista de modelos em ordem de preferência (nomes oficiais sem prefixo)
    GEMINI_MODELS = [
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.0-pro",
        "gemini-pro",          # fallback mais antigo
    ]
    # Remove modelos que não estão disponíveis
    _available_models = []
    for model in GEMINI_MODELS:
        try:
            # Testa se o modelo existe com uma chamada leve (apenas listagem)
            # Como não há um método direto, tentamos gerar uma resposta curta
            GEMINI_CLIENT.models.generate_content(
                model=model,
                contents="Teste"
            )
            _available_models.append(model)
        except Exception:
            continue
    GEMINI_MODELS = _available_models if _available_models else ["gemini-pro"]
    print(f"🔍 Modelos Gemini disponíveis: {GEMINI_MODELS}")
else:
    GEMINI_CLIENT = None
    GEMINI_MODELS = []

# Canal opcional para respostas automáticas (defina o ID ou None)
IA_CHANNEL_ID = None  # Exemplo: 123456789012345678

# ──────────────────────────────────────────────────────────────
#  CONFIGURAÇÃO DO BOT
# ──────────────────────────────────────────────────────────────
TOKEN         = os.environ.get("DISCORD_TOKEN")
PANEL_CHANNEL = int(os.environ.get("PANEL_CHANNEL_ID", "1515846128493658142"))
RANK_CHANNEL  = int(os.environ.get("RANK_CHANNEL_ID", "1515852084480839850"))
LOGS_CHANNEL  = int(os.environ.get("LOGS_CHANNEL_ID",  "1515846898156834956"))
DB            = os.environ.get("DB_PATH", "ponto.db")
BR_TZ         = pytz.timezone("America/Sao_Paulo")

# Canal onde ficará o painel de remoção de horas
REMOVE_PANEL_CHANNEL = 1515846758456885400

# Canal onde ficará o painel de recrutamento
RECRUIT_CHANNEL = 1480675270376558766

# IDs dos cargos autorizados a remover horas
AUTHORIZED_REMOVE_ROLE_IDS = [
    1480675269449617524,
    1480675269449617523,
    1480675269449617522,
    1480675269449617521,
    1480675269449617525,
]

# IDs fixos para remoção (opcional, mantido por compatibilidade)
AUTHORIZED_REMOVE_IDS = [1480675269449617525, 1508478383825354892]

# IDs para ajuste de horário (mantido)
AUTHORIZED_ADJUST_IDS = [
    1480675269449617524,
    1480675269449617521,
    1480675269449617523,
    1480675269449617522,
]

# IDs dos cargos que serão mencionados no botão de recrutamento
RECRUIT_ROLE_IDS = [
    1496602784206950571,
    1497672467861475469,
    1480675269449617523,
    1480675269449617524,
    1480675269449617525,
    1480675269449617526,
    1480675269449617527,
]

# ──────────────────────────────────────────────────────────────
#  BOT
# ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members       = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_rank_lock   = asyncio.Lock()
_last_update: float = 0.0

# ──────────────────────────────────────────────────────────────
#  DATABASE
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
        """)
        await db.commit()


# ──────────────────────────────────────────────────────────────
#  UTILITÁRIOS
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
    """Extrai ID de uma menção ou retorna o número puro."""
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


# ──────────────────────────────────────────────────────────────
#  DADOS DO RANKING
# ──────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────
#  EMBEDS
# ──────────────────────────────────────────────────────────────
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


# ──────────────────────────────────────────────────────────────
#  ATUALIZAR RANKING
# ──────────────────────────────────────────────────────────────
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
#  VIEW — BOTÕES DE PONTO (painel principal)
# ──────────────────────────────────────────────────────────────
class PunchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="✅  Abrir Ponto",
        style=discord.ButtonStyle.success,
        custom_id="ecco:open",
    )
    async def open_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        uid  = str(itx.user.id)
        name = itx.user.display_name
        now  = now_br()

        async with aiosqlite.connect(DB) as db:
            async with db.execute("SELECT open_time FROM active WHERE user_id = ?", (uid,)) as c:
                row = await c.fetchone()

        if row:
            dt      = localize(datetime.datetime.fromisoformat(row[0]))
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
        e.add_field(name="👤 Colaborador",       value=f"**{name}**",                         inline=True)
        e.add_field(name="🕐 Horário de Entrada", value=now.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.set_thumbnail(url=str(itx.user.display_avatar.url))
        e.set_footer(text="ECCO HOSPITAL CENTER • Bate Ponto")
        await itx.response.send_message(embed=e, ephemeral=True)

        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="📥 Entrada Registrada", color=0x2ECC71, timestamp=now)
            le.add_field(name="Colaborador", value=f"{itx.user.mention}\n`{name}`",          inline=True)
            le.add_field(name="Horário",     value=now.strftime("%d/%m/%Y às %H:%M:%S"),     inline=True)
            le.set_thumbnail(url=str(itx.user.display_avatar.url))
            le.set_footer(text="ECCO HOSPITAL CENTER")
            await lch.send(embed=le)

        asyncio.create_task(refresh_rank())

    @discord.ui.button(
        label="🔴  Fechar Ponto",
        style=discord.ButtonStyle.danger,
        custom_id="ecco:close",
    )
    async def close_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        uid  = str(itx.user.id)
        name = itx.user.display_name
        now  = now_br()

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
        ws      = week_monday(open_dt).isoformat()

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT INTO sessions "
                "(user_id, user_name, open_time, close_time, dur_sec, week_start) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uid, name, row[0], now.isoformat(), dur_sec, ws),
            )
            await db.execute("DELETE FROM active WHERE user_id = ?", (uid,))
            await db.commit()

        e = discord.Embed(title="🔴 Ponto Fechado com Sucesso!", color=0xE74C3C)
        e.add_field(name="👤 Colaborador",        value=f"**{name}**",                         inline=False)
        e.add_field(name="🕐 Entrada",             value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.add_field(name="🕑 Saída",               value=now.strftime("%d/%m/%Y às %H:%M:%S"),     inline=True)
        e.add_field(name="⏱️ Duração da Sessão",   value=f"**{hms(dur_sec)}**",                    inline=False)
        e.set_thumbnail(url=str(itx.user.display_avatar.url))
        e.set_footer(text="ECCO HOSPITAL CENTER • Bate Ponto")
        await itx.response.send_message(embed=e, ephemeral=True)

        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="📤 Saída Registrada", color=0xE74C3C, timestamp=now)
            le.add_field(name="Colaborador", value=f"{itx.user.mention}\n`{name}`",               inline=True)
            le.add_field(name="Entrada",     value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"),      inline=True)
            le.add_field(name="Saída",       value=now.strftime("%d/%m/%Y às %H:%M:%S"),          inline=True)
            le.add_field(name="Duração",     value=f"**{hms(dur_sec)}**",                         inline=True)
            le.set_footer(text="ECCO HOSPITAL CENTER")
            await lch.send(embed=le)

        asyncio.create_task(refresh_rank())


# ──────────────────────────────────────────────────────────────
#  VIEW — PAINEL DE REMOÇÃO DE HORAS (botão + modal)
# ──────────────────────────────────────────────────────────────

class RemoveHoursFromMemberModal(discord.ui.Modal, title="Remover Horas de um Colaborador"):
    def __init__(self):
        super().__init__()

    membro = discord.ui.TextInput(
        label="Membro (ID ou menção)",
        placeholder="Digite o ID ou mencione o usuário",
        required=True,
        max_length=30
    )
    horas = discord.ui.TextInput(
        label="Horas a remover",
        placeholder="Ex: 1.5 (para 1h30)",
        required=True,
        max_length=10
    )

    async def on_submit(self, itx: discord.Interaction):
        # Verifica se o usuário tem um dos cargos autorizados
        has_role = any(role.id in AUTHORIZED_REMOVE_ROLE_IDS for role in itx.user.roles)
        if not has_role:
            return await itx.response.send_message(
                "❌ Você não tem permissão para remover horas.",
                ephemeral=True
            )

        # Extrai o ID do membro
        user_id = extract_user_id(self.membro.value)
        if not user_id:
            return await itx.response.send_message(
                "❌ ID ou menção inválida. Use o ID numérico ou mencione o usuário.",
                ephemeral=True
            )

        # Busca o membro no servidor
        member = itx.guild.get_member(user_id)
        if not member:
            return await itx.response.send_message(
                "❌ Membro não encontrado no servidor.",
                ephemeral=True
            )

        # Converte horas para segundos
        try:
            horas_remover = float(self.horas.value.replace(',', '.'))
            if horas_remover <= 0:
                raise ValueError
        except ValueError:
            return await itx.response.send_message(
                "❌ Valor inválido. Digite um número positivo (ex: 1.5).",
                ephemeral=True
            )

        segundos_remover = int(horas_remover * 3600)

        # Busca a última sessão fechada do membro
        uid = str(user_id)
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT id, open_time, close_time, dur_sec FROM sessions "
                "WHERE user_id = ? AND close_time IS NOT NULL "
                "ORDER BY close_time DESC LIMIT 1",
                (uid,)
            ) as c:
                row = await c.fetchone()

        if not row:
            return await itx.response.send_message(
                f"ℹ️ **{member.display_name}** não possui sessões fechadas.",
                ephemeral=True
            )

        session_id, open_time_str, close_time_str, dur_sec = row

        if dur_sec < segundos_remover:
            return await itx.response.send_message(
                f"❌ A última sessão tem apenas {hms(dur_sec)}, não é possível remover {hms(segundos_remover)}.",
                ephemeral=True
            )

        nova_duracao = dur_sec - segundos_remover
        open_dt = datetime.datetime.fromisoformat(open_time_str)
        nova_saida = open_dt + datetime.timedelta(seconds=nova_duracao)

        if nova_duracao <= 0:
            # Remove a sessão completamente
            async with aiosqlite.connect(DB) as db:
                await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                await db.commit()

            await itx.response.send_message(
                f"✅ Sessão de **{member.display_name}** foi **removida** completamente "
                f"(duração zerada após remover {horas_remover}h).",
                ephemeral=True
            )
            lch = bot.get_channel(LOGS_CHANNEL)
            if lch:
                le = discord.Embed(
                    title="🗑️ Sessão Removida (por remoção de horas)",
                    color=0xFF0000,
                    timestamp=now_br()
                )
                le.add_field(name="Colaborador", value=f"{member.mention} (`{member.display_name}`)", inline=True)
                le.add_field(name="Horas removidas", value=f"{horas_remover}h", inline=True)
                le.add_field(name="Removido por", value=itx.user.mention, inline=True)
                await lch.send(embed=le)
        else:
            # Atualiza a sessão
            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    "UPDATE sessions SET dur_sec = ?, close_time = ? WHERE id = ?",
                    (nova_duracao, nova_saida.isoformat(), session_id)
                )
                await db.commit()

            await itx.response.send_message(
                f"✅ Sessão de **{member.display_name}** ajustada:\n"
                f"Nova duração: **{hms(nova_duracao)}** (removido {horas_remover}h).",
                ephemeral=True
            )

            lch = bot.get_channel(LOGS_CHANNEL)
            if lch:
                le = discord.Embed(
                    title="⏱️ Horas Removidas da Sessão",
                    color=0xE67E22,
                    timestamp=now_br()
                )
                le.add_field(name="Colaborador", value=f"{member.mention} (`{member.display_name}`)", inline=True)
                le.add_field(name="Duração anterior", value=hms(dur_sec), inline=True)
                le.add_field(name="Nova duração", value=hms(nova_duracao), inline=True)
                le.add_field(name="Horas removidas", value=f"{horas_remover}h", inline=True)
                le.add_field(name="Nova saída", value=nova_saida.strftime("%d/%m/%Y %H:%M:%S"), inline=True)
                le.add_field(name="Removido por", value=itx.user.mention, inline=True)
                await lch.send(embed=le)

        # Atualiza ranking
        asyncio.create_task(refresh_rank(force=True))


class RemovePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⏱️ Remover Horas", style=discord.ButtonStyle.danger, custom_id="remove_hours_panel")
    async def remove_hours_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        # Abre o modal
        modal = RemoveHoursFromMemberModal()
        await itx.response.send_modal(modal)


# ──────────────────────────────────────────────────────────────
#  VIEW — PAINEL DE RECRUTAMENTO (botão + modal)
# ──────────────────────────────────────────────────────────────

class RecruitModal(discord.ui.Modal, title="📢 Novo Recrutamento"):
    mensagem = discord.ui.TextInput(
        label="Mensagem de recrutamento",
        placeholder="Digite o texto que deseja divulgar...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )

    async def on_submit(self, itx: discord.Interaction):
        # Monta a menção de todos os cargos
        role_mentions = " ".join(f"<@&{role_id}>" for role_id in RECRUIT_ROLE_IDS)

        # Envia a mensagem no mesmo canal onde o botão foi clicado
        canal = itx.channel
        if not canal:
            return await itx.response.send_message("❌ Não foi possível identificar o canal.", ephemeral=True)

        embed = discord.Embed(
            title="📢 Recrutamento",
            description=self.mensagem.value,
            color=0x00BFFF,
            timestamp=now_br()
        )
        embed.set_footer(text=f"Solicitado por {itx.user.display_name}", icon_url=itx.user.display_avatar.url)

        # Envia a mensagem com a menção
        await canal.send(content=role_mentions, embed=embed)

        await itx.response.send_message("✅ Mensagem de recrutamento enviada com sucesso!", ephemeral=True)

        # Log no canal de logs
        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            log_embed = discord.Embed(
                title="📢 Recrutamento realizado",
                color=0x00BFFF,
                timestamp=now_br()
            )
            log_embed.add_field(name="Solicitante", value=itx.user.mention, inline=True)
            log_embed.add_field(name="Canal", value=canal.mention, inline=True)
            log_embed.add_field(name="Mensagem", value=self.mensagem.value[:500] + ("..." if len(self.mensagem.value) > 500 else ""), inline=False)
            await lch.send(embed=log_embed)


class RecruitView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📢 Recrutamento", style=discord.ButtonStyle.primary, custom_id="recruit_button")
    async def recruit_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        modal = RecruitModal()
        await itx.response.send_modal(modal)


# ──────────────────────────────────────────────────────────────
#  VIEW — NOTIFICAÇÃO POR DM
# ──────────────────────────────────────────────────────────────
class DMNotifyView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=3600)
        self.user_id = user_id

    @discord.ui.button(label="✅ Ainda em serviço", style=discord.ButtonStyle.success)
    async def confirm_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        if itx.user.id != self.user_id:
            return await itx.response.send_message("❌ Esta mensagem não é para você.", ephemeral=True)
        await itx.response.send_message("👍 Confirmado! Continuamos contando suas horas.", ephemeral=True)

    @discord.ui.button(label="🔴 Fechar Ponto", style=discord.ButtonStyle.danger)
    async def close_from_dm_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        if itx.user.id != self.user_id:
            return await itx.response.send_message("❌ Esta mensagem não é para você.", ephemeral=True)

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
            await db.execute(
                "INSERT INTO sessions "
                "(user_id, user_name, open_time, close_time, dur_sec, week_start) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uid, name, row[0], now.isoformat(), dur_sec, ws),
            )
            await db.execute("DELETE FROM active WHERE user_id = ?", (uid,))
            await db.commit()

        e = discord.Embed(title="🔴 Ponto Fechado com Sucesso!", color=0xE74C3C)
        e.add_field(name="👤 Colaborador",        value=f"**{name}**",                         inline=False)
        e.add_field(name="🕐 Entrada",             value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"), inline=True)
        e.add_field(name="🕑 Saída",               value=now.strftime("%d/%m/%Y às %H:%M:%S"),     inline=True)
        e.add_field(name="⏱️ Duração da Sessão",   value=f"**{hms(dur_sec)}**",                    inline=False)
        e.set_thumbnail(url=str(itx.user.display_avatar.url))
        e.set_footer(text="ECCO HOSPITAL CENTER • Bate Ponto")
        await itx.response.send_message(embed=e)

        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(title="📤 Saída via DM", color=0xE74C3C, timestamp=now)
            le.add_field(name="Colaborador", value=f"{itx.user.mention}\n`{name}`",               inline=True)
            le.add_field(name="Entrada",     value=open_dt.strftime("%d/%m/%Y às %H:%M:%S"),      inline=True)
            le.add_field(name="Saída",       value=now.strftime("%d/%m/%Y às %H:%M:%S"),          inline=True)
            le.add_field(name="Duração",     value=f"**{hms(dur_sec)}**",                         inline=True)
            le.set_footer(text="ECCO HOSPITAL CENTER")
            await lch.send(embed=le)

        asyncio.create_task(refresh_rank())
        for child in self.children:
            child.disabled = True
        await itx.message.edit(view=self)


# ──────────────────────────────────────────────────────────────
#  VIEW + MODAL — AJUSTAR HORÁRIO (mantido)
# ──────────────────────────────────────────────────────────────
class AdjustModal(discord.ui.Modal, title="Ajustar Horário da Sessão"):
    def __init__(self, session_id: int, user: discord.Member):
        super().__init__()
        self.session_id = session_id
        self.user = user

    nova_entrada = discord.ui.TextInput(
        label="Nova Entrada (DD/MM/AAAA HH:MM)",
        placeholder="Deixe em branco para não alterar",
        required=False,
        max_length=20
    )
    nova_saida = discord.ui.TextInput(
        label="Nova Saída (DD/MM/AAAA HH:MM)",
        placeholder="Deixe em branco para não alterar",
        required=False,
        max_length=20
    )

    async def on_submit(self, itx: discord.Interaction):
        if itx.user.id not in AUTHORIZED_ADJUST_IDS:
            return await itx.response.send_message("❌ Você não tem permissão.", ephemeral=True)

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id, open_time, close_time, dur_sec FROM sessions WHERE id = ?",
                (self.session_id,)
            ) as c:
                row = await c.fetchone()

        if not row:
            return await itx.response.send_message("❌ Sessão não encontrada.", ephemeral=True)

        uid, old_open, old_close, old_dur = row
        if old_close is None:
            return await itx.response.send_message("❌ Não é possível ajustar uma sessão aberta.", ephemeral=True)

        new_open = None
        new_close = None

        if self.nova_entrada.value:
            try:
                new_open = parse_datetime_br(self.nova_entrada.value)
            except ValueError as e:
                return await itx.response.send_message(f"❌ Erro na nova entrada: {e}", ephemeral=True)

        if self.nova_saida.value:
            try:
                new_close = parse_datetime_br(self.nova_saida.value)
            except ValueError as e:
                return await itx.response.send_message(f"❌ Erro na nova saída: {e}", ephemeral=True)

        if not new_open and not new_close:
            return await itx.response.send_message("ℹ️ Nenhuma alteração fornecida.", ephemeral=True)

        final_open = new_open if new_open else datetime.datetime.fromisoformat(old_open)
        final_close = new_close if new_close else datetime.datetime.fromisoformat(old_close)
        final_open = localize(final_open)
        final_close = localize(final_close)

        if final_close <= final_open:
            return await itx.response.send_message("❌ A saída deve ser posterior à entrada.", ephemeral=True)

        new_dur = int((final_close - final_open).total_seconds())

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "UPDATE sessions SET open_time = ?, close_time = ?, dur_sec = ? WHERE id = ?",
                (final_open.isoformat(), final_close.isoformat(), new_dur, self.session_id)
            )
            await db.commit()

        lch = bot.get_channel(LOGS_CHANNEL)
        if lch:
            le = discord.Embed(
                title="🔄 Horário Ajustado por Admin",
                color=0x3498DB,
                timestamp=now_br()
            )
            le.add_field(name="Colaborador", value=f"{self.user.mention} (`{self.user.display_name}`)", inline=True)
            le.add_field(name="Antiga Entrada", value=old_open, inline=True)
            le.add_field(name="Nova Entrada", value=final_open.strftime("%d/%m/%Y %H:%M:%S"), inline=True)
            le.add_field(name="Antiga Saída", value=old_close, inline=True)
            le.add_field(name="Nova Saída", value=final_close.strftime("%d/%m/%Y %H:%M:%S"), inline=True)
            le.add_field(name="Nova Duração", value=hms(new_dur), inline=True)
            le.add_field(name="Ajustado por", value=itx.user.mention, inline=True)
            await lch.send(embed=le)

        await itx.response.send_message(
            f"✅ Sessão de **{self.user.display_name}** ajustada com sucesso!\n"
            f"Nova duração: **{hms(new_dur)}**",
            ephemeral=True
        )
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

        self.select = discord.ui.Select(placeholder="Selecione a sessão a ajustar", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, itx: discord.Interaction):
        if itx.user.id not in AUTHORIZED_ADJUST_IDS:
            return await itx.response.send_message("❌ Você não tem permissão.", ephemeral=True)

        selected = self.select.values[0]
        if selected == "none":
            return await itx.response.send_message("Nenhuma sessão disponível para ajuste.", ephemeral=True)

        sid = int(selected)
        modal = AdjustModal(sid, self.user)
        await itx.response.send_modal(modal)


# ──────────────────────────────────────────────────────────────
#  TASK — AUTO-REFRESH RANKING
# ──────────────────────────────────────────────────────────────
@tasks.loop(minutes=5)
async def auto_refresh():
    await refresh_rank(force=True)


# ──────────────────────────────────────────────────────────────
#  TASK — NOTIFICAÇÕES POR DM
# ──────────────────────────────────────────────────────────────
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
#  SLASH COMMANDS
# ──────────────────────────────────────────────────────────────

# /setup_ponto — Admin
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
        except (discord.NotFound, discord.Forbidden):
            pass

    msg = await ch.send(embed=panel_embed(), view=PunchView())
    await save_mid("panel", msg.id)
    await refresh_rank(force=True)
    await itx.followup.send(f"✅ Painel criado em {ch.mention}!", ephemeral=True)


# /setup_painel_remover — Admin  (NOVO)
@bot.tree.command(name="setup_painel_remover", description="[ADMIN] Cria o painel de remoção de horas no canal configurado")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_remove_panel(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    ch = bot.get_channel(REMOVE_PANEL_CHANNEL)
    if not ch:
        return await itx.followup.send("❌ Canal de remoção não encontrado!", ephemeral=True)

    # Remove mensagem antiga se existir
    old_mid = await load_mid("remove_panel")
    if old_mid:
        try:
            old_msg = await ch.fetch_message(old_mid)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
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

    view = RemovePanelView()
    msg = await ch.send(embed=embed, view=view)
    await save_mid("remove_panel", msg.id)
    await itx.followup.send(f"✅ Painel de remoção criado em {ch.mention}!", ephemeral=True)


# /setup_recrutamento — Admin (NOVO)
@bot.tree.command(name="setup_recrutamento", description="[ADMIN] Cria o painel de recrutamento no canal configurado")
@app_commands.default_permissions(administrator=True)
async def cmd_setup_recruit_panel(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    ch = bot.get_channel(RECRUIT_CHANNEL)
    if not ch:
        return await itx.followup.send("❌ Canal de recrutamento não encontrado!", ephemeral=True)

    # Remove mensagem antiga se existir
    old_mid = await load_mid("recruit_panel")
    if old_mid:
        try:
            old_msg = await ch.fetch_message(old_mid)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
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

    view = RecruitView()
    msg = await ch.send(embed=embed, view=view)
    await save_mid("recruit_panel", msg.id)
    await itx.followup.send(f"✅ Painel de recrutamento criado em {ch.mention}!", ephemeral=True)


# /meu_ponto — Todos
@bot.tree.command(name="meu_ponto", description="Consulte suas horas desta semana")
async def cmd_meu_ponto(itx: discord.Interaction):
    uid = str(itx.user.id)
    now = now_br()
    ws  = week_monday(now).isoformat()

    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT open_time, close_time, dur_sec FROM sessions "
            "WHERE user_id = ? AND week_start >= ? ORDER BY open_time DESC",
            (uid, ws),
        ) as c:
            sessions = await c.fetchall()
        async with db.execute("SELECT open_time FROM active WHERE user_id = ?", (uid,)) as c:
            active = await c.fetchone()

    total = sum(s[2] for s in sessions if s[2])
    desc  = ""

    if active:
        dt      = localize(datetime.datetime.fromisoformat(active[0]))
        elapsed = (now - dt).total_seconds()
        total  += elapsed
        desc    = f"🟢 **Em Serviço** desde `{dt.strftime('%H:%M:%S')}` (+{hms(elapsed)})\n\n"

    e = discord.Embed(
        title=f"📊 Meu Ponto — {itx.user.display_name}",
        description=desc,
        color=0x1565C0,
    )
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


# /rank_horas — Mod
@bot.tree.command(name="rank_horas", description="[MOD] Força atualização do ranking de horas")
@app_commands.default_permissions(manage_messages=True)
async def cmd_rank(itx: discord.Interaction):
    await itx.response.defer(ephemeral=True)
    await refresh_rank(force=True)
    await itx.followup.send("✅ Ranking atualizado no canal de ranking!", ephemeral=True)


# /fechar_ponto_admin — Admin
@bot.tree.command(
    name="fechar_ponto_admin",
    description="[ADMIN] Fecha o ponto de um colaborador forçadamente",
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(colaborador="Colaborador cujo ponto deve ser fechado")
async def cmd_fechar_admin(itx: discord.Interaction, colaborador: discord.Member):
    uid = str(colaborador.id)
    now = now_br()

    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT open_time FROM active WHERE user_id = ?", (uid,)) as c:
            row = await c.fetchone()

    if not row:
        return await itx.response.send_message(
            f"⚠️ **{colaborador.display_name}** não tem ponto aberto.", ephemeral=True
        )

    open_dt = localize(datetime.datetime.fromisoformat(row[0]))
    dur_sec = int((now - open_dt).total_seconds())
    ws      = week_monday(open_dt).isoformat()

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO sessions "
            "(user_id, user_name, open_time, close_time, dur_sec, week_start) VALUES (?,?,?,?,?,?)",
            (uid, colaborador.display_name, row[0], now.isoformat(), dur_sec, ws),
        )
        await db.execute("DELETE FROM active WHERE user_id = ?", (uid,))
        await db.commit()

    await itx.response.send_message(
        f"✅ Ponto de **{colaborador.display_name}** encerrado. Duração: `{hms(dur_sec)}`",
        ephemeral=True,
    )

    lch = bot.get_channel(LOGS_CHANNEL)
    if lch:
        le = discord.Embed(title="⚠️ Fechamento Forçado por Admin", color=0xFF8C00, timestamp=now)
        le.add_field(name="Colaborador",       value=f"{colaborador.mention} (`{colaborador.display_name}`)", inline=True)
        le.add_field(name="Admin Responsável", value=itx.user.mention,                                        inline=True)
        le.add_field(name="Duração",           value=f"**{hms(dur_sec)}**",                                   inline=True)
        le.set_footer(text="ECCO HOSPITAL CENTER")
        await lch.send(embed=le)

    asyncio.create_task(refresh_rank())


# /relatorio — Admin
@bot.tree.command(name="relatorio", description="[ADMIN] Relatório de horas de um colaborador")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    colaborador="Colaborador para gerar o relatório",
    semanas_atras="Quantas semanas atrás? (0 = semana atual)",
)
async def cmd_relatorio(
    itx: discord.Interaction,
    colaborador: discord.Member,
    semanas_atras: int = 0,
):
    uid  = str(colaborador.id)
    now  = now_br()
    t_ws = week_monday(now) - datetime.timedelta(weeks=semanas_atras)
    t_we = t_ws + datetime.timedelta(days=6)
    ws   = t_ws.isoformat()
    we   = (t_ws + datetime.timedelta(days=7)).isoformat()

    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT open_time, close_time, dur_sec FROM sessions "
            "WHERE user_id = ? AND week_start >= ? AND week_start < ? ORDER BY open_time DESC",
            (uid, ws, we),
        ) as c:
            sessions = await c.fetchall()

    total = sum(s[2] for s in sessions if s[2])

    e = discord.Embed(
        title=f"📄 Relatório — {colaborador.display_name}",
        description=f"📅 Semana: {t_ws.strftime('%d/%m')} — {t_we.strftime('%d/%m/%Y')}",
        color=0x9B59B6,
    )
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


# /pontos_abertos — Admin
@bot.tree.command(name="pontos_abertos", description="[ADMIN] Lista todos os colaboradores com ponto aberto")
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
        dt      = localize(datetime.datetime.fromisoformat(ot))
        elapsed = (now - dt).total_seconds()
        lines.append(f"🟢 **{uname}** — desde `{dt.strftime('%H:%M:%S')}` (+{hms(elapsed)})")

    e = discord.Embed(
        title="🟢 Colaboradores com Ponto Aberto",
        description="\n".join(lines),
        color=0x2ECC71,
    )
    e.set_footer(text=f"Total: {len(rows)} colaborador(es) • ECCO HOSPITAL CENTER")
    await itx.response.send_message(embed=e, ephemeral=True)


# /remover_horas — com suporte a cargo + IDs fixos (mantido)
@bot.tree.command(
    name="remover_horas",
    description="[AUTORIZADO] Remove horas de uma sessão específica de um colaborador (via select)"
)
@app_commands.describe(
    colaborador="Colaborador cuja sessão será ajustada"
)
async def cmd_remover_horas(itx: discord.Interaction, colaborador: discord.Member):
    # Permissão: cargo ou IDs fixos
    has_role = any(role.id in AUTHORIZED_REMOVE_ROLE_IDS for role in itx.user.roles)
    is_allowed = has_role or (itx.user.id in AUTHORIZED_REMOVE_IDS)

    if not is_allowed:
        return await itx.response.send_message(
            "❌ Você não tem permissão para usar este comando.",
            ephemeral=True
        )

    uid = str(colaborador.id)

    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT id, open_time, close_time, dur_sec FROM sessions "
            "WHERE user_id = ? AND close_time IS NOT NULL "
            "ORDER BY open_time DESC LIMIT 10",
            (uid,)
        ) as c:
            sessions = await c.fetchall()

    if not sessions:
        return await itx.response.send_message(
            f"ℹ️ **{colaborador.display_name}** não possui sessões fechadas.",
            ephemeral=True
        )

    view = RemoveSessionView(colaborador, sessions)
    embed = discord.Embed(
        title="🗑️ Remover Horas de uma Sessão",
        description=f"Selecione a sessão de **{colaborador.display_name}** e depois clique no botão.",
        color=0xE67E22
    )
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


# /ajustar_horario — Apenas IDs autorizados
@bot.tree.command(
    name="ajustar_horario",
    description="[AUTORIZADO] Ajusta a entrada e/ou saída de uma sessão já fechada"
)
@app_commands.describe(
    colaborador="Colaborador cuja sessão será ajustada"
)
async def cmd_ajustar_horario(itx: discord.Interaction, colaborador: discord.Member):
    if itx.user.id not in AUTHORIZED_ADJUST_IDS:
        return await itx.response.send_message(
            "❌ Você não tem permissão para usar este comando.",
            ephemeral=True
        )

    uid = str(colaborador.id)

    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT id, open_time, close_time, dur_sec FROM sessions "
            "WHERE user_id = ? AND close_time IS NOT NULL "
            "ORDER BY open_time DESC LIMIT 10",
            (uid,)
        ) as c:
            sessions = await c.fetchall()

    if not sessions:
        return await itx.response.send_message(
            f"ℹ️ **{colaborador.display_name}** não possui sessões fechadas para ajustar.",
            ephemeral=True
        )

    view = AdjustSessionView(colaborador, sessions)
    embed = discord.Embed(
        title="🔄 Ajustar Horário da Sessão",
        description=f"Selecione a sessão de **{colaborador.display_name}** que deseja ajustar.",
        color=0x3498DB
    )
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


# ──────────────────────────────────────────────────────────────
#  COMANDO /ia — Perguntar à IA (GEMINI com google.genai)
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="ia", description="Faça uma pergunta para a IA do Hospital ECCO")
@app_commands.describe(pergunta="Sua pergunta para a IA")
async def cmd_ia(itx: discord.Interaction, pergunta: str):
    if not GEMINI_API_KEY or GEMINI_CLIENT is None:
        return await itx.response.send_message(
            "❌ A IA não está configurada. Contate um administrador.",
            ephemeral=True
        )

    if not GEMINI_MODELS:
        return await itx.response.send_message(
            "❌ Nenhum modelo Gemini disponível. Verifique sua chave API.",
            ephemeral=True
        )

    await itx.response.defer(ephemeral=False)

    try:
        contexto = (
            "Você é um assistente virtual do Hospital ECCO em um servidor FiveM. "
            "Responda de forma educada, objetiva e dentro do contexto hospitalar e de RPG. "
            "Máximo de 500 caracteres."
        )

        resposta_texto = None
        ultimo_erro = None
        for modelo in GEMINI_MODELS:
            try:
                resposta = GEMINI_CLIENT.models.generate_content(
                    model=modelo,
                    contents=f"{contexto}\n\nPergunta: {pergunta}"
                )
                resposta_texto = resposta.text.strip()
                break
            except Exception as e:
                ultimo_erro = e
                continue

        if resposta_texto is None:
            raise Exception(f"Nenhum modelo disponível. Último erro: {ultimo_erro}")

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
        await itx.followup.send(f"❌ Erro ao processar sua pergunta: {str(e)[:200]}")


# ──────────────────────────────────────────────────────────────
#  VIEW — REMOVER HORAS (com select, usado pelo comando /remover_horas)
# ──────────────────────────────────────────────────────────────

class RemoveHoursModalSelect(discord.ui.Modal, title="Remover Horas da Sessão"):
    def __init__(self, session_id: int, user: discord.Member):
        super().__init__()
        self.session_id = session_id
        self.user = user

    horas = discord.ui.TextInput(
        label="Quantas horas remover? (ex: 1.5 ou 2)",
        placeholder="Digite um número decimal (ex: 1.5 para 1h30)",
        required=True,
        max_length=10
    )

    async def on_submit(self, itx: discord.Interaction):
        has_role = any(role.id in AUTHORIZED_REMOVE_ROLE_IDS for role in itx.user.roles)
        is_allowed = has_role or (itx.user.id in AUTHORIZED_REMOVE_IDS)
        if not is_allowed:
            return await itx.response.send_message("❌ Você não tem permissão.", ephemeral=True)

        try:
            horas_remover = float(self.horas.value.replace(',', '.'))
            if horas_remover <= 0:
                raise ValueError
        except ValueError:
            return await itx.response.send_message(
                "❌ Valor inválido. Digite um número positivo (ex: 1.5).",
                ephemeral=True
            )

        segundos_remover = int(horas_remover * 3600)

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id, open_time, close_time, dur_sec FROM sessions WHERE id = ?",
                (self.session_id,)
            ) as c:
                row = await c.fetchone()

        if not row:
            return await itx.response.send_message("❌ Sessão não encontrada.", ephemeral=True)

        uid, open_time_str, close_time_str, dur_sec = row
        if close_time_str is None:
            return await itx.response.send_message("❌ Não é possível remover horas de uma sessão aberta.", ephemeral=True)

        if dur_sec < segundos_remover:
            return await itx.response.send_message(
                f"❌ A sessão tem apenas {hms(dur_sec)}, não é possível remover {hms(segundos_remover)}.",
                ephemeral=True
            )

        nova_duracao = dur_sec - segundos_remover
        open_dt = datetime.datetime.fromisoformat(open_time_str)
        nova_saida = open_dt + datetime.timedelta(seconds=nova_duracao)

        if nova_duracao <= 0:
            async with aiosqlite.connect(DB) as db:
                await db.execute("DELETE FROM sessions WHERE id = ?", (self.session_id,))
                await db.commit()

            await itx.response.send_message(
                f"✅ Sessão de **{self.user.display_name}** foi **removida** completamente "
                f"(duração zerada após remover {horas_remover}h).",
                ephemeral=True
            )
            lch = bot.get_channel(LOGS_CHANNEL)
            if lch:
                le = discord.Embed(
                    title="🗑️ Sessão Removida (por remoção de horas)",
                    color=0xFF0000,
                    timestamp=now_br()
                )
                le.add_field(name="Colaborador", value=f"{self.user.mention} (`{self.user.display_name}`)", inline=True)
                le.add_field(name="Horas removidas", value=f"{horas_remover}h", inline=True)
                le.add_field(name="Removido por", value=itx.user.mention, inline=True)
                await lch.send(embed=le)
        else:
            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    "UPDATE sessions SET dur_sec = ?, close_time = ? WHERE id = ?",
                    (nova_duracao, nova_saida.isoformat(), self.session_id)
                )
                await db.commit()

            await itx.response.send_message(
                f"✅ Sessão de **{self.user.display_name}** ajustada:\n"
                f"Nova duração: **{hms(nova_duracao)}** (removido {horas_remover}h).",
                ephemeral=True
            )

            lch = bot.get_channel(LOGS_CHANNEL)
            if lch:
                le = discord.Embed(
                    title="⏱️ Horas Removidas da Sessão",
                    color=0xE67E22,
                    timestamp=now_br()
                )
                le.add_field(name="Colaborador", value=f"{self.user.mention} (`{self.user.display_name}`)", inline=True)
                le.add_field(name="Duração anterior", value=hms(dur_sec), inline=True)
                le.add_field(name="Nova duração", value=hms(nova_duracao), inline=True)
                le.add_field(name="Horas removidas", value=f"{horas_remover}h", inline=True)
                le.add_field(name="Nova saída", value=nova_saida.strftime("%d/%m/%Y %H:%M:%S"), inline=True)
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
            options.append(discord.SelectOption(label="Nenhuma sessão disponível", value="none"))

        self.select = discord.ui.Select(placeholder="Selecione a sessão", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, itx: discord.Interaction):
        if self.select.values[0] == "none":
            return await itx.response.send_message("Nenhuma sessão selecionável.", ephemeral=True)
        self.selected_session_id = int(self.select.values[0])
        self.remove_button.disabled = False
        await itx.response.edit_message(view=self)

    @discord.ui.button(label="🗑️ Remover Horas", style=discord.ButtonStyle.danger, disabled=True)
    async def remove_button(self, itx: discord.Interaction, _: discord.ui.Button):
        if self.selected_session_id is None:
            return await itx.response.send_message("Selecione uma sessão primeiro.", ephemeral=True)

        modal = RemoveHoursModalSelect(self.selected_session_id, self.user)
        await itx.response.send_modal(modal)


# ──────────────────────────────────────────────────────────────
#  EVENTO OPCIONAL — RESPOSTA AUTOMÁTICA EM CANAL ESPECÍFICO
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_message(msg: discord.Message):
    # Ignora mensagens do próprio bot
    if msg.author.bot:
        return

    # Se o canal for o definido e não for um comando
    if IA_CHANNEL_ID and msg.channel.id == IA_CHANNEL_ID and not msg.content.startswith("!"):
        if not GEMINI_API_KEY or GEMINI_CLIENT is None or not GEMINI_MODELS:
            return

        # Evita responder a comandos slash
        if msg.content.startswith("/"):
            return

        async with msg.channel.typing():
            try:
                contexto = (
                    "Você é um assistente do Hospital ECCO em um servidor FiveM. "
                    "Responda de forma breve e útil. Máximo de 300 caracteres."
                )
                resposta_texto = None
                for modelo in GEMINI_MODELS:
                    try:
                        resposta = GEMINI_CLIENT.models.generate_content(
                            model=modelo,
                            contents=f"{contexto}\n\nUsuário: {msg.content}"
                        )
                        resposta_texto = resposta.text.strip()
                        break
                    except Exception:
                        continue
                if resposta_texto:
                    await msg.reply(resposta_texto[:1900], mention_author=False)
            except Exception:
                pass

    # Processar comandos
    await bot.process_commands(msg)


# ──────────────────────────────────────────────────────────────
#  ON READY
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()

    # Views persistentes
    bot.add_view(PunchView())
    bot.add_view(RemovePanelView())
    bot.add_view(RecruitView())

    # Painel principal
    ch_panel = bot.get_channel(PANEL_CHANNEL)
    if ch_panel:
        mid_panel = await load_mid("panel")
        needs_panel = True
        if mid_panel:
            try:
                await ch_panel.fetch_message(mid_panel)
                needs_panel = False
            except (discord.NotFound, discord.Forbidden):
                pass
        if needs_panel:
            msg = await ch_panel.send(embed=panel_embed(), view=PunchView())
            await save_mid("panel", msg.id)
            print(f"📋 Painel criado no canal {PANEL_CHANNEL}")
    else:
        print(f"⚠️  Canal do painel ({PANEL_CHANNEL}) não encontrado.")

    # Painel de remoção
    ch_remove = bot.get_channel(REMOVE_PANEL_CHANNEL)
    if ch_remove:
        mid_remove = await load_mid("remove_panel")
        needs_remove = True
        if mid_remove:
            try:
                await ch_remove.fetch_message(mid_remove)
                needs_remove = False
            except (discord.NotFound, discord.Forbidden):
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
        print(f"⚠️  Canal de remoção ({REMOVE_PANEL_CHANNEL}) não encontrado.")

    # Painel de recrutamento
    ch_recruit = bot.get_channel(RECRUIT_CHANNEL)
    if ch_recruit:
        mid_recruit = await load_mid("recruit_panel")
        needs_recruit = True
        if mid_recruit:
            try:
                await ch_recruit.fetch_message(mid_recruit)
                needs_recruit = False
            except (discord.NotFound, discord.Forbidden):
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
        print(f"⚠️  Canal de recrutamento ({RECRUIT_CHANNEL}) não encontrado.")

    # Ranking
    ch_rank = bot.get_channel(RANK_CHANNEL)
    if not ch_rank:
        print(f"⚠️  Canal de ranking ({RANK_CHANNEL}) não encontrado.")
    else:
        await refresh_rank(force=True)
        auto_refresh.start()

    notify_active_users.start()

    try:
        synced = await bot.tree.sync()
        print(
            f"✅  {bot.user} (ID: {bot.user.id}) online!\n"
            f"    {len(synced)} slash commands sincronizados\n"
            f"    {len(bot.guilds)} servidor(es)"
        )
    except Exception as exc:
        print(f"❌  Erro ao sincronizar slash commands: {exc}")


# ──────────────────────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌  Defina a variável de ambiente DISCORD_TOKEN antes de iniciar o bot.")
    bot.run(TOKEN)
