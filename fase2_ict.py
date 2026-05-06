import pandas as pd
import numpy as np

try:
    import MetaTrader5 as mt5
    MT5_DISPONIBLE = True
except ImportError:
    MT5_DISPONIBLE = False
    mt5 = None

# ─── OBTENER VELAS POR TIMEFRAME ─────────────────────────
def get_velas(simbolo="XAUUSD", timeframe=mt5.TIMEFRAME_M15, cantidad=200):
    velas = mt5.copy_rates_from_pos(simbolo, timeframe, 0, cantidad)
    if velas is None:
        return None
    df = pd.DataFrame(velas)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("time")
    df = df[["open", "high", "low", "close", "tick_volume"]].copy()
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    return df

# ─── 1. ORDER BLOCKS ─────────────────────────────────────
def detectar_order_blocks(df, lookback=50):
    """
    OB Bajista: última vela ALCISTA antes de un impulso bajista fuerte
    OB Alcista: última vela BAJISTA antes de un impulso alcista fuerte
    """
    obs = []
    df  = df.tail(lookback).copy()

    for i in range(2, len(df) - 1):
        vela_actual = df.iloc[i]
        vela_prev   = df.iloc[i - 1]
        vela_sig    = df.iloc[i + 1]

        rango_actual = abs(vela_actual["close"] - vela_actual["open"])
        rango_sig    = abs(vela_sig["close"]    - vela_sig["open"])

        # OB Bajista — vela alcista seguida de vela bajista fuerte
        if (vela_actual["close"] > vela_actual["open"] and   # actual alcista
            vela_sig["close"]    < vela_sig["open"]    and   # siguiente bajista
            rango_sig > rango_actual * 1.5):                 # impulso fuerte

            obs.append({
                "tipo":  "bajista",
                "time":  df.index[i],
                "high":  vela_actual["high"],
                "low":   vela_actual["low"],
                "open":  vela_actual["open"],
                "close": vela_actual["close"],
                "mitigado": False
            })

        # OB Alcista — vela bajista seguida de vela alcista fuerte
        if (vela_actual["close"] < vela_actual["open"] and   # actual bajista
            vela_sig["close"]    > vela_sig["open"]    and   # siguiente alcista
            rango_sig > rango_actual * 1.5):                 # impulso fuerte

            obs.append({
                "tipo":  "alcista",
                "time":  df.index[i],
                "high":  vela_actual["high"],
                "low":   vela_actual["low"],
                "open":  vela_actual["open"],
                "close": vela_actual["close"],
                "mitigado": False
            })

    # Verificar si fue mitigado (precio volvió a tocarlo)
    precio_actual = df["close"].iloc[-1]
    for ob in obs:
        if ob["tipo"] == "bajista" and precio_actual >= ob["low"]:
            ob["mitigado"] = True
        if ob["tipo"] == "alcista" and precio_actual <= ob["high"]:
            ob["mitigado"] = True

    return obs


# ─── 2. FAIR VALUE GAPS ──────────────────────────────────
def detectar_fvg(df, lookback=50):
    """
    FVG Bajista: low[i] > high[i+2]  — hueco entre vela 1 y vela 3
    FVG Alcista: high[i] < low[i+2]  — hueco entre vela 1 y vela 3
    """
    fvgs = []
    df   = df.tail(lookback).copy()

    for i in range(len(df) - 2):
        v1 = df.iloc[i]
        v3 = df.iloc[i + 2]

        # FVG Bajista — hueco hacia abajo
        if v1["low"] > v3["high"]:
            fvgs.append({
                "tipo":     "bajista",
                "time":     df.index[i],
                "high":     v1["low"],
                "low":      v3["high"],
                "llenado":  False
            })

        # FVG Alcista — hueco hacia arriba
        if v1["high"] < v3["low"]:
            fvgs.append({
                "tipo":     "alcista",
                "time":     df.index[i],
                "high":     v3["low"],
                "low":      v1["high"],
                "llenado":  False
            })

    # Verificar si fue llenado
    precio_actual = df["close"].iloc[-1]
    for fvg in fvgs:
        if fvg["low"] <= precio_actual <= fvg["high"]:
            fvg["llenado"] = True
        elif fvg["tipo"] == "bajista" and precio_actual < fvg["low"]:
            fvg["llenado"] = True
        elif fvg["tipo"] == "alcista" and precio_actual > fvg["high"]:
            fvg["llenado"] = True

    return fvgs


# ─── 3. BOS / CHoCH ──────────────────────────────────────
def detectar_bos(df, lookback=50):
    """
    BOS  — Break of Structure: rompe máximo/mínimo previo en misma dirección
    CHoCH — Change of Character: rompe en dirección CONTRARIA (cambio de tendencia)
    """
    eventos = []
    df      = df.tail(lookback).copy()

    swing_highs = []
    swing_lows  = []

    # Detectar swings (máximos y mínimos locales)
    for i in range(2, len(df) - 2):
        # Swing High
        if (df["high"].iloc[i] > df["high"].iloc[i-1] and
            df["high"].iloc[i] > df["high"].iloc[i-2] and
            df["high"].iloc[i] > df["high"].iloc[i+1] and
            df["high"].iloc[i] > df["high"].iloc[i+2]):
            swing_highs.append({"time": df.index[i], "precio": df["high"].iloc[i]})

        # Swing Low
        if (df["low"].iloc[i] < df["low"].iloc[i-1] and
            df["low"].iloc[i] < df["low"].iloc[i-2] and
            df["low"].iloc[i] < df["low"].iloc[i+1] and
            df["low"].iloc[i] < df["low"].iloc[i+2]):
            swing_lows.append({"time": df.index[i], "precio": df["low"].iloc[i]})

    precio_actual = df["close"].iloc[-1]

    # Verificar BOS / CHoCH en últimos swings
    if len(swing_highs) >= 2:
        ultimo_high      = swing_highs[-1]["precio"]
        penultimo_high   = swing_highs[-2]["precio"]

        if precio_actual > ultimo_high:
            tipo = "BOS alcista" if ultimo_high > penultimo_high else "CHoCH alcista"
            eventos.append({
                "tipo":   tipo,
                "nivel":  ultimo_high,
                "time":   swing_highs[-1]["time"]
            })

    if len(swing_lows) >= 2:
        ultimo_low     = swing_lows[-1]["precio"]
        penultimo_low  = swing_lows[-2]["precio"]

        if precio_actual < ultimo_low:
            tipo = "BOS bajista" if ultimo_low < penultimo_low else "CHoCH bajista"
            eventos.append({
                "tipo":   tipo,
                "nivel":  ultimo_low,
                "time":   swing_lows[-1]["time"]
            })

    return eventos, swing_highs[-3:] if len(swing_highs) >= 3 else swing_highs, \
                    swing_lows[-3:]  if len(swing_lows)  >= 3 else swing_lows


# ─── 4. LIQUIDEZ ─────────────────────────────────────────
def detectar_liquidez(df, lookback=50, tolerancia=0.5):
    """
    Zonas de liquidez: máximos o mínimos casi iguales
    donde se acumulan stops de otros traders
    """
    zonas = []
    df    = df.tail(lookback).copy()

    highs = df["high"].values
    lows  = df["low"].values
    times = df.index

    # Buscar highs similares (equal highs)
    for i in range(len(highs)):
        similares = []
        for j in range(i + 1, len(highs)):
            if abs(highs[i] - highs[j]) <= tolerancia:
                similares.append(j)
        if len(similares) >= 1:
            zonas.append({
                "tipo":   "liquidez_high",
                "nivel":  highs[i],
                "time":   times[i],
                "toques": len(similares) + 1
            })

    # Buscar lows similares (equal lows)
    for i in range(len(lows)):
        similares = []
        for j in range(i + 1, len(lows)):
            if abs(lows[i] - lows[j]) <= tolerancia:
                similares.append(j)
        if len(similares) >= 1:
            zonas.append({
                "tipo":   "liquidez_low",
                "nivel":  lows[i],
                "time":   times[i],
                "toques": len(similares) + 1
            })

    return zonas


# ─── 5. SCORE MULTI-TIMEFRAME ────────────────────────────
def calcular_score(simbolo="XAUUSD"):
    """
    Analiza D1, H4, H1, M15 y calcula score 0-100
    """
    timeframes = {
        "D1":  mt5.TIMEFRAME_D1,
        "H4":  mt5.TIMEFRAME_H4,
        "H1":  mt5.TIMEFRAME_H1,
        "M15": mt5.TIMEFRAME_M15,
    }

    resultado = {}
    score_long  = 0
    score_short = 0

    for nombre, tf in timeframes.items():
        df = get_velas(simbolo, tf, 200)
        if df is None:
            continue

        obs   = detectar_order_blocks(df)
        fvgs  = detectar_fvg(df)
        bos, swing_highs, swing_lows = detectar_bos(df)

        precio = df["close"].iloc[-1]

        # Tendencia simple del TF
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        ma50 = df["close"].rolling(50).mean().iloc[-1]
        tendencia = "alcista" if ma20 > ma50 else "bajista"

        # OB activos (no mitigados)
        ob_alcistas = [o for o in obs if o["tipo"] == "alcista" and not o["mitigado"]]
        ob_bajistas = [o for o in obs if o["tipo"] == "bajista" and not o["mitigado"]]

        # FVG activos (no llenados)
        fvg_alcistas = [f for f in fvgs if f["tipo"] == "alcista" and not f["llenado"]]
        fvg_bajistas = [f for f in fvgs if f["tipo"] == "bajista" and not f["llenado"]]

        # BOS recientes
        bos_alcista = any("alcista" in b["tipo"] for b in bos)
        bos_bajista = any("bajista" in b["tipo"] for b in bos)

        # Score por TF
        peso = {"D1": 4, "H4": 3, "H1": 2, "M15": 1}[nombre]

        if tendencia == "alcista":
            score_long += 5 * peso
        else:
            score_short += 5 * peso

        if ob_alcistas: score_long  += 5 * peso
        if ob_bajistas: score_short += 5 * peso
        if fvg_alcistas: score_long  += 4 * peso
        if fvg_bajistas: score_short += 4 * peso
        if bos_alcista:  score_long  += 6 * peso
        if bos_bajista:  score_short += 6 * peso

        resultado[nombre] = {
            "tendencia":    tendencia,
            "ob_alcistas":  len(ob_alcistas),
            "ob_bajistas":  len(ob_bajistas),
            "fvg_alcistas": len(fvg_alcistas),
            "fvg_bajistas": len(fvg_bajistas),
            "bos_alcista":  bos_alcista,
            "bos_bajista":  bos_bajista,
            "swing_highs":  swing_highs,
            "swing_lows":   swing_lows,
            "obs":          obs,
            "fvgs":         fvgs,
            "precio":       precio,
        }

    # Normalizar score a 0-100
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
    }


# ─── TEST DIRECTO ─────────────────────────────────────────
if __name__ == "__main__":
    mt5.initialize()

    print("=== ANÁLISIS ICT XAUUSD ===\n")

    resultado = calcular_score()

    print(f"Dirección: {resultado['direccion']}")
    print(f"Score:     {resultado['score']}%")
    print(f"Long:      {resultado['long_pct']}%")
    print(f"Short:     {resultado['short_pct']}%")
    print()

    for tf, data in resultado["por_tf"].items():
        print(f"── {tf} ──────────────────────")
        print(f"  Tendencia:    {data['tendencia']}")
        print(f"  OB alcistas:  {data['ob_alcistas']}")
        print(f"  OB bajistas:  {data['ob_bajistas']}")
        print(f"  FVG alcistas: {data['fvg_alcistas']}")
        print(f"  FVG bajistas: {data['fvg_bajistas']}")
        print(f"  BOS alcista:  {data['bos_alcista']}")
        print(f"  BOS bajista:  {data['bos_bajista']}")
        print()

    mt5.shutdown()