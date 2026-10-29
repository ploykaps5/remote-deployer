#!/usr/bin/env python3
"""
remote_deployer.py — Полуавтоматический инструмент удалённого развёртывания доступа.

Возможности:
  • Проверка доступности портов (WinRM 5985 / SSH 22 / RDP 3389) перед подключением
  • Интерактивный режим при отсутствии аргументов CLI
  • Логирование в файл рядом с бинарником/скриптом (с фолбэком в CWD и домашнюю папку)
"""
import argparse
import getpass
import logging
import os
import socket
import sys
import time
from typing import Optional

try:
    import winrm
    import paramiko
except ImportError as e:
    print(f"Ошибка: не установлены необходимые библиотеки: {e}")
    print("Установите их командой: pip install pywinrm paramiko requests")
    sys.exit(1)


# ---------- Логирование ----------
def setup_logging() -> tuple[logging.Logger, str]:
    """Лог в файл рядом с exe/скриптом, с фолбэком в CWD и домашнюю папку."""
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(os.path.dirname(sys.executable))
    else:
        candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    candidates.append(os.getcwd())
    candidates.append(os.path.expanduser("~"))

    logger = logging.getLogger("remote_deployer")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    log_path = "(лог-файл недоступен)"
    for d in candidates:
        try:
            p = os.path.join(d, "remote_deployer.log")
            fh = logging.FileHandler(p, mode="a", encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(fmt)
            logger.addHandler(fh)
            log_path = p
            break
        except OSError:
            continue

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger, log_path


log = logging.getLogger("remote_deployer")


# ---------- Проверка портов ----------
def check_port(host: str, port: int, timeout: float = 3.0) -> tuple[bool, int, Optional[str]]:
    """TCP-проверка порта. Возвращает (ok, elapsed_ms, error)."""
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
        return True, int((time.monotonic() - start) * 1000), None
    except socket.timeout:
        return False, int((time.monotonic() - start) * 1000), f"timeout ({timeout:.0f}s)"
    except ConnectionRefusedError:
        return False, int((time.monotonic() - start) * 1000), "connection refused"
    except OSError as e:
        return False, int((time.monotonic() - start) * 1000), str(e)


def preflight_check(host: str, os_type: str, method: str) -> dict[int, tuple[bool, int, Optional[str]]]:
    """Проверяет порты, необходимые для выбранного метода."""
    ports: list[int] = []
    if os_type == "windows":
        ports.append(5985)          # WinRM — критичный
    else:
        ports.append(22)            # SSH — критичный
    if method in ("rdp", "xrdp"):
        ports.append(3389)          # RDP — информативный

    results: dict[int, tuple[bool, int, Optional[str]]] = {}
    log.info("=== Проверка доступности портов ===")
    for port in ports:
        ok, ms, err = check_port(host, port)
        results[port] = (ok, ms, err)
        if ok:
            log.info(f"  {host}:{port} — открыт ({ms} ms)")
        else:
            log.warning(f"  {host}:{port} — закрыт/недоступен ({ms} ms, {err})")
    log.info("===================================")
    return results


# ---------- Основной класс ----------
class RemoteDeployer:
    def __init__(self, host: str, user: str, password: str, os_type: str):
        self.host = host
        self.user = user
        self.password = password
        self.os_type = os_type.lower()
        self.session = None

    def connect(self) -> bool:
        log.info(f"Подключение к {self.host} ({self.os_type})...")
        if self.os_type == "windows":
            if "\\" not in self.user and "@" not in self.user:
                log.warning(
                    f"Логин '{self.user}' без домена/компьютера. Для локальной учётной "
                    f"записи используйте '.\\{self.user}' или 'ИМЯ_КОМПЬЮТЕРА\\{self.user}'."
                )
            try:
                self.session = winrm.Session(
                    f"http://{self.host}:5985/wsman",
                    auth=(self.user, self.password),
                    transport="ntlm",
                )
                result = self.session.run_cmd("hostname")
                if result.status_code == 0:
                    hostname = result.std_out.decode(errors="replace").strip()
                    log.info(f"Успешное подключение к Windows-хосту: {hostname}")
                    return True
                log.error(f"Ошибка выполнения: {result.std_err.decode(errors='replace')}")
                return False
            except winrm.exceptions.InvalidCredentialsError:
                log.error(
                    "401 Unauthorized. Возможные причины:\n"
                    "  1) Локальный логин без префикса — попробуйте '.\\USER' или 'PCNAME\\USER'.\n"
                    "  2) UAC Remote Restrictions — на целевой машине (PowerShell админ):\n"
                    '     reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System" '
                    "/v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f\n"
                    "     Restart-Service WinRM\n"
                    "  3) Пользователь не в группе Administrators — WinRM по NTLM требует админских прав.\n"
                    "  4) Basic-аутентификация как fallback: на сервере\n"
                    "     Set-Item -Path WSMan:\\localhost\\Service\\Auth\\Basic -Value $true\n"
                    "     и в коде transport='basic'."
                )
                return False
            except Exception as e:
                log.exception(f"Ошибка подключения к Windows: {e}")
                return False
        elif self.os_type in ("linux", "ubuntu", "astralinux", "mint"):
            try:
                self.session = paramiko.SSHClient()
                self.session.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.session.connect(
                    self.host, username=self.user,
                    password=self.password, timeout=10,
                )
                log.info("SSH-соединение установлено.")
                return True
            except Exception as e:
                log.exception(f"Ошибка SSH-подключения: {e}")
                return False
        log.error(f"Неподдерживаемая ОС: {self.os_type}")
        return False

    def _run_linux_cmd(self, cmd: str) -> Optional[str]:
        if not self.session:
            return None
        log.debug(f"[linux] $ {cmd}")
        _, stdout, stderr = self.session.exec_command(cmd)
        code = stdout.channel.recv_exit_status()
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        if code == 0:
            if out:
                log.debug(f"[linux] -> {out}")
            return out
        log.warning(f"[linux] ! {cmd} -> exit {code}: {err}")
        return None

    def _run_windows_ps(self, script: str) -> Optional[str]:
        if not self.session:
            return None
        log.debug(f"[windows] PS> {script.strip()[:120]}...")
        result = self.session.run_ps(script)
        if result.status_code == 0:
            return result.std_out.decode(errors="replace").strip()
        log.warning(f"[windows] PS failed: {result.std_err.decode(errors='replace').strip()}")
        return None

    def enable_windows_rdp(self) -> bool:
        log.info("Включение RDP на Windows через реестр...")
        ps_script = r"""
        Set-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Terminal Server' -Name 'fDenyTSConnections' -Value 0
        Enable-NetFirewallRule -DisplayGroup "Remote Desktop"
        """
        if self._run_windows_ps(ps_script) is not None:
            log.info("RDP успешно включен. Порт: 3389.")
            return True
        return False

    def install_linux_xrdp(self) -> bool:
        log.info("Установка и настройка xrdp на Linux...")
        self._run_linux_cmd("sudo apt-get update -y")
        self._run_linux_cmd("sudo apt-get install -y xrdp")
        self._run_linux_cmd("sudo systemctl enable xrdp")
        self._run_linux_cmd("sudo systemctl start xrdp")
        self._run_linux_cmd("sudo ufw allow 3389/tcp 2>/dev/null || true")
        status = self._run_linux_cmd("systemctl is-active xrdp")
        if status and "active" in status:
            log.info("xrdp успешно установлен и запущен. Порт: 3389.")
            return True
        log.error("Не удалось запустить xrdp. Проверьте логи.")
        return False

    def deploy_anydesk(self) -> bool:
        log.info("Развёртывание AnyDesk...")
        if self.os_type == "windows":
            return self._deploy_anydesk_windows()
        if self.os_type in ("linux", "ubuntu", "astralinux", "mint"):
            return self._deploy_anydesk_linux()
        return False

    def _deploy_anydesk_windows(self) -> bool:
        anydesk_pass = "YourStrongPassword123"  # замените при необходимости
        ps_script = rf"""
    $ErrorActionPreference = 'Stop'
    $pass = "{anydesk_pass}"

    $exePath = "C:\ProgramData\AnyDesk\AnyDesk.exe"
    $url = "https://download.anydesk.com/AnyDesk.exe"

    # 1. Установка (если ещё не установлен)
    if (-not (Test-Path $exePath)) {{
        $tmp = "C:\Windows\Temp\AnyDesk.exe"
        Invoke-WebRequest -Uri $url -OutFile $tmp
        Start-Process -FilePath $tmp -ArgumentList "--install", "C:\ProgramData\AnyDesk", "--start-with-win", "--silent" -Wait
        Start-Sleep -Seconds 3
    }}

    # 2. Убедиться, что служба запущена
    $svc = Get-Service -Name "AnyDesk" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -ne "Running") {{
        Start-Service AnyDesk -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }}

    # 3. Дождаться появления ID в реестре (до 30 секунд)
    $id = $null
    $regPaths = @(
        "HKLM:\SOFTWARE\WOW6432Node\AnyDesk",
        "HKLM:\SOFTWARE\AnyDesk",
        "HKCU:\Software\AnyDesk"
    )
    for ($i = 0; $i -lt 30; $i++) {{
        foreach ($p in $regPaths) {{
            $v = Get-ItemProperty -Path $p -Name "ad.anynet.id" -ErrorAction SilentlyContinue
            if ($v -and $v."ad.anynet.id") {{
                $id = $v."ad.anynet.id"
                break
            }}
        }}
        if ($id) {{ break }}
        Start-Sleep -Seconds 1
    }}

    # 4. Fallback: попросить ID у самого AnyDesk (может сработать, если служба уже готова)
    if (-not $id -and (Test-Path $exePath)) {{
        $id = (& $exePath --get-id 2>$null | Out-String).Trim()
    }}

    # 5. Установить пароль (нужен запущенный сервис)
    if (Test-Path $exePath) {{
        Start-Process -FilePath $exePath -ArgumentList "--set-password", $pass -Wait -ErrorAction SilentlyContinue
    }}

    if ($id) {{
        Write-Output "AnyDesk ID: $id"
    }} else {{
        Write-Output "AnyDesk ID: (не получен — служба ещё стартует, откройте AnyDesk на удалённой машине вручную)"
    }}
    Write-Output "AnyDesk Password: $pass"
    """
        result = self._run_windows_ps(ps_script)
        if result:
            log.info(f"\n{result}")
            log.info("AnyDesk установлен. Используйте ID и пароль выше для подключения.")
            return True
        return False

    def _deploy_anydesk_linux(self) -> bool:
        arch = self._run_linux_cmd("uname -m")
        if not arch or "x86_64" not in arch:
            log.error("Неподдерживаемая архитектура Linux.")
            return False
        url = "https://download.anydesk.com/linux/anydesk-6.3.2-amd64.deb"
        self._run_linux_cmd(f"wget -O /tmp/anydesk.deb {url}")
        self._run_linux_cmd("sudo dpkg -i /tmp/anydesk.deb || sudo apt-get install -f -y")
        self._run_linux_cmd("anydesk --get-id")
        log.info("AnyDesk установлен на Linux. Запустите его вручную для получения ID.")
        return True


# ---------- CLI / интерактивный ввод ----------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Полуавтоматический инструмент удалённого развёртывания доступа",
        epilog="Если аргументы не указаны — они будут запрошены интерактивно.",
    )
    p.add_argument("--host", help="IP-адрес или имя хоста")
    p.add_argument("--user", help="Имя пользователя")
    p.add_argument("--password", help="Пароль (лучше ввести интерактивно)")
    p.add_argument("--os", choices=["windows", "linux", "ubuntu", "astralinux", "mint"],
                   help="Тип ОС")
    p.add_argument("--method", choices=["rdp", "xrdp", "anydesk"],
                   help="Метод развёртывания")
    p.add_argument("--skip-check", action="store_true",
                   help="Пропустить предварительную проверку портов")
    p.add_argument("--check-only", action="store_true",
                   help="Только проверить порты и выйти")
    p.add_argument("--show-password", action="store_true",
                   help="Вводить пароль открытым текстом (для сред без TTY)")
    return p


def _prompt(text: str) -> str:
    """input() с принудительным flush — чтобы приглашение точно дошло до экрана."""
    sys.stdout.write(text)
    sys.stdout.flush()
    return input()


def _read_password() -> str:
    """Пароль: getpass, если доступен; иначе — обычный input с предупреждением."""
    use_getpass = sys.stdin.isatty() and hasattr(sys.stdin, "fileno")
    if not use_getpass:
        log.warning("stdin не является терминалом (PyCharm Run / перенаправление). "
                    "Пароль будет виден на экране.")
        return _prompt("Пароль (ОТКРЫТО, т.к. нет TTY): ")

    try:
        import getpass
        return getpass.getpass("Пароль (ввод скрыт): ")
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception as e:
        log.warning(f"getpass недоступен ({e}). Использую обычный ввод.")
        return _prompt("Пароль (ОТКРЫТО): ")


def interactive_fill(args: argparse.Namespace) -> argparse.Namespace:
    """Дополняет недостающие аргументы через диалог."""
    print("\n=== Интерактивный режим ===", flush=True)
    if not args.host:
        args.host = _prompt("IP/хост: ").strip()
    if not args.user:
        args.user = _prompt("Пользователь: ").strip()
    if not args.password:
        if args.show_password:
            args.password = _prompt("Пароль (ОТКРЫТО по флагу): ")
        else:
            args.password = _read_password()
    if not args.os:
        print("Доступные ОС: windows, linux, ubuntu, astralinux, mint", flush=True)
        while True:
            args.os = _prompt("ОС: ").strip().lower()
            if args.os in ("windows", "linux", "ubuntu", "astralinux", "mint"):
                break
            print("  [!] Некорректный выбор, попробуйте снова.", flush=True)
    if not args.method:
        print("Методы: rdp (только Windows), xrdp (только Linux), anydesk (универсально)", flush=True)
        while True:
            args.method = _prompt("Метод: ").strip().lower()
            if args.method in ("rdp", "xrdp", "anydesk"):
                break
            print("  [!] Некорректный выбор, попробуйте снова.", flush=True)
    return args


def validate(args: argparse.Namespace) -> bool:
    if args.method == "rdp" and args.os != "windows":
        log.error("Метод RDP применим только к Windows.")
        return False
    if args.method == "xrdp" and args.os == "windows":
        log.error("Метод xrdp применим только к Linux.")
        return False
    return True


def main() -> None:
    global log
    log, log_path = setup_logging()
    log.info(f"Лог-файл: {log_path}")
    log.info("Запуск remote_deployer")

    parser = build_parser()
    args = parser.parse_args()

    if not all([args.host, args.user, args.password, args.os, args.method]):
        args = interactive_fill(args)

    if not validate(args):
        sys.exit(2)

    log.info(f"Параметры: host={args.host}, os={args.os}, method={args.method}, user={args.user}")

    if not args.skip_check:
        results = preflight_check(args.host, args.os, args.method)
        critical_port = 5985 if args.os == "windows" else 22
        ok, _, err = results.get(critical_port, (False, 0, "not checked"))
        if not ok:
            log.error(
                f"Критический порт {critical_port} недоступен ({err}). "
                f"Проверьте сеть/файрвол. Для принудительного продолжения: --skip-check"
            )
            sys.exit(3)
        if args.check_only:
            log.info("Режим --check-only: выход без изменений.")
            return
    elif args.check_only:
        log.warning("--check-only без проверки бессмыслен; выходим.")
        return

    deployer = RemoteDeployer(args.host, args.user, args.password, args.os)
    if not deployer.connect():
        log.error("Не удалось установить соединение.")
        sys.exit(1)

    success = False
    if args.method == "rdp":
        success = deployer.enable_windows_rdp()
    elif args.method == "xrdp":
        success = deployer.install_linux_xrdp()
    elif args.method == "anydesk":
        success = deployer.deploy_anydesk()

    if success:
        log.info("Задача выполнена успешно.")
    else:
        log.error("Не удалось выполнить развёртывание. Проверьте права и настройки сети.")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[!] Прервано пользователем.")
        sys.exit(130)