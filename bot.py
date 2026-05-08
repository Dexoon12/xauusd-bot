import threading
import time
import requests
from datetime import datetime, timezone
from collections import deque

import yfinance as yf

# Intenta importar MT5 — si no está disponible usa yFinance
try:
    import MetaTrader5 as mt5
    MT5_DISPONIBLE = True
except ImportError:
    MT5_DISPONIBLE = False

from fase2_ict import calcular_score
from fase3_noticias import (
    obtener_calendario, obtener_noticias_oro,
    alerta_noticia_proxima
)
from fase4_alertas import (
    analizar_sentimiento_ia, calcular_score_final,
    calcular_setup, formatear_alerta, enviar_telegram,
    puede_alertar, ultima_alerta
)
from memoria import (
    inicializar_db, guardar_señal,
    verificar_señales_pendientes, imprimir_reporte
)

# ─── CONFIG ──────────────────────────────────────────────
SIMBOLO      = "XAUUSD"
SCORE_MINIMO = 72

# ─── ESTADO GLOBAL ───────────────────────────────────────
estado = {
    "precio":      0.0,
    "precio_prev": 0.0,
    "bid":         0.0,
    "ask":         0.0,
    "fuente":      "—",
    "conectado":   False,
    "ultimo_tick": "--",
}

cache = {
    "score_ict":      None,
    "score_final":    None,
    "sentimiento_ia": None,
    "noticias":       [],
    "calendario":     [],
    "alertas_hoy":    0,
}

# ─── FUENTE DE DATOS ─────────────────────────────────────
fuente_activa = "yfinance"

def iniciar_fuente():
    global fuente_activa
    if MT5_DISPONIBLE:
        if mt5.initialize():
            info = mt5.account_info()
            if info:
                mt5.symbol_select(SIMBOLO, True)
                estado["conectado"] = True
                estado["fuente"]    = f"MT5 — {info.company}"
                fuente_activa       = "mt5"
                print(f"✅ MT5 conectado: {info.name} | {info.company}")
                return "mt5"
    print("⚠️  MT5 no disponible — usando yFinance")
    estado["fuente"]    = "yFinance"
    estado["conectado"] = True
    fuente_activa       = "yfinance"
    return "yfinance"

def obtener_precio_actual():
    try:
        if fuente_activa == "mt5":
            tick = mt5.symbol_info_tick(SIMBOLO)
            if tick and tick.bid > 0:
                return tick.bid, tick.ask
        # yFinance fallback
        precio = yf.Ticker("GC=F").fast_info["last_price"]
        return precio, precio + 0.20
    except Exception as e:
        print(f"Error obteniendo precio: {e}")
        return None, None

def obtener_velas_para_ict(timeframe_str="15m"):
    """Obtiene velas para el análisis ICT"""
    try:
        if fuente_activa == "mt5":
            import pandas as pd
            mapa = {
                "1m":  mt5.TIMEFRAME_M1,
                "5m":  mt5.TIMEFRAME_M5,
                "15m": mt5.TIMEFRAME_M15,
                "1h":  mt5.TIMEFRAME_H1,
                "4h":  mt5.TIMEFRAME_H4,
                "1d":  mt5.TIMEFRAME_D1,
            }
            tf    = mapa.get(timeframe_str, mt5.TIMEFRAME_M15)
            velas = mt5.copy_rates_from_pos(SIMBOLO, tf, 0, 300)
            df    = pd.DataFrame(velas)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            return df.set_index("time")
        else:
            import pandas as pd
            periodo = "1d" if timeframe_str in ["1m","5m"] else "5d"
            df = yf.Ticker("GC=F").history(
                interval=timeframe_str, period=periodo
            )
            df.columns = [c.lower() for c in df.columns]
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            return df
    except Exception as e:
        print(f"Error obteniendo velas: {e}")
        return None

# ─── LOOP PRECIO ─────────────────────────────────────────
def loop_precio():
    """Actualiza el precio cada 2 segundos"""
    print("Loop precio iniciado")
    while True:
        try:
            bid, ask = obtener_precio_actual()
            if bid and bid > 0:
                estado["precio_prev"] = estado["precio"]
                estado["precio"]      = bid
                estado["bid"]         = bid
                estado["ask"]         = ask
                estado["ultimo_tick"] = datetime.now().strftime(
                    "%H:%M:%S"
                )
        except Exception as e:
            print(f"Error precio: {e}")
        time.sleep(2)

# ─── LOOP ICT ────────────────────────────────────────────
def loop_ict():
    """Calcula análisis ICT cada 5 minutos"""
    print("Loop ICT iniciado")
    while True:
        try:
            ahora = datetime.now().strftime("%H:%M:%S")
            print(f"[{ahora}] Calculando ICT...")
            resultado = calcular_score(SIMBOLO)
            cache["score_ict"] = resultado
            print(f"  ICT: {resultado['direccion']} "
                  f"{resultado['score']}% | "
                  f"TFs: {sum(1 for d in resultado['por_tf'].values() if d['tendencia'] == ('alcista' if resultado['direccion']=='LONG' else 'bajista'))}/4")
        except Exception as e:
            print(f"Error ICT: {e}")
        time.sleep(300)  # cada 5 minutos

# ─── LOOP NOTICIAS Y ALERTAS ─────────────────────────────
def loop_noticias_alertas():
    """Analiza noticias y envía alertas cada 30 minutos"""
    print("Loop noticias+alertas iniciado")
    time.sleep(20)  # espera que ICT tenga datos

    while True:
        try:
            ahora = datetime.now().strftime("%H:%M:%S")
            print(f"\n[{ahora}] Analizando noticias...")

            # Obtener noticias y calendario
            calendario = obtener_calendario()
            noticias   = obtener_noticias_oro()
            cache["calendario"] = calendario
            cache["noticias"]   = noticias

            print(f"  Noticias encontradas: {len(noticias)}")

            if not noticias:
                print("  Sin noticias — saltando análisis")
                time.sleep(1800)
                continue

            # Analizar con Claude IA
            print("  Analizando con Claude IA...")
            sentimiento_ia = analizar_sentimiento_ia(noticias)
            cache["sentimiento_ia"] = sentimiento_ia

            print(f"  IA: {sentimiento_ia['direccion'].upper()} "
                  f"{sentimiento_ia['score']}%")
            print(f"  {sentimiento_ia['resumen'][:80]}...")

            # Necesitamos ICT para el score final
            score_ict = cache.get("score_ict")
            if not score_ict:
                print("  ICT no listo aún — esperando...")
                time.sleep(1800)
                continue

            # Score final combinado
            sf = calcular_score_final(
                score_ict, sentimiento_ia, calendario
            )
            cache["score_final"] = sf

            print(f"  Score final: {sf['direccion']} "
                  f"{sf['score']}% | "
                  f"{sf['confianza']} | "
                  f"{sf['tfs_confluencia']}/4 TFs")

            # Evento económico próximo
            hay_noticia, evento, diff_min = alerta_noticia_proxima(calendario)
            if hay_noticia and evento:
                estado_news = "YA OCURRIÓ" if diff_min is not None and diff_min < 0 else "PRÓXIMO"
                print(f"  ⚠️  EVENTO {estado_news}: {evento['titulo']} "
                      f"a las {evento['hora']}")

            # ¿Cumple condiciones para alertar?
            precio = estado["precio"]
            condiciones = (
                sf["score"] >= SCORE_MINIMO and
                sf["confianza"] in ["ALTA", "MEDIA"] and
                sf["tfs_confluencia"] >= 2 and
                precio > 0 and
                puede_alertar(sf["direccion"])
            )

            if condiciones:
                print(f"\n🚨 CONDICIONES CUMPLIDAS — enviando alerta")

                setup   = calcular_setup(
                    precio, sf["direccion"], score_ict
                )
                mensaje = formatear_alerta(
                    sf, setup, sentimiento_ia, score_ict, precio
                )

                # Enviar a Telegram
                enviar_telegram(mensaje)
                cache["alertas_hoy"] += 1

                # Guardar en memoria
                señal_id = guardar_señal(
                    sf, setup, sentimiento_ia, score_ict
                )
                print(f"  Señal guardada con ID: {señal_id}")
                print(f"  Total alertas hoy: {cache['alertas_hoy']}")

                # Actualizar cooldown
                ultima_alerta["tiempo"]    = datetime.now(timezone.utc)
                ultima_alerta["direccion"] = sf["direccion"]

            else:
                razones = []
                if sf["score"] < SCORE_MINIMO:
                    razones.append(f"score {sf['score']}% < {SCORE_MINIMO}%")
                if sf["confianza"] not in ["ALTA", "MEDIA"]:
                    razones.append(f"confianza {sf['confianza']}")
                if sf["tfs_confluencia"] < 2:
                    razones.append(f"solo {sf['tfs_confluencia']}/4 TFs")
                if not puede_alertar(sf["direccion"]):
                    razones.append("en cooldown")
                print(f"  Sin señal: {' | '.join(razones)}")

        except Exception as e:
            print(f"Error noticias/alertas: {e}")

        print(f"  Próximo análisis en 30 minutos\n")
        time.sleep(1800)

# ─── LOOP VERIFICACIÓN ───────────────────────────────────
def loop_verificacion():
    """Verifica TP/SL de señales pendientes cada 5 min"""
    print("Loop verificación iniciado")
    time.sleep(60)
    while True:
        try:
            verificar_señales_pendientes()
        except Exception as e:
            print(f"Error verificación: {e}")
        time.sleep(300)

# ─── LOOP REPORTE DIARIO ─────────────────────────────────
def loop_reporte_diario():
    """Envía reporte por Telegram una vez al día a las 23:00 UTC"""
    while True:
        try:
            ahora = datetime.now(timezone.utc)
            if ahora.hour == 23 and ahora.minute == 0:
                print("Enviando reporte diario...")
                from memoria import obtener_resumen
                resumen = obtener_resumen(dias=1)
                stats   = resumen["stats"]

                if stats and stats[0]:
                    total    = stats[0] or 0
                    ganadas  = stats[1] or 0
                    perdidas = stats[2] or 0
                    pips     = round(stats[6] or 0, 2)
                    win_rate = round(
                        (ganadas/(ganadas+perdidas))*100, 1
                    ) if (ganadas+perdidas) > 0 else 0

                    c_pip = "📈" if pips >= 0 else "📉"
                    msg   = (
                        f"📊 <b>Reporte diario XAUUSD</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Señales hoy: {total}\n"
                        f"✅ Ganadas:  {ganadas}\n"
                        f"❌ Perdidas: {perdidas}\n"
                        f"Win rate:   {win_rate}%\n"
                        f"{c_pip} Pips netos: {pips:+.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"Fuente: {estado['fuente']}\n"
                        f"Alertas enviadas: {cache['alertas_hoy']}\n"
                        f"🕐 {ahora.strftime('%d/%m/%Y')}"
                    )
                    enviar_telegram(msg)
                    cache["alertas_hoy"] = 0  # resetea contador

                time.sleep(61)  # evita doble envío
        except Exception as e:
            print(f"Error reporte diario: {e}")
        time.sleep(30)

# ─── STATUS EN CONSOLA ───────────────────────────────────
def loop_status():
    """Muestra status cada 10 minutos en consola"""
    while True:
        time.sleep(600)
        try:
            sf  = cache.get("score_final")
            ict = cache.get("score_ict")
            sia = cache.get("sentimiento_ia")

            print(f"\n{'─'*50}")
            print(f"STATUS [{datetime.now().strftime('%H:%M:%S')}]")
            print(f"  Precio:  {estado['precio']} "
                  f"({estado['fuente']})")
            if ict:
                print(f"  ICT:     {ict['direccion']} {ict['score']}%")
            if sia:
                print(f"  Noticias: {sia['direccion'].upper()} "
                      f"{sia['score']}%")
            if sf:
                print(f"  Score:   {sf['direccion']} {sf['score']}% "
                      f"| {sf['confianza']}")
            print(f"  Alertas hoy: {cache['alertas_hoy']}")
            print(f"{'─'*50}\n")
        except Exception:
            pass

# ─── MAIN ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 50)
    print("  XAUUSD ICT BOT")
    print(f"  {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    print("=" * 50)

    # Inicializar base de datos
    inicializar_db()

    # Iniciar fuente de datos
    fuente_activa = iniciar_fuente()

    # Mostrar reporte histórico al arrancar
    print()
    imprimir_reporte(dias=7)

    # Cálculo ICT inicial (síncrono para tener datos desde el inicio)
    print("Calculando ICT inicial...")
    try:
        cache["score_ict"] = calcular_score(SIMBOLO)
        ict = cache["score_ict"]
        print(f"ICT: {ict['direccion']} {ict['score']}%\n")
    except Exception as e:
        print(f"Error ICT inicial: {e}\n")

    # Iniciar todos los hilos
    hilos = [
        threading.Thread(target=loop_precio,          daemon=True),
        threading.Thread(target=loop_ict,             daemon=True),
        threading.Thread(target=loop_noticias_alertas,daemon=True),
        threading.Thread(target=loop_verificacion,    daemon=True),
        threading.Thread(target=loop_reporte_diario,  daemon=True),
        threading.Thread(target=loop_status,          daemon=True),
    ]

    for hilo in hilos:
        hilo.start()

    print("Bot corriendo. Ctrl+C para detener.\n")

    # Mantener el proceso vivo
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nBot detenido.")
        if fuente_activa == "mt5" and MT5_DISPONIBLE:
            mt5.shutdown()