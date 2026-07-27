import os
import socket
import time
import threading
import subprocess
import RPi.GPIO
import config
from typing import Type, Optional
from modem_handler import ModemHandler
from logging_controller import LoggingController


class InternetWatchdog:
    def __init__(
        self,
        interval: int = 60 * 60,
        modem_handler_instance: Type[ModemHandler] = None,
        logging_controller_instance: Optional[LoggingController] = None,
    ):
        self.interval = interval
        self.modem_handler_instance = modem_handler_instance
        self.logging_controller_instance = logging_controller_instance
        self.error_counter = 0
        self.GPIO = RPi.GPIO
        self.GPIO.setmode(self.GPIO.BOARD)
        self.GPIO.setwarnings(False)
        self.GPIO.setup(config.INTERNET_WATCHDOG_MODEM_POWER_PIN, self.GPIO.OUT)
        self.GPIO.output(config.INTERNET_WATCHDOG_MODEM_POWER_PIN, False)
        print(f"Internet watchdog enabled, checking connection every: {self.interval} seconds")
        self.thread = threading.Thread(target=self._watchdog_worker)
        self.thread.daemon = True
        self.thread.start()

    def get_error_count(self) -> int:
        return self.error_counter

    def _watchdog_worker(self) -> None:
        while True:
            time.sleep(self.interval)
            if self._internet_connected() and self.modem_handler_instance.get_mm_number() != -1:
                self.error_counter = 0
                continue
            self.error_counter += 1
            print(f"No Internet connection / Modem found. Counter: {self.error_counter}. Attempting recovery...")

            # Stufe 1: Sanfter Software-Reset via mmcli
            if self._reset_modem():
                print("Recovery successful via software reset.")
                continue

            # Stufe 2: GPIO Power-Pin (vorhandene Logik)
            print("Software reset failed. Trying GPIO power cycle...")
            self._restart_modem()
            time.sleep(15)
            if self._internet_connected() and self.modem_handler_instance.get_mm_number() != -1:
                print("Recovery successful via GPIO power cycle.")
                continue

            # Stufe 3: USB-Power-Cycle via uhubctl
            print("GPIO power cycle failed. Performing USB power cycle via uhubctl...")
            self._usb_power_cycle()

    # ── Stufe 1 ────────────────────────────────────────────────────────────────

    def _reset_modem(self) -> bool:
        """Sanfter Software-Reset via mmcli – kein USB-Eingriff nötig."""
        try:
            modem_number = self.modem_handler_instance.get_mm_number()
            if modem_number == -1:
                print("No modem found via mmcli, cannot do software reset.")
                return False
            print(f"Resetting modem {modem_number} via mmcli...")
            result = subprocess.run(
                ["mmcli", "-m", str(modem_number), "--reset"],
                capture_output=True, text=True, check=True
            )
            print("Modem software reset successful:", result.stdout)
            time.sleep(15)  # Modem braucht Zeit zum Neustart
            return self._internet_connected() and self.modem_handler_instance.get_mm_number() != -1
        except subprocess.CalledProcessError as e:
            print("Software reset failed:", e.stderr)
            return False

    # ── Stufe 2 ────────────────────────────────────────────────────────────────

    def _restart_modem(self) -> None:
        """GPIO Power-Pin kurz schalten (vorhandene Logik)."""
        print("Toggling GPIO modem power pin...")
        self.GPIO.output(config.INTERNET_WATCHDOG_MODEM_POWER_PIN, True)
        time.sleep(5)
        self.GPIO.output(config.INTERNET_WATCHDOG_MODEM_POWER_PIN, False)

    # ── Stufe 3 ────────────────────────────────────────────────────────────────

    def _usb_power_cycle(self) -> None:
        """
        Vollständiger USB-Power-Cycle via uhubctl.
        Da alle Ports am Pi ganged sind, werden ALLE USB-Ports kurz abgeschaltet
        (inkl. USB-Stick) → vorher sauber unmounten, danach wieder mounten.
        """
        self._unmount_usb_stick()
        self._uhubctl_power("off")
        time.sleep(5)   # Modem muss komplett stromlos werden
        self._uhubctl_power("on")
        time.sleep(15)  # Zeit für USB-Enumeration und Modem-Init
        self._mount_usb_stick()

        # Warten bis Modem von ModemManager erkannt wird (max. 60s)
        print("Waiting for modem to re-enumerate...")
        for _ in range(12):
            if self.modem_handler_instance.get_mm_number() != -1:
                print("Modem re-detected after USB power cycle.")
                return
            time.sleep(5)
        print("WARNING: Modem not detected after USB power cycle!")

    def _uhubctl_power(self, action: str) -> None:
        """Schaltet alle USB-Ports des Pi via uhubctl ein oder aus.
        -l 1-1 = Haupt-Hub, -p 2 = Kontrollport (ganged → alle Ports betroffen)
        """
        try:
            result = subprocess.run(
                ["uhubctl", "-l", "1-1", "-p", "2", "-a", action],
                capture_output=True, text=True
            )
            print(f"uhubctl {action}: {result.stdout.strip()}")
        except FileNotFoundError:
            print("ERROR: uhubctl not found! Install with: apt-get install uhubctl")
        except Exception as e:
            print(f"uhubctl {action} failed: {e}")

    def _unmount_usb_stick(self) -> None:
        """Unmountet den USB-Stick sauber bevor der USB-Bus abgeschaltet wird."""
        if not os.path.ismount('/mnt/storage'):
            print("USB stick not mounted, skipping unmount.")
            return
        # Kurz warten damit laufende rsync/write-Ops abschliessen können
        print("Waiting for pending write operations before unmount...")
        time.sleep(3)
        try:
            subprocess.run(["umount", "/mnt/storage"], check=True, timeout=30)
            print("USB stick unmounted successfully.")
        except subprocess.CalledProcessError:
            print("Unmount failed, trying lazy unmount...")
            try:
                # Lazy unmount: trennt sofort, wartet auf letzten Zugriff
                subprocess.run(["umount", "-l", "/mnt/storage"], check=True, timeout=10)
                print("Lazy unmount successful.")
            except Exception as e:
                print(f"Lazy unmount also failed: {e}")

    def _mount_usb_stick(self) -> None:
        """Mountet den USB-Stick nach dem USB-Power-Cycle wieder ein."""
        if os.path.ismount('/mnt/storage'):
            print("USB stick already mounted.")
            return
        try:
            # mount -a liest /etc/fstab und mountet alles was dort konfiguriert ist
            subprocess.run(["mount", "-a"], check=True, timeout=15)
            if os.path.ismount('/mnt/storage'):
                print("USB stick mounted successfully.")
            else:
                print("WARNING: USB stick not mounted after mount -a!")
        except Exception as e:
            print(f"Mount failed: {e}")

    # ── Hilfsfunktionen ────────────────────────────────────────────────────────

    # Adopted from: https://stackoverflow.com/questions/3764291
    def _internet_connected(self, host: str = "1.1.1.1", port: int = 53, timeout: int = 10) -> bool:
        try:
            socket.setdefaulttimeout(timeout)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            return True
        except socket.error:
            return False


if __name__ == "__main__":
    wd = InternetWatchdog(10)
    while True:
        time.sleep(1)
