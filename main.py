"""
╔══════════════════════════════════════════════════════════════╗
║          ECCO HOSPITAL CENTER — BOT DE BATE PONTO           ║
║         VERSÃO COM ARMAZENAMENTO EM MEMÓRIA (RAM)           ║
║            OS DADOS SÃO PERDIDOS AO REINICIAR               ║
╚══════════════════════════════════════════════════════════════╝
"""

import asyncio
import datetime
import os
import re
import logging
import time
from typing import Dict, List, Optional

import discord
import pytz
from discord import app_commands
from discord.ext import commands, tasks

# ──────────────────────────────────────────────────────────────
#  CONFIGURAÇÃO DE LOGGING
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%d/%m/%Y %H:%M:%S'
)
logger = logging.getLogger('EccoBot')

# ──────────────────────────────────────────────────────────────
#  CONFIGURAÇÃO DO BOT E CANAIS
# ──────────────────────────────────────────────────────────────
TOKEN         = os.environ.get("DISCORD_TOKEN")
PANEL_CHANNEL = int(os.environ.get("PANEL_CHANNEL_ID", "1515846128493658142"))
RANK_CHANNEL  = int(os.environ.get("RANK_CHANNEL_ID", "1515852084480839850"))
LOGS_CHANNEL  = int(os.environ.get("LOGS_CHANNEL_ID",  "1515846898156834956"))
REMOVE_PANEL_CHANNEL = int(os.environ.get("REMOVE_PANEL_CHANNEL_ID", "1515846758456885400"))
BR_TZ         = pytz.timezone("America/Sao_Paulo")

# Cargos autorizados para remover horas
AUTHORIZED_REMOVE_ROLE_IDS = [
    1480675269449617524, 1480675269449617523,
    1480675269449617522, 1480675269449617521,
    1480675269449617525
]

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_rank_lock = asyncio.Lock()
_last_update: float = 0.0

# ──────────────────────────────────────────────────────────────
#  ARMAZENAMENTO EM MEMÓRIA
# ──────────────────────────────────────────────────────────────
# active_sessions: user_id -> {"name": str, "open_time": datetime}
active_sessions: Dict[str, Dict] = {}

# closed_sessions: lista de dicionários com os campos:
#   user_id, user_name, open_time, close_time, dur_sec, week_start
closed_sessions: List[Dict] = []

# msg_store: key -> message_id (para os painéis fixos)
msg_store: Dict[str, int] = {}

# Contador para IDs das sessões (apenas para referência interna, não usado)
_session_counter = 0

# ──────────────────────────────────────────────────────────────
#  FUNÇÕES AUXILIARES
# ──────────────────────────────────────────────────────────────
def now_br() -> datetime.datetime:
    return datetime.datetime.now(tz=BR_TZ)

def week_monday(dt: datetime.datetime = None) -> datetime.datetime:
    if dt is None:
        dt = now_br()
    monday = dt - datetime.timedelta(days=dt.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)

def hms(sec: float) -> str:
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}h {m:02d}m {s:02d}s"

def extract_user_id(text: str) -> int:
    match = re.search(r'<@!?(\d+)>', text)
    if match:
        return int(match.group(1))
    try:
        return int(text.strip())
    except ValueError:
        return None

async def load_mid(key: str) -> Optional[int]:
    return msg_store.get(key)

async def save_mid(key: str, msg_id: int):
    msg_store[key] = msg_id

async def get_rank() -> list:
    """
    Retorna lista de (user_id, [user_name, total_seconds]) ordenada por total decrescente.
    """
    now = now_br()
    ws = week_monday().isoformat()

    # Soma das sessões fechadas
    totals = {}
    for sess in closed_sessions:
        if sess["week_start"] >= ws:
            uid = sess["user_id"]
            if uid not in totals:
                totals[uid] = [sess["user_name"], 0]
            totals[uid][1] += sess["dur_sec"]

    # Soma das sessões ativas
    for uid, data in active_sessions.items():
        dt = data["open_time"]  # já é datetime com timezone
        elapsed = (now - dt).total_seconds()
        if uid in totals:
            totals[uid][1] += elapsed
        else:
            totals[uid] = [data["name"], elapsed]

    return sorted(totals.items(), key=lambda x: x[1][1], reverse=True)

async def get_user_sessions(user_id: str, week_start: str) -> List[Dict]:
    """Retorna sessões fechadas de um usuário na semana especificada."""
    return [s for s in closed_sessions if s["user_id"] == user_id and s["week_start"] >= week_start]

# ──────────────────────────────────────────────────────────────
#  PAINÉIS EMBEDS
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

def remove_panel_embed() -> discord.Embed:
    e = discord.Embed(
        title="⏱️ Administração — Gerenciamento de Horas",
        description=(
            "**Painel Administrativo**\n"
            "Selecione um membro no menu abaixo para ajustar ou remover horas contabilizadas indevidamente.\n\n"
            "🛠️ **Comandos Disponíveis:**\n"
            "`/meu_ponto` - Consulte as suas horas trabalhadas nesta semana.\n"
            "`/sync` - [Admin] Sincroniza os comandos de barra do bot."
        ),
        color=0xE74C3C
    )
    e.set_footer(text="Apenas membros autorizados podem executar alterações.")
    return e

async def rank_embed() -> discord.Embed:
    rank = await get_rank()
    now = now_br()
    ws = week_monday(now)
    we = ws + datetime.timedelta(days=6)

    active_n = len(active_sessions)

    e = discord.Embed(
        title="🏆 RANKING SEMANAL DE HORAS",
        description=f"**ECCO HOSPITAL CENTER**\n📅 {ws.strftime('%d/%m')} — {we.strftime('%d/%m/%Y')}",
        color=0xFFD700
    )
    MEDALS = ["🥇", "🥈", "🥉"]

    if not rank:
        e.add_field(name="Sem Registros", value="Nenhuma hora registrada esta semana.", inline=False)
    else:
        page, part = "", 0
        for i, (uid, (uname, secs)) in enumerate(rank):
            prefix = MEDALS[i] if i < 3 else f"`#{i+1:>3}`"
            line = f"{prefix} **{uname}** — `{hms(secs)}`\n"
            if len(page) + len(line) > 950:
                e.add_field(
                    name="👥 Colaboradores" if part == 0 else f"👥 Colaboradores (pt.{part + 1})",
                    value=page,
                    inline=False
                )
                page, part = line, part + 1
            else:
                page += line
        if page:
            e.add_field(
                name="👥 Colaboradores" if part == 0 else f"👥 Colaboradores (pt.{part + 1})",
                value=page,
                inline=False
            )

    e.add_field(
        name="🟢 Em Serviço Agora",
        value=f"**{active_n}** colaborador(es) com ponto aberto",
        inline=False
    )
    e.set_footer(text=f"Atualizado em {now.strftime('%d/%m/%Y às %H:%M:%S')} • ECCO HOSPITAL CENTER")
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
            return

        emb = await rank_embed()
        mid = await load_mid("rank")
        if mid:
            try:
                msg = await ch.fetch_message(mid)
                await msg.edit(embed=emb)
                return
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                logger.error(f"Erro ao editar rank: {e}")
                return

        try:
            msg = await ch.send(embed=emb)
            await save_mid("rank", msg.id)
        except Exception as e:
            logger.error(f"Erro ao enviar painel de rank: {e}")

# ──────────────────────────────────────────────────────────────
#  VIEWS INTERATIVAS
# ──────────────────────────────────────────────────────────────
class PunchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅  Abrir Ponto", style=discord.ButtonStyle.success, custom_id="ecco:open")
    async def open_btn(self, itx: discord.Interaction, _: discord.ui.Button):
        uid = str(itx.user.id)
        name = itx.user.display_name
        now = now_br()

        if uid in active_sessions:
            dt = active_sessions[uid]["open_time"]
            e = discord.Embed(
                title="⚠️ Ponto Já Aberto!",
                description=(
                    f"Você já tem um ponto aberto desde **{dt.strftime('%d/%m/%Y às %H:%M:%S')}**.\n"
                    f"Tempo decorrido: **{hms((now - dt).total_seconds())}**\n\n"
                    "Para encerrar, clique em 🔴 **Fechar Ponto**."
                ),
                color=0xFFA500
            )
            return await itx.response.send_message(embed=e, ephemeral=True)

        active_sessions[uid] = {"name": name, "open_time": now}

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
        uid = str(itx.user.id)
        name = itx.user.display_name
        now = now_br()

        if uid not in active_sessions:
            return await itx.response.send_message(
                embed=discord.Embed(title="⚠️ Sem Ponto Aberto!", description="Você não tem ponto aberto.", color=0xFFA500),
                ephemeral=True
            )

        open_dt = active_sessions[uid]["open_time"]
        dur_sec = int((now - open_dt).total_seconds())
        ws = week_monday(open_dt).isoformat()

        # Registrar sessão fechada
        closed_sessions.append({
            "user_id": uid,
            "user_name": name,
            "open_time": open_dt.isoformat(),
            "close_time": now.isoformat(),
            "dur_sec": dur_sec,
            "week_start": ws
        })

        # Remover da lista de ativos
        del active_sessions[uid]

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

# ─── SISTEMA DE REMOÇÃO DE HORAS ───
class RemoveHoursAmountModal(discord.ui.Modal):
    horas = discord.ui.TextInput(
        label="Horas a remover (Ex: 1.5)",
        placeholder="Digite um número...",
        required=True
    )

    def __init__(self, target_user: discord.Member):
        title_name = target_user.display_name[:20]
        super().__init__(title=f"Remover hrs de {title_name}")
        self.target_user = target_user

    async def on_submit(self, itx: discord.Interaction):
        if not any(r.id in AUTHORIZED_REMOVE_ROLE_IDS for r in itx.user.roles):
            return await itx.response.send_message("❌ Sem permissão.", ephemeral=True)

        try:
            hrs = float(self.horas.value.replace(',', '.'))
        except ValueError:
            return await itx.response.send_message("❌ Valor numérico inválido.", ephemeral=True)

        uid = str(self.target_user.id)

        # Encontrar a última sessão fechada deste usuário (mais recente)
        user_sessions = [s for s in closed_sessions if s["user_id"] == uid and s["close_time"] is not None]
        if not user_sessions:
            return await itx.response.send_message(
                f"ℹ️ Nenhuma sessão fechada encontrada para {self.target_user.mention}.",
                ephemeral=True
            )

        # Ordenar por close_time decrescente
        user_sessions.sort(key=lambda s: s["close_time"], reverse=True)
        last_sess = user_sessions[0]

        dur = last_sess["dur_sec"]
        rem_sec = int(hrs * 3600)

        if dur < rem_sec:
            return await itx.response.send_message(
                f"❌ A última sessão é muito curta ({hms(dur)}).",
                ephemeral=True
            )

        nova_dur = dur - rem_sec

        if nova_dur <= 0:
            # Remover completamente a sessão da lista
            closed_sessions.remove(last_sess)
            msg = "Sessão totalmente removida."
        else:
            # Atualizar a duração e o close_time
            nova_saida = datetime.datetime.fromisoformat(last_sess["open_time"]) + datetime.timedelta(seconds=nova_dur)
            last_sess["dur_sec"] = nova_dur
            last_sess["close_time"] = nova_saida.isoformat()
            msg = f"Sessão ajustada para {hms(nova_dur)}."

        await itx.response.send_message(f"✅ Sucesso para {self.target_user.mention}: {msg}", ephemeral=True)
        asyncio.create_task(refresh_rank(force=True))

class RemoveHoursUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(
            placeholder="👥 Selecione o membro para remover horas...",
            min_values=1,
            max_values=1,
            custom_id="select_user_remove_hours"
        )

    async def callback(self, itx: discord.Interaction):
        if not any(r.id in AUTHORIZED_REMOVE_ROLE_IDS for r in itx.user.roles):
            return await itx.response.send_message("❌ Você não tem permissão para usar este painel.", ephemeral=True)
        selected_user = self.values[0]
        await itx.response.send_modal(RemoveHoursAmountModal(selected_user))

class RemovePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RemoveHoursUserSelect())

# ─── NOTIFICAÇÃO DE USUÁRIOS ATIVOS ───
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
            return
        uid = str(itx.user.id)
        name = itx.user.display_name
        now = now_br()

        if uid not in active_sessions:
            return await itx.response.send_message("⚠️ Sem ponto aberto.", ephemeral=True)

        open_dt = active_sessions[uid]["open_time"]
        dur = int((now - open_dt).total_seconds())
        ws = week_monday(open_dt).isoformat()

        closed_sessions.append({
            "user_id": uid,
            "user_name": name,
            "open_time": open_dt.isoformat(),
            "close_time": now.isoformat(),
            "dur_sec": dur,
            "week_start": ws
        })
        del active_sessions[uid]

        await itx.response.send_message(f"🔴 Ponto Fechado! Duração: **{hms(dur)}**")
        asyncio.create_task(refresh_rank())

# ──────────────────────────────────────────────────────────────
#  COMANDOS DE BARRA
# ──────────────────────────────────────────────────────────────
@bot.tree.command(name="meu_ponto", description="Consulte suas horas trabalhadas nesta semana")
async def cmd_meu_ponto(itx: discord.Interaction):
    uid = str(itx.user.id)
    ws = week_monday(now_br()).isoformat()

    # Sessões fechadas
    user_sessions = [s for s in closed_sessions if s["user_id"] == uid and s["week_start"] >= ws]
    total = sum(s["dur_sec"] for s in user_sessions)

    # Sessão ativa
    desc = ""
    if uid in active_sessions:
        dt = active_sessions[uid]["open_time"]
        total += (now_br() - dt).total_seconds()
        desc = f"🟢 **Em Serviço** desde `{dt.strftime('%H:%M:%S')}`\n\n"

    e = discord.Embed(title=f"📊 Meu Ponto", description=desc, color=0x1565C0)
    e.add_field(name="⏱️ Total", value=f"**{hms(total)}**")
    await itx.response.send_message(embed=e, ephemeral=True)

@bot.tree.command(name="sync", description="[ADMIN] Sincroniza os comandos de barra")
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
    for uid, data in active_sessions.items():
        user = bot.get_user(int(uid))
        if not user:
            continue
        try:
            embed = discord.Embed(
                title="⏰ Verificação de Ponto",
                description=f"Olá {data['name']}, você ainda está em serviço?",
                color=0x3498DB
            )
            await user.send(embed=embed, view=DMNotifyView(int(uid)))
        except discord.Forbidden:
            pass
        except Exception as e:
            logger.error(f"Erro ao notificar usuário {uid}: {e}")

# ──────────────────────────────────────────────────────────────
#  ON READY
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    # Registrar views persistentes
    bot.add_view(PunchView())
    bot.add_view(RemovePanelView())

    # Criar/atualizar painéis
    for ch_id, key, view_cls, emb_func in [
        (PANEL_CHANNEL, "panel", PunchView, panel_embed),
        (REMOVE_PANEL_CHANNEL, "remove_panel", RemovePanelView, remove_panel_embed)
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

    try:
        synced = await bot.tree.sync()
        logger.info(f"Comandos sincronizados: {len(synced)}")
    except Exception as exc:
        logger.error(f"Erro ao sincronizar comandos: {exc}")

    logger.info(f"✅ {bot.user} online! (Armazenamento em memória)")

# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN ausente.")
    bot.run(TOKEN)