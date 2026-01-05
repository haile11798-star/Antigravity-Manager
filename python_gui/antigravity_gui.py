"""
Windows-friendly Antigravity GUI with real Google OAuth and quota retrieval.

Key capabilities now implemented:
- OAuth flow to obtain Google access/refresh tokens (offline access, consent prompt).
- Automatic user info fetch to bind accounts by email.
- Token refresh with expiry tracking.
- Real quota query via cloudcode-pa APIs, including subscription tier detection.
- Account list with current marker, proxy toggle, quota refresh, and JSON export.
- Proxy settings editor with stateful start/stop toggle (no network binding for safety).

State is persisted to ``~/.antigravity_gui_state.json`` so logins and quotas survive restarts.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# OAuth constants (same as the Tauri backend)
CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

LOAD_CODE_ASSIST_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
QUOTA_API_URL = "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels"
USER_AGENT = "antigravity/python-gui"

STATE_PATH = Path(os.path.expanduser("~")) / ".antigravity_gui_state.json"


# ---------------------------- Data models ---------------------------- #
@dataclass
class ModelQuota:
    name: str
    percentage: int
    reset_time: str


@dataclass
class QuotaData:
    models: List[ModelQuota] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)
    subscription_tier: Optional[str] = None
    is_forbidden: bool = False


@dataclass
class TokenData:
    access_token: str
    refresh_token: str
    expires_at: float
    project_id: Optional[str] = None

    def is_expiring(self) -> bool:
        return self.expires_at <= time.time() + 300


@dataclass
class Account:
    id: str
    email: str
    name: Optional[str]
    token: TokenData
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


# ---------------------------- OAuth helpers ---------------------------- #
def build_auth_url(redirect_uri: str) -> str:
    scopes = [
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/cclog",
        "https://www.googleapis.com/auth/experimentsandconfigs",
    ]
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(code: str, redirect_uri: str) -> Dict[str, object]:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str) -> Dict[str, object]:
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_user_info(access_token: str) -> Dict[str, object]:
    resp = requests.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def run_local_server_for_code(port: int = 8400, timeout: int = 120) -> Tuple[str, str]:
    """
    Starts a lightweight HTTP server to capture the OAuth redirect.
    Returns (code, redirect_uri).
    """
    code_holder: Dict[str, str] = {}
    event = threading.Event()
    redirect_uri = f"http://127.0.0.1:{port}/oauth2callback"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if "code" in qs:
                code_holder["code"] = qs["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<h3>Authorization successful. You can close this window.</h3>")
                event.set()
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, fmt, *args):  # noqa: D401
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    start_url = build_auth_url(redirect_uri)
    webbrowser.open(start_url)

    if not event.wait(timeout):
        server.shutdown()
        raise TimeoutError("OAuth callback timed out")

    server.shutdown()
    return code_holder["code"], redirect_uri


# ---------------------------- Quota API helpers ---------------------------- #
def fetch_project_and_tier(access_token: str, email: str) -> Tuple[Optional[str], Optional[str]]:
    payload = {"metadata": {"ideType": "ANTIGRAVITY"}}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    resp = requests.post(LOAD_CODE_ASSIST_URL, json=payload, headers=headers, timeout=15)
    if not resp.ok:
        return None, None
    data = resp.json()
    project_id = data.get("cloudaicompanionProject")
    paid = (data.get("paidTier") or {}).get("id")
    current = (data.get("currentTier") or {}).get("id")
    subscription_tier = paid or current
    return project_id, subscription_tier


def fetch_quota(access_token: str, email: str) -> Tuple[QuotaData, Optional[str]]:
    project_id, subscription_tier = fetch_project_and_tier(access_token, email)
    effective_project = project_id or "bamboo-precept-lgxtn"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }
    payload = {"project": effective_project}
    resp = requests.post(QUOTA_API_URL, json=payload, headers=headers, timeout=15)

    if resp.status_code == 403:
        q = QuotaData(is_forbidden=True, subscription_tier=subscription_tier)
        return q, project_id

    resp.raise_for_status()
    body = resp.json()
    models_info = body.get("models", {}) if isinstance(body, dict) else {}

    quota = QuotaData(subscription_tier=subscription_tier)
    for name, info in models_info.items():
        quota_info = info.get("quotaInfo") if isinstance(info, dict) else None
        if not quota_info:
            continue
        remaining = quota_info.get("remainingFraction", 0)
        reset = quota_info.get("resetTime", "")
        pct = int(remaining * 100)
        if "gemini" in name or "claude" in name:
            quota.models.append(ModelQuota(name=name, percentage=pct, reset_time=reset))
    quota.last_updated = time.time()
    return quota, project_id


def ensure_fresh_token(token: TokenData) -> TokenData:
    if not token.is_expiring():
        return token
    refreshed = refresh_access_token(token.refresh_token)
    access_token = refreshed.get("access_token")
    expires_in = refreshed.get("expires_in", 3600)
    new_token = TokenData(
        access_token=access_token,
        refresh_token=token.refresh_token,
        expires_at=time.time() + int(expires_in),
        project_id=token.project_id,
    )
    return new_token


# ---------------------------- State management ---------------------------- #
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
            pcfg = raw.get("proxy_config", {})
            self.proxy_config = ProxyConfig(
                port=pcfg.get("port", 8045),
                api_key=pcfg.get("api_key", secrets.token_urlsafe(20)),
                request_timeout=pcfg.get("request_timeout", 120),
                auto_start=pcfg.get("auto_start", False),
                allow_lan_access=pcfg.get("allow_lan_access", False),
                auth_mode=pcfg.get("auth_mode", "off"),
                enable_logging=pcfg.get("enable_logging", False),
                upstream_proxy=UpstreamProxyConfig(**pcfg.get("upstream_proxy", {"enabled": False, "url": ""})),
                scheduling_mode=pcfg.get("scheduling_mode", "Balance"),
            )
            self.proxy_running = raw.get("proxy_running", False)

            accounts_payload = raw.get("accounts", [])
            self.accounts = []
            for item in accounts_payload:
                token_raw = item.get("token", {})
                token = TokenData(
                    access_token=token_raw.get("access_token", ""),
                    refresh_token=token_raw.get("refresh_token", ""),
                    expires_at=token_raw.get("expires_at", 0),
                    project_id=token_raw.get("project_id"),
                )
                quota_raw = item.get("quota", {})
                models = [ModelQuota(**m) for m in quota_raw.get("models", [])]
                quota = QuotaData(
                    models=models,
                    last_updated=quota_raw.get("last_updated", time.time()),
                    subscription_tier=quota_raw.get("subscription_tier"),
                    is_forbidden=quota_raw.get("is_forbidden", False),
                )
                acc = Account(
                    id=item.get("id", str(uuid.uuid4())),
                    email=item["email"],
                    name=item.get("name"),
                    token=token,
                    created_at=item.get("created_at", time.time()),
                    last_used=item.get("last_used", time.time()),
                    proxy_enabled=item.get("proxy_enabled", True),
                    quota=quota,
                )
                self.accounts.append(acc)
            self.current_account_id = raw.get("current_account_id")
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("State Load Failed", f"Could not read state file: {exc}")

    def save(self) -> None:
        with self._lock:
            payload = {
                "accounts": [
                    {
                        **asdict(acc),
                        "token": asdict(acc.token),
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

    def upsert_account(self, email: str, name: Optional[str], token: TokenData, quota: Optional[QuotaData]) -> Account:
        for acc in self.accounts:
            if acc.email.lower() == email.lower():
                acc.name = name
                acc.token = token
                if quota:
                    acc.quota = quota
                acc.last_used = time.time()
                self.current_account_id = acc.id
                self.save()
                return acc
        account = Account(
            id=str(uuid.uuid4()),
            email=email,
            name=name,
            token=token,
            quota=quota or QuotaData(),
        )
        self.accounts.append(account)
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
                fresh_token = ensure_fresh_token(acc.token)
                quota, project_id = fetch_quota(fresh_token.access_token, acc.email)
                if project_id:
                    fresh_token.project_id = project_id
                acc.token = fresh_token
                acc.quota = quota
                acc.last_used = time.time()
                break
        self.save()


class ProxyService:
    """Stateful placeholder to mirror start/stop UX without binding network ports."""

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


# ---------------------------- GUI ---------------------------- #
class AntigravityApp(tk.Tk):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.proxy_service = ProxyService(state)
        self.title("Antigravity Manager (Python)")
        self.geometry("1040x680")
        self.minsize(920, 600)

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

        ttk.Button(toolbar, text="OAuth 授权登录", command=self._oauth_login).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(toolbar, text="手动新增账户", command=self._open_add_dialog).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="删除所选", command=self._delete_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="设置为当前", command=self._set_current).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="切换反代开关", command=self._toggle_proxy).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="刷新额度", command=self._refresh_selected_quota).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="导出所选", command=self._export_selected).pack(side=tk.LEFT, padx=4)

        self.current_label = ttk.Label(toolbar, text=f"当前账户: {self._current_email() or '无'}", foreground="#2563eb")
        self.current_label.pack(side=tk.RIGHT)

        columns = ("email", "tier", "proxy", "updated")
        tree = ttk.Treeview(self.account_frame, columns=columns, show="headings", height=20, selectmode="extended")
        tree.heading("email", text="邮箱")
        tree.heading("tier", text="订阅类型")
        tree.heading("proxy", text="反代启用")
        tree.heading("updated", text="额度更新时间")
        tree.column("email", width=360)
        tree.column("tier", width=120, anchor=tk.CENTER)
        tree.column("proxy", width=90, anchor=tk.CENTER)
        tree.column("updated", width=180, anchor=tk.CENTER)
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
            tier = acc.quota.subscription_tier or "未知"
            tag = ("current",) if acc.id == self.state.current_account_id else ()
            self.account_tree.insert(
                "", tk.END, iid=acc.id, values=(acc.email, tier, proxy_status, updated), tags=tag
            )
        self.account_tree.tag_configure("current", background="#e0f2fe")
        self.current_label.config(text=f"当前账户: {self._current_email() or '无'}")

    def _open_add_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("手动新增账户")
        dialog.grab_set()
        ttk.Label(dialog, text="邮箱").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        email_entry = ttk.Entry(dialog, width=36)
        email_entry.grid(row=0, column=1, padx=8, pady=6)
        ttk.Label(dialog, text="Access Token").grid(row=1, column=0, padx=8, pady=6, sticky="w")
        access_entry = ttk.Entry(dialog, width=36)
        access_entry.grid(row=1, column=1, padx=8, pady=6)
        ttk.Label(dialog, text="Refresh Token").grid(row=2, column=0, padx=8, pady=6, sticky="w")
        refresh_entry = ttk.Entry(dialog, width=36, show="*")
        refresh_entry.grid(row=2, column=1, padx=8, pady=6)
        ttk.Label(dialog, text="有效期秒数").grid(row=3, column=0, padx=8, pady=6, sticky="w")
        expiry_entry = ttk.Entry(dialog, width=20)
        expiry_entry.insert(0, "3600")
        expiry_entry.grid(row=3, column=1, padx=8, pady=6, sticky="w")

        def submit() -> None:
            email = email_entry.get().strip()
            access = access_entry.get().strip()
            refresh = refresh_entry.get().strip()
            try:
                expires_in = int(expiry_entry.get().strip() or "3600")
            except ValueError:
                expires_in = 3600
            if not email or not access or not refresh:
                messagebox.showerror("缺少字段", "邮箱、Access Token 和 Refresh Token 均为必填")
                return
            token = TokenData(
                access_token=access,
                refresh_token=refresh,
                expires_at=time.time() + expires_in,
                project_id=None,
            )
            self.state.upsert_account(email, None, token, quota=None)
            self._reload_accounts()
            dialog.destroy()

        ttk.Button(dialog, text="确认添加", command=submit).grid(row=4, column=0, columnspan=2, pady=10)

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
        failed: List[str] = []
        for account_id in targets:
            try:
                self.state.refresh_quota(account_id)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{account_id}: {exc}")
        if failed:
            messagebox.showwarning("部分失败", "\n".join(failed))
        else:
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
        payload = [
            {
                "email": acc.email,
                "refresh_token": acc.token.refresh_token,
                "access_token": acc.token.access_token,
                "expires_at": acc.token.expires_at,
            }
            for acc in accounts
        ]
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        messagebox.showinfo("导出成功", f"保存至: {path}")

    def _oauth_login(self) -> None:
        try:
            code, redirect_uri = run_local_server_for_code()
            token_res = exchange_code_for_token(code, redirect_uri)
            access_token = token_res.get("access_token")
            refresh_token = token_res.get("refresh_token")
            expires_in = token_res.get("expires_in", 3600)
            if not refresh_token:
                messagebox.showwarning("缺少 Refresh Token", "Google 未返回 refresh_token，请在 Google 授权设置中撤销后重试。")
                return

            user_info = fetch_user_info(access_token)
            email = user_info.get("email")
            name = user_info.get("name")
            if not email:
                messagebox.showerror("获取邮箱失败", "无法从用户信息中解析邮箱。")
                return

            token = TokenData(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=time.time() + int(expires_in),
            )

            quota, project_id = fetch_quota(access_token, email)
            token.project_id = project_id

            self.state.upsert_account(email, name, token, quota)
            self._reload_accounts()
            messagebox.showinfo("授权成功", f"已获取 {email} 的配额信息")
        except TimeoutError as exc:
            messagebox.showerror("授权超时", str(exc))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("授权失败", str(exc))

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

        ttk.Label(form, text="监听端口").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.port_var = tk.IntVar(value=cfg.port)
        ttk.Entry(form, textvariable=self.port_var, width=12).grid(row=0, column=1, sticky="w", padx=6, pady=6)

        ttk.Label(form, text="请求超时 (秒)").grid(row=0, column=2, sticky="w", padx=6, pady=6)
        self.timeout_var = tk.IntVar(value=cfg.request_timeout)
        ttk.Entry(form, textvariable=self.timeout_var, width=12).grid(row=0, column=3, sticky="w", padx=6, pady=6)

        ttk.Label(form, text="认证模式").grid(row=0, column=4, sticky="w", padx=6, pady=6)
        self.auth_var = tk.StringVar(value=cfg.auth_mode)
        ttk.Combobox(form, textvariable=self.auth_var, values=["off", "strict", "all_except_health", "auto"]).grid(
            row=0, column=5, sticky="w", padx=6, pady=6
        )

        ttk.Label(form, text="API Key").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.api_key_var = tk.StringVar(value=cfg.api_key)
        ttk.Entry(form, textvariable=self.api_key_var, width=40).grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=6)

        self.allow_lan_var = tk.BooleanVar(value=cfg.allow_lan_access)
        ttk.Checkbutton(form, text="允许局域网访问", variable=self.allow_lan_var).grid(row=1, column=4, sticky="w", padx=6, pady=6)

        self.auto_start_var = tk.BooleanVar(value=cfg.auto_start)
        ttk.Checkbutton(form, text="开机自启", variable=self.auto_start_var).grid(row=1, column=5, sticky="w", padx=6, pady=6)

        ttk.Label(form, text="上游代理地址").grid(row=2, column=0, sticky="w", padx=6, pady=6)
        self.upstream_var = tk.StringVar(value=cfg.upstream_proxy.url)
        ttk.Entry(form, textvariable=self.upstream_var, width=50).grid(row=2, column=1, columnspan=4, sticky="w", padx=6, pady=6)
        self.upstream_enabled_var = tk.BooleanVar(value=cfg.upstream_proxy.enabled)
        ttk.Checkbutton(form, text="启用上游代理", variable=self.upstream_enabled_var).grid(row=2, column=5, sticky="w", padx=6, pady=6)

        ttk.Label(form, text="调度模式").grid(row=3, column=0, sticky="w", padx=6, pady=6)
        self.scheduling_var = tk.StringVar(value=cfg.scheduling_mode)
        ttk.Combobox(form, textvariable=self.scheduling_var, values=["Balance", "CacheFirst", "PerformanceFirst"]).grid(
            row=3, column=1, sticky="w", padx=6, pady=6
        )

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
