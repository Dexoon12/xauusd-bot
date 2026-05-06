import sqlite3
import json
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5

DB_PATH = "bot_memoria.db"

# ─── CREAR BASE DE DATOS ─────────────────────────────────
def inicializar_db():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    # Tabla de señales enviadas
    c.execute("""
        CREATE TABLE IF NOT EXISTS señales (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha           TEXT,
            hora            TEXT,
            direccion       TEXT,
            score           INTEGER,
            confianza       TEXT,
            tfs_confluencia INTEGER,
            precio_entry    REAL,
            sl              REAL,
            tp              REAL,
            rr              REAL,
            score_ict       INTEGER,
            score_noticias  INTEGER,
            resumen_noticias TEXT,
            tfs_detalle     TEXT,
            resultado       TEXT DEFAULT 'pendiente',
            precio_cierre   REAL DEFAULT 0,
            ganancia_pips   REAL DEFAULT 0,
            duracion_minutos INTEGER DEFAULT 0,
            hora_cierre     TEXT DEFAULT ''
        )
    """)

    # Tabla de estadísticas diarias
    c.execute("""
        CREATE TABLE IF NOT EXISTS estadisticas_diarias (
            fecha       TEXT PRIMARY KEY,
            total       INTEGER DEFAULT 0,
            ganadas     INTEGER DEFAULT 0,
            perdidas    INTEGER DEFAULT 0,
            pendientes  INTEGER DEFAULT 0,
            win_rate    REAL DEFAULT 0,
            pips_netos  REAL DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()
    print("Base de datos inicializada")


# ─── GUARDAR SEÑAL ───────────────────────────────────────
def guardar_señal(score_final, setup, sentimiento_ia, score_ict):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    ahora = datetime.now(timezone.utc)

    # Detalle de TFs como JSON
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
            score_ict, score_noticias, resumen_noticias, tfs_detalle
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        json.dumps(tfs_detalle)
    ))

    señal_id = c.lastrowid
    conn.commit()
    conn.close()

    print(f"Señal guardada con ID: {señal_id}")
    return señal_id


# ─── VERIFICAR RESULTADO ─────────────────────────────────
def verificar_señales_pendientes():
    """
    Revisa señales pendientes y verifica si
    llegaron al TP o SL según el precio actual de MT5
    """
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

    # Precio actual
    tick = mt5.symbol_info_tick("XAUUSD")
    if not tick:
        conn.close()
        return

    precio_actual = tick.bid
    ahora         = datetime.now(timezone.utc)

    for señal in pendientes:
        id_señal, direccion, entry, sl, tp, hora, fecha = señal

        # Calcular tiempo transcurrido
        try:
            dt_señal = datetime.strptime(
                f"{fecha} {hora}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
            minutos = int((ahora - dt_señal).total_seconds() / 60)
        except Exception:
            minutos = 0

        resultado      = None
        precio_cierre  = 0
        ganancia_pips  = 0

        if direccion == "LONG":
            if precio_actual >= tp:
                resultado     = "ganada"
                precio_cierre = tp
                ganancia_pips = round(tp - entry, 2)
            elif precio_actual <= sl:
                resultado     = "perdida"
                precio_cierre = sl
                ganancia_pips = round(sl - entry, 2)
            elif minutos >= 240:  # 4 horas sin resolver → expirada
                resultado     = "expirada"
                precio_cierre = precio_actual
                ganancia_pips = round(precio_actual - entry, 2)

        else:  # SHORT
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
                    ganancia_pips=?, duracion_minutos=?,
                    hora_cierre=?
                WHERE id=?
            """, (
                resultado, precio_cierre,
                ganancia_pips, minutos,
                ahora.strftime("%H:%M:%S"),
                id_señal
            ))
            print(f"Señal #{id_señal}: {resultado.upper()} "
                  f"| {ganancia_pips:+.2f} pips "
                  f"| {minutos} min")

    conn.commit()
    conn.close()
    actualizar_estadisticas()


# ─── ACTUALIZAR ESTADÍSTICAS ─────────────────────────────
def actualizar_estadisticas():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    # Por cada fecha con señales
    c.execute("SELECT DISTINCT fecha FROM señales")
    fechas = [r[0] for r in c.fetchall()]

    for fecha in fechas:
        c.execute("""
            SELECT resultado, ganancia_pips
            FROM señales WHERE fecha=?
        """, (fecha,))
        rows = c.fetchall()

        total     = len(rows)
        ganadas   = sum(1 for r in rows if r[0] == "ganada")
        perdidas  = sum(1 for r in rows if r[0] == "perdida")
        pendientes= sum(1 for r in rows if r[0] == "pendiente")
        pips      = sum(r[1] for r in rows if r[0] in ["ganada","perdida"])
        win_rate  = round((ganadas / (ganadas+perdidas)) * 100, 1) \
                    if (ganadas+perdidas) > 0 else 0

        c.execute("""
            INSERT OR REPLACE INTO estadisticas_diarias
            (fecha, total, ganadas, perdidas, pendientes, win_rate, pips_netos)
            VALUES (?,?,?,?,?,?,?)
        """, (fecha, total, ganadas, perdidas, pendientes, win_rate, pips))

    conn.commit()
    conn.close()


# ─── OBTENER RESUMEN ─────────────────────────────────────
def obtener_resumen(dias=7):
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()

    desde = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")

    # Señales recientes
    c.execute("""
        SELECT fecha, hora, direccion, score, confianza,
               resultado, ganancia_pips, precio_entry, tp, sl
        FROM señales
        WHERE fecha >= ?
        ORDER BY fecha DESC, hora DESC
    """, (desde,))
    señales = c.fetchall()

    # Estadísticas globales
    c.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN resultado='ganada'  THEN 1 ELSE 0 END) as ganadas,
            SUM(CASE WHEN resultado='perdida' THEN 1 ELSE 0 END) as perdidas,
            SUM(CASE WHEN resultado='expirada' THEN 1 ELSE 0 END) as expiradas,
            SUM(CASE WHEN resultado='pendiente' THEN 1 ELSE 0 END) as pendientes,
            AVG(CASE WHEN resultado IN ('ganada','perdida')
                THEN ganancia_pips END) as avg_pips,
            SUM(ganancia_pips) as total_pips,
            AVG(score) as avg_score
        FROM señales WHERE fecha >= ?
    """, (desde,))
    stats = c.fetchone()

    # Win rate por dirección
    c.execute("""
        SELECT direccion,
               COUNT(*) as total,
               SUM(CASE WHEN resultado='ganada' THEN 1 ELSE 0 END) as ganadas
        FROM señales
        WHERE fecha >= ? AND resultado IN ('ganada','perdida')
        GROUP BY direccion
    """, (desde,))
    por_direccion = c.fetchall()

    # Mejor hora del día
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
        "señales":        señales,
        "stats":          stats,
        "por_direccion":  por_direccion,
        "mejores_horas":  mejores_horas,
    }


# ─── IMPRIMIR REPORTE ────────────────────────────────────
def imprimir_reporte(dias=7):
    r     = obtener_resumen(dias)
    stats = r["stats"]

    print(f"\n{'='*45}")
    print(f"  REPORTE ÚLTIMOS {dias} DÍAS")
    print(f"{'='*45}")

    if stats and stats[0]:
        total     = stats[0] or 0
        ganadas   = stats[1] or 0
        perdidas  = stats[2] or 0
        expiradas = stats[3] or 0
        pendientes= stats[4] or 0
        avg_pips  = round(stats[5] or 0, 2)
        total_pips= round(stats[6] or 0, 2)
        avg_score = round(stats[7] or 0, 1)
        win_rate  = round((ganadas/(ganadas+perdidas))*100, 1) \
                    if (ganadas+perdidas) > 0 else 0

        print(f"  Total señales:  {total}")
        print(f"  ✅ Ganadas:     {ganadas}")
        print(f"  ❌ Perdidas:    {perdidas}")
        print(f"  ⏳ Expiradas:   {expiradas}")
        print(f"  🔄 Pendientes:  {pendientes}")
        print(f"  Win rate:       {win_rate}%")
        print(f"  Pips netos:     {total_pips:+.2f}")
        print(f"  Pips promedio:  {avg_pips:+.2f}")
        print(f"  Score promedio: {avg_score}%")
    else:
        print("  Sin señales aún")

    if r["por_direccion"]:
        print(f"\n  Por dirección:")
        for dir_data in r["por_direccion"]:
            direccion, total, ganadas = dir_data
            wr = round((ganadas/total)*100,1) if total > 0 else 0
            print(f"    {direccion}: {ganadas}/{total} ({wr}%)")

    if r["mejores_horas"]:
        print(f"\n  Mejores horas UTC:")
        for h in r["mejores_horas"]:
            hora, total, ganadas = h
            wr = round((ganadas/total)*100,1) if total > 0 else 0
            print(f"    {hora}:00 UTC → {wr}% win rate ({total} señales)")

    print(f"\n  Últimas señales:")
    for s in r["señales"][:5]:
        fecha, hora, dir, score, conf, res, pips, entry, tp, sl = s
        emoji = "✅" if res=="ganada" else \
                "❌" if res=="perdida" else \
                "⏳" if res=="pendiente" else "➡️"
        print(f"    {emoji} {fecha} {hora} | {dir} {score}% "
              f"| {res.upper()} {pips:+.2f} pips")
    print(f"{'='*45}\n")


# ─── TEST ─────────────────────────────────────────────────
if __name__ == "__main__":
    inicializar_db()

    mt5.initialize()

    print("Verificando señales pendientes...")
    verificar_señales_pendientes()

    imprimir_reporte(dias=30)

    mt5.shutdown()