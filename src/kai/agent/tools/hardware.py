import logging
import os
import platform
import shutil

logger = logging.getLogger(__name__)


def _format_bytes(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _memory_info() -> tuple[int | None, int | None]:
    total: int | None = None
    available: int | None = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) * 1024
                if total is not None and available is not None:
                    break
    except (FileNotFoundError, ValueError, IndexError, OSError):
        logger.debug("could not read /proc/meminfo", exc_info=True)
    return total, available


def _disk_info() -> tuple[int | None, int | None]:
    try:
        usage = shutil.disk_usage("/")
        return usage.total, usage.free
    except OSError:
        logger.debug("could not read disk usage", exc_info=True)
        return None, None


def _cpu_load_avg() -> str | None:
    try:
        load = os.getloadavg()
        return f"{load[0]:.2f} (1m)"
    except (OSError, AttributeError):
        logger.debug("load average unavailable", exc_info=True)
        return None


def get_hardware_info() -> dict[str, str]:
    info: dict[str, str] = {}

    info["os"] = platform.platform()
    info["python_version"] = platform.python_version()

    arch = platform.machine() or platform.processor()
    if arch:
        info["cpu_architecture"] = arch

    cpu_count = os.cpu_count()
    if cpu_count:
        info["cpu_count"] = str(cpu_count)

    load = _cpu_load_avg()
    if load is not None:
        info["cpu_load"] = load

    mem_total, mem_available = _memory_info()
    if mem_total is not None:
        info["memory_total"] = _format_bytes(mem_total)
    if mem_available is not None:
        info["memory_available"] = _format_bytes(mem_available)

    disk_total, disk_free = _disk_info()
    if disk_total is not None:
        info["disk_total"] = _format_bytes(disk_total)
    if disk_free is not None:
        info["disk_free"] = _format_bytes(disk_free)

    return info
