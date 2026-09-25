#!/usr/bin/env python3
"""Hermes watchdog: kiểm tra và fix deep sleep trên phone mỗi 30 phút.
Chạy qua ADB — phone luôn connected tới VM này."""

import subprocess, json, time, sys, os

ADB = "adb shell"

def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() + r.stderr.strip()
    except Exception as e:
        return f"ERR:{e}"

def adb(cmd, timeout=15):
    return run(f"{ADB} \"{cmd}\"", timeout)

def main():
    # 1. Check device connected
    devices = run("adb devices -l")
    if "device " not in devices or "offline" in devices:
        print("NO_DEVICE")
        return  # silent exit

    changes = []

    # 2. Trick battery: USB powered → false (ADB still works!)
    # Phone won't enter deep idle while "charging"
    usb = adb("dumpsys battery get usb")
    status = adb("dumpsys battery get status")
    if usb == "true" or status != "1":
        adb("dumpsys battery set status 1")
        adb("dumpsys battery set usb 0")
        adb("dumpsys battery set ac 0")
        changes.append(f"battery_trick:usb={usb}_status={status}->discharging")

    # 3. Check power save mode
    psm = adb("settings get system POWER_SAVE_MODE_OPEN")
    if psm != "1":
        adb("settings put system POWER_SAVE_MODE_OPEN 1")
        changes.append(f"power_save_mode:{psm}->1")

    # 4. Check WiFi/BT scanning
    for k in ["wifi_scan_always_enabled", "ble_scan_always_enabled"]:
        v = adb(f"settings get global {k}")
        if v != "0":
            adb(f"settings put global {k} 0")
            changes.append(f"{k}:{v}->0")

    # 5. Check app standby
    appst = adb("settings get global app_standby_enabled")
    if appst != "1":
        adb("settings put global app_standby_enabled 1")
        changes.append(f"app_standby:{appst}->1")

    # 6. Check if CPU is stuck high — sensor abuse
    cpu_freq = adb("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null")
    try:
        freq_mhz = int(cpu_freq.strip()) // 1000
    except:
        freq_mhz = 0

    wakefulness = adb("dumpsys power | grep mWakefulness | sed 's/.*=//'").strip().split('\n')[0]
    state = adb("dumpsys deviceidle | grep 'mState=' | sed 's/.*=//'").strip()
    charging = adb("dumpsys deviceidle | grep mCharging | sed 's/.*=//'").strip()

    # 7. Force idle if we tricked battery but haven't reached IDLE yet
    if state not in ("IDLE", "IDLE_MAINTENANCE") and charging == "false" and 'Awake' not in wakefulness:
        adb("cmd deviceidle step light 2>/dev/null")
        adb("cmd deviceidle step deep 2>/dev/null")
        adb("cmd deviceidle force-idle 2>/dev/null")
        changes.append(f"forced_idle:{state}->override")

    # 8. Check significant motion sensor abuse
    sig_count = adb("dumpsys sensorservice 2>/dev/null | grep '0x00000011.*active-count' | sed 's/.*active-count=//' | tail -1")
    try:
        sig = int(sig_count)
    except:
        sig = 0

    # 9. Kill sensor hogs if needed
    if sig >= 2:
        adb("am force-stop com.google.android.gms")
        adb("am force-stop com.google.android.googlequicksearchbox")
        changes.append("killed_gms_sensor_hog")
    
    # 10. CPU stuck high + screen off + dozing = sensor lock
    if freq_mhz >= 1700 and 'Dozing' in wakefulness:
        adb("am force-stop com.google.android.gms")
        adb("am force-stop com.google.android.gms.persistent")
        adb("am force-stop com.google.android.googlequicksearchbox")
        changes.append(f"cpu_stuck_{freq_mhz}mhz_killed_gms")

    # 11. If still Awake after everything, force stop big hogs
    if 'Awake' in wakefulness and state != "IDLE":
        adb("am force-stop com.mi.globalminusscreen")
        adb("am force-stop com.mi.appfinder")
        changes.append("force_stopped_miui_hogs")

    # Report (only report real problems, INACTIVE is normal during transition)
    real_problems = [c for c in changes if 'forced_idle' not in c or 'Awake' in wakefulness]
    if real_problems or (freq_mhz >= 1700 and state not in ("IDLE", "IDLE_MAINTENANCE")):
        print(f"FIXED: {' | '.join(changes)}")
        print(f"CPU:{freq_mhz}MHz | State:{state} | Wake:{wakefulness} | Charge:{charging} | SigSensor:{sig}")
    else:
        pass  # Silent

if __name__ == "__main__":
    main()
