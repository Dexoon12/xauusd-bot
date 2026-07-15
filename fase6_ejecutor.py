"""
Auto-ejecutor MT5 para el bot XAUUSD.

Riesgo fijo por confianza:
  ALTA:  $20 de riesgo → TP esperado ~$60 (1:3 RR)
  MEDIA: $10 de riesgo → TP esperado ~$30 (1:3 RR)
  BAJA:  no ejecuta automáticamente

Lote dinámico según SL real:
  lote = riesgo_dolares / (puntos_sl × 100)
  mínimo 0.01 lotes

Límites de seguridad:
  - Máximo 1 posición/orden abierta a la vez (bot)
  - Pérdida máxima diaria $50
  - RR mínimo 2.0
  - Solo MEDIA y ALTA
"""

from datetime import datetime, timezone, date, timedelta

try:
    import MetaTrader5 as mt5
    MT5_DISPONIBLE = True
except ImportError:
    MT5_DISPONIBLE = False
    mt5 = None

# ─── CONFIG ──────────────────────────────────────────────
SIMBOLO            = "XAUUSD"
MAGIC_NUMBER       = 20260714   # identifica órdenes del bot en MT5
MAX_POSICIONES     = 1          # máximo 1 operación abierta a la vez
MAX_PERDIDA_DIARIA = 50.0       # para el bot (no la cuenta total)
RIESGO_ALTA        = 20.0       # dólares de riesgo en señal ALTA
RIESGO_MEDIA       = 10.0       # dólares de riesgo en señal MEDIA
LOTE_MINIMO        = 0.01
RR_MINIMO          = 2.0
DEVIATION          = 20         # slippage máximo en puntos


# ─── CÁLCULO DE LOTE ─────────────────────────────────────
def calcular_lote(riesgo_dolares, puntos_sl):
    """
    XAUUSD: 0.01 lotes = 1 oz → P&L = lote × 100 × puntos
    Despejando: lote = riesgo / (puntos_sl × 100)

    Ejemplos:
      $10 riesgo, SL 10pts → 0.01 lotes → $10 riesgo real
      $20 riesgo, SL 10pts → 0.02 lotes → $20 riesgo real
      $10 riesgo, SL 20pts → 0.005 → min 0.01 ($20 riesgo, ligeramente mayor)
    """
    if puntos_sl <= 0:
        return LOTE_MINIMO
    lote = riesgo_dolares / (puntos_sl * 100)
    lote = round(round(lote / 0.01) * 0.01, 2)
    return max(LOTE_MINIMO, lote)


# ─── ESTADO POSICIONES ───────────────────────────────────
def get_posiciones_bot():
    """Retorna posiciones abiertas y órdenes pendientes del bot."""
    if not MT5_DISPONIBLE or not mt5:
        return [], []
    try:
        posiciones = mt5.positions_get(symbol=SIMBOLO) or []
        ordenes    = mt5.orders_get(symbol=SIMBOLO)    or []
        posiciones = [p for p in posiciones if p.magic == MAGIC_NUMBER]
        ordenes    = [o for o in ordenes    if o.magic == MAGIC_NUMBER]
        return list(posiciones), list(ordenes)
    except Exception as e:
        print(f"Error get_posiciones_bot: {e}")
        return [], []


def hay_operacion_abierta():
    """True si el bot tiene posición abierta O orden pendiente."""
    pos, ords = get_posiciones_bot()
    return len(pos) > 0 or len(ords) > 0


# ─── PÉRDIDA DIARIA ──────────────────────────────────────
def perdida_diaria():
    """
    Consulta el historial de deals del día y suma P&L del bot.
    Retorna el P&L como float (negativo = pérdida).
    """
    if not MT5_DISPONIBLE or not mt5:
        return 0.0
    try:
        hoy   = datetime.combine(date.today(), datetime.min.time())
        manana = hoy + timedelta(days=1)
        deals = mt5.history_deals_get(hoy, manana) or []
        pnl   = sum(d.profit for d in deals if d.magic == MAGIC_NUMBER)
        return round(pnl, 2)
    except Exception as e:
        print(f"Error perdida_diaria: {e}")
        return 0.0


# ─── VALIDACIÓN PREVIA ───────────────────────────────────
def puede_ejecutar(sf, setup):
    """
    Verifica todas las condiciones antes de enviar orden.
    Retorna (bool, razon_str).
    """
    if not MT5_DISPONIBLE or not mt5:
        return False, "MT5 no disponible"

    if sf["confianza"] not in ["ALTA", "MEDIA"]:
        return False, f"confianza {sf['confianza']} no ejecuta automáticamente"

    if setup["rr"] < RR_MINIMO:
        return False, f"RR {setup['rr']}:1 < mínimo {RR_MINIMO}:1"

    if hay_operacion_abierta():
        return False, "ya hay operación abierta"

    pnl_hoy = perdida_diaria()
    if pnl_hoy <= -MAX_PERDIDA_DIARIA:
        return False, f"límite diario alcanzado (P&L hoy: ${pnl_hoy:.2f})"

    # Verificar que MT5 sigue conectado
    info = mt5.account_info()
    if not info:
        return False, "MT5 desconectado"

    return True, "ok"


# ─── ENVIAR ORDEN ────────────────────────────────────────
def ejecutar_orden(sf, setup, precio_actual):
    """
    Envía la orden a MT5.
    Retorna dict con resultado.
    """
    if not MT5_DISPONIBLE or not mt5:
        return {"exito": False, "error": "MT5 no disponible", "ticket": None}

    direccion  = sf["direccion"]
    confianza  = sf["confianza"]
    tipo_entry = setup.get("tipo_entrada", "MARKET")

    # Lote según confianza
    riesgo_d = RIESGO_ALTA if confianza == "ALTA" else RIESGO_MEDIA
    lote     = calcular_lote(riesgo_d, setup["riesgo"])

    comment = f"ICT {confianza} {sf['score']}% RR1:{setup['rr']}"

    # Tipo de orden
    if tipo_entry == "LIMIT":
        action = mt5.TRADE_ACTION_PENDING
        tipo   = mt5.ORDER_TYPE_BUY_LIMIT  if direccion == "LONG" else mt5.ORDER_TYPE_SELL_LIMIT
        precio = setup["entry"]
    else:
        action = mt5.TRADE_ACTION_DEAL
        tipo   = mt5.ORDER_TYPE_BUY if direccion == "LONG" else mt5.ORDER_TYPE_SELL
        tick   = mt5.symbol_info_tick(SIMBOLO)
        if not tick:
            return {"exito": False, "error": "no se pudo obtener tick", "ticket": None}
        precio = tick.ask if direccion == "LONG" else tick.bid

    request = {
        "action":       action,
        "symbol":       SIMBOLO,
        "volume":       lote,
        "type":         tipo,
        "price":        precio,
        "sl":           setup["sl"],
        "tp":           setup["tp"],
        "deviation":    DEVIATION,
        "magic":        MAGIC_NUMBER,
        "comment":      comment,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    # LIMIT orders necesitan caducidad
    if tipo_entry == "LIMIT":
        request["type_time"] = mt5.ORDER_TIME_DAY

    resultado = mt5.order_send(request)

    if resultado is None:
        error = str(mt5.last_error())
        return {"exito": False, "error": error, "ticket": None}

    if resultado.retcode == mt5.TRADE_RETCODE_DONE:
        ticket = resultado.order
        print(f"  ✅ Orden ejecutada | Ticket: {ticket} | {lote} lotes | "
              f"{tipo_entry} {direccion} @ {precio} | SL {setup['sl']} | TP {setup['tp']}")
        return {
            "exito":      True,
            "ticket":     ticket,
            "lote":       lote,
            "riesgo_usd": round(lote * 100 * setup["riesgo"], 2),
            "tp_usd":     round(lote * 100 * setup["reward"],  2),
            "precio":     precio,
            "tipo":       tipo_entry,
        }
    else:
        error = f"retcode {resultado.retcode}: {resultado.comment}"
        print(f"  ❌ Error MT5: {error}")
        # Reintento con ORDER_FILLING_FOK si fue problema de filling
        if resultado.retcode == 10030:
            request["type_filling"] = mt5.ORDER_FILLING_FOK
            resultado2 = mt5.order_send(request)
            if resultado2 and resultado2.retcode == mt5.TRADE_RETCODE_DONE:
                return {
                    "exito":      True,
                    "ticket":     resultado2.order,
                    "lote":       lote,
                    "riesgo_usd": round(lote * 100 * setup["riesgo"], 2),
                    "tp_usd":     round(lote * 100 * setup["reward"],  2),
                    "precio":     precio,
                    "tipo":       tipo_entry,
                }
        return {"exito": False, "error": error, "ticket": None}


# ─── CANCELAR ÓRDENES PENDIENTES BOT ─────────────────────
def cancelar_ordenes_pendientes():
    """Cancela todas las órdenes pendientes del bot (fin de sesión, etc.)."""
    if not MT5_DISPONIBLE or not mt5:
        return 0
    _, ordenes = get_posiciones_bot()
    canceladas = 0
    for orden in ordenes:
        req = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order":  orden.ticket,
        }
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            canceladas += 1
            print(f"  Orden #{orden.ticket} cancelada")
    return canceladas


# ─── RESUMEN ESTADO ──────────────────────────────────────
def resumen_estado():
    """Retorna string con estado actual del ejecutor."""
    if not MT5_DISPONIBLE or not mt5:
        return "MT5 no disponible"

    pos, ords = get_posiciones_bot()
    pnl       = perdida_diaria()
    lineas    = [f"P&L hoy: ${pnl:+.2f} | Límite: ${MAX_PERDIDA_DIARIA}"]

    for p in pos:
        lado = "BUY" if p.type == 0 else "SELL"
        lineas.append(f"  Posición #{p.ticket}: {lado} {p.volume} lotes | "
                      f"P&L: ${p.profit:+.2f}")

    for o in ords:
        tipo = "BUY LIMIT" if o.type == 2 else "SELL LIMIT"
        lineas.append(f"  Pendiente #{o.ticket}: {tipo} @ {o.price_open}")

    if not pos and not ords:
        lineas.append("  Sin operaciones abiertas")

    return "\n".join(lineas)


# ─── FORMATEAR MENSAJE DE EJECUCIÓN PARA TELEGRAM ────────
def formatear_ejecucion(resultado_orden, sf, setup):
    """Genera mensaje Telegram confirmando ejecución."""
    if not resultado_orden["exito"]:
        return (f"❌ <b>Error al ejecutar orden</b>\n"
                f"{resultado_orden['error']}")

    emoji = "🟢" if sf["direccion"] == "LONG" else "🔴"
    tipo  = resultado_orden["tipo"]

    return (
        f"{emoji} <b>ORDEN ENVIADA A MT5</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  Tipo:    {tipo} {sf['direccion']}\n"
        f"  Lote:    {resultado_orden['lote']}\n"
        f"  Precio:  {resultado_orden['precio']}\n"
        f"  SL:      {setup['sl']}  (riesgo ${resultado_orden['riesgo_usd']})\n"
        f"  TP:      {setup['tp']}  (ganancia ${resultado_orden['tp_usd']})\n"
        f"  Ticket:  #{resultado_orden['ticket']}\n"
        f"  Confianza: {sf['confianza']} {sf['score']}%"
    )


# ─── TEST ─────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 45)
    print("  FASE 6 — EJECUTOR MT5")
    print("=" * 45)

    if not MT5_DISPONIBLE:
        print("MT5 no instalado — solo disponible con MetaTrader5 package")
    else:
        if mt5.initialize():
            print(resumen_estado())
            print(f"\nPuede ejecutar: {puede_ejecutar({'confianza': 'MEDIA', 'score': 72, 'direccion': 'SHORT'}, {'riesgo': 10, 'reward': 25, 'rr': 2.5, 'tipo_entrada': 'LIMIT', 'entry': 4200.0, 'sl': 4210.0, 'tp': 4175.0})}")
            mt5.shutdown()
        else:
            print("No se pudo conectar a MT5")
