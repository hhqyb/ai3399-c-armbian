"""Flash the AI3399-C Armbian image to the SD card through adb.

On the Android side used to flash this board, the SD card is
/dev/block/mmcblk1. After Linux boots, that card is mmcblk1p1.

Usage:
  python flash_sd.py ai3399-c-armbian.img
  python flash_sd.py ai3399-c-armbian.img --no-reboot
"""
import argparse
import os
import subprocess
import sys
import threading
import time

DEFAULT_ADB = r"D:\Software\platform-tools\adb.exe"

def adb_bin():
    env = os.environ.get("ADB")
    if env:
        return env
    if os.path.exists(DEFAULT_ADB):
        return DEFAULT_ADB
    return "adb"

def logp(msg):
    print(msg, flush=True)

def main():
    parser = argparse.ArgumentParser(description="dd an AI3399-C Armbian image to the SD card via adb")
    parser.add_argument("image")
    parser.add_argument("--no-reboot", action="store_true")
    parser.add_argument("--serial", default=os.environ.get("SERIAL_PORT", "COM4"))
    parser.add_argument("--baud", type=int, default=int(os.environ.get("SERIAL_BAUD", "1500000")))
    args = parser.parse_args()
    if not os.path.isfile(args.image):
        raise SystemExit("no such image: " + args.image)
    size = os.path.getsize(args.image)
    if size < 800 * 1024 * 1024:
        raise SystemExit("image looks too small: %d" % size)
    with open(args.image, "rb") as handle:
        head = handle.read(512)
    if head[510:512] != b"\x55\xaa":
        raise SystemExit("image has no MBR signature")
    adb = adb_bin()
    remote = "/data/local/tmp/armbian.img"
    logp("push %s (%d bytes)" % (args.image, size))
    rc = subprocess.call([adb, "push", args.image, remote])
    if rc != 0:
        raise SystemExit("adb push failed")
    logp("writing SD card")
    cmd = "dd if=%s of=/dev/block/mmcblk1 bs=1048576 conv=fsync && sync && echo DD_OK && rm -f %s && echo RM_OK" % (remote, remote)
    proc = subprocess.run([adb, "shell", cmd], capture_output=True)
    logp(proc.stdout.decode("utf-8", "replace"))
    logp(proc.stderr.decode("utf-8", "replace"))
    if b"DD_OK" not in proc.stdout:
        raise SystemExit("dd failed")
    if args.no_reboot:
        logp("left the board running")
        return
    try:
        import serial
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyserial"])
        import serial
    ser = serial.Serial(args.serial, args.baud, timeout=0.2)
    buf = bytearray()
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            data = ser.read(8192)
            if data:
                buf.extend(data)

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(0.3)
    logp("reboot")
    subprocess.run([adb, "reboot"], check=False)
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(5)
        logp("t=%d serial_bytes=%d" % (int(time.time() - t0), len(buf)))
    stop.set()
    time.sleep(0.4)
    ser.close()
    text = buf.decode("utf-8", "replace")
    log_path = os.path.join(os.path.dirname(os.path.abspath(args.image)), "bootlog.txt")
    open(log_path, "w", encoding="utf-8", errors="replace").write(text)
    logp("--- TAIL ---")
    print(text[-4000:])

if __name__ == "__main__":
    main()
