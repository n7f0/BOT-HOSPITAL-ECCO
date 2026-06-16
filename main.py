"""
╔══════════════════════════════════════════════════════════════╗
║          ECCO HOSPITAL CENTER — BOT DE BATE PONTO           ║
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
#  CONFIGURAÇÃO
# ──────────────────────────────────────────────────────────────
TOKEN         = os.environ.get("DISCORD_TOKEN")
PANEL_CHANNEL = int(os.environ.get("PANEL_CHANNEL_ID", "1515846128493658142"))
RANK_CHANNEL  = int(os.environ.get("RANK_CHANNEL_ID", "1515852084480839850"))
LOGS_CHANNEL  = int(os.environ.get("LOGS_CHANNEL_ID",  "1515846898156834956"))
DB            = os.environ.get("DB_PATH", "ponto.db")
BR_TZ         = pytz.timezone("America/Sao_Paulo")

# IDs autorizados para remover horas (permanecem)
AUTHORIZED_REMOVE_IDS = [1480675269449617525, 1508478383825354892]

# IDs autorizados para ajustar horário
AUTHORIZED_ADJUST_IDS = [
    1480675269449617524,
    1480675269449617521,
    1480675269449617523,
    1480675269449617522,
]

# Nome do cargo que também pode usar /remover_horas (adicione o cargo exato)
ALLOWED_ROLE_NAME = "Gestor"   # ← ALTERE AQUI se necessário

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
    """Converte string DD/MM/AAAA HH:MM para datetime com timezone BR."""
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
#  VIEW — BOTÕES DE PONTO (painel)
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
            le.set_thumbnail(url=str(itx.user.display_avatar.url))
            le.set_footer(text="ECCO HOSPITAL CENTER")
            await lch.send(embed=le)

        asyncio.create_task(refresh_rank())


# ──────────────────────────────────────────────────────────────
#  VIEW — NOTIFICAÇÃO POR DM
# ──────────────────────────────────────────────────────────────
class DMNotifyView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=3600)  # 1 hora de validade
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
#  VIEW + MODAL — REMOVER HORAS (NOVA VERSÃO)
# ──────────────────────────────────────────────────────────────

class RemoveHoursModal(discord.ui.Modal, title="Remover Horas da Sessão"):
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
        # Verifica permissão novamente
        is_allowed = (
            itx.user.id in AUTHORIZED_REMOVE_IDS or
            any(role.name == ALLOWED_ROLE_NAME for role in itx.user.roles)
        )
        if not is_allowed:
            return await itx.response.send_message("❌ Você não tem permissão.", ephemeral=True)

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

        # Buscar dados atuais da sessão
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
            # Remove a sessão completamente
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
            # Atualiza a sessão com nova duração e nova saída
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


class RemoveHoursView(discord.ui.View):
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
        # Habilita o botão de remover
        self.remove_button.disabled = False
        await itx.response.edit_message(view=self)

    @discord.ui.button(label="🗑️ Remover Horas", style=discord.ButtonStyle.danger, disabled=True)
    async def remove_button(self, itx: discord.Interaction, _: discord.ui.Button):
        if self.selected_session_id is None:
            return await itx.response.send_message("Selecione uma sessão primeiro.", ephemeral=True)

        # Abrir modal
        modal = RemoveHoursModal(self.selected_session_id, self.user)
        await itx.response.send_modal(modal)


# ──────────────────────────────────────────────────────────────
#  VIEW + MODAL — AJUSTAR HORÁRIO
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


# ──────────────────────────────────────────────────────────────
#  /remover_horas — VERSÃO ATUALIZADA COM MODAL E CARGO
# ──────────────────────────────────────────────────────────────
@bot.tree.command(
    name="remover_horas",
    description="[AUTORIZADO] Remove horas de uma sessão específica de um colaborador"
)
@app_commands.describe(
    colaborador="Colaborador cuja sessão será ajustada"
)
async def cmd_remover_horas(itx: discord.Interaction, colaborador: discord.Member):
    # Verifica permissão por ID ou cargo
    is_allowed = (
        itx.user.id in AUTHORIZED_REMOVE_IDS or
        any(role.name == ALLOWED_ROLE_NAME for role in itx.user.roles)
    )
    if not is_allowed:
        return await itx.response.send_message(
            "❌ Você não tem permissão para usar este comando.",
            ephemeral=True
        )

    uid = str(colaborador.id)

    # Buscar últimas 10 sessões fechadas (qualquer semana)
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

    view = RemoveHoursView(colaborador, sessions)
    embed = discord.Embed(
        title="🗑️ Remover Horas de uma Sessão",
        description=f"Selecione a sessão de **{colaborador.display_name}** e depois clique no botão para abrir o modal.",
        color=0xE67E22
    )
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


# ──────────────────────────────────────────────────────────────
#  /ajustar_horario — Apenas IDs autorizados
# ──────────────────────────────────────────────────────────────
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
#  ON READY
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()

    bot.add_view(PunchView())

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
        print(f"⚠️  Canal do painel ({PANEL_CHANNEL}) não encontrado. "
              f"Verifique se o bot tem acesso e use /setup_ponto.")

    ch_rank = bot.get_channel(RANK_CHANNEL)
    if not ch_rank:
        print(f"⚠️  Canal de ranking ({RANK_CHANNEL}) não encontrado. "
              f"O ranking não será exibido até que o canal exista e o bot tenha acesso.")
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