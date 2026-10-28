-- Catálogo de posiciones por tipología de vehículo.
-- Ejecutar en Supabase: SQL Editor > New query > pegar y ejecutar.
-- Ajustar el tipo de id_tipologia si catalogo_tipologias.id no es bigint.

-- 1. Resumen en el catálogo de tipologías (una fila por tipología)
alter table catalogo_tipologias
    add column if not exists ejes integer,
    add column if not exists posiciones integer;

-- 2. Detalle por posición (una fila por posición)
create table if not exists tipologia_posiciones (
    id_tipologia bigint  not null references catalogo_tipologias(id) on delete cascade,
    posicion     integer not null check (posicion > 0),
    eje          integer not null check (eje >= 0),   -- 0 = repuesto (sin eje)
    tipo_eje     text    not null check (tipo_eje in ('direccional', 'traccion', 'libre', 'repuesto')),
    lado         text,                                -- izq, der, izq ext, izq int, der int, der ext
    primary key (id_tipologia, posicion),
    -- el eje 0 es exclusivo de los repuestos, y los repuestos solo van en el eje 0
    check ((eje = 0) = (tipo_eje = 'repuesto'))
);

-- 3. Lectura pública (catálogo estático, igual que catalogo_tipologias)
alter table tipologia_posiciones enable row level security;
drop policy if exists "lectura tipologia_posiciones" on tipologia_posiciones;
create policy "lectura tipologia_posiciones" on tipologia_posiciones
    for select using (true);

-- 4. Verificación: compara el resumen del catálogo con el detalle.
--    Debe devolver 0 filas.
create or replace view validacion_tipologias as
select t.id, t.tipologia,
       t.ejes       as ejes_catalogo,
       count(distinct p.eje) filter (where p.eje > 0) as ejes_detalle,
       t.posiciones as posiciones_catalogo,
       count(p.posicion) as posiciones_detalle
from catalogo_tipologias t
left join tipologia_posiciones p on p.id_tipologia = t.id
group by t.id, t.tipologia, t.ejes, t.posiciones
having t.ejes is distinct from count(distinct p.eje) filter (where p.eje > 0)
    or t.posiciones is distinct from count(p.posicion);

-- Ejemplo de carga (C2: 2 ejes, 6 llantas + 1 repuesto). Reemplazar 1 por el id real.
-- update catalogo_tipologias set ejes = 2, posiciones = 7 where id = 1;
-- insert into tipologia_posiciones (id_tipologia, posicion, eje, tipo_eje, lado) values
--     (1, 1, 1, 'direccional', 'izq'),
--     (1, 2, 1, 'direccional', 'der'),
--     (1, 3, 2, 'traccion',    'izq ext'),
--     (1, 4, 2, 'traccion',    'izq int'),
--     (1, 5, 2, 'traccion',    'der int'),
--     (1, 6, 2, 'traccion',    'der ext'),
--     (1, 7, 0, 'repuesto',    null);
