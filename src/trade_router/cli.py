from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .core.config import RouterPaths, daemon_base_url
from .core.secrets import DpapiSecretStore
from .models import CredentialUsage, ProviderEnvironment, RiskLimits, StrategyConfig

app = typer.Typer(help="Local multi-broker trading router")
daemon_app = typer.Typer(help="Manage the central daemon")
profile_app = typer.Typer(help="Manage broker connection profiles")
account_app = typer.Typer(help="Manage trading account aliases")
strategy_app = typer.Typer(help="Manage SDK strategy registrations")
app.add_typer(daemon_app, name="daemon")
app.add_typer(profile_app, name="profile")
app.add_typer(account_app, name="account")
app.add_typer(strategy_app, name="strategy")
console = Console()


def _background_python() -> str:
    if os.name != "nt":
        return sys.executable
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return pythonw if os.path.exists(pythonw) else sys.executable


def _token() -> str:
    return DpapiSecretStore(RouterPaths.discover().secrets).installation_token()


def _request(
    method: str,
    path: str,
    *,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float = 15,
) -> Any:
    try:
        response = httpx.request(
            method,
            daemon_base_url() + path,
            json=json_body,
            params=params,
            headers={"authorization": f"Bearer {_token()}"},
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        raise typer.BadParameter(
            "Trade Router daemon is not running or cannot be reached."
        ) from exc
    if response.status_code >= 400:
        try:
            body = response.json()
            message = (
                f"{body.get('code', 'ERROR')}: {body.get('message', response.text)}"
            )
        except Exception:
            message = f"HTTP {response.status_code}"
        raise typer.BadParameter(message)
    return None if response.status_code == 204 else response.json()


def _print_json(value: Any) -> None:
    console.print_json(data=value)


def _daemon_alive() -> bool:
    try:
        _request("GET", "/v1/system/health", timeout=1)
        return True
    except Exception:
        return False


@daemon_app.command("start")
def daemon_start(
    foreground: bool = typer.Option(
        False, "--foreground", help="Keep the server attached to this terminal"
    ),
) -> None:
    if _daemon_alive():
        console.print("Trade Router daemon is already running.")
        return
    paths = RouterPaths.discover()
    paths.ensure()
    if foreground:
        from .server.run import main

        main()
        return
    log = open(paths.daemon_log, "a", encoding="utf-8")
    flags = (
        subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        if os.name == "nt"
        else 0
    )
    process = subprocess.Popen(
        [_background_python(), "-m", "trade_router.server.run"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        creationflags=flags,
        cwd=str(paths.home),
        close_fds=True,
    )
    paths.pid.write_text(str(process.pid), encoding="ascii")
    for _ in range(40):
        if _daemon_alive():
            console.print(
                f"Trade Router started on {daemon_base_url()} (PID {process.pid})."
            )
            return
        if process.poll() is not None:
            break
        time.sleep(0.25)
    raise typer.BadParameter(f"Daemon did not start. See {paths.daemon_log}")


@daemon_app.command("stop")
def daemon_stop() -> None:
    paths = RouterPaths.discover()
    if not _daemon_alive():
        paths.pid.unlink(missing_ok=True)
        console.print("Trade Router daemon is not running.")
        return
    _request("POST", "/v1/system/shutdown")
    for _ in range(40):
        if not _daemon_alive():
            paths.pid.unlink(missing_ok=True)
            console.print("Trade Router stopped.")
            return
        time.sleep(0.25)
    console.print("Shutdown was requested; the daemon is still finishing pending work.")


@daemon_app.command("status")
def daemon_status() -> None:
    _print_json(_request("GET", "/v1/system/health"))


@app.command("status")
def status_alias() -> None:
    daemon_status()


@profile_app.command("add")
def profile_add(
    profile_id: str,
    provider: str = typer.Option(..., help="kis, namuh, or simulated"),
    usage: CredentialUsage = typer.Option(CredentialUsage.AUTO),
    display_name: str | None = typer.Option(None),
    settings: str = typer.Option("{}", help="Non-secret JSON settings"),
) -> None:
    provider = provider.lower()
    app_key: str | None = None
    app_secret: str | None = None
    if provider != "simulated":
        app_key = typer.prompt("App Key", hide_input=True)
        app_secret = typer.prompt("App Secret", hide_input=True)
    try:
        parsed_settings = json.loads(settings)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter("--settings must be a JSON object") from exc
    _print_json(
        _request(
            "POST",
            "/v1/profiles",
            json_body={
                "id": profile_id,
                "provider": provider,
                "usage": usage.value,
                "app_key": app_key,
                "app_secret": app_secret,
                "display_name": display_name,
                "settings": parsed_settings,
            },
        )
    )


@profile_app.command("list")
def profile_list() -> None:
    rows = _request("GET", "/v1/profiles")
    table = Table("ID", "Provider", "Credential usage", "Secrets")
    for row in rows:
        table.add_row(
            row["id"],
            row["provider"],
            row["usage"],
            "yes" if row["secret_configured"] else "no",
        )
    console.print(table)


@profile_app.command("check")
def profile_check(profile_id: str) -> None:
    _print_json(_request("POST", f"/v1/profiles/{profile_id}/check"))


@profile_app.command("remove")
def profile_remove(profile_id: str) -> None:
    _request("DELETE", f"/v1/profiles/{profile_id}")
    console.print(f"Removed profile {profile_id}.")


def _risk(
    max_order_krw: int | None,
    max_daily_buy_krw: int | None,
    max_exposure_krw: int | None,
    max_open_orders: int | None,
) -> dict[str, Any]:
    return RiskLimits(
        max_order_krw=max_order_krw,
        max_daily_buy_krw=max_daily_buy_krw,
        max_exposure_krw=max_exposure_krw,
        max_open_orders=max_open_orders,
    ).model_dump(mode="json")


@account_app.command("add")
def account_add(
    alias: str,
    profile: str = typer.Option(...),
    environment: ProviderEnvironment | None = typer.Option(None),
    max_order_krw: int | None = None,
    max_daily_buy_krw: int | None = None,
    max_exposure_krw: int | None = None,
    max_open_orders: int | None = None,
) -> None:
    account_number = typer.prompt("Account number", hide_input=True)
    body = {
        "alias": alias,
        "profile_id": profile,
        "environment": environment.value if environment else None,
        "account_number": account_number,
        "risk": _risk(
            max_order_krw, max_daily_buy_krw, max_exposure_krw, max_open_orders
        ),
    }
    _print_json(_request("POST", "/v1/accounts", json_body=body))


@account_app.command("list")
def account_list() -> None:
    rows = _request("GET", "/v1/accounts")
    table = Table("Alias", "Profile", "Environment", "Account", "Armed")
    for row in rows:
        table.add_row(
            row["alias"],
            row["profile_id"],
            row["environment"],
            row["masked_account"],
            "yes" if row["armed"] else "no",
        )
    console.print(table)


@account_app.command("arm")
def account_arm(alias: str) -> None:
    _print_json(_request("POST", f"/v1/accounts/{alias}/arm"))


@account_app.command("disarm")
def account_disarm(alias: str) -> None:
    _print_json(_request("POST", f"/v1/accounts/{alias}/disarm"))


@strategy_app.command("register")
def strategy_register(
    strategy_id: str,
    account: str = typer.Option(...),
    market_data: str = typer.Option(...),
    market_environment: ProviderEnvironment | None = typer.Option(None),
    max_order_krw: int | None = None,
    max_daily_buy_krw: int | None = None,
    max_exposure_krw: int | None = None,
    max_open_orders: int | None = None,
) -> None:
    config = StrategyConfig.model_validate(
        {
            "id": strategy_id,
            "trading_account": account,
            "market_data": market_data,
            "market_environment": (
                market_environment.value if market_environment else None
            ),
            "risk": _risk(
                max_order_krw, max_daily_buy_krw, max_exposure_krw, max_open_orders
            ),
        }
    )
    _print_json(
        _request(
            "POST",
            "/v1/strategies",
            json_body={"config": config.model_dump(mode="json")},
        )
    )


@strategy_app.command("show")
def strategy_show(strategy_id: str | None = None) -> None:
    path = f"/v1/strategies/{strategy_id}" if strategy_id else "/v1/strategies"
    _print_json(_request("GET", path))


@strategy_app.command("arm")
def strategy_arm(strategy_id: str) -> None:
    _print_json(_request("POST", f"/v1/strategies/{strategy_id}/arm"))


@strategy_app.command("disarm")
def strategy_disarm(strategy_id: str) -> None:
    _print_json(_request("POST", f"/v1/strategies/{strategy_id}/disarm"))


@app.command("capabilities")
def capabilities(provider: str | None = None) -> None:
    _print_json(
        _request(
            "GET",
            "/v1/system/capabilities",
            params={"provider": provider} if provider else None,
        )
    )


@app.command("doctor")
def doctor() -> None:
    paths = RouterPaths.discover()
    table = Table("Check", "Result")
    table.add_row("Platform", "Windows" if os.name == "nt" else "unsupported for DPAPI")
    table.add_row("Runtime home", str(paths.home))
    try:
        token = _token()
        table.add_row("DPAPI vault", "ok" if token else "failed")
    except Exception as exc:
        table.add_row("DPAPI vault", f"failed: {type(exc).__name__}")
    table.add_row("Daemon", "ready" if _daemon_alive() else "not running")
    console.print(table)


if __name__ == "__main__":
    app()
