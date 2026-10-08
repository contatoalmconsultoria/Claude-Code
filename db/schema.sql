-- Banco de pendências do Radar Diário: PROPOSTA de migração para o Supabase (projeto intranet-alm).
-- NÃO APLICADO. Só aplicar depois da aprovação da direção.
-- Equivale a data/pendencias.json + execucoes/execucoes.jsonl, com histórico imutável.

create schema if not exists radar;

create table radar.pendencias (
  id                   text primary key,                       -- ALM-0001
  cliente              text not null,
  descricao            text not null,
  responsavel          text not null,
  categoria            text not null check (categoria in ('direcao','equipe_tecnica','administrativa','comercial','cliente')),
  origem               text not null,
  data_identificacao   date not null,
  prazo                date,
  prazo_texto          text,                                    -- "Sem prazo informado", "Outubro"...
  prioridade           text not null check (prioridade in ('P1','P2','P3','P4')),
  status               text not null check (status in ('Aberta','Em andamento','Aguardando cliente',
                         'Aguardando equipe','Aguardando direção','Concluída','Cancelada')),
  fase                 text check (fase in ('Organização','Gestão','Planejamento')),
  precisa_confirmacao  boolean not null default false,
  ultima_movimentacao  date not null,
  evidencia_conclusao  text,
  closing_client_id    uuid references public.closing_clients(id),
  criado_em            timestamptz not null default now(),
  constraint concluida_exige_evidencia check (status <> 'Concluída' or evidencia_conclusao is not null),
  constraint prazo_definido check (prazo is not null or prazo_texto is not null)
);

-- Histórico somente de inserção (auditoria). Updates e deletes são bloqueados por trigger.
create table radar.historico (
  id           bigserial primary key,
  pendencia_id text not null references radar.pendencias(id),
  data         date not null,
  evento       text not null,              -- identificada | atualizada | concluída | cancelada | verificação | divergência
  fonte        text not null,
  detalhe      text,
  alteracoes   jsonb,
  registrado_em timestamptz not null default now()
);

create or replace function radar.bloquear_alteracao() returns trigger language plpgsql as $$
begin
  raise exception 'radar.historico e radar.pendencias não permitem exclusão; o histórico é imutável';
end $$;

create trigger historico_imutavel before update or delete on radar.historico
  for each row execute function radar.bloquear_alteracao();
create trigger pendencias_sem_delete before delete on radar.pendencias
  for each row execute function radar.bloquear_alteracao();

-- Registro de execuções. A chave única por data é a trava contra execuções duplicadas.
create table radar.execucoes (
  data              date primary key,
  modo              text not null check (modo in ('simulacao','producao')),
  status            text not null check (status in ('em_andamento','concluida','parcial','falhou')),
  inicio            timestamptz not null default now(),
  fim               timestamptz,
  etapas_concluidas text[] not null default '{}',
  erros             jsonb not null default '[]',
  envio             jsonb,
  observacao        text
);

alter table radar.pendencias enable row level security;
alter table radar.historico  enable row level security;
alter table radar.execucoes  enable row level security;
-- Políticas: leitura para usuários autenticados da intranet. Escrita só pela service role usada pela rotina.
create policy leitura_pendencias on radar.pendencias for select to authenticated using (true);
create policy leitura_historico  on radar.historico  for select to authenticated using (true);
create policy leitura_execucoes  on radar.execucoes  for select to authenticated using (true);
