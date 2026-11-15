#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AstroEQ 太陽GoTo用座標計算

初期姿勢:
  望遠鏡 : NCP方向
  ウエイト軸 : 鉛直下向き
  RA : 子午線基準
  DEC : +90度

機能:
  1. USB GPSからUTC・緯度・経度取得
  2. 太陽の赤経・赤緯計算
  3. 地方恒星時・太陽時角計算
  4. RA/DECホームからの移動角度計算
  5. AstroEQモーターカウント数への変換
  6. 太陽高度・方位角計算

AstroEQへのコマンド送信は行わない。
"""

import math
from datetime import datetime, timezone
import gps

# 実機から取得したAstroEQのCPR
RA_CPR = 4608000
DEC_CPR = 11712000

RAD = math.pi / 180.0


def sind(deg):
    return math.sin(deg * RAD)


def cosd(deg):
    return math.cos(deg * RAD)


def wrap180(deg):
    """角度を -180度から+180度に正規化する。"""
    return (deg + 180.0) % 360.0 - 180.0


def julian_date(dt):
    """UTC日時をユリウス日に変換する。"""
    return dt.timestamp() / 86400.0 + 2440587.5


def sun_coordinates(jd):
    """
    USNOの太陽位置近似式。

    戻り値:
      赤経（度）
      赤緯（度）
    """
    d = jd - 2451545.0

    # 太陽の平均近点角
    g = (357.529 + 0.98560028 * d) % 360.0

    # 太陽の平均黄経
    q = (280.459 + 0.98564736 * d) % 360.0

    # 見かけの黄経（近似）
    L = (
        q
        + 1.915 * sind(g)
        + 0.020 * sind(2.0 * g)
    ) % 360.0

    # 黄道傾斜角
    e = 23.439 - 0.00000036 * d

    # 赤経
    ra = math.degrees(
        math.atan2(
            cosd(e) * sind(L),
            cosd(L)
        )
    ) % 360.0

    # 赤緯
    dec = math.degrees(
        math.asin(sind(e) * sind(L))
    )

    return ra, dec


def local_sidereal_time(jd, longitude):
    """
    USNOの近似式で地方視恒星時を求める。

    longitude:
      東経を正とする。
    """
    jd0 = math.floor(jd - 0.5) + 0.5
    dut = jd0 - 2451545.0
    h = (jd - jd0) * 24.0
    dtt = jd - 2451545.0
    t = dtt / 36525.0

    # グリニッジ平均恒星時（時）
    gmst = (
        6.697375
        + 0.065709824279 * dut
        + 1.0027379 * h
        + 0.0000258 * t * t
    ) % 24.0

    # 分点差の近似補正
    omega = 125.04 - 0.052954 * dtt
    L = 280.47 + 0.98565 * dtt
    eps = 23.4393 - 0.0000004 * dtt

    dpsi = (
        -0.000319 * sind(omega)
        -0.000024 * sind(2.0 * L)
    )

    eqeq = dpsi * cosd(eps)

    # 地方視恒星時（度）
    return ((gmst + eqeq) * 15.0 + longitude) % 360.0


def horizontal_coordinates(lat, dec, ha):
    """
    太陽の高度と方位角を計算する。

    方位角:
      北0度、東90度、南180度、西270度
    """
    altitude = math.degrees(math.asin(
        sind(lat) * sind(dec)
        + cosd(lat) * cosd(dec) * cosd(ha)
    ))

    # 東・北方向の成分
    east = -cosd(dec) * sind(ha)
    north = (
        cosd(lat) * sind(dec)
        - sind(lat) * cosd(dec) * cosd(ha)
    )

    azimuth = math.degrees(
        math.atan2(east, north)
    ) % 360.0

    return altitude, azimuth


def get_gps():
    """gpsdから有効なUTC・緯度・経度を取得する。"""
    print("GPS測位データ待機中...", flush=True)

    session = gps.gps(
        mode=gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE
    )

    try:
        for report in session:
            if report.get("class") != "TPV":
                continue

            if report.get("mode", 0) < 2:
                continue

            lat = report.get("lat")
            lon = report.get("lon")
            utc = report.get("time")

            if not isinstance(lat, (int, float)):
                continue
            if not isinstance(lon, (int, float)):
                continue
            if not isinstance(utc, str):
                continue

            if not (
                math.isfinite(lat)
                and math.isfinite(lon)
                and -90 <= lat <= 90
                and -180 <= lon <= 180
            ):
                continue

            try:
                dt = datetime.fromisoformat(
                    utc.replace("Z", "+00:00")
                )
            except ValueError:
                continue

            if dt.tzinfo is None or dt.year < 2020:
                continue

            return dt.astimezone(timezone.utc), lat, lon

    finally:
        session.close()

    raise RuntimeError("GPSデータを取得できません")


def main():
    utc, latitude, longitude = get_gps()

    jd = julian_date(utc)

    # 太陽の赤経・赤緯
    sun_ra, sun_dec = sun_coordinates(jd)

    # 地方視恒星時
    lst = local_sidereal_time(jd, longitude)

    # 太陽時角（西を正とする）
    ha = wrap180(lst - sun_ra)

    # NCPホーム姿勢からの理論移動量
    ra_move = ha
    dec_move = 90.0 - sun_dec

    # カウント数（符号は幾何学上の方向）
    ra_steps = round(
        ra_move * RA_CPR / 360.0
    )
    dec_steps = round(
        dec_move * DEC_CPR / 360.0
    )

    altitude, azimuth = horizontal_coordinates(
        latitude, sun_dec, ha
    )

    print()
    print("===================================")
    print(" AstroEQ Solar GoTo Calculation")
    print("===================================")

    print("UTC       :", utc.isoformat())
    print("Latitude  :", f"{latitude:.8f} deg")
    print("Longitude :", f"{longitude:.8f} deg")

    print()
    print("--- 太陽座標 ---")
    print(f"RA    : {sun_ra / 15.0:.8f} hours")
    print(f"RA角度: {sun_ra:.8f} deg")
    print(f"DEC   : {sun_dec:+.8f} deg")

    def dms(angle):
        sign = "-" if angle < 0 else "+"
        total = abs(angle) * 3600
        d = int(total // 3600)
        m = int((total % 3600) // 60)
        sec = total % 60
        return f"{sign}{d}° {m:02d}' {sec:06.3f}\\\""

    print(f"RA度分秒 : {dms(sun_ra)}")
    print(f"DEC度分秒: {dms(sun_dec)}")
    print(f"LST   : {lst / 15.0:.8f} hours")
    print(f"HA    : {ha:+.8f} deg")

    print()
    print("--- 太陽の地平座標 ---")
    print(f"高度  : {altitude:+.6f} deg")
    print(f"方位角: {azimuth:.6f} deg")

    print()
    print("--- ホームからの移動量 ---")
    print("初期位置: NCP / CW鉛直下")
    print(f"RA角度 : {ra_move:+.8f} deg")
    print(f"DEC角度: {dec_move:+.8f} deg")

    print()
    print("--- AstroEQカウント換算 ---")
    print(f"RA CPR : {RA_CPR}")
    print(f"DEC CPR: {DEC_CPR}")
    print(f"RA  : {ra_steps:+d} counts")
    print(f"DEC : {dec_steps:+d} counts")

    print()
    print("注意: カウントの正負は天文学的な方向。")
    print("AstroEQの方向0/1への変換は未確定。")
    print("モーター駆動コマンドは送信していません。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中断しました")
