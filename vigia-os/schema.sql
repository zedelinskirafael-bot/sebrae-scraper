-- Banco apollo (Postgres local da KVM8). Aplicado em 05/10/2026.
-- Desfazer: drop table sebrae_os_vistas, sebrae_os_vigia_estado;

create table if not exists sebrae_os_vistas (
  conta          text        not null,
  os             text        not null,
  data_os        date,
  solicitante    text,
  equipe         text,
  empresa        text,
  objeto         text,
  fluxo          text,
  valor          text,
  visto_em       timestamptz not null default now(),
  atualizado_em  timestamptz not null default now(),
  avisado_em     timestamptz,
  primary key (conta, os)
);

create table if not exists sebrae_os_vigia_estado (
  chave          text primary key,
  valor          text,
  atualizado_em  timestamptz not null default now()
);

-- Vigia de notas fiscais (Financeiro > Consultar Nota Fiscal). Aplicado em 06/10/2026.
-- Estado/contador de falhas reutiliza sebrae_os_vigia_estado com chaves "nf:<conta>:*".
-- Desfazer: drop table sebrae_nf_vistas;
create table if not exists sebrae_nf_vistas (
  conta             text        not null,
  codigo            text        not null,  -- "Codigo Nota Fiscal" interno do portal (ou nf:N|os:N se faltar)
  nf                text,
  os                text,
  empresa           text,
  credenciado       text,
  valor             text,
  optante_simples   boolean,
  status            text,
  data_recebimento  date,
  data_apropriacao  date,
  data_pagamento    date,
  ultima_interacao  text,
  visto_em          timestamptz not null default now(),
  atualizado_em     timestamptz not null default now(),
  avisado_em        timestamptz,
  primary key (conta, codigo)
);
