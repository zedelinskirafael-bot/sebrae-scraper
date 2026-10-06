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
