import subprocess
import time
from pathlib import Path

from musiq.display import console

_CONFIG_PATH = Path.home() / ".musiq" / "config"
_SYS_CLASS_NET = Path("/sys/class/net")


def _load_vpn_interface() -> str | None:
    """Return VPN_INTERFACE from ~/.musiq/config, or None if unset/disabled."""
    if _CONFIG_PATH.is_file():
        try:
            for line in _CONFIG_PATH.read_text().splitlines():
                line = line.strip()
                if line.startswith("VPN_INTERFACE="):
                    value = line.split("=", 1)[1].strip()
                    return value or None
        except OSError:
            pass
    return None


def is_vpn_up(interface: str) -> bool:
    """Check if the WireGuard interface is active.

    WireGuard operstate is unreliable (often 'unknown'), so we check
    whether /sys/class/net/{interface}/ exists instead.
    """
    return (_SYS_CLASS_NET / interface).is_dir()


def ensure_vpn_up() -> str | None:
    """Bring up the WireGuard VPN if VPN_INTERFACE is configured.

    Returns the interface name, or None if VPN is not configured.
    Aborts the process if the interface is configured but cannot be brought up.
    """
    iface = _load_vpn_interface()
    if iface is None:
        return None

    if is_vpn_up(iface):
        return iface

    console.print(f"[bold blue]\u2b06[/bold blue] [dim]Bringing up VPN interface: {iface}...[/dim]")
    try:
        subprocess.run(["sudo", "wg-quick", "up", iface], check=True, capture_output=True)
        time.sleep(0.5)
        try:
            subprocess.run(["sudo", "resolvconf", "-u"], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            console.print("[yellow]\u26a0 resolvconf -u failed — DNS may be stale, but VPN is up[/yellow]")
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode().strip() if exc.stderr else str(exc)
        console.print(
            f"[red]\u2716 Could not bring up VPN interface '{iface}'.[/red]\n"
            f"  [dim]Run manually: sudo wg-quick up {iface} && sudo resolvconf -u[/dim]\n"
            f"  [dim]{stderr}[/dim]"
        )
        raise SystemExit(1)
    except FileNotFoundError:
        console.print("[red]\u2716 'sudo' or 'wg-quick' not found. Cannot bring up VPN.[/red]")
        raise SystemExit(1)

    console.print(f"[green]\u2713 VPN up:[/green] {iface}")
    return iface
