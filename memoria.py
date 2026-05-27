import sqlite3
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

try:
    import MetaTrader5 as mt5
    MT5_DISPONIBLE = True
except ImportError:
    MT5_DISPONIBLE = False
    mt5 = None

DB_PATH = "bot_memoria.db"


# ─── CREAR / MIGRAR BASE DE DATOS ────────────────────────
def inicializar_db():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS señales (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha            TEXT,
            hora             TEXT,
            direccion        TEXT,
            score            INTEGER,
            confianza        TEXT,
            tfs_confluencia  INTEGER,
            precio_entry     REAL,
            sl               REAL,
            tp               REAL,
            rr               REAL,
            score_ict        INTEGER,
            score_noticias   INTEGER,
            resumen_noticias TEXT,
            tfs_detalle      TEXT,
            resultado        TEXT DEFAULT 'pendiente',
            precio_cierre    REAL DEFAULT 0,
            ganancia_pips    REAL DEFAULT 0,
            duracion_minutos INTEGER DEFAULT 0,
            hora_cierre      TEXT DEFAULT ''
        )
    """)

    # Migración: añade columna atr si no existe (safe en SQLite)
    try:
        c.execute("ALTER TABLE señales ADD COLUMN atr REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # columna ya existe

    # Migración: añade columna tipo_entrada si no existe
    try:
        c.execute("ALTER TABLE señales ADD COLUMN tipo_entrada TEXT DEFAULT 'MARKET'")
    except sqlite3.OperationalError:
        pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS estadisticas_diarias (
            fecha      TEXT PRIMARY KEY,
            total      INTEGER DEFAULT 0,
            ganadas    INTEGER DEFAULT 0,
            perdidas   INTEGER DEFAULT 0,
            pendientes INTEGER DEFAULT 0,
            win_rate   REAL DEFAULT 0,
            pips_netos REAL DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()
    print("Base de datos inicializada")


# ─── GUARDAR SEÑAL ───────────────────────────────────────
def guardar_señal(score_final, setup, sentimiento_ia, score_ict, atr=0.0):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    ahora = datetime.now(timezone.utc)

    tfs_detalle = {}
    for tf, data in score_ict["por_tf"].items():
        tfs_detalle[tf] = {
            "tendencia":    data["tendencia"],
            "ob_alcistas":  data["ob_alcistas"],
            "ob_bajistas":  data["ob_bajistas"],
            "fvg_alcistas": data["fvg_alcistas"],
            "fvg_bajistas": data["fvg_bajistas"],
        }

    c.execute("""
        INSERT INTO señales (
            fecha, hora, direccion, score, confianza,
            tfs_confluencia, precio_entry, sl, tp, rr,
            score_ict, score_noticias, resumen_noticias, tfs_detalle,
            atr, tipo_entrada
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        ahora.strftime("%Y-%m-%d"),
        ahora.strftime("%H:%M:%S"),
        score_final["direccion"],
        score_final["score"],
        score_final["confianza"],
        score_final["tfs_confluencia"],
        setup["entry"],
        setup["sl"],
        setup["tp"],
        setup["rr"],
        score_ict["score"],
        sentimiento_ia["score"],
        sentimiento_ia.get("resumen", ""),
        json.dumps(tfs_detalle),
        round(atr, 2),
        setup.get("tipo_entrada", "MARKET"),
    ))

    señal_id = c.lastrowid
    conn.commit()
    conn.close()

    print(f"Señal guardada con ID: {señal_id}")
    return señal_id


# ─── VERIFICAR RESULTADO ─────────────────────────────────
def verificar_señales_pendientes():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        SELECT id, direccion, precio_entry, sl, tp, hora, fecha
        FROM señales
        WHERE resultado = 'pendiente'
    """)
    pendientes = c.fetchall()

    if not pendientes:
        conn.close()
        return

    precio_actual = None
    if MT5_DISPONIBLE and mt5:
        tick = mt5.symbol_info_tick("XAUUSD")
        if tick:
            precio_actual = tick.bid

    if not precio_actual:
        try:
            import yfinance as yf
            precio_actual = yf.Ticker("GC=F").fast_info["last_price"]
        except Exception:
            conn.close()
            return

    ahora = datetime.now(timezone.utc)

    for señal in pendientes:
        id_señal, direccion, entry, sl, tp, hora, fecha = señal

        try:
            dt_señal = datetime.strptime(
                f"{fecha} {hora}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            minutos = int((ahora - dt_señal).total_seconds() / 60)
        except Exception:
            minutos = 0

        resultado     = None
        precio_cierre = 0
        ganancia_pips = 0

        if direccion == "LONG":
            if precio_actual >= tp:
                resultado     = "ganada"
                precio_cierre = tp
                ganancia_pips = round(tp - entry, 2)
            elif precio_actual <= sl:
                resultado     = "perdida"
                precio_cierre = sl
                ganancia_pips = round(sl - entry, 2)
            elif minutos >= 240:
                resultado     = "expirada"
                precio_cierre = precio_actual
                ganancia_pips = round(precio_actual - entry, 2)
        else:
            if precio_actual <= tp:
                resultado     = "ganada"
                precio_cierre = tp
                ganancia_pips = round(entry - tp, 2)
            elif precio_actual >= sl:
                resultado     = "perdida"
                precio_cierre = sl
                ganancia_pips = round(entry - sl, 2)
            elif minutos >= 240:
                resultado     = "expirada"
                precio_cierre = precio_actual
                ganancia_pips = round(entry - precio_actual, 2)

        if resultado:
            c.execute("""
                UPDATE señales
                SET resultado=?, precio_cierre=?,
                    ganancia_pips=?, duracion_minutos=?, hora_cierre=?
                WHERE id=?
            """, (resultado, precio_cierre, ganancia_pips, minutos,
                  ahora.strftime("%H:%M:%S"), id_señal))
            print(f"Señal #{id_señal}: {resultado.upper()} "
                  f"| {ganancia_pips:+.2f} pips | {minutos} min")

    conn.commit()
    conn.close()
    actualizar_estadisticas()


# ─── ACTUALIZAR ESTADÍSTICAS ─────────────────────────────
def actualizar_estadisticas():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("SELECT DISTINCT fecha FROM señales")
    fechas = [r[0] for r in c.fetchall()]

    for fecha in fechas:
        c.execute("""
            SELECT resultado, ganancia_pips FROM señales WHERE fecha=?
        """, (fecha,))
        rows = c.fetchall()

        total      = len(rows)
        ganadas    = sum(1 for r in rows if r[0] == "ganada")
        perdidas   = sum(1 for r in rows if r[0] == "perdida")
        pendientes = sum(1 for r in rows if r[0] == "pendiente")
        pips       = sum(r[1] for r in rows if r[0] in ["ganada", "perdida"])
        win_rate   = round((ganadas / (ganadas + perdidas)) * 100, 1) \
                     if (ganadas + perdidas) > 0 else 0

        c.execute("""
            INSERT OR REPLACE INTO estadisticas_diarias
            (fecha, total, ganadas, perdidas, pendientes, win_rate, pips_netos)
            VALUES (?,?,?,?,?,?,?)
        """, (fecha, total, ganadas, perdidas, pendientes, win_rate, pips))

    conn.commit()
    conn.close()


# ─── THRESHOLD DINÁMICO ──────────────────────────────────
# Ajusta el score mínimo según el win rate de las últimas `ventana` señales.
# Si el sistema está fallando, sube el umbral para ser más selectivo.
# Si está funcionando bien, puede bajar un poco para capturar más oportunidades.
def obtener_score_minimo_dinamico(base=72, ventana=30):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        SELECT resultado FROM señales
        WHERE resultado IN ('ganada', 'perdida')
        ORDER BY id DESC
        LIMIT ?
    """, (ventana,))
    rows = c.fetchall()
    conn.close()

    if len(rows) < 10:
        return base  # sin suficiente historial, usa el base

    ganadas  = sum(1 for r in rows if r[0] == "ganada")
    win_rate = ganadas / len(rows)

    if win_rate < 0.35:
        nuevo = min(base + 10, 90)
        print(f"  [Threshold] WR={win_rate:.0%} muy bajo → score mínimo subido a {nuevo}%")
        return nuevo
    elif win_rate < 0.45:
        nuevo = min(base + 5, 88)
        print(f"  [Threshold] WR={win_rate:.0%} bajo → score mínimo subido a {nuevo}%")
        return nuevo
    elif win_rate > 0.60:
        nuevo = max(base - 3, 65)
        print(f"  [Threshold] WR={win_rate:.0%} bueno → score mínimo bajado a {nuevo}%")
        return nuevo

    return base


# ─── ANÁLISIS DE EFICACIA ────────────────────────────────
# Correlaciona las condiciones al momento de cada señal con su resultado.
# Identifica qué dirección, confianza, hora y rango de score funcionan mejor.
def analizar_eficacia_condiciones(min_muestras=5):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    c.execute("""
        SELECT
            direccion,
            confianza,
            tfs_confluencia,
            substr(hora, 1, 2) as hora_utc,
            CASE
                WHEN score >= 85 THEN '85+'
                WHEN score >= 80 THEN '80-84'
                WHEN score >= 75 THEN '75-79'
                WHEN score >= 70 THEN '70-74'
                ELSE '<70'
            END as rango_score,
            tipo_entrada,
            resultado
        FROM señales
        WHERE resultado IN ('ganada', 'perdida')
        ORDER BY id DESC
        LIMIT 300
    """)
    rows = c.fetchall()
    conn.close()

    if not rows:
        return {}

    stats = defaultdict(lambda: {"ganadas": 0, "total": 0})

    for row in rows:
        direccion, confianza, tfs, hora, rango, tipo_entrada, resultado = row
        es_ganada = resultado == "ganada"

        for clave in [
            f"dir_{direccion}",
            f"conf_{confianza}",
            f"tfs_{tfs}",
            f"hora_{hora}h",
            f"score_{rango}",
            f"entrada_{tipo_entrada}",
        ]:
            stats[clave]["total"] += 1
            if es_ganada:
                stats[clave]["ganadas"] += 1

    resultado_final = {}
    for clave, data in sorted(stats.items()):
        if data["total"] >= min_muestras:
            wr = round(data["ganadas"] / data["total"] * 100, 1)
            resultado_final[clave] = {
                "win_rate": wr,
                "total":    data["total"],
                "ganadas":  data["ganadas"],
            }

    return resultado_final


# ─── OBTENER RESUMEN ─────────────────────────────────────
def obtener_resumen(dias=7):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")

    c.execute("""
        SELECT fecha, hora, direccion, score, confianza,
               resultado, ganancia_pips, precio_entry, tp, sl
        FROM señales
        WHERE fecha >= ?
        ORDER BY fecha DESC, hora DESC
    """, (desde,))
    señales = c.fetchall()

    c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN resultado='ganada'    THEN 1 ELSE 0 END) as ganadas,
            SUM(CASE WHEN resultado='perdida'   THEN 1 ELSE 0 END) as perdidas,
            SUM(CASE WHEN resultado='expirada'  THEN 1 ELSE 0 END) as expiradas,
            SUM(CASE WHEN resultado='pendiente' THEN 1 ELSE 0 END) as pendientes,
            AVG(CASE WHEN resultado IN ('ganada','perdida') THEN ganancia_pips END) as avg_pips,
            SUM(ganancia_pips) as total_pips,
            AVG(score) as avg_score
        FROM señales WHERE fecha >= ?
    """, (desde,))
    stats = c.fetchone()

    c.execute("""
        SELECT direccion,
               COUNT(*) as total,
               SUM(CASE WHEN resultado='ganada' THEN 1 ELSE 0 END) as ganadas
        FROM señales
        WHERE fecha >= ? AND resultado IN ('ganada','perdida')
        GROUP BY direccion
    """, (desde,))
    por_direccion = c.fetchall()

    c.execute("""
        SELECT substr(hora,1,2) as hora_dia,
               COUNT(*) as total,
               SUM(CASE WHEN resultado='ganada' THEN 1 ELSE 0 END) as ganadas
        FROM señales
        WHERE fecha >= ? AND resultado IN ('ganada','perdida')
        GROUP BY hora_dia
        ORDER BY ganadas DESC
        LIMIT 3
    """, (desde,))
    mejores_horas = c.fetchall()

    conn.close()

    return {
        "señales":       señales,
        "stats":         stats,
        "por_direccion": por_direccion,
        "mejores_horas": mejores_horas,
    }


# ─── IMPRIMIR REPORTE ────────────────────────────────────
def imprimir_reporte(dias=7):
    r     = obtener_resumen(dias)
    stats = r["stats"]

    print(f"\n{'='*45}")
    print(f"  REPORTE ÚLTIMOS {dias} DÍAS")
    print(f"{'='*45}")

    if stats and stats[0]:
        total      = stats[0] or 0
        ganadas    = stats[1] or 0
        perdidas   = stats[2] or 0
        expiradas  = stats[3] or 0
        pendientes = stats[4] or 0
        avg_pips   = round(stats[5] or 0, 2)
        total_pips = round(stats[6] or 0, 2)
        avg_score  = round(stats[7] or 0, 1)
        win_rate   = round((ganadas / (ganadas + perdidas)) * 100, 1) \
                     if (ganadas + perdidas) > 0 else 0

        print(f"  Total señales:  {total}")
        print(f"  ✅ Ganadas:     {ganadas}")
        print(f"  ❌ Perdidas:    {perdidas}")
        print(f"  ⏳ Expiradas:   {expiradas}")
        print(f"  🔄 Pendientes:  {pendientes}")
        print(f"  Win rate:       {win_rate}%")
        print(f"  Pips netos:     {total_pips:+.2f}")
        print(f"  Pips promedio:  {avg_pips:+.2f}")
        print(f"  Score promedio: {avg_score}%")

        # Threshold dinámico actual
        score_min = obtener_score_minimo_dinamico(ventana=30)
        print(f"  Score mínimo:   {score_min}% (dinámico)")
    else:
        print("  Sin señales aún")

    if r["por_direccion"]:
        print(f"\n  Por dirección:")
        for dir_data in r["por_direccion"]:
            direccion, total, ganadas = dir_data
            wr = round((ganadas / total) * 100, 1) if total > 0 else 0
            print(f"    {direccion}: {ganadas}/{total} ({wr}%)")

    if r["mejores_horas"]:
        print(f"\n  Mejores horas UTC:")
        for h in r["mejores_horas"]:
            hora, total, ganadas = h
            wr = round((ganadas / total) * 100, 1) if total > 0 else 0
            print(f"    {hora}:00 UTC → {wr}% win rate ({total} señales)")

    print(f"\n  Últimas señales:")
    for s in r["señales"][:5]:
        fecha, hora, dir, score, conf, res, pips, entry, tp, sl = s
        emoji = "✅" if res == "ganada"   else \
                "❌" if res == "perdida"  else \
                "⏳" if res == "pendiente" else "➡️"
        print(f"    {emoji} {fecha} {hora} | {dir} {score}% "
              f"| {res.upper()} {pips:+.2f} pips")
    print(f"{'='*45}\n")


# ─── IMPRIMIR ANÁLISIS DE EFICACIA ───────────────────────
def imprimir_analisis_eficacia():
    eficacia = analizar_eficacia_condiciones(min_muestras=3)
    if not eficacia:
        print("  Sin suficientes datos para análisis de eficacia")
        return

    print(f"\n{'='*45}")
    print("  EFICACIA POR CONDICIÓN")
    print(f"{'='*45}")

    grupos = {
        "Dirección":    [k for k in eficacia if k.startswith("dir_")],
        "Confianza":    [k for k in eficacia if k.startswith("conf_")],
        "TFs confluen.":[k for k in eficacia if k.startswith("tfs_")],
        "Hora UTC":     [k for k in eficacia if k.startswith("hora_")],
        "Rango score":  [k for k in eficacia if k.startswith("score_")],
        "Tipo entrada": [k for k in eficacia if k.startswith("entrada_")],
    }

    for grupo, claves in grupos.items():
        if not claves:
            continue
        print(f"\n  {grupo}:")
        for clave in sorted(claves, key=lambda k: eficacia[k]["win_rate"], reverse=True):
            d = eficacia[clave]
            barra = "█" * int(d["win_rate"] / 10)
            print(f"    {clave:<20} {d['win_rate']:>5.1f}%  {barra}  ({d['ganadas']}/{d['total']})")

    print(f"{'='*45}\n")


# ─── TEST ─────────────────────────────────────────────────
if __name__ == "__main__":
    inicializar_db()

    if MT5_DISPONIBLE:
        mt5.initialize()

    print("Verificando señales pendientes...")
    verificar_señales_pendientes()

    imprimir_reporte(dias=30)
    imprimir_analisis_eficacia()

    if MT5_DISPONIBLE:
        mt5.shutdown()
