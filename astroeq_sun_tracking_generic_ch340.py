#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AstroEQ 太陽GoTo
OpenAstro不使用 / USB直接通信

初期姿勢:
  望遠鏡 NCP方向
  ウエイト軸 鉛直下方向

既存のastroeq_sun_position.pyを再利用する。
"""

import argparse
import time
import serial
from serial.tools import list_ports

from astroeq_sun_position import (
    get_gps, julian_date, sun_coordinates,
    local_sidereal_time, horizontal_coordinates,
    wrap180
)

BAUD = 9600
TIMEOUT = 1800

# 読み取り応答を確認済みのUSB候補（Arduino Mega / CH340）。
# VID/PIDはAstroEQの証明ではないため、接続後にプロトコルを検証する。
ASTROEQ_USB_IDS = {(0x2341, 0x0042), (0x1A86, 0x7523)}


def find_astroeq_port(override=None):
    """USB順序に依存せず、AstroEQ候補を1台だけ選ぶ。"""
    devices = list(list_ports.comports())
    if override:
        # 明示指定は接続中のシリアルポートに一致する場合だけ許可。
        from pathlib import Path
        import os
        path = os.path.realpath(override)
        matches = [p for p in devices if os.path.realpath(p.device) == path]
        if len(matches) != 1:
            raise RuntimeError(f"指定されたUSBシリアルポートが見つかりません: {override}")
        p = matches[0]
        if (p.vid, p.pid) == (0x1546, 0x01A7):
            raise RuntimeError("GPS受信機をAstroEQとして指定できません")
    else:
        matches = [p for p in devices if (p.vid, p.pid) in ASTROEQ_USB_IDS]
        if len(matches) != 1:
            raise RuntimeError(
                f"AstroEQ候補が{len(matches)}台です（1台必要）。"
                " USB識別子が異なる個体は --astroeq-port で指定してください"
            )
        p = matches[0]
    print(f"AstroEQ候補: {p.device} VID:PID={p.vid:04X}:{p.pid:04X} "
          f"SER={p.serial_number or 'なし'}", flush=True)
    # 現在の実デバイス名を使う。毎回検出するためttyACM番号の変化にも対応。
    return p.device


def read_controller_params(ser):
    """駆動前にAstroEQ応答とCPRを検証する。0や巨大値を拒否。"""
    fw_ra, fw_dec = read24(ser, "e1"), read24(ser, "e2")
    ra_cpr, dec_cpr = read24(ser, "a1"), read24(ser, "a2")
    if fw_ra == 0 or fw_dec == 0:
        raise RuntimeError("AstroEQファームウェア識別に失敗")
    timer_ra, timer_dec = read24(ser, "b1"), read24(ser, "b2")
    for label, val in (("RA CPR", ra_cpr), ("DEC CPR", dec_cpr)):
        if not 1000 <= val <= 0xFFFFFF:
            raise RuntimeError(f"{label}が不正: {val}")
    if not 1 <= timer_ra <= 0xFFFFFF or not 1 <= timer_dec <= 0xFFFFFF:
        raise RuntimeError("タイマー情報が不正")
    print(f"Controller FW: RA={fw_ra:06X} DEC={fw_dec:06X}", flush=True)
    print(f"RA CPR={ra_cpr} DEC CPR={dec_cpr} "
          f"RA timer={timer_ra} DEC timer={timer_dec}", flush=True)
    return ra_cpr, dec_cpr



def hex24(value):
    """整数をAstroEQの24bit形式に変換。"""
    if not 0 <= value <= 0xFFFFFF:
        raise ValueError("移動カウント範囲外")
    return value.to_bytes(3, "little").hex().upper()


def send(ser, cmd):
    """AstroEQにコマンドを送信する。"""
    ser.reset_input_buffer()
    ser.write(f":{cmd}\r".encode("ascii"))
    ser.flush()

    response = ser.read_until(b"\r")
    if not cmd.startswith(("f", "j")):
        print(f"TX={cmd} RX={response!r}", flush=True)
    if not response.startswith(b"=") or not response.endswith(b"\r"):
        raise RuntimeError(f"{cmd}: {response!r}")

    return response[1:-1].decode("ascii")


def read24(ser, cmd):
    value = send(ser, cmd)
    if len(value) != 6:
        raise RuntimeError(f"24bit応答異常: {cmd}")
    return int.from_bytes(bytes.fromhex(value), "little")


def status_stopped(value):
    """初期化済み・停止状態か確認。"""
    return (
        len(value) == 3
        and value[1] == "0"
        and value[2] == "1"
    )


def prepare_axis(ser, axis):
    """必要に応じて軸を初期化する。"""
    status = send(ser, f"f{axis}")

    if status == "100":
        send(ser, f"F{axis}")
        status = send(ser, f"f{axis}")

    if not status_stopped(status):
        raise RuntimeError(
            f"軸{axis}が停止状態ではありません: {status}"
        )


def stop_both(ser):
    """両軸への停止指令を試みる。"""
    for axis in (1, 2):
        try:
            send(ser, f"K{axis}")
            print(f"軸{axis} 停止指令送信", flush=True)
        except Exception as e:
            print(f"軸{axis} 停止指令失敗: {e}", flush=True)



def fine_goto_axis(ser, axis, before, target_delta, ra_cpr, dec_cpr):
    """
    高速GoTo後の残量を低速GoToで補正する。

    target_delta:
      開始カウンターを基準とした符号付き目標移動量。
      方向0 = 正、方向1 = 負。

    最大2回補正し、残差10カウント以内で成功とする。
    """
    cpr = ra_cpr if axis == 1 else dec_cpr

    for attempt in range(2):
        current = read24(ser, f"j{axis}")

        moved = (
            (current - before + 0x800000)
            % 0x1000000
        ) - 0x800000

        remaining = target_delta - moved

        print(
            f"軸{axis} 補正{attempt + 1}: "
            f"残り={remaining} counts, "
            f"{remaining * 360 / cpr:+.6f}度",
            flush=True
        )

        if abs(remaining) <= 10:
            return

        if abs(remaining) > 0x7FFFFF:
            raise RuntimeError("補正移動量が大きすぎます")

        status = send(ser, f"f{axis}")
        if not status_stopped(status):
            raise RuntimeError(
                f"軸{axis}が停止していません: {status}"
            )

        direction = 0 if remaining > 0 else 1

        # 低速GoToモードへ切り替える。
        send(ser, f"G{axis}2{direction}")
        send(ser, f"H{axis}" + hex24(abs(remaining)))
        send(ser, f"J{axis}")

        start = time.monotonic()

        while True:
            time.sleep(0.2)
            status = send(ser, f"f{axis}")

            if status_stopped(status):
                break

            if time.monotonic() - start > 180:
                raise TimeoutError(
                    f"軸{axis}の低速補正タイムアウト"
                )

    # 最大補正回数に到達した場合も残差を再確認。
    current = read24(ser, f"j{axis}")

    moved = (
        (current - before + 0x800000)
        % 0x1000000
    ) - 0x800000

    if abs(target_delta - moved) > 10:
        raise RuntimeError(
            f"軸{axis}の低速補正が未完了"
        )


def signed24_delta(current, origin):
    """24bit位置カウンター差を符号付きで取得する。"""
    return ((current - origin + 0x800000) % 0x1000000) - 0x800000


def wait_axes_stopped(ser, timeout=15):
    """K停止指令後、両軸が本当に停止したことを確認する。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s1 = send(ser, "f1")
        s2 = send(ser, "f2")
        if status_stopped(s1) and status_stopped(s2):
            return
        time.sleep(0.2)
    raise TimeoutError(f"両軸の停止確認失敗: RA={s1} DEC={s2}")


def goto_home(ser, home_ra, home_dec, ra_cpr, dec_cpr):
    """現在のカウンター位置から、起動時に記録したホームへ復帰する。"""
    # 停止が確認できない限り次のGoToは発行しない。
    wait_axes_stopped(ser)
    now_ra = read24(ser, "j1")
    now_dec = read24(ser, "j2")
    target_ra = signed24_delta(home_ra, now_ra)
    target_dec = signed24_delta(home_dec, now_dec)

    print("\n===== GoTo Home =====", flush=True)
    print(f"ホーム RA={home_ra} DEC={home_dec}", flush=True)
    print(f"現在値 RA={now_ra} DEC={now_dec}", flush=True)
    print(f"復帰量 RA={target_ra:+d} DEC={target_dec:+d}", flush=True)

    # 24bit符号付き差で一意に表せない移動は許可しない。
    # 低速補正関数と同じカウンター差表現を使う。
    for axis, move in ((1, target_ra), (2, target_dec)):
        if abs(move) >= 0x800000:
            raise RuntimeError(f"軸{axis} ホーム復帰量が範囲外")
        if abs(move) > 10:
            direction = 0 if move > 0 else 1
            send(ser, f"G{axis}0{direction}")
            send(ser, f"H{axis}" + hex24(abs(move)))

    # 両軸を高速GoTo。未移動軸には開始指令を送らない。
    if abs(target_ra) > 10:
        send(ser, "J1")
    if abs(target_dec) > 10:
        send(ser, "J2")

    start = time.monotonic()
    last_report = -5.0
    while True:
        s1, s2 = send(ser, "f1"), send(ser, "f2")
        if status_stopped(s1) and status_stopped(s2):
            break
        elapsed = time.monotonic() - start
        if elapsed - last_report >= 5:
            print(f"Home {elapsed:.0f}s RA={s1} DEC={s2}", flush=True)
            last_report = elapsed
        if elapsed > 180:
            raise TimeoutError("GoTo Home高速移動タイムアウト")
        time.sleep(0.2)

    # 高速GoTo終了の位置残差を、検証済みの低速GoToで補正する。
    if abs(target_ra) > 10:
        fine_goto_axis(ser, 1, now_ra, target_ra, ra_cpr, dec_cpr)
    if abs(target_dec) > 10:
        fine_goto_axis(ser, 2, now_dec, target_dec, ra_cpr, dec_cpr)

    # 追尾が止まっていても、カウンターを最終確認する。
    wait_axes_stopped(ser)
    err_ra = signed24_delta(read24(ser, "j1"), home_ra)
    err_dec = signed24_delta(read24(ser, "j2"), home_dec)
    print(f"ホーム残差 RA={err_ra:+d} DEC={err_dec:+d}", flush=True)
    if abs(err_ra) > 10 or abs(err_dec) > 10:
        raise RuntimeError("GoTo Home: ホーム残差が10カウントを超えました")
    print("GoTo Home完了・両軸停止・LED2消灯", flush=True)


def calculate_target(args, ra_cpr, dec_cpr):
    """SW1押下時のGPS測位値を使用し、従来のGoTo計算を実行する。"""
    # GPSから現在の時刻と観測地を取得する。
    utc, lat, lon = get_gps()
    jd = julian_date(utc)

    ra, dec = sun_coordinates(jd)
    lst = local_sidereal_time(jd, lon)
    ha = wrap180(lst - ra)

    altitude, azimuth = horizontal_coordinates(lat, dec, ha)

    # NCP・ウエイト鉛直下ホームからの移動角度。
    # NCPホーム・ウエイト鉛直下をRA時角-90度とする
    # DEC方向1で東側を向く実機配置に対応
    # 太陽の時角により、子午線の東西を判定する。
    # HA < 0 : 子午線の東側
    # HA >= 0: 子午線上または西側
    #
    # ホーム姿勢:
    # 望遠鏡はNCP方向、ウエイト軸は鉛直下。
    #
    # 子午線通過後はRA/DECの両方を反対側に回す。

    if ha < 0.0:
        ra_angle = ha + 90.0
        dec_angle = 90.0 - dec
        pier_mode = "MERIDIAN_EAST"
    else:
        ra_angle = ha - 90.0
        dec_angle = -(90.0 - dec)
        pier_mode = "MERIDIAN_WEST"

    ra_steps = round(abs(ra_angle) * ra_cpr / 360.0)
    dec_steps = round(abs(dec_angle) * dec_cpr / 360.0)

    # 既存の符号付き24bit差・低速位置補正で一意に扱える範囲に限定。
    # これを越える構成は無理に動かさず、実機別の座標管理を要する。
    if ra_steps >= 0x800000 or dec_steps >= 0x800000:
        raise RuntimeError(
            f"GoTo移動量が24bit安全範囲外: RA={ra_steps} DEC={dec_steps}"
        )

    print("\n===== 太陽座標 =====")
    print("UTC:", utc.isoformat())
    print(f"緯度: {lat:.8f}°")
    print(f"経度: {lon:.8f}°")
    print(f"太陽RA : {ra / 15:.8f} h ({ra:.8f}°)")
    print(f"太陽DEC: {dec:+.8f}°")
    print(f"太陽時角: {ha:+.8f}°")
    print(f"高度: {altitude:+.6f}°")
    print(f"方位角: {azimuth:.6f}°")

    print("\n===== NCPホームからの移動 =====")
    print(f"RA移動角 : {ra_angle:+.8f}°")
    print(f"DEC移動角: {dec_angle:+.8f}°")
    print(f"RAカウント : {ra_steps}")
    print(f"DECカウント: {dec_steps}")

    if args.ra_west_dir is None or args.dec_south_dir is None:
        print("\n方向対応未設定：計算のみで終了")
        print("AstroEQへの駆動指令は送りません。")
        return

    # 正の時角は西、負の時角は東。
    ra_dir = (
        args.ra_west_dir if ra_angle >= 0
        else 1 - args.ra_west_dir
    )

    # DECはNCPから赤緯を減少させる方向。
    dec_dir = (
        args.dec_south_dir if dec_angle >= 0
        else 1 - args.dec_south_dir
    )

    print("導入経路:", pier_mode)
    print("\n===== AstroEQコマンド =====")
    print(f"RA : G10{ra_dir} H1{hex24(ra_steps)} J1")
    print(f"DEC: G20{dec_dir} H2{hex24(dec_steps)} J2")

    if not args.execute:
        print("\n計算・コマンド生成のみ。駆動なし。")
        return

    if altitude <= 0.0 and not args.allow_below_horizon:
        raise RuntimeError("太陽が水平線以下のためGoToを禁止します")
    return ra_steps, dec_steps, ra_dir, dec_dir


def sun_above_horizon(utc, lat, lon):
    """GPS由来の時刻・位置で太陽の幾何学的高度を求める。"""
    jd = julian_date(utc)
    ra, dec = sun_coordinates(jd)
    ha = wrap180(local_sidereal_time(jd, lon) - ra)
    altitude, _ = horizontal_coordinates(lat, dec, ha)
    return altitude


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--allow-below-horizon", action="store_true",
        help="試験用: GPS測位が有効なら太陽高度0度以下でもGoToを許可",
    )
    parser.add_argument(
        "--astroeq-port", default=None,
        help="USB識別子が異なるAstroEQのポートを明示指定（例: /dev/serial/by-id/...）",
    )
    parser.add_argument("--inspect-controller", action="store_true",
                        help="AstroEQ情報を取得して終了。モーター駆動なし")
    parser.add_argument("--ra-west-dir", type=int, choices=(0, 1))
    parser.add_argument("--dec-south-dir", type=int, choices=(0, 1))
    args = parser.parse_args()

    # GPIO入力を初期化。SW1はGND接続、内部プルアップを利用する。
    from gpiozero import LED, Button

    if args.execute and (args.ra_west_dir is None or args.dec_south_dir is None):
        parser.error("--executeには両軸の方向指定が必要です")

    if args.inspect_controller or not args.execute:
        # 計算モードもUSB実機のCPRを使う。読み取りのみ（USBオープンで
        # Arduinoはリセットされ得る）。実行中の赤道儀には接続しない。
        port = find_astroeq_port(args.astroeq_port)
        with serial.Serial(port, BAUD, timeout=2, write_timeout=2) as ser:
            time.sleep(2)
            ra_cpr, dec_cpr = read_controller_params(ser)
        if not args.inspect_controller:
            calculate_target(args, ra_cpr, dec_cpr)
        return

    with LED(21) as led1, Button(22, pull_up=True, bounce_time=0.05) as sw1:
        led1.off()
        print("GPS測位・太陽高度確認中。LED1は条件成立時に点灯します", flush=True)
        print("望遠鏡NCP方向・ウエイト軸鉛直下にセットしてください", flush=True)

        # ホーム復帰成功時はここに戻り、新しいGPS測位結果でLED1を更新する。
        # SW1は一度離されてから押された場合のみ受け付ける。
        while True:
            # SW1押しっぱなし起動は無効にする。必ず一度離してから押す。
            sw1_armed = not sw1.is_pressed
            next_check = 0.0
            permitted = False
            last_altitude = None
            while True:
                now = time.monotonic()
                if now >= next_check:
                    # GPSフィックスの有無を更新し、太陽の地平線上/下を判定。
                    # get_gps() が測位待ちになる間はSW1は処理されない。
                    try:
                        utc, lat, lon = get_gps()
                        last_altitude = sun_above_horizon(utc, lat, lon)
                        permitted = (
                            -90.0 <= lat <= 90.0
                            and -180.0 <= lon <= 180.0
                            and (last_altitude > 0.0 or args.allow_below_horizon)
                        )
                    except Exception as exc:
                        permitted = False
                        last_altitude = None
                        print(f"GPS/高度更新失敗: {exc}", flush=True)

                    if permitted:
                        led1.on()
                    else:
                        led1.off()
                    print(
                        f"LED1={'点灯' if permitted else '消灯'} "
                        f"太陽高度={last_altitude if last_altitude is not None else '不明'}",
                        flush=True,
                    )
                    next_check = time.monotonic() + 15.0

                if not sw1_armed and not sw1.is_pressed:
                    sw1_armed = True
                if sw1_armed and sw1.is_pressed:
                    if not permitted:
                        print(
                            "SW1無効: GPS未測位または太陽が水平線以下"
                            if not args.allow_below_horizon else "SW1無効: GPS未測位",
                            flush=True,
                        )
                        sw1_armed = False
                    else:
                        print("SW1押下: 太陽GoToを開始します", flush=True)
                        break
                time.sleep(0.1)

            # 駆動中はSW1入力を受付けない。
            # 機種固有USB番号を使わず、その時点で接続中の候補を検出。
            port = find_astroeq_port(args.astroeq_port)
            with serial.Serial(
                port, BAUD, timeout=2, write_timeout=2
            ) as ser:

                time.sleep(2)

                # 移動指令前にファームウェア応答・CPR等を確認する。
                # 停止を確認してからGPSを再取得し、実機CPRで目標計算。
                ra_cpr, dec_cpr = read_controller_params(ser)
                prepare_axis(ser, 1)
                prepare_axis(ser, 2)
                try:
                    ra_steps, dec_steps, ra_dir, dec_dir = calculate_target(
                        args, ra_cpr, dec_cpr
                    )
                except BaseException:
                    led1.off()
                    raise

                before1 = read24(ser, "j1")
                before2 = read24(ser, "j2")

                print("RA開始位置:", before1)
                print("DEC開始位置:", before2)

                # 駆動中の例外では両軸の停止を試みる。
                motion_requested = False

                try:
                    if ra_steps:
                        send(ser, f"G10{ra_dir}")
                        send(ser, "H1" + hex24(ra_steps))

                    if dec_steps:
                        send(ser, f"G20{dec_dir}")
                        send(ser, "H2" + hex24(dec_steps))

                    motion_requested = True

                    if ra_steps:
                        send(ser, "J1")
                    if dec_steps:
                        send(ser, "J2")

                    print("太陽GoTo開始", flush=True)
                    start = time.monotonic()
                    last_report = -5

                    while True:
                        time.sleep(1)

                        s1 = send(ser, "f1")
                        s2 = send(ser, "f2")

                        elapsed = time.monotonic() - start

                        if elapsed - last_report >= 5:
                            pos1 = read24(ser, "j1")
                            pos2 = read24(ser, "j2")
                            print(
                                f"{elapsed:.0f}s "
                                f"RA={s1} pos={pos1} "
                                f"DEC={s2} pos={pos2}",
                                flush=True
                            )
                            last_report = elapsed

                            if elapsed >= 20:
                                if ((ra_steps and pos1 == before1) or
                                        (dec_steps and pos2 == before2)):
                                    raise RuntimeError(
                                        "位置カウンター変化なし: 停止します"
                                    )

                        done1 = status_stopped(s1)
                        done2 = status_stopped(s2)

                        if done1 and done2:
                            break

                        if time.monotonic() - start > TIMEOUT:
                            raise TimeoutError("GoToタイムアウト")

                    # 高速GoTo後、残りを低速GoToで補正する。
                    expected1 = ra_steps * (1 if ra_dir == 0 else -1)
                    expected2 = dec_steps * (1 if dec_dir == 0 else -1)

                    if ra_steps:
                        fine_goto_axis(ser, 1, before1, expected1, ra_cpr, dec_cpr)

                    if dec_steps:
                        fine_goto_axis(ser, 2, before2, expected2, ra_cpr, dec_cpr)

                    after1 = read24(ser, "j1")
                    after2 = read24(ser, "j2")

                    def delta(after, before):
                        return (
                            (after - before + 0x800000)
                            % 0x1000000
                        ) - 0x800000

                    d1 = delta(after1, before1)
                    d2 = delta(after2, before2)

                    expected1 = ra_steps * (1 if ra_dir == 0 else -1)
                    expected2 = dec_steps * (1 if dec_dir == 0 else -1)

                    print("\n===== GoTo結果 =====")
                    print("RA移動カウント:", d1)
                    print("DEC移動カウント:", d2)
                    print("RA移動角:", d1 * 360 / ra_cpr)
                    print("DEC移動角:", d2 * 360 / dec_cpr)

                    # カウンター上の目標到達を確認する。
                    if (abs(d1 - expected1) > 10 or
                            abs(d2 - expected2) > 10):
                        raise RuntimeError(
                            "目標カウントとの差が大きすぎます"
                        )

                    print("太陽GoTo完了・両軸停止")

                    # ----------------------------------------
                    # 太陽時追尾（RA軸のみ）
                    # ----------------------------------------
                    from gpiozero import LED, Button

                    led2 = LED(20)
                    led2.off()
                    # SW2: GPIO26、GNDへ接続したプルアップ入力。
                    sw2 = Button(26, pull_up=True, bounce_time=0.05)
                    home_requested = False
                    sw2_armed = not sw2.is_pressed
                    if not sw2_armed:
                        print("SW2を離すまでホーム指令を受け付けません", flush=True)

                    try:
                        # AstroEQからRA軸の設定値を取得する。
                        cpr = read24(ser, "a1")
                        freq = read24(ser, "b1")

                        # 平均太陽時追尾のステップ周期。
                        period = round(freq * 86400.0 / cpr)

                        if not 1 <= period <= 0xFFFFFF:
                            raise RuntimeError("追尾周期が不正です")

                        actual_day = cpr * period / freq

                        print("===== 太陽時追尾 =====", flush=True)
                        print("RA CPR:", cpr, flush=True)
                        print("Timer:", freq, flush=True)
                        print("周期:", period, flush=True)
                        print(
                            f"設定上のRA一周時間: "
                            f"{actual_day:.3f}秒",
                            flush=True
                        )

                        # GoTo完了後の停止状態を確認する。
                        status = send(ser, "f1")
                        if not status_stopped(status):
                            raise RuntimeError(
                                f"RA停止状態異常: {status}"
                            )

                        # RA方向0で太陽時追尾を開始する。
                        send(ser, "G110")
                        send(ser, "I1" + hex24(period))
                        send(ser, "J1")

                        # 実際に駆動中状態へ移行したことを確認。
                        tracking_ok = False
                        for _ in range(10):
                            time.sleep(0.2)
                            status = send(ser, "f1")

                            if (
                                len(status) == 3
                                and (int(status[0], 16) & 1)
                                and (int(status[1], 16) & 1)
                                and (int(status[2], 16) & 1)
                            ):
                                tracking_ok = True
                                break

                        if not tracking_ok:
                            raise RuntimeError(
                                f"RA追尾開始を確認できません: {status}"
                            )

                        # 追尾開始を確認したのでLED2点灯。
                        led2.on()
                        print(
                            "太陽時追尾開始・LED2点灯",
                            flush=True
                        )

                        # 追尾状態を継続監視する。
                        start_track = time.monotonic()

                        next_report = time.monotonic() + 5
                        while True:
                            time.sleep(0.1)

                            # SW2は一度離されてから押されたときのみ有効。
                            if not sw2_armed and not sw2.is_pressed:
                                sw2_armed = True
                            if sw2_armed and sw2.is_pressed:
                                home_requested = True
                                print("SW2押下：太陽追尾を停止しGoTo Homeへ", flush=True)
                                break

                            # 既存どおり約5秒ごとに追尾の正常状態を確認。
                            if time.monotonic() < next_report:
                                continue
                            next_report = time.monotonic() + 5
                            status = send(ser, "f1")

                            if not (
                                len(status) == 3
                                and (int(status[0], 16) & 1)
                                and (int(status[1], 16) & 1)
                                and (int(status[2], 16) & 1)
                            ):
                                raise RuntimeError(
                                    f"RA追尾状態異常: {status}"
                                )

                            elapsed = time.monotonic() - start_track
                            print(
                                f"追尾中: {elapsed:.0f}秒 "
                                f"RA状態={status}",
                                flush=True
                            )

                    except KeyboardInterrupt:
                        print(
                            "追尾終了要求を受信",
                            flush=True
                        )

                    finally:
                        # LEDを消灯する。
                        led2.off()
                        led2.close()
                        sw2.close()
                        print("LED2消灯", flush=True)

                    if home_requested:
                        # ホーム復帰を始める前にLED1を消灯する。
                        # GPSの有効性と太陽高度は、ホーム復帰後に再判定する。
                        led1.off()
                        print("LED1消灯：GoTo Home実行中", flush=True)
                        # 停止要求が通らない場合は危険なのでホーム移動を行わない。
                        stop_both(ser)
                        wait_axes_stopped(ser)
                        goto_home(ser, before1, before2, ra_cpr, dec_cpr)

                    # 既存の外側finallyでも両軸の停止指令を送信する。


                finally:
                    if motion_requested:
                        stop_both(ser)

            # シリアルポートを閉じてから次の待機サイクルへ。
            # ホーム未実行（Ctrl+C等）の場合は元どおりプログラム終了。
            if not home_requested:
                break
            # GPSの有効性を新たに確認するまでLED1を消灯したままにする。
            led1.off()
            print("ホーム復帰完了。GPS再測位後、SW1入力待ちに戻ります", flush=True)


if __name__ == "__main__":
    main()
