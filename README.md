# 🏥 ECCO HOSPITAL CENTER — Bot de Bate Ponto

Bot de Discord para controle de ponto eletrônico com ranking semanal de horas.

---

## 📦 Funcionalidades

- ✅ **Abrir Ponto** — registra a entrada com horário exato
- 🔴 **Fechar Ponto** — encerra o expediente e salva a duração
- 🏆 **Ranking semanal** — classifica todos os colaboradores por horas trabalhadas na semana
- 📥📤 **Logs automáticos** — envia embed de entrada/saída no canal de logs
- ⏱️ Atualização automática do ranking a cada 5 minutos
- 🔁 Painel e botões sobrevivem a reinicializações do bot

---

## 🚀 Deploy no Railway

### 1. Criar o bot no Discord
1. Acesse [discord.com/developers/applications](https://discord.com/developers/applications)
2. Crie uma nova aplicação → vá em **Bot** → copie o **Token**
3. Em **Privileged Gateway Intents**, ative:
   - ✅ Server Members Intent
   - ✅ Message Content Intent
4. Em **OAuth2 → URL Generator**, selecione:
   - Scopes: `bot`, `applications.commands`
   - Permissões: `Send Messages`, `Embed Links`, `Read Message History`, `View Channels`
5. Copie o link gerado e convide o bot para o servidor

### 2. Deploy no Railway
1. Faça fork/upload deste projeto no GitHub
2. Acesse [railway.app](https://railway.app) e crie um novo projeto a partir do repositório
3. Vá em **Variables** e adicione:

| Variável | Valor |
|---|---|
| `DISCORD_TOKEN` | Token do seu bot |
| `PANEL_CHANNEL_ID` | `1515846128493658142` |
| `LOGS_CHANNEL_ID` | `1515846898156834956` |
| `DB_PATH` | `/data/ponto.db` |

4. Vá em **Settings → Volumes** e adicione um volume montado em `/data` (para persistir o banco de dados)
5. Railway detectará o `Procfile` e iniciará o bot automaticamente

---

## 💻 Rodando localmente

```bash
pip install -r requirements.txt
cp .env.example .env
# Edite o .env com seu token
python main.py
```

---

## 📋 Slash Commands

| Comando | Permissão | Descrição |
|---|---|---|
| `/setup_ponto` | Admin | Recria o painel de bate ponto |
| `/meu_ponto` | Todos | Consulta suas horas da semana |
| `/rank_horas` | Moderador | Força atualização do ranking |
| `/fechar_ponto_admin` | Admin | Fecha o ponto de um colaborador |
| `/pontos_abertos` | Admin | Lista quem está com ponto aberto |
| `/relatorio` | Admin | Relatório de horas (por colaborador/semana) |

---

## 🗄️ Banco de dados

SQLite com 3 tabelas:
- `sessions` — histórico de sessões encerradas
- `active` — pontos atualmente abertos
- `msg_store` — IDs das mensagens do painel e ranking

> ⚠️ **Importante:** Configure um Volume no Railway montado em `/data` e use `DB_PATH=/data/ponto.db` para que os dados persistam entre deploys.
