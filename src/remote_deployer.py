#!/usr/bin/env python3
"""
remote_deployer.py — Полуавтоматический инструмент для организации удаленного доступа.
Поддерживает: Windows (RDP), Linux (xrdp), кроссплатформенный AnyDesk.
Требует прав администратора на удаленной машине для режимов RDP/xrdp.
"""

import argparse
import sys
import subprocess
import os
import platform
import requests
import tempfile

# --- Библиотеки для удаленного выполнения ---
try:
    import winrm
    import paramiko
except ImportError as e:
    print(f"Ошибка: не установлены необходимые библиотеки: {e}")
    print("Установите их командой: pip install pywinrm paramiko requests")
    sys.exit(1)

class RemoteDeployer:
    def __init__(self, host, user, password, os_type):
        self.host = host
        self.user = user
        self.password = password
        self.os_type = os_type.lower()
        self.session = None

    def connect(self):
        """Устанавливает соединение с удаленным хостом."""
        print(f"[*] Подключение к {self.host} ({self.os_type})...")
        if self.os_type == "windows":
            try:
                self.session = winrm.Session(
                    f'http://{self.host}:5985/wsman',
                    auth=(self.user, self.password),
                    transport='ntlm'  # Или 'basic', если настроено
                )
                # Проверка соединения
                result = self.session.run_cmd('hostname')
                if result.status_code == 0:
                    print(f"[+] Успешное подключение к Windows-хосту: {result.std_out.decode().strip()}")
                    return True
                else:
                    print(f"[-] Ошибка выполнения команды: {result.std_err.decode()}")
                    return False
            except Exception as e:
                print(f"[-] Ошибка подключения к Windows: {e}")
                return False
        elif self.os_type in ["linux", "ubuntu", "astralinux", "mint"]:
            try:
                self.session = paramiko.SSHClient()
                self.session.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.session.connect(self.host, username=self.user, password=self.password)
                print("[+] SSH-соединение установлено.")
                return True
            except Exception as e:
                print(f"[-] Ошибка SSH-подключения: {e}")
                return False
        else:
            print(f"[-] Неподдерживаемая ОС: {self.os_type}")
            return False

    def _run_linux_cmd(self, cmd):
        """Выполняет команду на Linux-хосте."""
        if not self.session:
            return None
        stdin, stdout, stderr = self.session.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status == 0:
            return stdout.read().decode().strip()
        else:
            print(f"[-] Ошибка выполнения '{cmd}': {stderr.read().decode().strip()}")
            return None

    def _run_windows_ps(self, script):
        """Выполняет PowerShell-скрипт на Windows-хосте."""
        if not self.session:
            return None
        result = self.session.run_ps(script)
        if result.status_code == 0:
            return result.std_out.decode().strip()
        else:
            print(f"[-] Ошибка PowerShell: {result.std_err.decode().strip()}")
            return None

    # --- Режим 1: Включение RDP на Windows ---
    def enable_windows_rdp(self):
        print("[*] Включение RDP на Windows через реестр...")
        ps_script = r"""
        # Включаем RDP
        Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name 'fDenyTSConnections' -Value 0
        # Включаем правило брандмауэра для RDP
        Enable-NetFirewallRule -DisplayGroup "Remote Desktop"
        # (Опционально) Отключаем NLA, если нужно — может потребовать перезагрузки
        # Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name 'UserAuthentication' -Value 0
        """
        result = self._run_windows_ps(ps_script)
        if result is not None:
            print("[+] RDP успешно включен. Порт: 3389.")
            return True
        return False

    # --- Режим 2: Установка xrdp на Linux ---
    def install_linux_xrdp(self):
        print("[*] Установка и настройка xrdp на Linux...")
        # Обновление и установка
        self._run_linux_cmd("sudo apt-get update -y")
        self._run_linux_cmd("sudo apt-get install -y xrdp")
        # Включение и запуск службы
        self._run_linux_cmd("sudo systemctl enable xrdp")
        self._run_linux_cmd("sudo systemctl start xrdp")
        # Открытие порта в UFW (если используется)
        self._run_linux_cmd("sudo ufw allow 3389/tcp 2>/dev/null || true")
        # Проверка статуса
        status = self._run_linux_cmd("systemctl is-active xrdp")
        if status and "active" in status:
            print("[+] xrdp успешно установлен и запущен. Порт: 3389.")
            return True
        else:
            print("[-] Не удалось запустить xrdp. Проверьте логи.")
            return False

    # --- Режим 3: Развертывание AnyDesk (портативно/тихо) ---
    def deploy_anydesk(self):
        print("[*] Развертывание AnyDesk...")
        if self.os_type == "windows":
            return self._deploy_anydesk_windows()
        elif self.os_type in ["linux", "ubuntu", "astralinux", "mint"]:
            return self._deploy_anydesk_linux()
        return False

    def _deploy_anydesk_windows(self):
        """Скачивает и запускает AnyDesk на Windows с установкой пароля."""
        # Пароль и соль можно сгенерировать или задать статически для сессии
        # В реальности лучше использовать уникальный пароль
        anydesk_pass = "YourStrongPassword123"  # Замените на свой
        # Хэш и соль для AnyDesk (пример, нужно сгенерировать свой)
        # Для простоты используем параметр --set-password, который работает в новых версиях
        ps_script = f"""
        $url = "https://download.anydesk.com/AnyDesk.exe"
        $out = "C:\\Windows\\Temp\\AnyDesk.exe"
        Invoke-WebRequest -Uri $url -OutFile $out
        # Тихая установка
        Start-Process -FilePath $out -ArgumentList "--install", "C:\\ProgramData\\AnyDesk", "--start-with-win", "--silent" -Wait
        # Установка пароля (может потребовать прав)
        Start-Process -FilePath "C:\\ProgramData\\AnyDesk\\AnyDesk.exe" -ArgumentList "--set-password", "{anydesk_pass}" -Wait
        # Получение ID
        $id = & "C:\\ProgramData\\AnyDesk\\AnyDesk.exe" --get-id
        Write-Output "AnyDesk ID: $id"
        Write-Output "AnyDesk Password: {anydesk_pass}"
        """
        result = self._run_windows_ps(ps_script)
        if result:
            print(result)
            print("[+] AnyDesk установлен. Используйте ID и пароль выше для подключения.")
            return True
        return False

    def _deploy_anydesk_linux(self):
        """Скачивает и запускает AnyDesk на Linux."""
        # Для Linux можно использовать .deb пакет или портативную версию (AppImage)
        # Проверяем архитектуру
        arch = self._run_linux_cmd("uname -m")
        if "x86_64" in arch:
            url = "https://download.anydesk.com/linux/anydesk-6.3.2-amd64.deb"  # Пример URL
        else:
            print("[-] Неподдерживаемая архитектура Linux.")
            return False
        # Скачивание и установка
        self._run_linux_cmd(f"wget -O /tmp/anydesk.deb {url}")
        self._run_linux_cmd("sudo dpkg -i /tmp/anydesk.deb || sudo apt-get install -f -y")
        # Запуск (может потребовать графической сессии)
        self._run_linux_cmd("anydesk --get-id")
        print("[+] AnyDesk установлен на Linux. Запустите его вручную для получения ID.")
        return True

def main():
    parser = argparse.ArgumentParser(description="Инструмент удаленного развертывания доступа")
    parser.add_argument("--host", required=True, help="IP-адрес или имя хоста")
    parser.add_argument("--user", required=True, help="Имя пользователя")
    parser.add_argument("--password", required=True, help="Пароль")
    parser.add_argument("--os", required=True, choices=["windows", "linux", "ubuntu", "astralinux", "mint"], help="Тип ОС")
    parser.add_argument("--method", required=True, choices=["rdp", "xrdp", "anydesk"], help="Метод развертывания")
    args = parser.parse_args()

    deployer = RemoteDeployer(args.host, args.user, args.password, args.os)
    if not deployer.connect():
        sys.exit(1)

    success = False
    if args.method == "rdp":
        if args.os != "windows":
            print("[-] Метод RDP применим только к Windows.")
        else:
            success = deployer.enable_windows_rdp()
    elif args.method == "xrdp":
        if args.os == "windows":
            print("[-] Метод xrdp применим только к Linux.")
        else:
            success = deployer.install_linux_xrdp()
    elif args.method == "anydesk":
        success = deployer.deploy_anydesk()

    if success:
        print("\n[✅] Задача выполнена успешно.")
    else:
        print("\n[❌] Не удалось выполнить развертывание. Проверьте права и настройки сети.")
        sys.exit(1)

if __name__ == "__main__":
    main()