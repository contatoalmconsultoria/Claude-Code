# Radar Diário de Gestão: ALM Consultoria

Rotina diária que acompanha a operação, os clientes e a gestão da ALM. Ela identifica compromissos,
pendências, atrasos e decisões da direção e entrega à Liliane o relatório
**GESTÃO ALM | Radar Diário | DD/MM/AAAA**.

**Situação atual: simulação concluída, rotina NÃO ativada e envio de e-mail DESATIVADO.**
Para ativar, veja a seção "Aprovações pendentes".

## Estrutura

| Caminho | Conteúdo |
|---|---|
| `prompts/rotina_diaria.md` | Prompt executado pela rotina agendada: regras, etapas e comandos |
| `scripts/radar.py` | Motor da rotina: trava, retomada, validação, lotes de alteração, relatório e controle de envio |
| `config/radar.config.json` | Fontes, agendamento, modo (`simulacao`/`producao`) e e-mail |
| `data/pendencias.json` | Banco de pendências (fonte da verdade nesta fase) |
| `execucoes/AAAA-MM-DD/` | Estado da execução, contexto coletado, cópia do banco antes das alterações e log de alterações |
| `execucoes/execucoes.jsonl` | Registro de todas as execuções (auditoria) |
| `relatorios/AAAA-MM-DD/` | `radar.md`, `radar.html` (detalhado), `email.html` (corpo do e-mail) e `email.json` |
| `db/schema.sql` | Proposta de migração do banco para o Supabase (não aplicada) |

## Integrações (testadas em 08/10/2026, só leitura)

| Fonte | Situação | Uso |
|---|---|---|
| Google Agenda | ✅ disponível | `contato@almconsultoria.com.br`, mais agendas da equipe (deise@, lisiane@, tatieli@) |
| Google Drive | ✅ disponível | Transcrições Tactiq (texto completo desde 08/10), resumos de reunião, pastas de clientes e relatórios anteriores |
| Supabase `intranet-alm` | ✅ disponível | `closing_clients`, `closing_cycles`, `closing_tasks`, `closing_state` (só leitura) |
| Gmail | ⚠️ parcial | A conta conectada é `contato.almconsultoria@gmail.com`, que recebe cópias e não tem enviados |
| Gmail @almconsultoria.com.br (Workspace) | ❌ não conectada | Sem ela não dá para ver respostas a clientes nem propostas enviadas |
| Notion | ➖ disponível, sem conteúdo útil | Só páginas de 2024 |

## Banco de pendências

Campos de cada registro: `id` (ALM-0001…), `cliente`, `descricao`, `responsavel`, `categoria`
(`direcao` | `equipe_tecnica` | `administrativa` | `comercial` | `cliente`), `origem`, `data_identificacao`,
`prazo` / `prazo_texto`, `prioridade` (P1–P4), `status`, `fase`, `precisa_confirmacao`,
`ultima_movimentacao`, `historico[]` e `evidencia_conclusao`.

Status permitidos: Aberta · Em andamento · Aguardando cliente · Aguardando equipe · Aguardando direção · Concluída · Cancelada.

Garantias implementadas em `scripts/radar.py`:
- Nada é excluído. Alterações entram como eventos no `historico`, com fonte, data e valor anterior e novo.
- `concluir` exige evidência e `cancelar` exige motivo.
- Item novo parecido com outro do mesmo cliente (similaridade ≥ 0,82) é ignorado como duplicata.
- Lote que deixaria o banco inválido não é gravado. A gravação é atômica.
- Antes de qualquer alteração, a execução guarda uma cópia do banco em `execucoes/<data>/pendencias_antes.json`.

Linha de base: 68 registros (49 importados do relatório "Pendências Clientes – Semana 2026-10-05
(atualização 08-10)", 3 do Supabase e 16 das transcrições e e-mails de 05 a 08/10). Desses, 66 estão abertos
e 2 foram concluídos com evidência.

## Execução

```bash
D=2026-10-08
python3 scripts/radar.py iniciar --data $D          # trava do dia; responde iniciar/retomar/pular/abortar
python3 scripts/radar.py checkpoint --data $D --etapa agenda
python3 scripts/radar.py aplicar --data $D --lote execucoes/$D/lote.json [--simular]
python3 scripts/radar.py relatorio --data $D --contexto execucoes/$D/contexto.json
python3 scripts/radar.py enviar --data $D           # só verifica as autorizações; quem envia é o conector Gmail
python3 scripts/radar.py finalizar --data $D --status concluida
python3 scripts/radar.py dia-util --data 2026-10-12 # feriados nacionais calculados (inclui Páscoa móvel)
```

Proteções:
- **Execução duplicada:** existe um `estado.json` por dia. Execução concluída é pulada. Execução em andamento há menos de 120 min faz a nova abortar.
- **Retomada:** com a trava expirada, a execução seguinte recebe só as etapas que faltam (checkpoints).
- **Erros:** cada etapa registra a falha em `estado.json` e no log, e a rotina continua com as fontes disponíveis.
- **Dias não úteis:** fins de semana e feriados nacionais são pulados. Feriados locais vão em `agendamento.feriados_adicionais`.

## Agendamento proposto

- **Onde:** uma Rotina do Claude Code (agendador na nuvem, sem sessão interativa), que abre uma sessão nova a cada disparo
  neste repositório, com os conectores Google Calendar, Google Drive, Gmail e Supabase.
- **Quando:** `CRON_TZ=America/Sao_Paulo 52 6 * * 1-5`, ou seja, 06h52 de segunda a sexta, antes do expediente. Feriados são
  pulados pelo próprio script.
- **Persistência:** cada execução faz commit e push de `data/`, `execucoes/` e `relatorios/` na branch configurada.
  O git guarda o histórico completo. Opcionalmente, o banco pode migrar para o Supabase (`db/schema.sql`).

## Estratégia de envio de e-mail

O envio começa desligado e avança em três fases, cada uma com aprovação da direção:

1. **Simulação (atual):** gera `email.html` e não envia nada.
2. **Rascunho:** `email.modo_envio = "rascunho"`. A rotina cria um rascunho no Gmail para a Liliane revisar e enviar.
3. **Envio automático:** `email.modo_envio = "envio"`. A rotina envia para `email.destinatario`.

O envio só acontece se **todas** estas condições forem verdadeiras: `execucao.modo = "producao"`,
`email.envio_autorizado = true`, `email.destinatario` preenchido e a variável `RADAR_ENVIO_AUTORIZADO=1` no
ambiente da rotina. O resultado (ID da mensagem ou erro) fica em `estado.json`. Sem ID devolvido pelo serviço,
o relatório não diz "enviado".

## Permissões necessárias

- **Conectores na rotina:** Google Calendar e Google Drive (leitura), Gmail (leitura; criação de rascunho e envio só nas
  fases 2 e 3) e Supabase (`execute_sql` com SELECT nas tabelas `closing_*`).
- **GitHub:** push na branch da rotina.
- **Recomendado:** conectar a conta Google Workspace `contato@almconsultoria.com.br` (Gmail), para cobrir
  e-mails enviados e respostas de clientes.

## Testes feitos (08/10/2026)

- Leitura em todas as fontes: Agenda (contato@, deise@, lisiane@), Gmail (47 conversas em 15 dias),
  Drive (relatórios anteriores, 4 transcrições novas, aditivos) e Supabase (4 tabelas). Nenhuma escrita externa.
- `validar`: 68 registros válidos.
- Trava: uma segunda `iniciar` no mesmo dia retornou `abortar` (código 3).
- `aplicar --simular`: duplicata ignorada, inclusão e atualização com histórico, e conclusão sem evidência recusada.
- `relatorio`: gerou MD, HTML e e-mail. `enviar` retornou `nao_enviar` com 4 bloqueios.
- `dia-util`: 12/10/2026 aparece como feriado (Nossa Senhora Aparecida).

## Aprovações pendentes (antes de ativar)

1. Endereço de e-mail da direção que vai receber o relatório.
2. Fase de envio inicial: rascunho (recomendado) ou envio direto.
3. Criação da Rotina agendada (06h52, dias úteis), com os conectores listados.
4. Onde fica o banco: git (atual) ou migração para o Supabase (`db/schema.sql`).
5. Conectar o Gmail do Workspace @almconsultoria.com.br (recomendado).
6. Confirmar a lista de clientes ativos e as 19 pendências marcadas com ⚠️.
