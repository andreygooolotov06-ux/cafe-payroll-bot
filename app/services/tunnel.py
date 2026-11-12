from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiogram import Bot
from aiogram.types import MenuButtonCommands

from app.config import BASE_DIR, Settings

logger = logging.getLogger(__name__)

_TRYCLOUDFLARE_RE = re.compile(
    r"https://(?!api\.|www\.|developers\.|dash\.)[a-z0-9-]+\.trycloudflare\.com"
)
_LOCALHOSTRUN_RE = re.compile(r"https://[a-z0-9-]+\.lhr\.life")


@dataclass
class MiniAppRuntime:
    url: str | None = None
    status: str = "starting"  # "starting" | "online" | "disabled" | "error"
    provider: str | None = None
    error: str | None = None


miniapp_runtime = MiniAppRuntime()


def find_cloudflared_binary() -> Path | None:
    candidates = [
        BASE_DIR / "bin" / "cloudflared.exe",
        BASE_DIR / "bin" / "cloudflared",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    found = shutil.which("cloudflared")
    return Path(found) if found else None


def find_ssh_binary() -> Path | None:
    found = shutil.which("ssh")
    if found:
        return Path(found)
    win_ssh = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe"
    if win_ssh.is_file():
        return win_ssh
    return None


async def restore_telegram_menu_button(bot: Bot) -> None:
    """Повертає нижню кнопку меню у стандартний режим команд (/start, /menu, /app, /cancel), щоб не блокувати навігацію."""
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info("Кнопку меню Telegram повернуто у режим команд (MenuButtonCommands)")
    except Exception as exc:
        logger.warning("Не вдалося відновити MenuButtonCommands: %s", exc)


async def update_telegram_menu_button(bot: Bot, url: str | None = None) -> None:
    """Сумісна обгортка: завжди залишає стандартні команди замість липкої WebApp-кнопки."""
    _ = url
    await restore_telegram_menu_button(bot)


async def _terminate_proc(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=4.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass


async def _probe_tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _probe_public_health(url: str, timeout: float = 8.0) -> bool:
    health_url = f"{url.rstrip('/')}/health"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as client:
            async with client.get(health_url, allow_redirects=False) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json(content_type=None)
                return bool(isinstance(data, dict) and data.get("ok") is True)
    except Exception:
        return False


async def _run_tunnel_process(
    cmd: list[str],
    url_regex: re.Pattern[str],
    provider_name: str,
    bot: Bot,
    startup_timeout: float = 15.0,
) -> bool:
    """Запускає тунель-процес, чекає HTTPS URL, перевіряє /health і тримає процес живим. Повертає True якщо тунель було піднято."""
    _ = bot
    proc: asyncio.subprocess.Process | None = None
    watchdog_task: asyncio.Task[None] | None = None
    url_found = False
    try:
        miniapp_runtime.status = "starting"
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        async def _health_watchdog(public_url: str) -> None:
            failures = 0
            await asyncio.sleep(10.0)
            while proc is not None and proc.returncode is None:
                ok = await _probe_public_health(public_url, timeout=8.0)
                if ok:
                    failures = 0
                else:
                    failures += 1
                    logger.warning(
                        "%s: перевірка /health не вдалась (%d/2) для %s",
                        provider_name,
                        failures,
                        public_url,
                    )
                    if failures >= 2:
                        miniapp_runtime.status = "error"
                        miniapp_runtime.error = f"{provider_name}: публічний URL не відповідає на /health"
                        logger.warning(
                            "%s: тунель перестав відповідати (%s) — перезапуск...",
                            provider_name,
                            public_url,
                        )
                        await _terminate_proc(proc)
                        return
                await asyncio.sleep(30.0)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + startup_timeout

        if proc.stdout is not None:
            while True:
                if not url_found:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        logger.warning("%s: таймаут отримання HTTPS URL (%.0f с)", provider_name, startup_timeout)
                        await _terminate_proc(proc)
                        return False
                    try:
                        raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                    except asyncio.TimeoutError:
                        logger.warning("%s: таймаут очікування виводу HTTPS URL", provider_name)
                        await _terminate_proc(proc)
                        return False
                else:
                    raw_line = await proc.stdout.readline()

                if not raw_line:
                    break

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                if not url_found:
                    m = url_regex.search(line)
                    if m:
                        url = m.group(0)
                        url_found = True
                        miniapp_runtime.url = url
                        miniapp_runtime.status = "online"
                        miniapp_runtime.provider = provider_name
                        miniapp_runtime.error = None
                        logger.info("🌐 Mini App HTTPS URL готовий (%s): %s", provider_name, url)
                        watchdog_task = asyncio.create_task(_health_watchdog(url))

        rc = await proc.wait()
        if url_found:
            miniapp_runtime.status = "error"
            miniapp_runtime.error = f"{provider_name} завершився з кодом {rc}"
            logger.warning("%s завершився (%s), перепідключення...", provider_name, rc)
        return url_found
    except asyncio.CancelledError:
        await _terminate_proc(proc)
        raise
    except Exception as exc:
        await _terminate_proc(proc)
        miniapp_runtime.error = f"{provider_name}: {exc}"
        logger.warning("Помилка тунелю %s: %s", provider_name, exc)
        return url_found
    finally:
        if watchdog_task is not None and not watchdog_task.done():
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass
        if url_found and miniapp_runtime.provider == provider_name:
            miniapp_runtime.url = None


async def start_tunnel_supervisor(bot: Bot, settings: Settings) -> None:
    """Керує HTTPS-тунелем (Cloudflare / localhost.run) або фіксованим MINIAPP_URL."""
    if settings.miniapp_url:
        miniapp_runtime.url = settings.miniapp_url
        miniapp_runtime.status = "online"
        miniapp_runtime.provider = "custom"
        miniapp_runtime.error = None
        logger.info("Mini App працює за фіксованим HTTPS URL: %s", miniapp_runtime.url)
        return

    if not settings.miniapp_auto_tunnel:
        miniapp_runtime.url = None
        miniapp_runtime.status = "disabled"
        miniapp_runtime.provider = None
        miniapp_runtime.error = None
        logger.info("Автотунель Mini App вимкнено (MINIAPP_AUTO_TUNNEL=false)")
        return

    cf_bin = find_cloudflared_binary()
    ssh_bin = find_ssh_binary()
    if cf_bin is None and ssh_bin is None:
        miniapp_runtime.url = None
        miniapp_runtime.status = "error"
        miniapp_runtime.error = "Не знайдено ні cloudflared, ні ssh для автотунелю"
        logger.warning("Не знайдено ні cloudflared, ні ssh — Mini App тунель не запущено")
        return

    target_url = f"http://{settings.miniapp_host}:{settings.miniapp_port}"
    target_host_port = f"{settings.miniapp_host}:{settings.miniapp_port}"
    backoff = 2.0

    while True:
        worked = False

        if cf_bin is not None:
            if await _probe_tcp("api.trycloudflare.com", 443, timeout=2.0):
                worked = await _run_tunnel_process(
                    [str(cf_bin), "tunnel", "--no-autoupdate", "--url", target_url],
                    _TRYCLOUDFLARE_RE,
                    "cloudflared",
                    bot,
                    startup_timeout=10.0,
                )
            else:
                logger.info("api.trycloudflare.com:443 недоступний — перемикаюсь на резервний HTTPS-тунель (localhost.run)")

        if not worked and ssh_bin is not None:
            worked = await _run_tunnel_process(
                [
                    str(ssh_bin),
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    f"UserKnownHostsFile={os.devnull}",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    "-o",
                    "ConnectTimeout=8",
                    "-o",
                    "TCPKeepAlive=yes",
                    "-o",
                    "ServerAliveInterval=20",
                    "-o",
                    "ServerAliveCountMax=3",
                    "-T",
                    "-R",
                    f"80:{target_host_port}",
                    "nokey@localhost.run",
                ],
                _LOCALHOSTRUN_RE,
                "localhost.run",
                bot,
                startup_timeout=15.0,
            )

        if worked:
            backoff = 2.0
        else:
            miniapp_runtime.status = "error"
            if not miniapp_runtime.error:
                miniapp_runtime.error = "Не вдалося підняти жоден HTTPS-тунель"
            backoff = min(backoff * 1.5, 30.0)

        await asyncio.sleep(backoff)
