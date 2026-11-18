"""
db.py — Capa de acceso a datos para SILL usando Supabase (REST API).
Reemplaza la conexión a Google Sheets manteniendo la misma interfaz:
  leer_hoja(nombre) → pd.DataFrame
  escribir_hoja(nombre, df) → pd.DataFrame | None
"""
import os
import streamlit as st
import pandas as pd
from supabase import create_client, Client

# ─────────────────────────────────────────────────────────────
# Conexión a Supabase
# ─────────────────────────────────────────────────────────────

def _get_supabase_url() -> str:
    return os.environ.get("SUPABASE_URL") or st.secrets.get("SUPABASE_URL", "")

def _get_supabase_key() -> str:
    return os.environ.get("SUPABASE_KEY") or st.secrets.get("SUPABASE_KEY", "")

@st.cache_resource
def get_supabase_client() -> Client:
    """Singleton del cliente Supabase."""
    url = _get_supabase_url()
    key = _get_supabase_key()
    if not url or not key:
        st.error("⚠️ Faltan las variables SUPABASE_URL y/o SUPABASE_KEY. "
                 "Configúralas en .streamlit/secrets.toml o como variables de entorno.")
        st.stop()
    return create_client(url, key)

# ─────────────────────────────────────────────────────────────
# Mapeo de nombres de hoja → tabla/vista
# ─────────────────────────────────────────────────────────────

TABLE_MAP = {
    "clientes": "clientes",
    "vehiculos": "vehiculos",
    "llantas": "llantas",
    "servicios": "servicios",
    "usuarios": "usuarios",
    "movimientos": "vw_movimientos",   # Vista (lectura) — escritura va a servicios
    "alineaciones": "alineaciones",
}

PRIMARY_KEYS = {
    "clientes": "id_cliente",
    "vehiculos": "id_vehiculo",
    "llantas": "id_llanta",
    "servicios": "id_servicio",
    "usuarios": "id_usuario",
    "alineaciones": "id_alineacion",
}

# Columnas de movimientos → servicios (para escritura)
MOV_TO_SERV_COLUMNS = {
    "id_movimiento": "id_servicio",
    "tipo": "tipo_servicio",
    "usuario": "usuario_registro",
}

# ─────────────────────────────────────────────────────────────
# Lectura
# ─────────────────────────────────────────────────────────────

# Supabase/PostgREST devuelve como máximo ~1000 filas por consulta.
# Se lee por páginas para no perder registros en tablas grandes.
_PAGE_SIZE = 1000

# Columna para ordenar la paginación (orden estable entre páginas)
_ORDER_COLS = {
    "clientes": "id_cliente",
    "vehiculos": "id_vehiculo",
    "llantas": "id_llanta",
    "servicios": "id_servicio",
    "usuarios": "id_usuario",
    "vw_movimientos": "id_servicio",
    "alineaciones": "id_alineacion",
}


def _select_all(sb: Client, table_name: str, columns: str = "*", filtro=None) -> list:
    """Lee TODAS las filas de una tabla/vista paginando de 1000 en 1000."""
    order_col = _ORDER_COLS.get(table_name)
    filas = []
    inicio = 0
    while True:
        query = sb.table(table_name).select(columns)
        if filtro is not None:
            query = filtro(query)
        if order_col:
            query = query.order(order_col)
        resp = query.range(inicio, inicio + _PAGE_SIZE - 1).execute()
        lote = resp.data or []
        filas.extend(lote)
        if len(lote) < _PAGE_SIZE:
            break
        inicio += _PAGE_SIZE
    return filas


def leer_hoja(nombre_hoja: str) -> pd.DataFrame:
    """Lee una tabla/vista de Supabase y retorna un DataFrame."""
    try:
        sb = get_supabase_client()
        table_name = TABLE_MAP.get(nombre_hoja, nombre_hoja)
        data = _select_all(sb, table_name)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df = _postprocess(df, nombre_hoja)
        return df
    except Exception as e:
        st.error(f"Error leyendo {nombre_hoja}: {str(e)}")
        return pd.DataFrame()


def leer_hoja_fresco(nombre_hoja: str) -> pd.DataFrame:
    """Lee sin caché (en Supabase siempre es fresco)."""
    return leer_hoja(nombre_hoja)


# ─────────────────────────────────────────────────────────────
# Escritura
# ─────────────────────────────────────────────────────────────

def escribir_hoja(nombre_hoja: str, df: pd.DataFrame) -> pd.DataFrame | None:
    """
    Escribe un DataFrame completo a Supabase (simula el overwrite de Google Sheets).
    Estrategia: UPSERT todas las filas + DELETE las que ya no están.
    Retorna el DataFrame actualizado desde la BD.
    """
    try:
        if df is None or df.empty:
            print(f"[DB] escribir_hoja({nombre_hoja}): DataFrame vacío o None, retornando.")
            return df

        sb = get_supabase_client()

        # Caso especial: escribir en "movimientos" → insertar en servicios
        if nombre_hoja == "movimientos":
            return _escribir_movimientos(sb, df)

        table_name = TABLE_MAP.get(nombre_hoja, nombre_hoja)
        pk = PRIMARY_KEYS.get(nombre_hoja)

        if not pk:
            st.error(f"No se encontró primary key para {nombre_hoja}")
            return None

        # Limpiar DataFrame para serialización
        df_clean = _prepare_for_write(df, nombre_hoja)
        print(f"[DB] escribir_hoja({nombre_hoja}): {len(df_clean)} filas, columnas: {list(df_clean.columns)}")

        # Obtener PKs actuales en la BD
        existing = _select_all(sb, table_name, pk)
        existing_pks = {row[pk] for row in existing}

        # PKs en el nuevo DataFrame
        new_pks = set(df_clean[pk].astype(str).values)
        print(f"[DB] existentes: {len(existing_pks)}, nuevas: {len(new_pks)}, a eliminar: {len(existing_pks - new_pks)}")

        # Eliminar filas que ya no existen
        pks_to_delete = existing_pks - new_pks
        if pks_to_delete:
            for pk_val in pks_to_delete:
                sb.table(table_name).delete().eq(pk, pk_val).execute()

        # Upsert todas las filas del DataFrame
        records = df_clean.to_dict(orient="records")
        # Limpiar NaN/inf que no son JSON-serializables
        records = _clean_records(records)
        if records:
            print(f"[DB] Upserting {len(records)} records. Sample: {records[-1]}")
            # Supabase upsert en lotes de 500
            batch_size = 500
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                resp = sb.table(table_name).upsert(batch).execute()
                print(f"[DB] Upsert batch response: {len(resp.data)} rows returned")

        # Retornar datos frescos
        return leer_hoja(nombre_hoja)

    except Exception as e:
        st.error(f"Error escribiendo en {nombre_hoja}: {str(e)}")
        return None


def _escribir_movimientos(sb: Client, df: pd.DataFrame) -> pd.DataFrame | None:
    """
    Escribe movimientos en la tabla servicios (con mapeo de columnas).
    Solo hace upsert/delete de registros cuyo tipo_servicio es de tipo movimiento.
    """
    try:
        # Renombrar columnas de movimiento → servicio
        df_serv = df.copy()
        df_serv = df_serv.rename(columns=MOV_TO_SERV_COLUMNS)

        pk = "id_servicio"
        if pk not in df_serv.columns:
            return None

        # Asegurar que tipo_servicio está en los valores de movimiento
        tipos_mov = ('montaje', 'desmontaje', 'rotacion', 'aprobacion_reencauche')

        # Obtener los movimientos existentes en servicios
        existing = _select_all(sb, "servicios", "id_servicio",
                               filtro=lambda q: q.in_("tipo_servicio", list(tipos_mov)))
        existing_pks = {row["id_servicio"] for row in existing}

        new_pks = set(df_serv[pk].astype(str).values)

        # Eliminar movimientos que ya no existen
        pks_to_delete = existing_pks - new_pks
        if pks_to_delete:
            for pk_val in pks_to_delete:
                sb.table("servicios").delete().eq("id_servicio", pk_val).execute()

        # Preparar registros para upsert
        df_serv = _prepare_for_write(df_serv, "servicios")
        records = _clean_records(df_serv.to_dict(orient="records"))
        if records:
            batch_size = 500
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                sb.table("servicios").upsert(batch).execute()

        return leer_hoja("movimientos")

    except Exception as e:
        st.error(f"Error escribiendo movimientos: {str(e)}")
        return None


# ─────────────────────────────────────────────────────────────
# Carga masiva: SOLO AGREGA (nunca modifica ni borra lo existente)
# ─────────────────────────────────────────────────────────────

def _id_texto(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ''
    s = str(x).strip()
    return '' if s.lower() in ('nan', 'none') else s


# Dato "natural" que identifica un registro además de su ID.
# Solo clientes (NIT) y vehículos (placa): en llantas y servicios un dato
# repetido puede ser legítimo (p. ej. un servicio que se volvió a ejecutar).
_LLAVES_NATURALES = {
    "clientes": "nit",
    "vehiculos": "placa_vehiculo",
}


def _norm_natural(col: str, x) -> str:
    """Normaliza placa/NIT para comparar: sin espacios, puntos ni guiones, en mayúsculas."""
    s = _id_texto(x)
    if col == 'nit':
        s = _normalize_nit(s)
    for ch in (' ', '.', '-'):
        s = s.replace(ch, '')
    return s.upper()


def validar_carga_masiva(nombre_hoja: str, df: pd.DataFrame) -> dict:
    """
    Revisa (sin escribir nada) un DataFrame que se quiere AGREGAR a una tabla.
    Retorna un dict con:
      tabla, pk, total_actual, df_nuevos (filas listas para insertar),
      ids_vacios (n.º de filas sin ID), ids_duplicados_archivo (lista),
      ids_existentes (lista de IDs que ya están en la BD), columnas_ignoradas,
      llave_natural (columna o None), naturales_existentes (valores que ya
      están en la BD), naturales_duplicados_archivo (repetidos en el archivo).
    """
    sb = get_supabase_client()
    df = df.copy()

    if nombre_hoja == "movimientos":
        df = df.rename(columns=MOV_TO_SERV_COLUMNS)
        tabla = "servicios"
    else:
        tabla = TABLE_MAP.get(nombre_hoja, nombre_hoja)
    pk = PRIMARY_KEYS.get(tabla)
    if not pk:
        raise ValueError(f"No se conoce la llave primaria de {nombre_hoja}")
    if pk not in df.columns:
        raise ValueError(f"El archivo no tiene la columna obligatoria '{pk}'")

    valid_cols = _get_table_columns(tabla) or set(df.columns)
    columnas_ignoradas = [c for c in df.columns if c not in valid_cols]

    df[pk] = df[pk].apply(_id_texto)
    ids_vacios = int((df[pk] == '').sum())
    df = df[df[pk] != '']

    dup_mask = df[pk].duplicated(keep=False)
    ids_duplicados_archivo = sorted(df.loc[dup_mask, pk].unique().tolist())

    # IDs que ya existen en la tabla (todas las filas, con paginación)
    existentes = {_id_texto(r[pk]) for r in _select_all(sb, tabla, pk)}
    ids_existentes = sorted(set(df[pk]) & existentes)

    omitir = dup_mask | df[pk].isin(existentes)

    # Validación por dato natural (placa / NIT), igual que el registro manual
    llave_natural = _LLAVES_NATURALES.get(tabla)
    naturales_existentes, naturales_duplicados_archivo = [], []
    naturales_vacios = 0
    if llave_natural:
        if llave_natural not in df.columns:
            raise ValueError(f"El archivo no tiene la columna obligatoria '{llave_natural}'")
        if llave_natural == 'placa_vehiculo':
            df[llave_natural] = df[llave_natural].apply(lambda x: _id_texto(x).upper())
        norm = df[llave_natural].apply(lambda x: _norm_natural(llave_natural, x))
        vacio_nat = norm == ''
        dup_nat = norm.duplicated(keep=False) & ~vacio_nat
        en_bd = {_norm_natural(llave_natural, r[llave_natural])
                 for r in _select_all(sb, tabla, llave_natural)}
        en_bd.discard('')
        existe_nat = norm.isin(en_bd)
        naturales_existentes = sorted(df.loc[existe_nat, llave_natural].astype(str).unique().tolist())
        naturales_duplicados_archivo = sorted(df.loc[dup_nat, llave_natural].astype(str).unique().tolist())
        naturales_vacios = int((vacio_nat & ~omitir).sum())
        omitir = omitir | dup_nat | existe_nat | vacio_nat

    df_nuevos = df[~omitir]

    return {
        "tabla": tabla,
        "pk": pk,
        "total_actual": len(existentes),
        "df_nuevos": df_nuevos,
        "ids_vacios": ids_vacios,
        "ids_duplicados_archivo": ids_duplicados_archivo,
        "ids_existentes": ids_existentes,
        "columnas_ignoradas": columnas_ignoradas,
        "llave_natural": llave_natural,
        "naturales_existentes": naturales_existentes,
        "naturales_duplicados_archivo": naturales_duplicados_archivo,
        "naturales_vacios": naturales_vacios,
    }


def agregar_registros(tabla: str, df_nuevos: pd.DataFrame, batch_size: int = 500) -> int:
    """
    Inserta filas NUEVAS en la tabla usando INSERT (no upsert, no delete).
    Si algún ID ya existiera, la base de datos rechaza el lote en lugar de
    sobrescribir. Retorna el número de filas insertadas.
    """
    if df_nuevos is None or df_nuevos.empty:
        return 0
    sb = get_supabase_client()
    df_clean = _prepare_for_write(df_nuevos, tabla)
    records = _clean_records(df_clean.to_dict(orient="records"))
    insertadas = 0
    for i in range(0, len(records), batch_size):
        lote = records[i:i + batch_size]
        try:
            sb.table(tabla).insert(lote).execute()
        except Exception as e:
            raise RuntimeError(
                f"Se insertaron {insertadas} de {len(records)} filas antes del error: {e}"
            ) from e
        insertadas += len(lote)
    return insertadas


def contar_filas(tabla: str) -> int:
    """Cuenta las filas de una tabla."""
    sb = get_supabase_client()
    resp = sb.table(tabla).select(PRIMARY_KEYS.get(tabla, "*"), count="exact").limit(1).execute()
    return resp.count or 0


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _postprocess(df: pd.DataFrame, nombre_hoja: str) -> pd.DataFrame:
    """Post-procesamiento tras lectura (equivalente a lo que hacía leer_hoja original)."""
    if df.empty:
        return df

    # Normalizar NITs a string (preservando ceros iniciales)
    for col in ['nit', 'nit_cliente']:
        if col in df.columns:
            df[col] = df[col].apply(_normalize_nit)

    # Limpiar clientes_asignados
    if 'clientes_asignados' in df.columns:
        df['clientes_asignados'] = df['clientes_asignados'].apply(_limpiar_clientes_asignados)

    # Normalizar columnas ID a string
    columnas_id = ['id_vehiculo', 'id_llanta', 'id_servicio', 'id_movimiento',
                   'id_alineacion', 'id_usuario', 'placa_vehiculo', 'usuario']
    for col in columnas_id:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: str(x).strip() if pd.notna(x) and str(x).strip() != 'nan' else '')

    # La app existente representa los booleanos como "Sí"/"No", mientras
    # que PostgreSQL/Supabase los devuelve como True/False.
    bool_cols = ['balanceo', 'reparacion', 'despinche', 'regrabacion',
                 'torqueo', 'inspeccion', 'rotacion', 'activo']
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].apply(lambda value: 'Sí' if bool(value) else 'No')

    # Para movimientos: renombrar columnas de la vista para compatibilidad
    if nombre_hoja == "movimientos":
        rename_back = {v: k for k, v in MOV_TO_SERV_COLUMNS.items()}
        df = df.rename(columns=rename_back)

    return df


def _prepare_for_write(df: pd.DataFrame, nombre_hoja: str) -> pd.DataFrame:
    """Prepara un DataFrame para escritura en Supabase (limpieza de tipos)."""
    import math
    import numpy as np
    df = df.copy()

    # Obtener columnas válidas de la tabla destino
    table_name = TABLE_MAP.get(nombre_hoja, nombre_hoja)
    valid_cols = _get_table_columns(table_name)
    if valid_cols:
        # Solo mantener columnas que existen en la tabla
        df = df[[c for c in df.columns if c in valid_cols]]

    # Reemplazar NaN/NaT/None con None de Python (compatible JSON)
    df = df.replace({np.nan: None, pd.NaT: None})
    df = df.where(df.notna(), None)

    # Preservar NITs como strings exactos (no convertir numéricamente)
    for col in ['nit', 'nit_cliente']:
        if col in df.columns:
            df[col] = df[col].apply(lambda x: str(x).strip() if x is not None else None)

    # Convertir columnas numéricas
    numeric_cols = ['kilometraje', 'kilometraje_inicial', 'kilometraje_actual',
                    'kilometros_totales', 'km_ultimo_montaje', 'precio_vida1',
                    'precio_vida2', 'precio_vida3', 'precio_vida4',
                    'costo_km_vida1', 'costo_km_vida2', 'costo_km_vida3', 'costo_km_vida4',
                    'profundidad_1', 'profundidad_2', 'profundidad_3',
                    'precio_reencauche', 'total_regrabaciones']
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

    # Convertir booleanos
    bool_cols = ['balanceo', 'reparacion', 'despinche', 'regrabacion',
                 'torqueo', 'inspeccion', 'rotacion', 'activo']
    for col in bool_cols:
        if col in df.columns:
            df[col] = df[col].apply(_to_bool)

    # Nivel como int
    if 'nivel' in df.columns:
        df['nivel'] = pd.to_numeric(df['nivel'], errors='coerce').fillna(1).astype(int)

    # Vida como int
    if 'vida' in df.columns:
        df['vida'] = pd.to_numeric(df['vida'], errors='coerce').fillna(1).astype(int)
    if 'vida_actual' in df.columns:
        df['vida_actual'] = pd.to_numeric(df['vida_actual'], errors='coerce').fillna(1).astype(int)

    # frentes: asegurar que sea JSON válido
    if 'frentes' in df.columns:
        df['frentes'] = df['frentes'].apply(_ensure_json_list)

    return df


# Cache de columnas por tabla
_table_columns_cache: dict = {}

# Columnas conocidas como fallback si la tabla está vacía
_KNOWN_COLUMNS = {
    "clientes": {"id_cliente", "nit", "nombre_cliente", "frentes", "fecha_creacion"},
    "vehiculos": {"id_vehiculo", "nit_cliente", "marca", "linea", "tipologia", "placa_vehiculo",
                  "frente", "estado", "kilometraje_inicial", "calculo_kms", "fecha_creacion"},
    "llantas": {"id_llanta", "nit_cliente", "marca_llanta", "referencia", "dimension", "vida_actual",
                "disponibilidad", "kilometros_totales", "km_ultimo_montaje", "total_regrabaciones",
                "placa_actual", "posicion_actual", "estado_reencauche", "precio_vida1", "reencauche1",
                "precio_vida2", "reencauche2", "precio_vida3", "reencauche3", "precio_vida4",
                "costo_km_vida1", "costo_km_vida2", "costo_km_vida3", "costo_km_vida4",
                "fecha_creacion", "fecha_modificacion"},
    "servicios": {"id_servicio", "orden_trabajo", "planilla", "fecha", "id_llanta", "placa_vehiculo",
                  "posicion", "posicion_nueva", "vida", "tipologia", "tipo_servicio", "disponibilidad",
                  "kilometraje", "rotacion", "profundidad_1", "profundidad_2", "profundidad_3",
                  "balanceo", "reparacion", "despinche", "regrabacion", "torqueo", "inspeccion",
                  "insumos", "comentario_fvu", "observaciones", "nueva_disponibilidad",
                  "marca_reencauche", "ref_reencauche", "precio_reencauche", "operario",
                  "usuario_registro", "timestamp"},
    "usuarios": {"id_usuario", "usuario", "password_hash", "nivel", "nombre", "clientes_asignados"},
    "alineaciones": {"id_alineacion", "fecha", "placa_vehiculo", "nit_cliente", "kilometraje",
                     "alineador", "angulo_inicial_izq", "angulo_final_izq",
                     "angulo_inicial_der", "angulo_final_der", "observaciones",
                     "usuario_registro", "timestamp"},
}

def _get_table_columns(table_name: str) -> set | None:
    """Obtiene las columnas de una tabla (cacheado)."""
    if table_name in _table_columns_cache:
        return _table_columns_cache[table_name]
    try:
        sb = get_supabase_client()
        # Leer una fila para obtener las columnas
        resp = sb.table(table_name).select("*").limit(1).execute()
        if resp.data:
            cols = set(resp.data[0].keys())
            _table_columns_cache[table_name] = cols
            return cols
    except:
        pass
    # Fallback a columnas conocidas
    if table_name in _KNOWN_COLUMNS:
        return _KNOWN_COLUMNS[table_name]
    return None


def _normalize_nit(x) -> str:
    """Normaliza NIT a string preservando ceros iniciales. Solo quita .0 de floats."""
    if pd.isna(x) or x is None or x == '':
        return ''
    # Si es float (ej: 1234567890.0), convertir a int para quitar .0
    if isinstance(x, float):
        return str(int(x))
    # Si es int, convertir a string directo
    if isinstance(x, int):
        return str(x)
    # Si es string, preservar tal cual (no hacer int(float()) que pierde ceros)
    s = str(x).strip()
    # Solo quitar .0 si termina en .0 (vino como "1234567890.0")
    if s.endswith('.0') and s[:-2].isdigit():
        return s[:-2]
    return s


def _is_numeric(val) -> bool:
    try:
        float(val)
        return True
    except (ValueError, TypeError):
        return False


def _to_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if pd.isna(val) or val is None or val == '':
        return False
    s = str(val).strip().lower()
    return s in ('true', '1', 'sí', 'si', 'yes')


def _limpiar_clientes_asignados(valor) -> str:
    if pd.isna(valor) or valor == '' or valor is None:
        return ''
    if isinstance(valor, (int, float)):
        return str(int(valor))
    valor_str = str(valor)
    nits = []
    for nit in valor_str.split(','):
        nit = nit.strip()
        if nit:
            # Preservar el NIT como string, solo quitar .0 si aplica
            nits.append(_normalize_nit(nit))
    return ','.join(nits)


def _clean_records(records: list) -> list:
    """Limpia valores NaN/inf/numpy types de los records para que sean JSON-serializables."""
    import json
    import math
    import numpy as np

    def _sanitize_value(v):
        if v is None:
            return None
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating,)):
            val = float(v)
            return None if (math.isnan(val) or math.isinf(val)) else val
        if isinstance(v, np.bool_):
            return bool(v)
        if isinstance(v, np.ndarray):
            return v.tolist()
        if hasattr(v, 'isoformat'):
            return v.isoformat()
        if isinstance(v, (dict, list)):
            # Recursively sanitize nested structures
            return json.loads(json.dumps(v, default=str))
        return v

    cleaned = []
    for record in records:
        clean = {k: _sanitize_value(v) for k, v in record.items()}
        cleaned.append(clean)
    return cleaned


def _ensure_json_list(val):
    """Asegura que el valor sea una lista JSON válida."""
    import json
    if val is None or (isinstance(val, str) and val.strip() == ''):
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val.replace("'", '"'))
            return parsed if isinstance(parsed, list) else [parsed]
        except:
            return [val]
    return [str(val)]
