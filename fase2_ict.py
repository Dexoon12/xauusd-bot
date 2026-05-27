import pandas as pd
import numpy as np

try:
    import MetaTrader5 as mt5
    MT5_DISPONIBLE = True
except ImportError:
    MT5_DISPONIBLE = False
    mt5 = None


# ─── OBTENER VELAS POR TIMEFRAME ─────────────────────────
def get_velas(simbolo="XAUUSD", timeframe=None, cantidad=200):
    try:
        if MT5_DISPONIBLE and mt5:
            if timeframe is None:
                timeframe = mt5.TIMEFRAME_M15
            velas = mt5.copy_rates_from_pos(simbolo, timeframe, 0, cantidad)
            if velas is None:
                return None
            df = pd.DataFrame(velas)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df = df.set_index("time")
            df = df[["open", "high", "low", "close", "tick_volume"]].copy()
            df.rename(columns={"tick_volume": "volume"}, inplace=True)
            return df
        else:
            import yfinance as yf
            mapa = {1440: "1d", 240: "4h", 60: "1h", 15: "15m", 5: "5m", 1: "1m"}
            if timeframe is None:
                timeframe = 15
            intervalo = mapa.get(timeframe, "15m")
            periodo   = "1d" if intervalo in ["1m", "5m"] else "5d"
            df = yf.Ticker("GC=F").history(interval=intervalo, period=periodo)
            df.columns = [c.lower() for c in df.columns]
            df = df[["open", "high", "low", "close", "volume"]].copy()
            if df.index.tz is not None:
                df.index = df.index.tz_localize(None)
            return df
    except Exception as e:
        print(f"Error get_velas: {e}")
        return None


# ─── ATR ──────────────────────────────────────────────────
def calcular_atr(df, periodo=14):
    high = df["high"]
    low  = df["low"]
    prev = df["close"].shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev).abs(),
        (low  - prev).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(periodo).mean().iloc[-1]
    return round(float(val), 4) if not np.isnan(val) else 5.0


# ─── 1. ORDER BLOCKS ─────────────────────────────────────
# Cambios vs versión anterior:
# - Filtro de proximidad: solo OBs dentro de 3x ATR del precio actual
# - Proximidad ponderada (0-1): OBs más cercanos pesan más en el score
# - Velas doji ignoradas (rango < 10% ATR)
# - Mitigation fix: bajista se mitiga cuando precio supera su HIGH (no low),
#   alcista cuando precio cae bajo su LOW (no high)
def detectar_order_blocks(df, lookback=50, precio_actual=None, atr=None):
    obs = []
    df  = df.tail(lookback).copy()

    if precio_actual is None:
        precio_actual = float(df["close"].iloc[-1])
    if atr is None:
        atr = calcular_atr(df)

    rango_prox = atr * 3.0

    for i in range(2, len(df) - 1):
        va = df.iloc[i]
        vs = df.iloc[i + 1]

        rango_va = abs(float(va["close"]) - float(va["open"]))
        rango_vs = abs(float(vs["close"]) - float(vs["open"]))

        if rango_va < atr * 0.1:
            continue

        # OB Bajista: vela alcista → vela bajista grande (resistencia arriba del precio)
        if (va["close"] > va["open"] and
            vs["close"] < vs["open"] and
            rango_vs > rango_va * 1.5):

            ob_high = float(va["high"])
            ob_low  = float(va["low"])
            # válido solo si el OB está por encima del precio (precio debe subir hasta él)
            # dist = distancia desde precio hasta el piso del OB (0 si está dentro/encima)
            dist = max(0.0, ob_low - precio_actual)
            if dist <= rango_prox and precio_actual <= ob_high:
                obs.append({
                    "tipo":       "bajista",
                    "time":       df.index[i],
                    "high":       ob_high,
                    "low":        ob_low,
                    "open":       float(va["open"]),
                    "close":      float(va["close"]),
                    "mitigado":   False,
                    "distancia":  round(dist, 2),
                    "proximidad": round(1 - (dist / rango_prox), 3),
                })

        # OB Alcista: vela bajista → vela alcista grande (soporte abajo del precio)
        if (va["close"] < va["open"] and
            vs["close"] > vs["open"] and
            rango_vs > rango_va * 1.5):

            ob_high = float(va["high"])
            ob_low  = float(va["low"])
            # válido solo si el OB está por debajo del precio (precio debe bajar hasta él)
            dist = max(0.0, precio_actual - ob_high)
            if dist <= rango_prox and precio_actual >= ob_low:
                obs.append({
                    "tipo":       "alcista",
                    "time":       df.index[i],
                    "high":       ob_high,
                    "low":        ob_low,
                    "open":       float(va["open"]),
                    "close":      float(va["close"]),
                    "mitigado":   False,
                    "distancia":  round(dist, 2),
                    "proximidad": round(1 - (dist / rango_prox), 3),
                })

    # Mitigation correcta: OB bajista roto cuando precio supera su high;
    # OB alcista roto cuando precio cae bajo su low
    for ob in obs:
        if ob["tipo"] == "bajista" and precio_actual > ob["high"]:
            ob["mitigado"] = True
        if ob["tipo"] == "alcista" and precio_actual < ob["low"]:
            ob["mitigado"] = True

    return obs


# ─── 2. FAIR VALUE GAPS ──────────────────────────────────
# Cambios vs versión anterior:
# - Filtro de tamaño mínimo: gap debe ser >= 30% del ATR
# - Filtro de proximidad: solo FVGs dentro de 3x ATR del precio
# - Campo proximidad (0-1) para scoring ponderado
def detectar_fvg(df, lookback=50, precio_actual=None, atr=None):
    fvgs = []
    df   = df.tail(lookback).copy()

    if precio_actual is None:
        precio_actual = float(df["close"].iloc[-1])
    if atr is None:
        atr = calcular_atr(df)

    min_gap    = atr * 0.3
    rango_prox = atr * 3.0

    for i in range(len(df) - 2):
        v1 = df.iloc[i]
        v3 = df.iloc[i + 2]

        # FVG Bajista (gap por encima del precio — resistencia)
        if float(v1["low"]) > float(v3["high"]):
            gap_size = float(v1["low"]) - float(v3["high"])
            if gap_size < min_gap:
                continue
            gap_mid = (float(v1["low"]) + float(v3["high"])) / 2
            dist    = abs(gap_mid - precio_actual)
            if dist > rango_prox:
                continue
            fvgs.append({
                "tipo":       "bajista",
                "time":       df.index[i],
                "high":       float(v1["low"]),
                "low":        float(v3["high"]),
                "llenado":    False,
                "proximidad": round(1 - (dist / rango_prox), 3),
                "tamaño":     round(gap_size, 2),
            })

        # FVG Alcista (gap por debajo del precio — soporte)
        if float(v1["high"]) < float(v3["low"]):
            gap_size = float(v3["low"]) - float(v1["high"])
            if gap_size < min_gap:
                continue
            gap_mid = (float(v3["low"]) + float(v1["high"])) / 2
            dist    = abs(gap_mid - precio_actual)
            if dist > rango_prox:
                continue
            fvgs.append({
                "tipo":       "alcista",
                "time":       df.index[i],
                "high":       float(v3["low"]),
                "low":        float(v1["high"]),
                "llenado":    False,
                "proximidad": round(1 - (dist / rango_prox), 3),
                "tamaño":     round(gap_size, 2),
            })

    precio_actual_f = float(precio_actual)
    for fvg in fvgs:
        if fvg["low"] <= precio_actual_f <= fvg["high"]:
            fvg["llenado"] = True
        elif fvg["tipo"] == "bajista" and precio_actual_f < fvg["low"]:
            fvg["llenado"] = True
        elif fvg["tipo"] == "alcista" and precio_actual_f > fvg["high"]:
            fvg["llenado"] = True

    return fvgs


# ─── 3. BOS / CHoCH ──────────────────────────────────────
# Añade nivel_bos_alcista / nivel_bos_bajista: el próximo swing level
# que, si se rompe, confirmaría el siguiente BOS. Útil para TP y SL.
def detectar_bos(df, lookback=50):
    eventos     = []
    df          = df.tail(lookback).copy()
    swing_highs = []
    swing_lows  = []

    for i in range(2, len(df) - 2):
        if (df["high"].iloc[i] > df["high"].iloc[i-1] and
            df["high"].iloc[i] > df["high"].iloc[i-2] and
            df["high"].iloc[i] > df["high"].iloc[i+1] and
            df["high"].iloc[i] > df["high"].iloc[i+2]):
            swing_highs.append({"time": df.index[i], "precio": float(df["high"].iloc[i])})

        if (df["low"].iloc[i] < df["low"].iloc[i-1] and
            df["low"].iloc[i] < df["low"].iloc[i-2] and
            df["low"].iloc[i] < df["low"].iloc[i+1] and
            df["low"].iloc[i] < df["low"].iloc[i+2]):
            swing_lows.append({"time": df.index[i], "precio": float(df["low"].iloc[i])})

    precio_actual     = float(df["close"].iloc[-1])
    nivel_bos_alcista = None
    nivel_bos_bajista = None

    if len(swing_highs) >= 2:
        ultimo_high    = swing_highs[-1]["precio"]
        penultimo_high = swing_highs[-2]["precio"]
        nivel_bos_alcista = ultimo_high
        if precio_actual > ultimo_high:
            tipo = "BOS alcista" if ultimo_high > penultimo_high else "CHoCH alcista"
            eventos.append({"tipo": tipo, "nivel": ultimo_high, "time": swing_highs[-1]["time"]})

    if len(swing_lows) >= 2:
        ultimo_low    = swing_lows[-1]["precio"]
        penultimo_low = swing_lows[-2]["precio"]
        nivel_bos_bajista = ultimo_low
        if precio_actual < ultimo_low:
            tipo = "BOS bajista" if ultimo_low < penultimo_low else "CHoCH bajista"
            eventos.append({"tipo": tipo, "nivel": ultimo_low, "time": swing_lows[-1]["time"]})

    return (
        eventos,
        swing_highs[-3:] if len(swing_highs) >= 3 else swing_highs,
        swing_lows[-3:]  if len(swing_lows)  >= 3 else swing_lows,
        nivel_bos_alcista,
        nivel_bos_bajista,
    )


# ─── 4. LIQUIDEZ ─────────────────────────────────────────
def detectar_liquidez(df, lookback=50, tolerancia=0.5):
    zonas = []
    df    = df.tail(lookback).copy()

    highs = df["high"].values
    lows  = df["low"].values
    times = df.index

    for i in range(len(highs)):
        similares = [j for j in range(i + 1, len(highs))
                     if abs(highs[i] - highs[j]) <= tolerancia]
        if similares:
            zonas.append({"tipo": "liquidez_high", "nivel": highs[i],
                          "time": times[i], "toques": len(similares) + 1})

    for i in range(len(lows)):
        similares = [j for j in range(i + 1, len(lows))
                     if abs(lows[i] - lows[j]) <= tolerancia]
        if similares:
            zonas.append({"tipo": "liquidez_low", "nivel": lows[i],
                          "time": times[i], "toques": len(similares) + 1})

    return zonas


# ─── 5. RSI ──────────────────────────────────────────────
def calcular_rsi(serie, periodo=14):
    delta    = serie.diff()
    ganancia = delta.clip(lower=0).rolling(periodo).mean()
    perdida  = (-delta.clip(upper=0)).rolling(periodo).mean()
    rs       = ganancia / perdida.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))
    val      = rsi.iloc[-1]
    return round(val, 1) if not np.isnan(val) else 50.0


# ─── 6. SCORE MULTI-TIMEFRAME ────────────────────────────
# Cambios vs versión anterior:
# - OBs y FVGs scored por proximidad (0-5 y 0-4 puntos, ponderado por cercanía)
# - Umbral RSI más estricto: 60/40 en lugar de 55/45
# - ATR calculado por TF e incluido en resultado
# - nivel_bos_alcista/bajista incluidos para uso en setup
def calcular_score(simbolo="XAUUSD"):
    if MT5_DISPONIBLE and mt5:
        timeframes = {
            "D1":  mt5.TIMEFRAME_D1,
            "H4":  mt5.TIMEFRAME_H4,
            "H1":  mt5.TIMEFRAME_H1,
            "M15": mt5.TIMEFRAME_M15,
        }
    else:
        timeframes = {"D1": 1440, "H4": 240, "H1": 60, "M15": 15}

    resultado   = {}
    score_long  = 0.0
    score_short = 0.0

    for nombre, tf in timeframes.items():
        df = get_velas(simbolo, tf, 200)
        if df is None or len(df) < 50:
            continue

        precio = float(df["close"].iloc[-1])
        atr    = calcular_atr(df)
        ma20   = float(df["close"].rolling(20).mean().iloc[-1])
        ma50   = float(df["close"].rolling(50).mean().iloc[-1])
        rsi    = calcular_rsi(df["close"])

        obs  = detectar_order_blocks(df, precio_actual=precio, atr=atr)
        fvgs = detectar_fvg(df, precio_actual=precio, atr=atr)
        bos, swing_highs, swing_lows, nivel_bos_alc, nivel_bos_baj = detectar_bos(df)

        tendencia    = "alcista" if ma20 > ma50 else "bajista"
        ob_alcistas  = [o for o in obs  if o["tipo"] == "alcista" and not o["mitigado"]]
        ob_bajistas  = [o for o in obs  if o["tipo"] == "bajista" and not o["mitigado"]]
        fvg_alcistas = [f for f in fvgs if f["tipo"] == "alcista" and not f["llenado"]]
        fvg_bajistas = [f for f in fvgs if f["tipo"] == "bajista" and not f["llenado"]]
        bos_alcista  = any("alcista" in b["tipo"] for b in bos)
        bos_bajista  = any("bajista" in b["tipo"] for b in bos)

        peso = {"D1": 4, "H4": 3, "H1": 2, "M15": 1}[nombre]

        # Tendencia (binario)
        if tendencia == "alcista":
            score_long  += 5 * peso
        else:
            score_short += 5 * peso

        # OBs — ponderados por proximidad, cap en 5 × peso
        if ob_alcistas:
            score_long  += min(5.0, sum(o["proximidad"] * 5 for o in ob_alcistas)) * peso
        if ob_bajistas:
            score_short += min(5.0, sum(o["proximidad"] * 5 for o in ob_bajistas)) * peso

        # FVGs — ponderados por proximidad, cap en 4 × peso
        if fvg_alcistas:
            score_long  += min(4.0, sum(f["proximidad"] * 4 for f in fvg_alcistas)) * peso
        if fvg_bajistas:
            score_short += min(4.0, sum(f["proximidad"] * 4 for f in fvg_bajistas)) * peso

        # BOS/CHoCH (binario — confirmación de tendencia)
        if bos_alcista:
            score_long  += 6 * peso
        if bos_bajista:
            score_short += 6 * peso

        # RSI momentum — umbral 60/40 (más estricto que 55/45 anterior)
        if rsi > 60:
            score_long  += 3 * peso
        elif rsi < 40:
            score_short += 3 * peso

        resultado[nombre] = {
            "tendencia":         tendencia,
            "ob_alcistas":       len(ob_alcistas),
            "ob_bajistas":       len(ob_bajistas),
            "fvg_alcistas":      len(fvg_alcistas),
            "fvg_bajistas":      len(fvg_bajistas),
            "bos_alcista":       bos_alcista,
            "bos_bajista":       bos_bajista,
            "rsi":               rsi,
            "swing_highs":       swing_highs,
            "swing_lows":        swing_lows,
            "nivel_bos_alcista": nivel_bos_alc,
            "nivel_bos_bajista": nivel_bos_baj,
            "obs":               obs,
            "fvgs":              fvgs,
            "precio":            precio,
            "atr":               atr,
        }

    total = score_long + score_short
    if total > 0:
        pct_long  = round((score_long  / total) * 100)
        pct_short = round((score_short / total) * 100)
    else:
        pct_long = pct_short = 50

    direccion = "LONG" if pct_long > pct_short else "SHORT"
    score     = pct_long if direccion == "LONG" else pct_short

    return {
        "direccion":  direccion,
        "score":      score,
        "long_pct":   pct_long,
        "short_pct":  pct_short,
        "por_tf":     resultado,
        "atr_m15":    resultado.get("M15", {}).get("atr", 5.0),
    }


# ─── TEST DIRECTO ─────────────────────────────────────────
if __name__ == "__main__":
    if MT5_DISPONIBLE:
        mt5.initialize()

    print("=== ANÁLISIS ICT XAUUSD ===\n")
    resultado = calcular_score()

    print(f"Dirección: {resultado['direccion']}")
    print(f"Score:     {resultado['score']}%")
    print(f"Long:      {resultado['long_pct']}%")
    print(f"Short:     {resultado['short_pct']}%")
    print(f"ATR M15:   {resultado['atr_m15']:.2f} pts")
    print()

    for tf, data in resultado["por_tf"].items():
        print(f"── {tf} ──────────────────────")
        print(f"  Tendencia:    {data['tendencia']}")
        print(f"  ATR:          {data['atr']:.2f} pts")
        print(f"  RSI:          {data['rsi']}")
        print(f"  OB alcistas:  {data['ob_alcistas']}")
        print(f"  OB bajistas:  {data['ob_bajistas']}")
        print(f"  FVG alcistas: {data['fvg_alcistas']}")
        print(f"  FVG bajistas: {data['fvg_bajistas']}")
        print(f"  BOS alcista:  {data['bos_alcista']}")
        print(f"  BOS bajista:  {data['bos_bajista']}")
        if data.get("nivel_bos_alcista"):
            print(f"  Próx. BOS ↑:  {data['nivel_bos_alcista']:.2f}")
        if data.get("nivel_bos_bajista"):
            print(f"  Próx. BOS ↓:  {data['nivel_bos_bajista']:.2f}")
        print()

    if MT5_DISPONIBLE:
        mt5.shutdown()
