"""
Lightweight Windows-focused GUI for managing Antigravity accounts and proxy settings.

Key features:
- Account list with add/delete/export
- Mark current account and toggle proxy availability per account
- Simulated quota refresh to mirror the original dashboard behavior
- Proxy configuration editor with start/stop toggle (stateful placeholder)

The app persists its state to ``~/.antigravity_gui_state.json`` so it survives restarts.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


STATE_PATH = Path(os.path.expanduser("~")) / ".antigravity_gui_state.json"


@dataclass
class ModelQuota:
    name: str
    percentage: int
    reset_time: str


@dataclass
class QuotaData:
    models: List[ModelQuota] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)
    subscription_tier: str = "FREE"
    is_forbidden: bool = False


@dataclass
class Account:
    id: str
    email: str
    refresh_token: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    proxy_enabled: bool = True
    quota: QuotaData = field(default_factory=QuotaData)


@dataclass
class UpstreamProxyConfig:
    enabled: bool = False
    url: str = ""


@dataclass
class ProxyConfig:
    port: int = 8045
    api_key: str = field(default_factory=lambda: secrets.token_urlsafe(20))
    request_timeout: int = 120
    auto_start: bool = False
    allow_lan_access: bool = False
    auth_mode: str = "off"
    enable_logging: bool = False
    upstream_proxy: UpstreamProxyConfig = field(default_factory=UpstreamProxyConfig)
    scheduling_mode: str = "Balance"


class AppState:
    def __init__(self) -> None:
        self.accounts: List[Account] = []
        self.current_account_id: Optional[str] = None
        self.proxy_config: ProxyConfig = ProxyConfig()
        self.proxy_running: bool = False
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not STATE_PATH.exists():
            return
        try:
            raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            self.proxy_config = ProxyConfig(
                **{
                    "port": raw.get("proxy_config", {}).get("port", 8045),
                    "api_key": raw.get("proxy_config", {}).get("api_key", secrets.token_urlsafe(20)),
                    "request_timeout": raw.get("proxy_config", {}).get("request_timeout", 120),
                    "auto_start": raw.get("proxy_config", {}).get("auto_start", False),
                    "allow_lan_access": raw.get("proxy_config", {}).get("allow_lan_access", False),
                    "auth_mode": raw.get("proxy_config", {}).get("auth_mode", "off"),
                    "enable_logging": raw.get("proxy_config", {}).get("enable_logging", False),
                    "upstream_proxy": UpstreamProxyConfig(
                        **raw.get("proxy_config", {}).get("upstream_proxy", {"enabled": False, "url": ""})
                    ),
                    "scheduling_mode": raw.get("proxy_config", {}).get("scheduling_mode", "Balance"),
                }
            )
            self.proxy_running = raw.get("proxy_running", False)

            accounts_payload = raw.get("accounts", [])
            self.accounts = []
            for item in accounts_payload:
                quota = item.get("quota", {})
                models = [ModelQuota(**m) for m in quota.get("models", [])]
                quota_obj = QuotaData(
                    models=models,
                    last_updated=quota.get("last_updated", time.time()),
                    subscription_tier=quota.get("subscription_tier", "FREE"),
                    is_forbidden=quota.get("is_forbidden", False),
                )
                account = Account(
                    id=item["id"],
                    email=item["email"],
                    refresh_token=item.get("refresh_token", ""),
                    created_at=item.get("created_at", time.time()),
                    last_used=item.get("last_used", time.time()),
                    proxy_enabled=item.get("proxy_enabled", True),
                    quota=quota_obj,
                )
                self.accounts.append(account)
            self.current_account_id = raw.get("current_account_id")
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("State Load Failed", f"Could not read state file: {exc}")

    def save(self) -> None:
        with self._lock:
            payload = {
                "accounts": [
                    {
                        **asdict(acc),
                        "quota": {
                            **asdict(acc.quota),
                            "models": [asdict(m) for m in acc.quota.models],
                        },
                    }
                    for acc in self.accounts
                ],
                "current_account_id": self.current_account_id,
                "proxy_config": {
                    **asdict(self.proxy_config),
                    "upstream_proxy": asdict(self.proxy_config.upstream_proxy),
                },
                "proxy_running": self.proxy_running,
            }
            STATE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def add_account(self, email: str, refresh_token: str, tier: str) -> Account:
        account = Account(
            id=str(uuid.uuid4()),
            email=email.strip(),
            refresh_token=refresh_token.strip(),
            quota=QuotaData(subscription_tier=tier or "FREE"),
        )
        self.accounts.append(account)
        if not self.current_account_id:
            self.current_account_id = account.id
        self.save()
        return account

    def delete_accounts(self, ids: List[str]) -> None:
        self.accounts = [acc for acc in self.accounts if acc.id not in ids]
        if self.current_account_id in ids:
            self.current_account_id = self.accounts[0].id if self.accounts else None
        self.save()

    def set_current(self, account_id: str) -> None:
        if any(acc.id == account_id for acc in self.accounts):
            self.current_account_id = account_id
            self.save()

    def toggle_proxy(self, account_id: str) -> None:
        for acc in self.accounts:
            if acc.id == account_id:
                acc.proxy_enabled = not acc.proxy_enabled
                break
        self.save()

    def refresh_quota(self, account_id: str) -> None:
        for acc in self.accounts:
            if acc.id == account_id:
                acc.quota.last_updated = time.time()
                acc.quota.models = [
                    ModelQuota(name="gemini-3-flash", percentage=85, reset_time="24h"),
                    ModelQuota(name="claude-sonnet-4-5", percentage=70, reset_time="12h"),
                ]
                break
        self.save()


class ProxyService:
    """
    Placeholder proxy runner. In this rewrite we track the state and timing but
    intentionally avoid opening real network ports for safety.
    """

    def __init__(self, state: AppState) -> None:
        self.state = state
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self.state.proxy_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.state.proxy_running = True
        self.state.save()

    def stop(self) -> None:
        if not self.state.proxy_running:
            return
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        self.state.proxy_running = False
        self.state.save()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(0.5)


class AntigravityApp(tk.Tk):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.proxy_service = ProxyService(state)
        self.title("Antigravity Manager (Python)")
        self.geometry("980x640")
        self.minsize(860, 560)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self.account_frame = ttk.Frame(self.notebook, padding=10)
        self.proxy_frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.account_frame, text="账户管理")
        self.notebook.add(self.proxy_frame, text="代理设置")

        self._build_accounts_tab()
        self._build_proxy_tab()
        if self.state.proxy_config.auto_start:
            self.proxy_service.start()

    # Accounts UI ---------------------------------------------------------
    def _build_accounts_tab(self) -> None:
        toolbar = ttk.Frame(self.account_frame)
        toolbar.pack(fill=tk.X, pady=(0, 10))

        ttk.Button(toolbar, text="新增账户", command=self._open_add_dialog).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="删除所选", command=self._delete_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="设置为当前", command=self._set_current).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="切换反代开关", command=self._toggle_proxy).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="刷新额度", command=self._refresh_selected_quota).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="导出所选", command=self._export_selected).pack(side=tk.LEFT, padx=4)

        current_label = ttk.Label(
            toolbar, text=f"当前账户: {self._current_email() or '无'}", foreground="#2563eb"
        )
        current_label.pack(side=tk.RIGHT)
        self.current_label = current_label

        columns = ("email", "tier", "proxy", "updated")
        tree = ttk.Treeview(self.account_frame, columns=columns, show="headings", height=18, selectmode="extended")
        tree.heading("email", text="邮箱")
        tree.heading("tier", text="订阅类型")
        tree.heading("proxy", text="反代启用")
        tree.heading("updated", text="额度更新时间")
        tree.column("email", width=320)
        tree.column("tier", width=100, anchor=tk.CENTER)
        tree.column("proxy", width=90, anchor=tk.CENTER)
        tree.column("updated", width=160, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True)
        self.account_tree = tree
        self._reload_accounts()

    def _current_email(self) -> Optional[str]:
        for acc in self.state.accounts:
            if acc.id == self.state.current_account_id:
                return acc.email
        return None

    def _reload_accounts(self) -> None:
        self.account_tree.delete(*self.account_tree.get_children())
        for acc in self.state.accounts:
            updated = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(acc.quota.last_updated))
            proxy_status = "启用" if acc.proxy_enabled else "禁用"
            tier = acc.quota.subscription_tier or "FREE"
            tag = ("current",) if acc.id == self.state.current_account_id else ()
            self.account_tree.insert(
                "", tk.END, iid=acc.id, values=(acc.email, tier, proxy_status, updated), tags=tag
            )
        self.account_tree.tag_configure("current", background="#e0f2fe")
        self.current_label.config(text=f"当前账户: {self._current_email() or '无'}")

    def _open_add_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("新增账户")
        dialog.grab_set()
        ttk.Label(dialog, text="邮箱").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        email_entry = ttk.Entry(dialog, width=36)
        email_entry.grid(row=0, column=1, padx=8, pady=6)
        ttk.Label(dialog, text="Refresh Token").grid(row=1, column=0, padx=8, pady=6, sticky="w")
        token_entry = ttk.Entry(dialog, width=36, show="*")
        token_entry.grid(row=1, column=1, padx=8, pady=6)
        ttk.Label(dialog, text="订阅类型 (FREE/PRO/ULTRA)").grid(row=2, column=0, padx=8, pady=6, sticky="w")
        tier_entry = ttk.Entry(dialog, width=20)
        tier_entry.insert(0, "FREE")
        tier_entry.grid(row=2, column=1, padx=8, pady=6, sticky="w")

        def submit() -> None:
            email = email_entry.get().strip()
            token = token_entry.get().strip()
            tier = tier_entry.get().strip().upper() or "FREE"
            if not email or not token:
                messagebox.showerror("缺少字段", "邮箱和 Refresh Token 均为必填")
                return
            self.state.add_account(email, token, tier)
            self._reload_accounts()
            dialog.destroy()

        ttk.Button(dialog, text="确认添加", command=submit).grid(row=3, column=0, columnspan=2, pady=10)

    def _selected_ids(self) -> List[str]:
        return list(self.account_tree.selection())

    def _delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        if not messagebox.askyesno("确认删除", f"确定删除 {len(ids)} 个账户?"):
            return
        self.state.delete_accounts(ids)
        self._reload_accounts()

    def _set_current(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        self.state.set_current(ids[0])
        self._reload_accounts()

    def _toggle_proxy(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        for account_id in ids:
            self.state.toggle_proxy(account_id)
        self._reload_accounts()

    def _refresh_selected_quota(self) -> None:
        ids = self._selected_ids()
        targets = ids or [acc.id for acc in self.state.accounts]
        for account_id in targets:
            self.state.refresh_quota(account_id)
        messagebox.showinfo("刷新完成", f"已刷新 {len(targets)} 个账户额度")
        self._reload_accounts()

    def _export_selected(self) -> None:
        ids = self._selected_ids() or [acc.id for acc in self.state.accounts]
        accounts = [acc for acc in self.state.accounts if acc.id in ids]
        if not accounts:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")],
            initialfile="antigravity_accounts.json",
        )
        if not path:
            return
        payload = [{"email": acc.email, "refresh_token": acc.refresh_token} for acc in accounts]
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        messagebox.showinfo("导出成功", f"保存至: {path}")

    # Proxy UI -----------------------------------------------------------
    def _build_proxy_tab(self) -> None:
        cfg = self.state.proxy_config
        top_bar = ttk.Frame(self.proxy_frame)
        top_bar.pack(fill=tk.X, pady=(0, 10))

        self.proxy_status_label = ttk.Label(top_bar, text=self._proxy_status_text(), foreground="#047857")
        self.proxy_status_label.pack(side=tk.LEFT)

        ttk.Button(top_bar, text="启动/停止代理", command=self._toggle_proxy_service).pack(side=tk.RIGHT, padx=4)
        ttk.Button(top_bar, text="重新生成 API Key", command=self._regenerate_key).pack(side=tk.RIGHT, padx=4)

        form = ttk.Frame(self.proxy_frame)
        form.pack(fill=tk.BOTH, expand=True)

        # Row 1
        ttk.Label(form, text="监听端口").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.port_var = tk.IntVar(value=cfg.port)
        ttk.Entry(form, textvariable=self.port_var, width=12).grid(row=0, column=1, sticky="w", padx=6, pady=6)

        ttk.Label(form, text="请求超时 (秒)").grid(row=0, column=2, sticky="w", padx=6, pady=6)
        self.timeout_var = tk.IntVar(value=cfg.request_timeout)
        ttk.Entry(form, textvariable=self.timeout_var, width=12).grid(row=0, column=3, sticky="w", padx=6, pady=6)

        ttk.Label(form, text="认证模式").grid(row=0, column=4, sticky="w", padx=6, pady=6)
        self.auth_var = tk.StringVar(value=cfg.auth_mode)
        auth_menu = ttk.Combobox(form, textvariable=self.auth_var, values=["off", "strict", "all_except_health", "auto"])
        auth_menu.grid(row=0, column=5, sticky="w", padx=6, pady=6)

        # Row 2
        ttk.Label(form, text="API Key").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.api_key_var = tk.StringVar(value=cfg.api_key)
        ttk.Entry(form, textvariable=self.api_key_var, width=40).grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=6)

        self.allow_lan_var = tk.BooleanVar(value=cfg.allow_lan_access)
        ttk.Checkbutton(form, text="允许局域网访问", variable=self.allow_lan_var).grid(row=1, column=4, sticky="w", padx=6, pady=6)

        self.auto_start_var = tk.BooleanVar(value=cfg.auto_start)
        ttk.Checkbutton(form, text="开机自启", variable=self.auto_start_var).grid(row=1, column=5, sticky="w", padx=6, pady=6)

        # Row 3
        ttk.Label(form, text="上游代理地址").grid(row=2, column=0, sticky="w", padx=6, pady=6)
        self.upstream_var = tk.StringVar(value=cfg.upstream_proxy.url)
        ttk.Entry(form, textvariable=self.upstream_var, width=50).grid(row=2, column=1, columnspan=4, sticky="w", padx=6, pady=6)
        self.upstream_enabled_var = tk.BooleanVar(value=cfg.upstream_proxy.enabled)
        ttk.Checkbutton(form, text="启用上游代理", variable=self.upstream_enabled_var).grid(row=2, column=5, sticky="w", padx=6, pady=6)

        # Row 4
        ttk.Label(form, text="调度模式").grid(row=3, column=0, sticky="w", padx=6, pady=6)
        self.scheduling_var = tk.StringVar(value=cfg.scheduling_mode)
        sched_menu = ttk.Combobox(form, textvariable=self.scheduling_var, values=["Balance", "CacheFirst", "PerformanceFirst"])
        sched_menu.grid(row=3, column=1, sticky="w", padx=6, pady=6)

        self.logging_var = tk.BooleanVar(value=cfg.enable_logging)
        ttk.Checkbutton(form, text="启用日志", variable=self.logging_var).grid(row=3, column=2, sticky="w", padx=6, pady=6)

        ttk.Button(form, text="保存配置", command=self._save_proxy_config).grid(row=4, column=0, columnspan=6, pady=16)

    def _proxy_status_text(self) -> str:
        return "代理状态: 运行中" if self.state.proxy_running else "代理状态: 未运行"

    def _toggle_proxy_service(self) -> None:
        if self.state.proxy_running:
            self.proxy_service.stop()
        else:
            self._save_proxy_config()
            self.proxy_service.start()
        self.proxy_status_label.config(text=self._proxy_status_text())

    def _regenerate_key(self) -> None:
        self.api_key_var.set(secrets.token_urlsafe(20))
        self._save_proxy_config()

    def _save_proxy_config(self) -> None:
        cfg = self.state.proxy_config
        cfg.port = max(1, int(self.port_var.get() or 8045))
        cfg.request_timeout = max(30, int(self.timeout_var.get() or 120))
        cfg.auth_mode = self.auth_var.get()
        cfg.api_key = self.api_key_var.get()
        cfg.allow_lan_access = self.allow_lan_var.get()
        cfg.auto_start = self.auto_start_var.get()
        cfg.upstream_proxy.enabled = self.upstream_enabled_var.get()
        cfg.upstream_proxy.url = self.upstream_var.get()
        cfg.enable_logging = self.logging_var.get()
        cfg.scheduling_mode = self.scheduling_var.get()
        self.state.save()
        messagebox.showinfo("已保存", "代理配置已更新")


def main() -> None:
    state = AppState()
    app = AntigravityApp(state)
    app.mainloop()


if __name__ == "__main__":
    main()
