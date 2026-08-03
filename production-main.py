from __future__ import annotations

import base64
import hmac
import json
import os
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Dict, List, Optional

import requests
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_ROOT, "static")

CONFIG_PATH = os.environ.get("HETZNER_CONFIG_PATH", "/app/config.yaml")
WEB_CONFIG_PATH = os.environ.get("WEB_CONFIG_PATH", "/app/web_config.json")
THRESHOLD_STATE_PATH = os.environ.get("THRESHOLD_STATE_PATH", "/app/threshold_state.json")
REPORT_STATE_PATH = os.environ.get("REPORT_STATE_PATH", "/app/report_state.json")
REPORT_STATE_BACKUP_DIR = os.environ.get("REPORT_STATE_BACKUP_DIR", "/app/report_state_backups")
REPORT_STATE_BACKUP_KEEP = 7

ALERT_STATE: Dict[str, Dict[str, Optional[float]]] = {}
REBUILD_LOCKS: Dict[str, threading.Lock] = {}
SCHEDULE_STATE: Dict[str, Any] = {"last_daily_report": None, "last_task_runs": {}}
BOT_STATE: Dict[str, Any] = {"update_offset": 0, "last_message_id": None, "last_message_text": None}
QB_COOLDOWN_UNTIL: Dict[str, float] = {}
QB_REBUILD_COOLDOWN_SECONDS = 300
CF_RETRY_ATTEMPTS = 3
CF_RETRY_DELAY_SECONDS = 5
CF_REBUILD_SYNC_DELAY_SECONDS = 90
CF_VERIFY_DELAY_SECONDS = 120
REPORT_STATE_LOCK = threading.RLock()


class ReportStateError(RuntimeError):
    pass


def _load_yaml(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=False)


def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: Dict[str, Any]) -> None:
    _atomic_write_json(path, data)


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
        except (AttributeError, OSError):
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _load_threshold_state() -> Dict[str, int]:
    raw = _load_json(THRESHOLD_STATE_PATH)
    if not isinstance(raw, dict):
        return {}
    state: Dict[str, int] = {}
    for key, value in raw.items():
        try:
            state[str(key)] = int(value)
        except Exception:
            continue
    return state


def _save_threshold_state(state: Dict[str, int]) -> None:
    try:
        _save_json(THRESHOLD_STATE_PATH, state)
    except Exception as e:
        print(f"[alert] threshold state save failed: {e}")


def _persist_threshold_from_alert_state() -> None:
    levels: Dict[str, int] = {}
    for sid, data in ALERT_STATE.items():
        level = data.get("last_level")
        if level is None:
            continue
        try:
            levels[str(sid)] = int(level)
        except Exception:
            continue
    _save_threshold_state(levels)


def _now_local() -> datetime:
    return datetime.now().astimezone()


def _read_json_object(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        state = json.load(f)
    if not isinstance(state, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return state


def _report_state_backup_paths() -> List[str]:
    if not os.path.isdir(REPORT_STATE_BACKUP_DIR):
        return []
    return sorted(
        (
            os.path.join(REPORT_STATE_BACKUP_DIR, name)
            for name in os.listdir(REPORT_STATE_BACKUP_DIR)
            if name.startswith("report_state.json.bak.")
            and os.path.isfile(os.path.join(REPORT_STATE_BACKUP_DIR, name))
        ),
        reverse=True,
    )


def _load_report_state_unlocked() -> Dict[str, Any]:
    if not os.path.exists(REPORT_STATE_PATH):
        return {}
    try:
        return _read_json_object(REPORT_STATE_PATH)
    except Exception as active_error:
        for backup_path in _report_state_backup_paths():
            try:
                recovered = _read_json_object(backup_path)
                print(f"[alert] report state recovered from backup: {backup_path}")
                return recovered
            except Exception:
                continue
        raise ReportStateError(
            f"report state is invalid and no valid backup is available: {active_error}"
        ) from active_error


def _load_report_state() -> Dict[str, Any]:
    with REPORT_STATE_LOCK:
        return _load_report_state_unlocked()


def _save_report_state_unlocked(state: Dict[str, Any]) -> None:
    _backup_report_state()
    _atomic_write_json(REPORT_STATE_PATH, state)


def _save_report_state(state: Dict[str, Any]) -> None:
    with REPORT_STATE_LOCK:
        _save_report_state_unlocked(state)


def _update_report_state(mutator: Callable[[Dict[str, Any]], Any]) -> Any:
    with REPORT_STATE_LOCK:
        state = _load_report_state_unlocked()
        result = mutator(state)
        _save_report_state_unlocked(state)
        return result


def _backup_report_state() -> None:
    if not os.path.exists(REPORT_STATE_PATH):
        return
    try:
        active_state = _read_json_object(REPORT_STATE_PATH)
        os.makedirs(REPORT_STATE_BACKUP_DIR, exist_ok=True)
        ts = _now_local().strftime("%Y%m%d")
        filename = f"report_state.json.bak.{ts}"
        dst = os.path.join(REPORT_STATE_BACKUP_DIR, filename)
        if os.path.exists(dst):
            try:
                _read_json_object(dst)
                return
            except Exception:
                pass
        _atomic_write_json(dst, active_state)
        backups = sorted(
            name
            for name in os.listdir(REPORT_STATE_BACKUP_DIR)
            if name.startswith("report_state.json.bak.")
        )
        if len(backups) > REPORT_STATE_BACKUP_KEEP:
            for name in backups[: -REPORT_STATE_BACKUP_KEEP]:
                path = os.path.join(REPORT_STATE_BACKUP_DIR, name)
                if os.path.isfile(path):
                    os.remove(path)
    except Exception:
        pass


def _backfill_rebuild_stats(state: Dict[str, Any]) -> Dict[str, Any]:
    if state.get("rebuild_backfilled"):
        return state
    hourly = state.get("hourly", {}) or {}
    if not hourly:
        state["rebuild_backfilled"] = True
        return state
    stats = state.get("rebuild_stats", {}) or {}
    prev_out: Dict[str, float] = {}
    for key in sorted(hourly.keys()):
        snapshot = hourly.get(key, {}) or {}
        for sid, data in snapshot.items():
            if not isinstance(data, dict):
                continue
            name = data.get("name") or str(sid)
            out = data.get("outbound_bytes")
            if out is None:
                continue
            try:
                current = float(out)
            except Exception:
                continue
            prev = prev_out.get(name)
            if prev is not None and current < prev:
                entry = stats.get(name, {}) or {}
                entry["count"] = int(entry.get("count") or 0) + 1
                entry["last_time"] = key
                try:
                    parsed = datetime.strptime(key, "%Y-%m-%d %H:%M")
                    entry["last_time_iso"] = parsed.isoformat()
                except Exception:
                    entry["last_time_iso"] = None
                entry["last_source"] = "历史回填"
                entry["last_server_id"] = str(sid)
                sources = entry.get("sources", {}) or {}
                sources["历史回填"] = int(sources.get("历史回填") or 0) + 1
                entry["sources"] = sources
                stats[name] = entry
            prev_out[name] = current
    state["rebuild_stats"] = stats
    state["rebuild_backfilled"] = True
    return state


def _record_rebuild_event(server_id: int, server_name: str, source: str) -> None:
    def mutate(state: Dict[str, Any]) -> None:
        stats = state.get("rebuild_stats", {}) or {}
        key = server_name or str(server_id)
        entry = stats.get(key, {}) or {}
        entry["count"] = int(entry.get("count") or 0) + 1
        now = _now_local()
        entry["last_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
        entry["last_time_iso"] = now.isoformat()
        entry["last_source"] = source
        entry["last_server_id"] = str(server_id)
        sources = entry.get("sources", {}) or {}
        sources[source] = int(sources.get(source) or 0) + 1
        entry["sources"] = sources
        stats[key] = entry
        state["rebuild_stats"] = stats

    _update_report_state(mutate)


def _parse_rebuild_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception:
        return None
    local_tz = _now_local().tzinfo
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def _summarize_rebuild_stats(state: Dict[str, Any]) -> Dict[str, Any]:
    stats = state.get("rebuild_stats", {}) or {}
    total = 0
    auto_total = 0
    last_event = None
    last_time = None
    for name, entry in stats.items():
        total += int(entry.get("count") or 0)
        sources = entry.get("sources") or {}
        auto_total += int(sources.get("流量超标自动重建") or 0)
        iso = entry.get("last_time_iso")
        if not iso:
            continue
        parsed = _parse_rebuild_timestamp(iso)
        if parsed is None:
            continue
        if last_time is None or parsed > last_time:
            last_time = parsed
            last_event = {
                "time": entry.get("last_time"),
                "server": name,
                "source": entry.get("last_source"),
                "server_id": entry.get("last_server_id"),
            }
    return {"total": total, "auto_total": auto_total, "last": last_event, "stats": stats}


def _bytes_to_tb(value_bytes: float) -> Decimal:
    return (Decimal(value_bytes) / (Decimal(1024) ** 4)).quantize(
        Decimal("0.001"), rounding=ROUND_HALF_UP
    )


def _quantize_tb(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _normalize_qb_instances(qb_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    instances = qb_cfg.get("instances")
    if instances is None:
        url = qb_cfg.get("url")
        if url:
            instances = [
                {
                    "name": qb_cfg.get("name"),
                    "url": url,
                    "username": qb_cfg.get("username"),
                    "password": qb_cfg.get("password"),
                    "verify_ssl": qb_cfg.get("verify_ssl", True),
                    "timeout_seconds": qb_cfg.get("timeout_seconds"),
                    "login_retries": qb_cfg.get("login_retries"),
                    "login_retry_delay": qb_cfg.get("login_retry_delay"),
                    "counter_mode": qb_cfg.get("counter_mode"),
                }
            ]
    if not instances:
        return []
    normalized = []
    for entry in instances:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url") or entry.get("base_url")
        if not url:
            continue
        normalized.append(
            {
                "name": entry.get("name"),
                "url": url,
                "username": entry.get("username"),
                "password": entry.get("password"),
                "verify_ssl": entry.get("verify_ssl", True),
                "timeout_seconds": entry.get("timeout_seconds"),
                "login_retries": entry.get("login_retries"),
                "login_retry_delay": entry.get("login_retry_delay"),
                "counter_mode": entry.get("counter_mode"),
            }
        )
    return normalized


def _qb_login_succeeded(response: Optional[requests.Response]) -> bool:
    if response is None:
        return False
    if response.status_code == 204:
        return True
    return (
        response.status_code == 200
        and response.text.strip().lower().startswith("ok")
    )


def _fetch_qb_instance(instance: Dict[str, Any], counter_mode: str) -> Dict[str, Any]:
    base_url = str(instance.get("url") or "").rstrip("/")
    name = instance.get("name") or base_url
    username = instance.get("username") or ""
    password = instance.get("password") or ""
    timeout = float(instance.get("timeout_seconds") or 6)
    login_retries = max(1, int(instance.get("login_retries") or 3))
    login_retry_delay = max(0, float(instance.get("login_retry_delay") or 3))
    verify_ssl = instance.get("verify_ssl", True)
    if not base_url or not username or not password:
        return {
            "name": name,
            "url": base_url,
            "status": "error",
            "error": "missing_credentials",
            "counter_mode": counter_mode,
        }
    now = time.time()
    cooldown_until = QB_COOLDOWN_UNTIL.get(name) or QB_COOLDOWN_UNTIL.get(base_url)
    if cooldown_until and now < cooldown_until:
        return {
            "name": name,
            "url": base_url,
            "status": "error",
            "error": "cooldown",
            "counter_mode": counter_mode,
        }
    session = requests.Session()
    login = None
    last_error = None
    for attempt in range(login_retries):
        try:
            login = session.post(
                f"{base_url}/api/v2/auth/login",
                data={"username": username, "password": password},
                timeout=timeout,
                verify=verify_ssl,
            )
            if _qb_login_succeeded(login):
                break
            body = login.text.strip()
            if body:
                last_error = f"status={login.status_code} body={body}"
            else:
                last_error = f"status={login.status_code}"
        except Exception as exc:
            last_error = exc
        if attempt + 1 < login_retries:
            time.sleep(login_retry_delay)
    if not _qb_login_succeeded(login):
        return {
            "name": name,
            "url": base_url,
            "status": "error",
            "error": f"login_failed: {last_error}",
            "counter_mode": counter_mode,
        }
    try:
        info = session.get(
            f"{base_url}/api/v2/sync/maindata",
            timeout=timeout,
            verify=verify_ssl,
        )
        payload = info.json()
    except Exception as exc:
        return {
            "name": name,
            "url": base_url,
            "status": "error",
            "error": f"fetch_failed: {exc}",
            "counter_mode": counter_mode,
        }
    state = payload.get("server_state") or {}
    alltime_ul = state.get("alltime_ul")
    alltime_dl = state.get("alltime_dl")
    up_info = state.get("up_info_data")
    dl_info = state.get("dl_info_data")
    if counter_mode == "session":
        upload_bytes = up_info
        download_bytes = dl_info
    else:
        upload_bytes = alltime_ul if alltime_ul is not None else up_info
        download_bytes = alltime_dl if alltime_dl is not None else dl_info
    return {
        "name": name,
        "url": base_url,
        "status": "ok",
        "upload_bytes": upload_bytes,
        "download_bytes": download_bytes,
        "upload_speed": state.get("up_info_speed"),
        "download_speed": state.get("dl_info_speed"),
        "connection_status": state.get("connection_status"),
        "counter_mode": counter_mode,
    }


def _collect_qbittorrent_stats(config: Dict[str, Any]) -> Dict[str, Any]:
    qb_cfg = config.get("qbittorrent", {}) or {}
    if not qb_cfg.get("enabled"):
        return {"enabled": False, "instances": []}
    global QB_REBUILD_COOLDOWN_SECONDS
    if qb_cfg.get("rebuild_cooldown_seconds") is not None:
        try:
            QB_REBUILD_COOLDOWN_SECONDS = max(0, int(qb_cfg.get("rebuild_cooldown_seconds")))
        except Exception:
            QB_REBUILD_COOLDOWN_SECONDS = 300
    counter_mode = qb_cfg.get("counter_mode", "alltime")
    instances = _normalize_qb_instances(qb_cfg)
    if not instances:
        return {
            "enabled": True,
            "instances": [],
            "total_upload_bytes": 0,
            "total_download_bytes": 0,
            "counter_mode": counter_mode,
        }
    results = []
    total_upload = 0
    total_download = 0
    for instance in instances:
        instance_mode = instance.get("counter_mode") or counter_mode
        result = _fetch_qb_instance(instance, instance_mode)
        results.append(result)
        if result.get("status") == "ok":
            upload = result.get("upload_bytes")
            download = result.get("download_bytes")
            if isinstance(upload, (int, float)):
                total_upload += int(upload)
            if isinstance(download, (int, float)):
                total_download += int(download)
    return {
        "enabled": True,
        "instances": results,
        "total_upload_bytes": total_upload,
        "total_download_bytes": total_download,
        "counter_mode": counter_mode,
    }


def _qb_instance_map(qb_stats: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    instances = qb_stats.get("instances") or []
    return {str(inst.get("name")): inst for inst in instances if inst.get("name")}


def _build_qb_compare_line(
    server_name: str,
    outbound_bytes: Optional[float],
    inbound_bytes: Optional[float],
    qb_map: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    if outbound_bytes is None and inbound_bytes is None:
        return None
    if not qb_map:
        return None
    inst = qb_map.get(server_name)
    if not inst:
        return None
    if inst.get("status") != "ok":
        return f"🧲 qB: {inst.get('error') or 'error'}"
    upload_bytes = inst.get("upload_bytes")
    download_bytes = inst.get("download_bytes")
    if upload_bytes is None:
        return None
    qb_upload_tb = _bytes_to_tb_precise(float(upload_bytes))
    qb_download_tb = _bytes_to_tb_precise(float(download_bytes)) if download_bytes is not None else None
    diff = None
    if outbound_bytes is not None:
        outbound_tb = _bytes_to_tb_precise(float(outbound_bytes))
        diff = (outbound_tb - qb_upload_tb).quantize(Decimal("0.000"), rounding=ROUND_HALF_UP)
    diff_in = None
    if inbound_bytes is not None and qb_download_tb is not None:
        inbound_tb = _bytes_to_tb_precise(float(inbound_bytes))
        diff_in = (inbound_tb - qb_download_tb).quantize(Decimal("0.000"), rounding=ROUND_HALF_UP)
    lines = [f"🧲 qB 上传: {qb_upload_tb} TB"]
    if qb_download_tb is not None:
        lines.append(f"📥 qB 下载: {qb_download_tb} TB")
    if diff is not None:
        lines.append(f"📏 上传差值: {diff} TB")
    if diff_in is not None:
        lines.append(f"📏 下载差值: {diff_in} TB")
    return "\n".join(lines)


def _date_from_hour_key(key: str) -> Optional[str]:
    if not key:
        return None
    return key.split(" ", 1)[0] if " " in key else None


def _telegram_inline_keyboard(menu: str) -> Dict[str, Any]:
    if menu == "query":
        keyboard = [
            [
                {"text": "🖥 服务器列表", "callback_data": "cmd:/list"},
                {"text": "📄 列表(代码块)", "callback_data": "cmd:/listcode"},
            ],
            [
                {"text": "📈 系统状态", "callback_data": "cmd:/status"},
                {"text": "📊 流量汇总", "callback_data": "cmd:/traffic"},
            ],
            [
                {"text": "📅 今日流量", "callback_data": "cmd:/today"},
                {"text": "🕒 流量汇报", "callback_data": "cmd:/report"},
            ],
            [
                {"text": "📋 汇报状态", "callback_data": "cmd:/reportstatus"},
                {"text": "♻️ 重置汇报", "callback_data": "cmd:/reportreset"},
            ],
            [
                {"text": "📦 快照列表", "callback_data": "cmd:/snapshots"},
                {"text": "🔧 DNS测试 ID", "callback_data": "prompt:/dnstest"},
            ],
            [
                {"text": "✅ DNS检查 ID", "callback_data": "prompt:/dnscheck"},
                {"text": "🔁 DNS同步", "callback_data": "cmd:/dnsync"},
            ],
            [{"text": "❓ 帮助", "callback_data": "cmd:/help"}],
            [{"text": "⬅️ 返回", "callback_data": "menu:root"}],
        ]
    elif menu == "control":
        keyboard = [
            [
                {"text": "▶️ 启动服务器 ID", "callback_data": "prompt:/startserver"},
                {"text": "⏸️ 停止服务器 ID", "callback_data": "prompt:/stopserver"},
            ],
            [
                {"text": "🔄 重启服务器 ID", "callback_data": "prompt:/reboot"},
                {"text": "🔨 重建服务器 ID", "callback_data": "prompt:/rebuild"},
            ],
            [{"text": "🗑 删除服务器 ID confirm", "callback_data": "prompt:/delete"}],
            [{"text": "⬅️ 返回", "callback_data": "menu:root"}],
        ]
    elif menu == "snapshot":
        keyboard = [
            [
                {"text": "📦 快照列表", "callback_data": "cmd:/snapshots"},
                {"text": "📸 创建快照 ID", "callback_data": "prompt:/createsnapshot"},
            ],
            [
                {"text": "🧩 批量建机", "callback_data": "cmd:/createfromsnapshots"},
                {"text": "🧩 单台建机 ID", "callback_data": "prompt:/createfromsnapshot"},
            ],
            [{"text": "⬅️ 返回", "callback_data": "menu:root"}],
        ]
    elif menu == "schedule":
        keyboard = [
            [
                {"text": "✅ 开启定时", "callback_data": "cmd:/scheduleon"},
                {"text": "⏸️ 关闭定时", "callback_data": "cmd:/scheduleoff"},
            ],
            [
                {"text": "⏰ 定时状态", "callback_data": "cmd:/schedulestatus"},
                {
                    "text": "⚙️ 设置定时 示例",
                    "callback_data": "prompt:/scheduleset",
                },
            ],
            [{"text": "⬅️ 返回", "callback_data": "menu:root"}],
        ]
    else:
        keyboard = [
            [
                {"text": "📊 查询类", "callback_data": "menu:query"},
                {"text": "🔧 控制类", "callback_data": "menu:control"},
            ],
            [
                {"text": "💾 快照管理", "callback_data": "menu:snapshot"},
                {"text": "⏰ 定时任务", "callback_data": "menu:schedule"},
            ],
            [
                {"text": "🧾 代码块模式", "callback_data": "toggle:code"},
                {"text": "📖 命令大全", "callback_data": "cmd:/help"},
            ],
        ]

    return {"inline_keyboard": keyboard}


def _map_telegram_shortcut(text: str) -> str:
    cmd = (text or "").strip()
    if not cmd:
        return ""
    aliases = {
        "📊 查询类": "__menu_query__",
        "🔧 控制类": "__menu_control__",
        "💾 快照管理": "__menu_snapshot__",
        "⏰ 定时任务": "__menu_schedule__",
        "⬅️ 返回": "__menu_root__",
        "🧾 代码块模式": "__toggle_code__",
        "📖 命令大全": "/help",
        "🖥 服务器列表": "/list",
        "📄 列表(代码块)": "/listcode",
        "📈 系统状态": "/status",
        "📊 流量汇总": "/traffic",
        "📊 流量详情 ID": "/traffic",
        "📅 今日流量": "/today",
        "📅 今日流量 ID": "/today",
        "🕒 流量汇报": "/report",
        "📋 汇报状态": "/reportstatus",
        "♻️ 重置汇报": "/reportreset",
        "📦 快照列表": "/snapshots",
        "🔧 DNS测试 ID": "/dnstest",
        "✅ DNS检查 ID": "/dnscheck",
        "🔁 DNS同步": "/dnsync",
        "⏰ 定时状态": "/schedulestatus",
        "✅ 开启定时": "/scheduleon",
        "⏸️ 关闭定时": "/scheduleoff",
        "🧩 批量建机": "/createfromsnapshots",
        "🧩 单台建机 ID": "/createfromsnapshot",
        "▶️ 启动服务器 ID": "/startserver",
        "⏸️ 停止服务器 ID": "/stopserver",
        "🔄 重启服务器 ID": "/reboot",
        "🔨 重建服务器 ID": "/rebuild",
        "🗑 删除服务器 ID confirm": "/delete",
        "📸 创建快照 ID": "/createsnapshot",
        "⚙️ 设置定时 示例": "/scheduleset delete=23:50,01:00 create=08:00,09:00",
        "❓ 帮助": "/help",
    }
    for label, mapped in aliases.items():
        if cmd == label:
            return mapped
    prefix_aliases = {
        "📊 流量详情": "/traffic",
        "📅 今日流量": "/today",
        "🔧 DNS测试": "/dnstest",
        "✅ DNS检查": "/dnscheck",
        "🔁 DNS同步": "/dnsync",
        "🧩 单台建机": "/createfromsnapshot",
        "▶️ 启动服务器": "/startserver",
        "⏸️ 停止服务器": "/stopserver",
        "🔄 重启服务器": "/reboot",
        "🔨 重建服务器": "/rebuild",
        "🗑 删除服务器": "/delete",
        "📸 创建快照": "/createsnapshot",
    }
    for label, mapped in prefix_aliases.items():
        prefix = f"{label} "
        if cmd.startswith(prefix):
            return mapped + cmd[len(label) :]
    return cmd


def _merge_hourly_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}

    def _sum_optional(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if a is None and b is None:
            return None
        if a is None:
            return float(b)
        if b is None:
            return float(a)
        return float(a) + float(b)

    for sid, data in snapshot.items():
        name = data.get("name") or str(sid)
        entry = merged.setdefault(
            name, {"name": name, "outbound_bytes": None, "inbound_bytes": None}
        )
        entry["outbound_bytes"] = _sum_optional(entry.get("outbound_bytes"), data.get("outbound_bytes"))
        entry["inbound_bytes"] = _sum_optional(entry.get("inbound_bytes"), data.get("inbound_bytes"))
    return merged


def _merge_hourly_series(hourly: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {key: _merge_hourly_snapshot(snapshot) for key, snapshot in hourly.items()}


def _parse_hour(key: str) -> Optional[int]:
    try:
        return datetime.strptime(key, "%Y-%m-%d %H:%M").hour
    except Exception:
        return None


def _active_server_name_map(config: Dict[str, Any]) -> Dict[str, str]:
    try:
        client = HetznerClient(config["hetzner"]["api_token"])
        servers = client.get_servers()
    except Exception:
        return {}
    return {str(s["id"]): s.get("name") or str(s["id"]) for s in servers}


def _filter_snapshot(
    snapshot: Dict[str, Any],
    include_ids: Optional[set],
    name_map: Optional[Dict[str, str]] = None,
    include_names: Optional[set] = None,
) -> Dict[str, Any]:
    if not include_ids and not include_names:
        return snapshot
    filtered: Dict[str, Any] = {}
    for sid, data in snapshot.items():
        sid_str = str(sid)
        name = data.get("name") if isinstance(data, dict) else None
        if include_ids and sid_str in include_ids:
            pass
        elif include_names and name in include_names:
            pass
        else:
            continue
        if isinstance(data, dict):
            entry = dict(data)
            if name_map and sid_str in name_map:
                entry["name"] = name_map[sid_str]
            filtered[sid_str] = entry
        else:
            filtered[sid_str] = data
    return filtered


def _compute_cycle_data(
    hourly: Dict[str, Any],
    include_ids: Optional[set] = None,
    name_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    keys = sorted(hourly.keys())
    if len(keys) < 2:
        return {"servers": {}}

    server_ids = set()
    for snapshot in hourly.values():
        server_ids.update(snapshot.keys())
    if include_ids:
        server_ids = {sid for sid in server_ids if str(sid) in include_ids}

    servers: Dict[str, Any] = {}
    for sid in server_ids:
        cycle_out = Decimal("0.000")
        cycle_age = 0
        points: List[Dict[str, Any]] = []
        rebuilds: List[str] = []
        name = name_map.get(str(sid)) if name_map else None

        for i in range(1, len(keys)):
            prev_key = keys[i - 1]
            curr_key = keys[i]
            prev = hourly.get(prev_key, {})
            curr = hourly.get(curr_key, {})
            prev_data = prev.get(sid)
            curr_data = curr.get(sid)
            if curr_data and not name:
                name = curr_data.get("name") or str(sid)

            rebuild = False
            if prev_data and curr_data:
                prev_out = prev_data.get("outbound_bytes")
                curr_out = curr_data.get("outbound_bytes")
                if prev_out is not None and curr_out is not None and float(curr_out) < float(prev_out):
                    rebuild = True
            if rebuild:
                cycle_out = Decimal("0.000")
                cycle_age = 0
                rebuilds.append(curr_key)

            deltas = _delta_by_name(prev, curr)
            name_key = name or str(sid)
            data = deltas.get(name_key, {})
            total_out = data["out"] if data.get("has_out") else Decimal("0.000")
            cycle_out += total_out
            cycle_out = _quantize_tb(cycle_out)
            points.append(
                {
                    "time": curr_key,
                    "out_tb_h": str(_quantize_tb(total_out)),
                    "cycle_out_cum_tb": str(cycle_out),
                    "cycle_age_h": cycle_age,
                    "hour_of_day": _parse_hour(curr_key),
                }
            )
            cycle_age += 1

        if points:
            servers[str(sid)] = {"name": name or str(sid), "points": points, "rebuilds": rebuilds}

    return {"servers": servers}

def _delta_by_name(prev: Dict[str, Any], curr: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    aggregates: Dict[str, Dict[str, Any]] = {}
    prev_by_name = _merge_hourly_snapshot(prev)
    curr_by_name = _merge_hourly_snapshot(curr)
    for name, data in curr_by_name.items():
        prev_data = prev_by_name.get(name, {})
        prev_out = prev_data.get("outbound_bytes")
        curr_out = data.get("outbound_bytes")
        prev_in = prev_data.get("inbound_bytes")
        curr_in = data.get("inbound_bytes")
        out_delta = None
        in_delta = None
        if prev_out is not None and curr_out is not None:
            if float(curr_out) >= float(prev_out):
                out_delta = _bytes_to_tb(float(curr_out) - float(prev_out))
            else:
                out_delta = _bytes_to_tb(float(curr_out))
        if prev_in is not None and curr_in is not None:
            if float(curr_in) >= float(prev_in):
                in_delta = _bytes_to_tb(float(curr_in) - float(prev_in))
            else:
                in_delta = _bytes_to_tb(float(curr_in))
        entry = aggregates.setdefault(
            name, {"out": Decimal("0.000"), "in": Decimal("0.000"), "has_out": False, "has_in": False}
        )
        if out_delta is not None:
            entry["out"] += out_delta
            entry["has_out"] = True
        if in_delta is not None:
            entry["in"] += in_delta
            entry["has_in"] = True
    return aggregates


def _compute_tracking_totals(
    hourly: Dict[str, Any], start_override: Optional[str] = None
) -> Dict[str, Optional[str]]:
    keys = sorted(hourly.keys())
    if not keys:
        return {"start": None, "outbound_tb": "0.000", "inbound_tb": "0.000"}
    start_idx = 0
    start_label = keys[0]
    if start_override:
        if start_override <= keys[0]:
            start_override = keys[0]
        for idx, key in enumerate(keys):
            if key >= start_override:
                start_idx = idx
                start_label = start_override
                break
        else:
            return {"start": start_override, "outbound_tb": "0.000", "inbound_tb": "0.000"}
    total_out = Decimal("0.000")
    total_in = Decimal("0.000")
    for i in range(start_idx + 1, len(keys)):
        prev = hourly.get(keys[i - 1], {})
        curr = hourly.get(keys[i], {})
        deltas = _delta_by_name(prev, curr)
        for data in deltas.values():
            if data.get("has_out"):
                total_out += data["out"]
            if data.get("has_in"):
                total_in += data["in"]
    return {
        "start": start_label,
        "outbound_tb": str(_quantize_tb(total_out)),
        "inbound_tb": str(_quantize_tb(total_in)),
    }


def _detect_last_rebuilds(hourly: Dict[str, Any], name_map: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    keys = sorted(hourly.keys())
    last: Dict[str, str] = {}
    prev_out: Dict[str, float] = {}
    name_to_id = {name: sid for sid, name in (name_map or {}).items()}
    for key in keys:
        snapshot = hourly.get(key, {})
        for sid, data in snapshot.items():
            out = data.get("outbound_bytes")
            if out is None:
                continue
            try:
                current = float(out)
            except Exception:
                continue
            name = data.get("name") or (name_map.get(str(sid)) if name_map else None) or str(sid)
            prev = prev_out.get(name)
            if prev is not None and current < prev:
                mapped_id = name_to_id.get(name)
                last[str(mapped_id or name)] = key
            prev_out[name] = current
    return last


def _rebuild_locations(config: Dict[str, Any], old_location: str) -> List[str]:
    raw = (config.get("rebuild") or {}).get("location_fallbacks")
    if not isinstance(raw, list):
        raw = []
    locations: List[str] = []
    for value in raw:
        location = str(value or "").strip()
        if location and location not in locations:
            locations.append(location)
    if not locations and old_location:
        locations.append(old_location)
    return locations


def _configured_missing_servers(
    config: Dict[str, Any], servers: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    rebuild_cfg = config.get("rebuild") or {}
    options = _manual_create_options(config)
    if not options["enabled"]:
        return []

    server_type = options["default_server_type"]
    snapshot_map = rebuild_cfg.get("snapshot_id_map") or {}
    locations = options["locations"]
    if not isinstance(snapshot_map, dict):
        return []

    existing_names = {str(server.get("name") or "") for server in servers}
    location_text = " → ".join(locations)
    rows: List[Dict[str, Any]] = []
    for raw_name, snapshot_id in snapshot_map.items():
        name = str(raw_name or "").strip()
        if not name or name in existing_names or not snapshot_id:
            continue
        rows.append(
            {
                "name": name,
                "missing": True,
                "status": "missing",
                "server_type": server_type,
                "location": location_text,
                "snapshot_id": str(snapshot_id),
            }
        )
    return sorted(rows, key=lambda row: row["name"])


def _manual_create_options(config: Dict[str, Any]) -> Dict[str, Any]:
    rebuild_cfg = config.get("rebuild") or {}
    manual_cfg = rebuild_cfg.get("manual_create") or {}
    default_server_type = str(
        manual_cfg.get("server_type") or ""
    ).strip().lower()

    raw_types = manual_cfg.get("server_types")
    if not isinstance(raw_types, list):
        raw_types = []
    server_types: List[str] = []
    for value in raw_types:
        server_type = str(value or "").strip().lower()
        if server_type and server_type not in server_types:
            server_types.append(server_type)
    if default_server_type and default_server_type not in server_types:
        server_types.insert(0, default_server_type)

    locations = [
        str(location).strip().lower()
        for location in _rebuild_locations(config, "")
        if str(location).strip()
    ]
    enabled = bool(
        manual_cfg.get("enabled")
        and default_server_type
        and server_types
        and locations
    )
    return {
        "enabled": enabled,
        "server_types": server_types,
        "locations": locations,
        "default_server_type": default_server_type,
        "default_location": locations[0] if locations else "",
    }


def _hetzner_http_error(exc: Exception) -> Dict[str, Any]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    code = None
    message = str(exc)
    if response is not None:
        try:
            error = (response.json() or {}).get("error") or {}
            code = error.get("code")
            message = error.get("message") or message
        except Exception:
            pass
    return {"status": status, "code": code, "message": message}


class HetznerClient:
    BASE_URL = "https://api.hetzner.cloud/v1"
    CF_API_BASE = "https://api.cloudflare.com/client/v4"

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.BASE_URL}/{endpoint}"
        timeout = kwargs.pop("timeout", 20)
        resp = requests.request(method, url, headers=self.headers, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _request_paginated(self, endpoint: str, result_key: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        page = 1
        items: List[Dict[str, Any]] = []
        base_params = dict(params or {})
        while True:
            req_params = {**base_params, "page": page, "per_page": 50}
            data = self._request("GET", endpoint, params=req_params)
            chunk = data.get(result_key, [])
            if not chunk:
                break
            items.extend(chunk)
            pagination = data.get("meta", {}).get("pagination", {})
            if not pagination:
                break
            if page >= int(pagination.get("last_page") or page):
                break
            page += 1
        return items

    def _resource_exists(self, endpoint: str) -> bool:
        try:
            self._request("GET", endpoint)
            return True
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) == 404:
                return False
            raise

    def wait_for_rebuild_resources(
        self,
        server_id: int,
        primary_ip_ids: List[int],
        timeout_seconds: float,
        poll_seconds: float,
    ) -> Dict[str, Any]:
        timeout_seconds = max(0.0, float(timeout_seconds))
        poll_seconds = max(0.1, float(poll_seconds))
        resources = [(f"server:{server_id}", f"servers/{server_id}")]
        seen_primary_ips = set()
        for primary_ip_id in primary_ip_ids:
            normalized_id = int(primary_ip_id)
            if normalized_id in seen_primary_ips:
                continue
            seen_primary_ips.add(normalized_id)
            resources.append(
                (
                    f"primary_ip:{normalized_id}",
                    f"primary_ips/{normalized_id}",
                )
            )

        started_at = time.monotonic()
        while True:
            remaining_resources: List[str] = []
            for resource_name, endpoint in resources:
                try:
                    if self._resource_exists(endpoint):
                        remaining_resources.append(resource_name)
                except Exception as exc:
                    error = _hetzner_http_error(exc)
                    return {
                        "success": False,
                        "error": (
                            "检查旧服务器资源释放状态失败: "
                            f"{error.get('message') or exc}"
                        ),
                        "error_code": "primary_ip_release_check_failed",
                        "resource": resource_name,
                        "http_status": error.get("status"),
                    }

            elapsed = max(0.0, time.monotonic() - started_at)
            if not remaining_resources:
                return {
                    "success": True,
                    "waited_seconds": round(elapsed, 3),
                }

            remaining_time = timeout_seconds - elapsed
            if remaining_time <= 0:
                timeout_text = (
                    str(int(timeout_seconds))
                    if timeout_seconds.is_integer()
                    else str(timeout_seconds)
                )
                return {
                    "success": False,
                    "error": (
                        "等待旧服务器 Primary IP 释放超时"
                        f"（{timeout_text}秒）"
                    ),
                    "error_code": "primary_ip_release_timeout",
                    "remaining_resources": remaining_resources,
                    "waited_seconds": round(elapsed, 3),
                }

            time.sleep(min(poll_seconds, remaining_time))

    def get_servers(self) -> List[Dict[str, Any]]:
        return self._request_paginated("servers", "servers")

    def get_server(self, server_id: int) -> Optional[Dict[str, Any]]:
        try:
            data = self._request("GET", f"servers/{server_id}")
            return data.get("server")
        except Exception:
            return None

    def get_server_metrics(self, server_id: int, start: str, end: str) -> Dict[str, Any]:
        try:
            params = {"type": "traffic", "start": start, "end": end}
            data = self._request("GET", f"servers/{server_id}/metrics", params=params)
            return data.get("metrics", {})
        except Exception:
            return {}

    def delete_server(self, server_id: int) -> bool:
        try:
            self._request("DELETE", f"servers/{server_id}")
            return True
        except Exception:
            return False

    def power_on_server(self, server_id: int) -> bool:
        try:
            self._request("POST", f"servers/{server_id}/actions/poweron")
            return True
        except Exception:
            return False

    def power_off_server(self, server_id: int) -> bool:
        try:
            self._request("POST", f"servers/{server_id}/actions/poweroff")
            return True
        except Exception:
            return False

    def reboot_server(self, server_id: int) -> bool:
        try:
            self._request("POST", f"servers/{server_id}/actions/reboot")
            return True
        except Exception:
            return False

    def get_snapshots(self) -> List[Dict[str, Any]]:
        try:
            snapshots = self._request_paginated("images", "images", params={"type": "snapshot"})
            snapshots.sort(key=lambda x: x.get("created", ""), reverse=True)
            return snapshots
        except Exception:
            return []

    def create_snapshot(self, server_id: int, description: str = "") -> Optional[Dict[str, Any]]:
        try:
            payload: Dict[str, Any] = {"type": "snapshot"}
            if description:
                payload["description"] = description
            data = self._request("POST", f"servers/{server_id}/actions/create_image", json=payload)
            return data.get("image")
        except Exception:
            return None

    def create_server_from_snapshot(
        self,
        name: str,
        server_type: str,
        location: str,
        snapshot_id: int,
        ssh_keys: Optional[List[int]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not server_type or not location:
            return None
        payload: Dict[str, Any] = {
            "name": name,
            "server_type": server_type,
            "location": location,
            "image": snapshot_id,
        }
        if ssh_keys:
            payload["ssh_keys"] = ssh_keys
        try:
            data = self._request("POST", "servers", json=payload)
            return data.get("server")
        except Exception:
            return None

    def create_missing_server(
        self,
        name: str,
        config: Dict[str, Any],
        server_type: Optional[str] = None,
        preferred_location: Optional[str] = None,
        allow_fallback: bool = True,
    ) -> Dict[str, Any]:
        server_name = str(name or "").strip()
        rebuild_cfg = config.get("rebuild") or {}
        options = _manual_create_options(config)
        if not options["enabled"]:
            return {
                "success": False,
                "error": "手动创建未启用",
                "error_code": "manual_create_disabled",
            }

        snapshot_map = rebuild_cfg.get("snapshot_id_map") or {}
        snapshot_id = snapshot_map.get(server_name) if isinstance(snapshot_map, dict) else None
        if not server_name or not snapshot_id:
            return {
                "success": False,
                "error": "该服务器名称不在手动创建白名单中",
                "error_code": "name_not_allowed",
            }

        selected_server_type = str(
            server_type or options["default_server_type"]
        ).strip().lower()
        if selected_server_type not in options["server_types"]:
            return {
                "success": False,
                "error": f"不允许创建规格 {selected_server_type}",
                "error_code": "server_type_not_allowed",
            }

        selected_location = str(
            preferred_location or options["default_location"]
        ).strip().lower()
        if selected_location not in options["locations"]:
            return {
                "success": False,
                "error": f"不允许创建地区 {selected_location}",
                "error_code": "location_not_allowed",
            }
        locations = [selected_location]
        if allow_fallback:
            locations.extend(
                location
                for location in options["locations"]
                if location != selected_location
            )

        existing_names = {
            str(server.get("name") or "").strip() for server in self.get_servers()
        }
        if server_name in existing_names:
            return {
                "success": False,
                "error": f"服务器 {server_name} 已存在，已取消创建",
                "error_code": "already_exists",
            }

        attempted_locations: List[str] = []
        capacity_errors: List[Dict[str, Any]] = []
        for location in locations:
            create_data = {
                "name": server_name,
                "server_type": selected_server_type,
                "image": snapshot_id,
                "location": location,
                "start_after_create": True,
            }
            attempted_locations.append(location)
            try:
                response = self._request("POST", "servers", json=create_data)
                new_server = response.get("server")
                if not new_server:
                    return {
                        "success": False,
                        "error": f"{location} 创建服务器未返回 server",
                        "error_code": "empty_server_response",
                        "attempted_locations": attempted_locations,
                    }
                public_net = new_server.get("public_net") or {}
                ipv4 = public_net.get("ipv4") or {}
                return {
                    "success": True,
                    "new_server_id": new_server.get("id"),
                    "new_ip": ipv4.get("ip"),
                    "snapshot_id": snapshot_id,
                    "server_type": selected_server_type,
                    "new_location": location,
                    "attempted_locations": attempted_locations,
                }
            except requests.HTTPError as exc:
                error = _hetzner_http_error(exc)
                if (
                    error.get("status") == 412
                    and error.get("code") == "resource_unavailable"
                ):
                    capacity_errors.append({"location": location, **error})
                    continue
                return {
                    "success": False,
                    "error": f"{location}: {error.get('message')}",
                    "error_code": error.get("code"),
                    "http_status": error.get("status"),
                    "attempted_locations": attempted_locations,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"{location}: {exc}",
                    "error_code": "create_failed",
                    "attempted_locations": attempted_locations,
                }

        locations_text = "、".join(attempted_locations)
        return {
            "success": False,
            "error": (
                f"{selected_server_type} 创建失败：{locations_text} 均无可用容量；"
                "不自动重试"
            ),
            "error_code": "resource_unavailable",
            "http_status": 412,
            "attempted_locations": attempted_locations,
            "capacity_errors": capacity_errors,
        }

    def rebuild_server(self, server_id: int, config: Dict[str, Any]) -> Dict[str, Any]:
        old_server = self.get_server(server_id)
        if not old_server:
            return {"success": False, "error": "服务器不存在"}

        snapshot_id_map = config.get("rebuild", {}).get("snapshot_id_map", {})
        mapped_id = snapshot_id_map.get(str(server_id)) or snapshot_id_map.get(old_server.get("name"))
        if mapped_id:
            image = mapped_id
        else:
            snapshots = self.get_snapshots()
            if not snapshots:
                return {"success": False, "error": "没有可用快照，已取消重建"}
            image = snapshots[0]["id"]

        old_location = (
            old_server.get("location")
            or old_server.get("datacenter", {}).get("location")
            or {}
        ).get("name")
        locations = _rebuild_locations(config, old_location)
        if not locations:
            return {"success": False, "error": "没有可用的重建地区"}

        public_net = old_server.get("public_net") or {}
        primary_ip_ids: List[int] = []
        for family in ("ipv4", "ipv6"):
            primary_ip_id = (public_net.get(family) or {}).get("id")
            if primary_ip_id is not None:
                primary_ip_ids.append(int(primary_ip_id))

        if not self.delete_server(server_id):
            return {"success": False, "error": "删除服务器失败"}

        rebuild_cfg = config.get("rebuild") or {}
        timeout_seconds = max(
            0.0,
            _parse_float_or_default(
                rebuild_cfg.get("primary_ip_release_timeout_seconds"),
                120.0,
            ),
        )
        poll_seconds = max(
            0.1,
            _parse_float_or_default(
                rebuild_cfg.get("primary_ip_release_poll_seconds"),
                3.0,
            ),
        )
        release_result = self.wait_for_rebuild_resources(
            server_id,
            primary_ip_ids,
            timeout_seconds,
            poll_seconds,
        )
        if not release_result.get("success"):
            return release_result

        attempted_locations: List[str] = []
        capacity_errors: List[Dict[str, Any]] = []
        new_server: Optional[Dict[str, Any]] = None
        new_location: Optional[str] = None

        for location in locations:
            create_data = {
                "name": old_server["name"],
                "server_type": old_server["server_type"]["name"],
                "image": image,
                "location": location,
                "start_after_create": True,
            }
            attempted_locations.append(location)
            try:
                resp = self._request("POST", "servers", json=create_data)
                new_server = resp.get("server")
                if new_server:
                    new_location = location
                    break
                return {
                    "success": False,
                    "error": f"{location} 创建服务器未返回 server",
                    "attempted_locations": attempted_locations,
                }
            except requests.HTTPError as exc:
                error = _hetzner_http_error(exc)
                if error.get("status") == 412 and error.get("code") == "resource_unavailable":
                    capacity_errors.append({"location": location, **error})
                    continue
                return {
                    "success": False,
                    "error": f"{location}: {error.get('message')}",
                    "error_code": error.get("code"),
                    "http_status": error.get("status"),
                    "attempted_locations": attempted_locations,
                }
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"{location}: {exc}",
                    "attempted_locations": attempted_locations,
                }

        if not new_server:
            locations_text = "、".join(attempted_locations)
            return {
                "success": False,
                "error": (
                    f"{old_server['server_type']['name']} 创建失败："
                    f"{locations_text} 均无可用容量；未自动重试"
                ),
                "error_code": "resource_unavailable",
                "http_status": 412,
                "attempted_locations": attempted_locations,
                "capacity_errors": capacity_errors,
            }

        return {
            "success": True,
            "new_server_id": new_server["id"],
            "new_ip": new_server["public_net"]["ipv4"]["ip"],
            "snapshot_id": image,
            "new_location": new_location,
            "attempted_locations": attempted_locations,
        }

    def update_cloudflare_a_record(
        self,
        api_token: str,
        zone_id: str,
        record_name: str,
        ip: str,
        attempts: int = 3,
        delay_seconds: float = 3,
    ) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for _ in range(attempts):
            try:
                headers = {
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                }
                list_url = f"{self.CF_API_BASE}/zones/{zone_id}/dns_records"
                params = {"type": "A", "name": record_name}
                resp = requests.get(list_url, headers=headers, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                records = data.get("result", [])
                if not records:
                    return {"success": False, "error": "DNS记录不存在"}
                record = records[0]
                current_ip = record.get("content")
                if current_ip == ip:
                    return {
                        "success": True,
                        "changed": False,
                        "old_ip": current_ip,
                        "new_ip": ip,
                    }
                record_id = record.get("id")
                update_url = f"{self.CF_API_BASE}/zones/{zone_id}/dns_records/{record_id}"
                payload = {
                    "type": "A",
                    "name": record_name,
                    "content": ip,
                    "ttl": record.get("ttl", 1),
                    "proxied": record.get("proxied", False),
                }
                upd = requests.put(update_url, headers=headers, json=payload, timeout=15)
                upd.raise_for_status()
                return {
                    "success": True,
                    "changed": True,
                    "old_ip": current_ip,
                    "new_ip": ip,
                }
            except Exception as e:
                last_error = e
                if delay_seconds > 0:
                    time.sleep(delay_seconds)
        return {"success": False, "error": str(last_error)}


def _get_basic_auth(request: Request) -> Optional[tuple]:
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Basic "):
        return None
    raw = auth.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        if ":" not in decoded:
            return None
        user, pwd = decoded.split(":", 1)
        return user, pwd
    except Exception:
        return None


def _require_auth(request: Request) -> None:
    cfg = _load_json(WEB_CONFIG_PATH)
    auth = _get_basic_auth(request)
    if not auth:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"Hetzner Web\""},
        )
    user, pwd = auth
    expected_user = str(cfg.get("username") or "")
    expected_pwd = str(cfg.get("password") or "")
    if not (hmac.compare_digest(user, expected_user) and hmac.compare_digest(pwd, expected_pwd)):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic realm=\"Hetzner Web\""},
        )


def _parse_int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_alert_levels(raw_levels: Any) -> List[int]:
    if isinstance(raw_levels, list):
        levels = []
        for item in raw_levels:
            try:
                levels.append(int(item))
            except Exception:
                continue
        levels = [level for level in levels if level > 0]
        if levels:
            return sorted(set(levels))
    return [80, 90, 95, 100]


def _format_iso(dt: datetime) -> str:
    return dt.isoformat()


def _integrate_time_series(series: List[List[Any]]) -> float:
    total = 0.0
    if not series or len(series) < 2:
        return 0.0
    for i in range(len(series) - 1):
        try:
            value = float(series[i][1])
            t_curr = datetime.fromisoformat(series[i][0].replace("Z", "+00:00"))
            t_next = datetime.fromisoformat(series[i + 1][0].replace("Z", "+00:00"))
            duration = (t_next - t_curr).total_seconds()
            total += value * duration
        except Exception:
            continue
    return total


def _get_today_traffic_bytes(client: "HetznerClient", server_id: int) -> Dict[str, float]:
    now = _now_local()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    metrics = client.get_server_metrics(server_id, start=_format_iso(start), end=_format_iso(now))
    time_series = metrics.get("time_series", {}) if isinstance(metrics, dict) else {}
    out_series = time_series.get("traffic.0.out", [])
    in_series = time_series.get("traffic.0.in", [])
    return {
        "out_bytes": _integrate_time_series(out_series),
        "in_bytes": _integrate_time_series(in_series),
    }


def _send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> bool:
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code >= 400:
            print(f"[alert] telegram send failed: {resp.status_code} {resp.text}")
            return False
        return True
    except Exception as e:
        print(f"[alert] telegram send failed: {e}")
        return False


def _send_telegram_markdown(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> bool:
    if not bot_token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code >= 400:
            print(f"[alert] telegram send failed: {resp.status_code} {resp.text}")
            # Fallback to plain text if Markdown parse fails or chat errors are transient.
            return _send_telegram_message(bot_token, chat_id, text, reply_markup=reply_markup)
        return True
    except Exception as e:
        print(f"[alert] telegram send failed: {e}")
        return False


def _answer_telegram_callback(bot_token: str, callback_id: Optional[str]) -> None:
    if not bot_token or not callback_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_id}, timeout=10)
    except Exception as e:
        print(f"[alert] telegram callback answer failed: {e}")


def _maybe_wrap_codeblock(text: str) -> str:
    if not BOT_STATE.get("code_mode"):
        return text
    if "```" in text:
        return text
    return f"```text\n{text}\n```"


def _telegram_reply_keyboard_root() -> Dict[str, Any]:
    return {
        "keyboard": [
            ["📊 查询类", "🔧 控制类"],
            ["💾 快照管理", "⏰ 定时任务"],
            ["🧾 代码块模式", "📖 命令大全"],
        ],
        "is_persistent": True,
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }


def _bytes_to_gb(value_bytes: float) -> Decimal:
    return (Decimal(value_bytes) / (Decimal(1024) ** 3)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _bytes_to_tb_precise(value_bytes: float, places: str = "0.000") -> Decimal:
    return (Decimal(value_bytes) / (Decimal(1024) ** 4)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _progress_bar(percent: float) -> str:
    bars = int(max(0, min(100, percent)) / 10)
    return "█" * bars + "░" * (10 - bars)


def _format_traffic_notification(
    server_name: str,
    outbound_bytes: Optional[float],
    inbound_bytes: Optional[float],
    limit_tb: Decimal,
    percent: float,
    threshold: int,
    qb_line: Optional[str] = None,
) -> str:
    emojis = {
        10: "💧",
        20: "💦",
        30: "🌊",
        40: "🟢",
        50: "🟡",
        60: "🟠",
        70: "🔶",
        80: "🔴",
        90: "🚨",
        100: "💀",
    }
    emoji = emojis.get(threshold, "📊")
    outbound_tb = _bytes_to_tb(float(outbound_bytes)) if outbound_bytes is not None else Decimal("0.000")
    inbound_tb = _bytes_to_tb_precise(float(inbound_bytes)) if inbound_bytes is not None else Decimal("0.000")
    outbound_tb_precise = _bytes_to_tb_precise(float(outbound_bytes)) if outbound_bytes is not None else Decimal("0.000")
    remaining_tb = (limit_tb - outbound_tb).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    bar = _progress_bar(percent)
    message = (
        f"{emoji} *流量通知 - {threshold}%*\n\n"
        f"🖥 服务器: *{server_name}*\n"
        f"📊 使用进度:\n"
        f"`{bar}` {percent:.1f}%\n\n"
        f"💾 已用(出站): *{outbound_tb} TB* / {limit_tb} TB\n"
        f"📉 剩余: {remaining_tb} TB\n\n"
        f"📤 出站: {outbound_tb_precise} TB\n"
        f"📥 入站: {inbound_tb} TB"
    )
    if qb_line:
        message = f"{message}\n\n{qb_line}"
    return message


def _format_exceed_notification(server_name: str, percent: float) -> str:
    return (
        "🚨 *流量超限警报！*\n\n"
        f"🖥 服务器: *{server_name}*\n"
        f"📊 已达到: *{percent:.2f}%*\n\n"
        "⚡ 准备自动重建..."
    )


def _resolve_cf_record(record_cfg: Any, fallback_zone: str, fallback_token: str) -> Optional[Dict[str, str]]:
    if isinstance(record_cfg, str):
        return {"record": record_cfg, "zone_id": fallback_zone, "api_token": fallback_token}
    if isinstance(record_cfg, dict):
        record = record_cfg.get("record") or record_cfg.get("name")
        zone_id = record_cfg.get("zone_id") or fallback_zone
        api_token = record_cfg.get("api_token") or fallback_token
        if record and zone_id and api_token:
            return {"record": record, "zone_id": zone_id, "api_token": api_token}
    return None


def _verify_dns_record(record: str, expected_ip: str) -> Dict[str, Any]:
    try:
        socket.setdefaulttimeout(5)
        resolved = socket.gethostbyname(record)
        return {"ok": resolved == expected_ip, "resolved": resolved}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _build_daily_report(config: Dict[str, Any], client: "HetznerClient") -> str:
    traffic_cfg = config.get("traffic", {})
    limit_gb = traffic_cfg.get("limit_gb")
    limit_bytes = None
    if limit_gb:
        try:
            limit_bytes = float(Decimal(limit_gb) * (Decimal(1024) ** 3))
        except Exception:
            limit_bytes = None

    servers = client.get_servers()
    qb_stats = _collect_qbittorrent_stats(config)
    qb_map = _qb_instance_map(qb_stats) if qb_stats.get("enabled") else {}
    lines = [f"📅 **每日定时战报 ({_now_local().strftime('%Y-%m-%d')})**"]
    for s in servers:
        detail = client.get_server(s["id"]) or {}
        outgoing = detail.get("outgoing_traffic")
        ingoing = detail.get("ingoing_traffic")
        if outgoing is None or ingoing is None:
            lines.append(f"━━━━━━━━━━\n🖥️ `{s.get('name') or s['id']}`\n❌ 获取失败")
            continue
        percent = None
        if limit_bytes:
            percent = (float(outgoing) / limit_bytes) * 100
        outbound_tb = _bytes_to_tb(float(outgoing))
        inbound_tb = _bytes_to_tb(float(ingoing))
        percent_text = f" ({percent:.2f}%)" if percent is not None else ""
        qb_line = _build_qb_compare_line(
            detail.get("name") or s.get("name") or s["id"],
            outgoing,
            ingoing,
            qb_map,
        )
        block = (
            "━━━━━━━━━━\n"
            f"🖥️ `{detail.get('name') or s.get('name') or s['id']}`\n"
            f"📤 总上传: `{outbound_tb} TB`{percent_text}\n"
            f"📥 总下载: `{inbound_tb} TB`"
        )
        if qb_line:
            block = f"{block}\n{qb_line}"
        lines.append(block)
    return "\n".join(lines)


def _collect_traffic_snapshot(client: "HetznerClient") -> Dict[str, Any]:
    servers = client.get_servers()
    snapshot: Dict[str, Any] = {}
    for server in servers:
        sid = str(server["id"])
        detail = client.get_server(server["id"]) or {}
        snapshot[sid] = {
            "name": detail.get("name") or server.get("name") or sid,
            "outbound_bytes": detail.get("outgoing_traffic"),
            "inbound_bytes": detail.get("ingoing_traffic"),
        }
    return snapshot


def _record_hourly_snapshot(
    state: Dict[str, Any],
    now: datetime,
    client: "HetznerClient",
    interval_minutes: int = 60,
) -> None:
    interval = max(1, min(60, int(interval_minutes)))
    bucket_minute = (now.minute // interval) * interval
    bucket_time = now.replace(minute=bucket_minute, second=0, microsecond=0)
    hour_key = bucket_time.strftime("%Y-%m-%d %H:00") if interval >= 60 else bucket_time.strftime("%Y-%m-%d %H:%M")
    hourly = state.get("hourly", {})
    if hour_key in hourly:
        return
    hourly[hour_key] = _collect_traffic_snapshot(client)
    state["hourly"] = hourly


def _format_hourly_report(hourly: Dict[str, Any], hours: int = 24) -> str:
    if not hourly:
        return "小时分析: 暂无数据"
    keys = sorted(hourly.keys())
    keys = keys[-(hours + 1):]
    if len(keys) < 2:
        return "小时分析: 数据不足"

    servers: Dict[str, Any] = {}
    for i in range(1, len(keys)):
        prev_key = keys[i - 1]
        curr_key = keys[i]
        prev = hourly.get(prev_key, {})
        curr = hourly.get(curr_key, {})
        for sid, data in curr.items():
            if sid not in servers:
                servers[sid] = {"name": data.get("name", sid), "deltas": []}
            prev_out = prev.get(sid, {}).get("outbound_bytes")
            curr_out = data.get("outbound_bytes")
            if prev_out is None or curr_out is None or float(curr_out) < float(prev_out):
                delta_tb = None
            else:
                delta_tb = _bytes_to_tb(float(curr_out) - float(prev_out))
            servers[sid]["deltas"].append((curr_key[-5:], delta_tb))

    parts = ["🕘 *每小时出站(最近24h)*"]
    for data in servers.values():
        lines = [f"🖥 *{data['name']}*"]
        for label, delta_tb in data["deltas"]:
            val = f"{delta_tb} TB" if delta_tb is not None else "N/A"
            lines.append(f"{label}: {val}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _build_manual_report(config: Dict[str, Any], client: "HetznerClient") -> str:
    return _update_report_state(lambda state: _build_manual_report_locked(config, client, state))


def _build_manual_report_locked(
    config: Dict[str, Any], client: "HetznerClient", state: Dict[str, Any]
) -> str:
    now = _now_local()
    interval_minutes = (config.get("traffic") or {}).get("check_interval", 60)
    _record_hourly_snapshot(state, now, client, interval_minutes)

    last_time = state.get("last_time")
    last_snapshot = state.get("servers", {})
    current_snapshot = _collect_traffic_snapshot(client)

    traffic_cfg = config.get("traffic", {})
    limit_gb = traffic_cfg.get("limit_gb")
    limit_tb = None
    if limit_gb:
        try:
            limit_tb = (Decimal(limit_gb) / Decimal(1024)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        except Exception:
            limit_tb = None

    parts = ["🕒 *手动流量汇报*"]
    if last_time:
        parts.append(f"统计区间: {last_time} ~ {now.strftime('%Y-%m-%d %H:%M')}")
    else:
        parts.append("统计区间: 首次统计（仅显示累计出站）")

    for sid, data in current_snapshot.items():
        outbound = data.get("outbound_bytes")
        inbound = data.get("inbound_bytes")
        total_tb = _bytes_to_tb(float(outbound)) if outbound is not None else Decimal("0.000")
        usage = None
        if limit_tb and outbound is not None:
            usage = float((Decimal(outbound) / (Decimal(1024) ** 4) / limit_tb) * 100)

        last = last_snapshot.get(sid, {})
        last_out = last.get("outbound_bytes")
        delta_tb = None
        if outbound is not None and last_out is not None:
            delta = float(outbound) - float(last_out)
            if delta >= 0:
                delta_tb = _bytes_to_tb(delta)

        usage_text = f"{usage:.2f}%" if usage is not None else "N/A"
        delta_text = f"{delta_tb} TB" if delta_tb is not None else "N/A"
        inbound_tb = _bytes_to_tb(float(inbound)) if inbound is not None else Decimal("0.000")
        parts.append(
            f"🖥 *{data.get('name')}* (`{sid}`)\n"
            f"💾 累计出站: *{total_tb} TB* / {limit_tb if limit_tb is not None else 'N/A'} TB\n"
            f"📈 使用率: *{usage_text}*\n"
            f"📊 区间增量: *{delta_text}*\n"
            f"📥 入站: {inbound_tb} TB"
        )

    rebuild_summary = _summarize_rebuild_stats(state)
    rebuild_total = rebuild_summary.get("total") or 0
    rebuild_auto = rebuild_summary.get("auto_total") or 0
    last_rebuild = rebuild_summary.get("last") or {}
    parts.append(f"♻️ 重建统计: {rebuild_total} 次（自动 {rebuild_auto}）")
    if last_rebuild.get("time"):
        parts.append(
            f"🕒 最近重建: {last_rebuild.get('time')} · {last_rebuild.get('server')} ({last_rebuild.get('source')})"
        )

    parts.append(_format_hourly_report(state.get("hourly", {})))
    state["last_time"] = now.strftime("%Y-%m-%d %H:%M")
    state["servers"] = current_snapshot
    return "\n\n".join(parts)

def _perform_rebuild(
    server_id: int, server_name: str, config: Dict[str, Any], source: str, client: "HetznerClient"
) -> Dict[str, Any]:
    lock = REBUILD_LOCKS.setdefault(str(server_id), threading.Lock())
    if not lock.acquire(blocking=False):
        return {"success": False, "error": "重建正在进行中"}
    try:
        telegram_cfg = config.get("telegram", {})
        bot_token = telegram_cfg.get("bot_token", "")
        chat_id = telegram_cfg.get("chat_id", "")
        if telegram_cfg.get("enabled") and bot_token and chat_id:
            _send_telegram_markdown(
                bot_token,
                chat_id,
                "\n".join(
                    [
                        "🚨 *流量超限警报！*",
                        "",
                        f"🖥 服务器: *{server_name}*",
                        "⚡ 准备自动重建...",
                    ]
                ),
            )

        result = client.rebuild_server(server_id, config)
        if not result.get("success"):
            if telegram_cfg.get("enabled") and bot_token and chat_id:
                _send_telegram_markdown(
                    bot_token,
                    chat_id,
                    f"❌ *重建失败*\\n\\n错误: {result.get('error')}",
                )
            return result
        if QB_REBUILD_COOLDOWN_SECONDS > 0:
            QB_COOLDOWN_UNTIL[server_name] = time.time() + QB_REBUILD_COOLDOWN_SECONDS

        # Capture DNS mapping before config is updated, so auto DNS sync won't be skipped.
        cf_cfg = config.get("cloudflare", {}) or {}
        record_map = cf_cfg.get("record_map", {}) or {}
        record_cfg = record_map.get(str(server_id)) or record_map.get(server_name)

        new_id = result.get("new_server_id")
        if new_id:
            _update_config_mapping(config, str(server_id), str(new_id))
            _save_yaml(CONFIG_PATH, config)

        if not record_cfg and new_id:
            # If mapping was moved to the new ID, re-read it after config update.
            cf_cfg = config.get("cloudflare", {}) or {}
            record_map = cf_cfg.get("record_map", {}) or {}
            record_cfg = record_map.get(str(new_id)) or record_map.get(server_name)

        record_id = int(new_id) if new_id else server_id
        _record_rebuild_event(record_id, server_name, source)

        resolved = _resolve_cf_record(record_cfg, cf_cfg.get("zone_id", ""), cf_cfg.get("api_token", ""))
        attempts = _parse_int_or_default(cf_cfg.get("update_retries"), CF_RETRY_ATTEMPTS)
        delay_seconds = _parse_float_or_default(cf_cfg.get("update_retry_delay"), CF_RETRY_DELAY_SECONDS)
        sync_delay = _parse_int_or_default(
            cf_cfg.get("rebuild_sync_delay_seconds"), CF_REBUILD_SYNC_DELAY_SECONDS
        )
        dns_result = None
        if resolved:
            dns_result = client.update_cloudflare_a_record(
                resolved["api_token"],
                resolved["zone_id"],
                resolved["record"],
                result.get("new_ip", ""),
                attempts=attempts,
                delay_seconds=delay_seconds,
            )
            if sync_delay > 0:
                _schedule_cf_rebuild_sync(
                    client,
                    resolved,
                    result.get("new_ip", ""),
                    attempts,
                    delay_seconds,
                    sync_delay,
                )
        if telegram_cfg.get("enabled") and bot_token and chat_id:
            dns_text = ""
            verify_text = ""
            if dns_result:
                dns_text = "✅ DNS 已更新" if dns_result.get("success") else f"❌ DNS 失败: {dns_result.get('error')}"
                if dns_result.get("success") and resolved:
                    verify = _verify_dns_record(resolved["record"], result.get("new_ip", ""))
                    if verify.get("ok"):
                        verify_text = f"✅ DNS 解析一致: `{verify.get('resolved')}`"
                    elif verify.get("resolved"):
                        verify_text = f"⚠️ DNS 解析不一致: `{verify.get('resolved')}`"
                    elif verify.get("error"):
                        verify_text = f"⚠️ DNS 校验失败: {verify.get('error')}"
            lines = [
                "✅ *重建成功！流量已重置*",
                "",
                f"🆔 新ID: `{result.get('new_server_id')}`",
                f"🌐 新IP: `{result.get('new_ip')}`",
            ]
            new_location = result.get("new_location")
            if new_location:
                lines.append(f"📍 地区: `{new_location}`")
            if dns_text:
                lines.extend(["", dns_text])
                if verify_text:
                    lines.append(verify_text)
            _send_telegram_markdown(
                bot_token,
                chat_id,
                "\n".join(lines),
            )
            if dns_result and dns_result.get("success") and resolved:
                _schedule_dns_verify_notify(
                    resolved["record"],
                    result.get("new_ip", ""),
                    bot_token,
                    chat_id,
                )
        result["dns"] = dns_result
        return result
    finally:
        lock.release()


def _perform_manual_create(
    server_name: str,
    config: Dict[str, Any],
    client: "HetznerClient",
    server_type: Optional[str] = None,
    preferred_location: Optional[str] = None,
    allow_fallback: bool = True,
) -> Dict[str, Any]:
    name = str(server_name or "").strip()
    lock = REBUILD_LOCKS.setdefault(f"manual:{name}", threading.Lock())
    if not lock.acquire(blocking=False):
        return {
            "success": False,
            "error": f"服务器 {name} 正在创建中",
            "error_code": "manual_create_in_progress",
        }
    try:
        result = client.create_missing_server(
            name,
            config,
            server_type=server_type,
            preferred_location=preferred_location,
            allow_fallback=allow_fallback,
        )
        telegram_cfg = config.get("telegram") or {}
        bot_token = telegram_cfg.get("bot_token", "")
        chat_id = telegram_cfg.get("chat_id", "")
        telegram_enabled = bool(
            telegram_cfg.get("enabled") and bot_token and chat_id
        )

        if not result.get("success"):
            if telegram_enabled:
                _send_telegram_markdown(
                    bot_token,
                    chat_id,
                    "\n".join(
                        [
                            "❌ *手动创建失败*",
                            "",
                            f"🖥 服务器: *{name}*",
                            f"错误: {result.get('error')}",
                        ]
                    ),
                )
            return result

        cf_cfg = config.get("cloudflare") or {}
        record_map = cf_cfg.get("record_map") or {}
        record_cfg = record_map.get(name) if isinstance(record_map, dict) else None
        resolved = _resolve_cf_record(
            record_cfg,
            cf_cfg.get("zone_id", ""),
            cf_cfg.get("api_token", ""),
        )
        dns_result = None
        if resolved and result.get("new_ip"):
            attempts = _parse_int_or_default(
                cf_cfg.get("update_retries"), CF_RETRY_ATTEMPTS
            )
            delay_seconds = _parse_float_or_default(
                cf_cfg.get("update_retry_delay"), CF_RETRY_DELAY_SECONDS
            )
            dns_result = client.update_cloudflare_a_record(
                resolved["api_token"],
                resolved["zone_id"],
                resolved["record"],
                result["new_ip"],
                attempts=attempts,
                delay_seconds=delay_seconds,
            )
        result["dns"] = dns_result

        if telegram_enabled:
            lines = [
                "✅ *手动创建成功*",
                "",
                f"🖥 服务器: *{name}*",
                f"🆔 新ID: `{result.get('new_server_id')}`",
                f"🌐 新IP: `{result.get('new_ip')}`",
                f"💻 规格: `{result.get('server_type')}`",
                f"📍 地区: `{result.get('new_location')}`",
            ]
            if dns_result:
                if dns_result.get("success"):
                    lines.append("✅ DNS 已更新")
                else:
                    lines.append(f"❌ DNS 失败: {dns_result.get('error')}")
            _send_telegram_markdown(bot_token, chat_id, "\n".join(lines))
        return result
    finally:
        lock.release()


def _schedule_cf_rebuild_sync(
    client: "HetznerClient",
    resolved: Dict[str, str],
    ip: str,
    attempts: int,
    delay_seconds: float,
    sync_delay: int,
) -> None:
    if not ip or sync_delay <= 0:
        return

    def _worker() -> None:
        time.sleep(sync_delay)
        client.update_cloudflare_a_record(
            resolved["api_token"],
            resolved["zone_id"],
            resolved["record"],
            ip,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def _schedule_dns_verify_notify(
    record: str,
    expected_ip: str,
    bot_token: str,
    chat_id: str,
    delay_seconds: int = CF_VERIFY_DELAY_SECONDS,
) -> None:
    if not (record and expected_ip and bot_token and chat_id):
        return

    def _worker() -> None:
        time.sleep(max(5, delay_seconds))
        verify = _verify_dns_record(record, expected_ip)
        if verify.get("ok"):
            text = f"✅ DNS 解析一致: `{verify.get('resolved')}`"
        elif verify.get("resolved"):
            text = f"⚠️ DNS 解析不一致: `{verify.get('resolved')}`"
        else:
            text = f"⚠️ DNS 校验失败: {verify.get('error') or 'unknown error'}"
        _send_telegram_markdown(bot_token, chat_id, text)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


def _reconcile_cloudflare_records(config: Dict[str, Any], client: "HetznerClient") -> Dict[str, int]:
    cf_cfg = config.get("cloudflare", {}) or {}
    record_map = cf_cfg.get("record_map", {}) or {}
    result_counts = {
        "checked": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
    }
    if not record_map:
        return result_counts

    attempts = _parse_int_or_default(cf_cfg.get("update_retries"), CF_RETRY_ATTEMPTS)
    delay_seconds = _parse_float_or_default(cf_cfg.get("update_retry_delay"), CF_RETRY_DELAY_SECONDS)
    for server in client.get_servers():
        sid = str(server["id"])
        name = server.get("name") or ""
        record_cfg = record_map.get(sid) or record_map.get(name)
        resolved = _resolve_cf_record(record_cfg, cf_cfg.get("zone_id", ""), cf_cfg.get("api_token", ""))
        if not resolved:
            result_counts["skipped"] += 1
            continue

        public_net = server.get("public_net", {}) or {}
        ip = None
        if public_net.get("ipv4"):
            ip = public_net["ipv4"].get("ip")
        if not ip:
            detail = client.get_server(server["id"]) or {}
            if detail.get("public_net", {}).get("ipv4"):
                ip = detail["public_net"]["ipv4"].get("ip")
        if not ip:
            result_counts["skipped"] += 1
            continue

        result_counts["checked"] += 1
        try:
            result = client.update_cloudflare_a_record(
                resolved["api_token"],
                resolved["zone_id"],
                resolved["record"],
                ip,
                attempts=attempts,
                delay_seconds=delay_seconds,
            )
        except Exception as exc:
            result_counts["failed"] += 1
            print(f"[dns-sync] failed record={resolved['record']} error={exc}", flush=True)
            continue

        if not result.get("success"):
            result_counts["failed"] += 1
            print(
                f"[dns-sync] failed record={resolved['record']} error={result.get('error')}",
                flush=True,
            )
        elif result.get("changed"):
            result_counts["updated"] += 1
            print(
                f"[dns-sync] updated record={resolved['record']} "
                f"old={result.get('old_ip')} new={ip}",
                flush=True,
            )
        else:
            result_counts["unchanged"] += 1
    return result_counts


def _dns_sync_interval_seconds(config: Dict[str, Any]) -> int:
    raw = (config.get("cloudflare") or {}).get("sync_interval_seconds")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    return max(60, seconds)


def _sync_cloudflare_records(config: Dict[str, Any], client: "HetznerClient") -> Dict[str, int]:
    if not (config.get("cloudflare") or {}).get("sync_on_start"):
        return {
            "checked": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
        }
    return _reconcile_cloudflare_records(config, client)


def _dns_sync_loop() -> None:
    first_run = True
    while True:
        interval = 0
        try:
            config = _load_yaml(CONFIG_PATH)
            cf_cfg = config.get("cloudflare", {}) or {}
            interval = _dns_sync_interval_seconds(config)
            should_run = interval > 0 or (first_run and cf_cfg.get("sync_on_start"))
            if should_run:
                client = HetznerClient(config["hetzner"]["api_token"])
                counts = _reconcile_cloudflare_records(config, client)
                print(
                    "[dns-sync] summary "
                    + " ".join(f"{key}={value}" for key, value in counts.items()),
                    flush=True,
                )
        except Exception as exc:
            print(f"[dns-sync] cycle failed error={exc}", flush=True)
        first_run = False
        time.sleep(interval if interval > 0 else 60)


def _normalize_scheduler_tasks(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    scheduler_cfg = config.get("scheduler", {}) or {}
    tasks = scheduler_cfg.get("tasks")
    if isinstance(tasks, list) and tasks:
        return tasks
    delete_time = scheduler_cfg.get("delete_time")
    create_time = scheduler_cfg.get("create_time")
    normalized: List[Dict[str, Any]] = []
    if delete_time:
        normalized.append({"action": "delete_all", "times": [delete_time] if isinstance(delete_time, str) else delete_time})
    if create_time:
        normalized.append({"action": "create_from_snapshots", "times": [create_time] if isinstance(create_time, str) else create_time})
    return normalized


def _delete_all_servers(config: Dict[str, Any], client: "HetznerClient") -> None:
    whitelist_ids = set(str(x) for x in (config.get("whitelist", {}).get("server_ids") or []))
    whitelist_names = set(config.get("whitelist", {}).get("server_names") or [])
    servers = client.get_servers()
    for server in servers:
        sid = str(server["id"])
        if sid in whitelist_ids or server.get("name") in whitelist_names:
            continue
        client.delete_server(server["id"])
        time.sleep(1)


def _update_config_mapping(config: Dict[str, Any], old_id: str, new_id: str) -> None:
    rebuild_cfg = config.get("rebuild", {}) or {}
    snapshot_map = rebuild_cfg.get("snapshot_id_map", {}) or {}
    if old_id in snapshot_map:
        snapshot_map[new_id] = snapshot_map[old_id]
        snapshot_map.pop(old_id, None)
        rebuild_cfg["snapshot_id_map"] = snapshot_map
        config["rebuild"] = rebuild_cfg

    cf_cfg = config.get("cloudflare", {}) or {}
    record_map = cf_cfg.get("record_map", {}) or {}
    attempts = _parse_int_or_default(cf_cfg.get("update_retries"), CF_RETRY_ATTEMPTS)
    delay_seconds = _parse_float_or_default(cf_cfg.get("update_retry_delay"), CF_RETRY_DELAY_SECONDS)
    if old_id in record_map:
        record_map[new_id] = record_map[old_id]
        record_map.pop(old_id, None)
        cf_cfg["record_map"] = record_map
        config["cloudflare"] = cf_cfg


def _create_from_snapshot_map(config: Dict[str, Any], client: "HetznerClient") -> None:
    rebuild_cfg = config.get("rebuild", {}) or {}
    snapshot_map = rebuild_cfg.get("snapshot_id_map", {}) or {}
    if not snapshot_map:
        return

    template = rebuild_cfg.get("fallback_template", {}) or {}
    server_type = template.get("server_type")
    location = template.get("location")
    ssh_keys = template.get("ssh_keys") or []

    cf_cfg = config.get("cloudflare", {}) or {}
    record_map = cf_cfg.get("record_map", {}) or {}

    for old_id, snapshot_id in snapshot_map.items():
        record_cfg = record_map.get(str(old_id))
        record = None
        if isinstance(record_cfg, dict):
            record = record_cfg.get("record") or record_cfg.get("name")
        elif isinstance(record_cfg, str):
            record = record_cfg
        if record:
            name = record.split(".", 1)[0]
        else:
            name = f"auto-{old_id}"

        created = client.create_server_from_snapshot(
            name=name,
            server_type=server_type,
            location=location,
            snapshot_id=int(snapshot_id),
            ssh_keys=ssh_keys,
        )
        if not created:
            continue
        new_id = str(created.get("id"))
        new_ip = (created.get("public_net") or {}).get("ipv4", {}).get("ip")
        if new_id:
            _update_config_mapping(config, str(old_id), new_id)
            resolved = _resolve_cf_record(record_cfg, cf_cfg.get("zone_id", ""), cf_cfg.get("api_token", ""))
            if resolved and new_ip:
                client.update_cloudflare_a_record(
                    resolved["api_token"],
                    resolved["zone_id"],
                    resolved["record"],
                    new_ip,
                    attempts=attempts,
                    delay_seconds=delay_seconds,
                )


def _run_schedule_task(action: str, config: Dict[str, Any], client: "HetznerClient") -> None:
    if action == "delete_all":
        _delete_all_servers(config, client)
    elif action == "create_from_snapshots":
        _create_from_snapshot_map(config, client)


def _schedule_loop() -> None:
    while True:
        try:
            config = _load_yaml(CONFIG_PATH)
            scheduler_cfg = config.get("scheduler", {}) or {}
            if not scheduler_cfg.get("enabled"):
                time.sleep(30)
                continue
            tasks = _normalize_scheduler_tasks(config)
            if not tasks:
                time.sleep(30)
                continue

            now = _now_local()
            current_time = now.strftime("%H:%M")
            current_date = now.strftime("%Y-%m-%d")
            last_runs = SCHEDULE_STATE.setdefault("last_task_runs", {})

            for task in tasks:
                action = task.get("action")
                times = task.get("times") or []
                if isinstance(times, str):
                    times = [times]
                for t in times:
                    key = f"{action}:{t}"
                    if current_time == t and last_runs.get(key) != current_date:
                        client = HetznerClient(config["hetzner"]["api_token"])
                        _run_schedule_task(action, config, client)
                        _save_yaml(CONFIG_PATH, config)
                        last_runs[key] = current_date
        except Exception as e:
            print(f"[alert] schedule error: {e}")
        time.sleep(20)

def _monitor_traffic_loop() -> None:
    while True:
        try:
            config = _load_yaml(CONFIG_PATH)
            traffic_cfg = config.get("traffic", {})
            telegram_cfg = config.get("telegram", {})
            enabled = bool(telegram_cfg.get("enabled"))
            limit_gb = traffic_cfg.get("limit_gb")
            bot_token = telegram_cfg.get("bot_token", "")
            chat_id = telegram_cfg.get("chat_id", "")
            exceed_action = traffic_cfg.get("exceed_action", "")
            check_interval = traffic_cfg.get("check_interval", 5)
            interval_seconds = max(30, int(check_interval) * 60)

            if not limit_gb:
                time.sleep(interval_seconds)
                continue

            try:
                limit_bytes = float(Decimal(limit_gb) * (Decimal(1024) ** 3))
            except Exception:
                time.sleep(interval_seconds)
                continue

            levels = _parse_alert_levels(telegram_cfg.get("notify_levels"))
            client = HetznerClient(config["hetzner"]["api_token"])
            servers = client.get_servers()
            qb_stats = _collect_qbittorrent_stats(config)
            qb_map = _qb_instance_map(qb_stats) if qb_stats.get("enabled") else {}

            for s in servers:
                sid = str(s["id"])
                detail = client.get_server(s["id"]) or {}
                outgoing = detail.get("outgoing_traffic")
                if outgoing is None:
                    continue
                percent = (float(outgoing) / limit_bytes) * 100
                state = ALERT_STATE.setdefault(
                    sid, {"last_level": 0, "last_outgoing": None, "auto_rebuild": False}
                )
                last_outgoing = state.get("last_outgoing")
                if last_outgoing is not None and float(outgoing) < float(last_outgoing):
                    state["last_level"] = 0
                    state["auto_rebuild"] = False
                    _persist_threshold_from_alert_state()
                state["last_outgoing"] = float(outgoing)

                reached = [level for level in levels if percent >= level]
                if not reached:
                    continue
                last_level = int(state.get("last_level") or 0)
                levels_to_send = [level for level in levels if last_level < level <= percent]
                if not levels_to_send:
                    continue

                outbound_tb = _bytes_to_tb(float(outgoing))
                server_name = detail.get("name") or s.get("name") or sid
                if enabled and bot_token and chat_id:
                    limit_tb = (Decimal(limit_bytes) / (Decimal(1024) ** 4)).quantize(
                        Decimal("0.001"), rounding=ROUND_HALF_UP
                    )
                    qb_line = _build_qb_compare_line(
                        server_name,
                        outgoing,
                        detail.get("ingoing_traffic"),
                        qb_map,
                    )
                    for level in levels_to_send:
                        notify_text = _format_traffic_notification(
                            server_name,
                            outgoing,
                            detail.get("ingoing_traffic"),
                            limit_tb,
                            percent,
                            int(level),
                            qb_line,
                        )
                        if _send_telegram_markdown(bot_token, chat_id, notify_text):
                            state["last_level"] = int(level)
                            _persist_threshold_from_alert_state()
                            print(
                                f"[alert] telegram notify sent: server={server_name} "
                                f"percent={percent:.2f} level={int(level)}"
                            )

                if exceed_action in ("rebuild", "delete_rebuild") and float(outgoing) >= limit_bytes:
                    if not state.get("auto_rebuild"):
                        server_name = detail.get("name") or s.get("name") or sid
                        if enabled and bot_token and chat_id:
                            _send_telegram_markdown(
                                bot_token, chat_id, _format_exceed_notification(server_name, percent)
                            )
                        result = _perform_rebuild(
                            s["id"],
                            server_name,
                            config,
                            "流量超标自动重建",
                            client,
                        )
                        if result.get("success"):
                            state["auto_rebuild"] = True
                elif exceed_action == "delete" and float(outgoing) >= limit_bytes:
                    if not state.get("auto_rebuild"):
                        if client.delete_server(s["id"]):
                            state["auto_rebuild"] = True
        except Exception as e:
            print(f"[alert] monitor error: {e}")
        time.sleep(interval_seconds)


def _daily_report_loop() -> None:
    while True:
        try:
            config = _load_yaml(CONFIG_PATH)
            telegram_cfg = config.get("telegram", {})
            if not telegram_cfg.get("enabled"):
                time.sleep(30)
                continue
            daily_time = telegram_cfg.get("daily_report_time")
            bot_token = telegram_cfg.get("bot_token", "")
            chat_id = telegram_cfg.get("chat_id", "")
            if not daily_time or not bot_token or not chat_id:
                time.sleep(30)
                continue
            now = _now_local()
            current_time = now.strftime("%H:%M")
            current_date = now.strftime("%Y-%m-%d")
            if current_time == daily_time and SCHEDULE_STATE.get("last_daily_report") != current_date:
                client = HetznerClient(config["hetzner"]["api_token"])
                report = _build_daily_report(config, client)
                _send_telegram_markdown(bot_token, chat_id, report)
                SCHEDULE_STATE["last_daily_report"] = current_date
        except Exception as e:
            print(f"[alert] daily report error: {e}")
        time.sleep(30)


def _store_traffic_snapshot(config: Dict[str, Any], client: "HetznerClient") -> None:
    def mutate(state: Dict[str, Any]) -> None:
        interval_minutes = (config.get("traffic") or {}).get("check_interval", 5)
        now = _now_local()
        _record_hourly_snapshot(state, now, client, interval_minutes)
        hourly = state.get("hourly", {})
        if len(hourly) == 1:
            interval = max(1, min(60, int(interval_minutes)))
            bucket_minute = (now.minute // interval) * interval
            bucket_time = now.replace(minute=bucket_minute, second=0, microsecond=0)
            curr_key = (
                bucket_time.strftime("%Y-%m-%d %H:00")
                if interval >= 60
                else bucket_time.strftime("%Y-%m-%d %H:%M")
            )
            prev_time = bucket_time - timedelta(minutes=interval)
            prev_key = (
                prev_time.strftime("%Y-%m-%d %H:00")
                if interval >= 60
                else prev_time.strftime("%Y-%m-%d %H:%M")
            )
            if curr_key in hourly and prev_key not in hourly:
                hourly[prev_key] = hourly[curr_key]
                state["hourly"] = hourly

    _update_report_state(mutate)


def _snapshot_loop() -> None:
    while True:
        try:
            config = _load_yaml(CONFIG_PATH)
            token = (config.get("hetzner") or {}).get("api_token", "")
            if not token:
                time.sleep(60)
                continue
            client = HetznerClient(token)
            _store_traffic_snapshot(config, client)
        except Exception as e:
            print(f"[alert] snapshot error: {e}")
        time.sleep(300)


def _handle_bot_command(text: str, config: Dict[str, Any], client: "HetznerClient") -> str:
    raw = (text or "").strip()
    pending = BOT_STATE.pop("pending_cmd", None)
    if pending and raw and not raw.startswith("/"):
        text = f"{pending} {raw}"
    cmd = _map_telegram_shortcut(text)
    if not cmd:
        return "⚠️ 未知指令"
    if cmd == "__menu_root__":
        BOT_STATE["menu_state"] = "root"
        return "🏠 已切换到主菜单"
    if cmd == "__menu_query__":
        BOT_STATE["menu_state"] = "query"
        return "📊 已切换到查询菜单"
    if cmd == "__menu_control__":
        BOT_STATE["menu_state"] = "control"
        return "🔧 已切换到控制菜单"
    if cmd == "__menu_snapshot__":
        BOT_STATE["menu_state"] = "snapshot"
        return "💾 已切换到快照菜单"
    if cmd == "__menu_schedule__":
        BOT_STATE["menu_state"] = "schedule"
        return "⏰ 已切换到定时菜单"
    if cmd == "__toggle_code__":
        current = bool(BOT_STATE.get("code_mode"))
        BOT_STATE["code_mode"] = not current
        state = "开启" if BOT_STATE["code_mode"] else "关闭"
        return f"🧾 代码块模式已{state}"
    parts = cmd.split()
    command = parts[0].split("@")[0]
    args = parts[1:]

    if command in ("/start", "/help"):
        return (
            "📖 **命令大全**\n\n"
            "📊 查询类:\n"
            "/list - 🖥 服务器列表\n"
            "/status - 📈 系统状态\n"
            "/traffic ID - 📊 流量详情(无ID显示全部)\n"
            "/today ID - 📅 今日流量(无ID显示全部)\n"
            "/report - 🕒 手动流量汇报\n"
            "/reportstatus - 📋 上次汇报时间\n"
            "/reportreset - ♻️ 重置汇报区间\n"
            "/dnstest ID - 🔧 测试DNS更新\n"
            "/dnscheck ID - ✅ DNS解析检查\n"
            "/dnsync - 🔁 同步DNS记录\n\n"
            "🔧 控制类:\n"
            "/startserver <ID> - ▶️ 启动服务器\n"
            "/stopserver <ID> - ⏸️ 停止服务器\n"
            "/reboot <ID> - 🔄 重启服务器\n"
            "/delete <ID> confirm - 🗑 删除服务器\n"
            "/rebuild <ID> - 🔨 重建服务器\n\n"
            "💾 快照管理:\n"
            "/snapshots - 📦 查看所有快照\n"
            "/createsnapshot <ID> - 📸 手动创建快照\n\n"
            "⏰ 定时任务:\n"
            "/scheduleon - ✅ 开启定时删机\n"
            "/scheduleoff - ⏸️ 关闭定时删机\n"
            "/schedulestatus - 📋 查看定时状态\n"
            "/scheduleset delete=23:50,01:00 create=08:00,09:00 - 设置定时\n"
            "/createfromsnapshots - 🧩 依据快照批量创建\n\n"
            "/createfromsnapshot <ID> - 🧩 依据快照创建单台\n\n"
            "💡 服务器ID从 /list 获取"
        )

    if command == "/list":
        servers = client.get_servers()
        if not servers:
            return "📭 暂无服务器"
        lines = ["🖥 *服务器列表*\n"]
        for s in servers:
            ip = s.get("public_net", {}).get("ipv4", {}).get("ip", "N/A")
            status = "🟢 运行中" if s.get("status") == "running" else "🔴 已停止"
            lines.append(
                f"{status}\n"
                f"📛 *{s.get('name')}*\n"
                f"🆔 ID: `{s.get('id')}`\n"
                f"🌐 IP: `{ip}`\n"
                f"⚙️ 类型: {s.get('server_type', {}).get('name', 'N/A')}\n"
                "─────────────"
            )
        return "\n".join(lines)

    if command == "/listcode":
        servers = client.get_servers()
        if not servers:
            return "```text\n暂无服务器\n```"
        lines = ["服务器列表"]
        for s in servers:
            ip = s.get("public_net", {}).get("ipv4", {}).get("ip", "N/A")
            name = s.get("name") or s.get("id")
            lines.append(f"- {name} (id: {s.get('id')}) ip: {ip}")
        return "```text\n" + "\n".join(lines) + "\n```"

    if command in ("/status", "/ll"):
        servers = client.get_servers()
        total = len(servers)
        running_statuses = {"running"}
        starting_statuses = {"starting", "initializing", "rebuilding"}
        stopped_statuses = {"off", "stopping", "deleting"}
        running = 0
        starting = 0
        stopped = 0
        unknown = 0
        lines = []
        for s in servers:
            status = s.get("status") or "unknown"
            name = s.get("name") or s.get("id")
            if status in running_statuses:
                running += 1
                label = "🟢 运行中"
            elif status in starting_statuses:
                starting += 1
                label = "🟡 启动中"
            elif status in stopped_statuses:
                stopped += 1
                label = "🔴 已停止"
            else:
                unknown += 1
                label = "⚪ 未知"
            lines.append(f"{label} · {name} (`{s.get('id')}`)")
        telegram_cfg = config.get("telegram", {})
        levels = _parse_alert_levels(telegram_cfg.get("notify_levels"))
        notify_text = f"{', '.join(str(x) for x in levels)}%" if levels else "-"
        state = _load_report_state()
        rebuild_summary = _summarize_rebuild_stats(state)
        rebuild_total = rebuild_summary.get("total") or 0
        rebuild_auto = rebuild_summary.get("auto_total") or 0
        last_rebuild = rebuild_summary.get("last") or {}
        last_rebuild_text = (
            f"{last_rebuild.get('time')} · {last_rebuild.get('server')}"
            if last_rebuild.get("time")
            else "暂无"
        )
        return (
            "📊 *系统状态概览*\n\n"
            f"🖥 服务器总数: {total} 台\n"
            f"🟢 运行中: {running} 台\n"
            f"🟡 启动中: {starting} 台\n"
            f"🔴 已停止: {stopped} 台\n"
            f"⚪ 未知: {unknown} 台\n\n"
            f"♻️ 重建次数: {rebuild_total} (自动 {rebuild_auto})\n"
            f"🕒 最近重建: {last_rebuild_text}\n\n"
            f"🔔 通知间隔: {notify_text}\n"
            "✅ 监控系统正常运行\n\n"
            "🖥 服务器明细:\n"
            + ("\n".join(lines) if lines else "暂无服务器")
        )

    if command == "/traffic":
        traffic_cfg = config.get("traffic", {})
        limit_gb = traffic_cfg.get("limit_gb")
        limit_tb = None
        if limit_gb:
            try:
                limit_tb = (Decimal(limit_gb) / Decimal(1024)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            except Exception:
                limit_tb = None
        if not args:
            servers = client.get_servers()
            lines = ["📊 *流量汇总* (出站计费)\n"]
            for s in servers:
                detail = client.get_server(s["id"]) or {}
                outgoing = detail.get("outgoing_traffic")
                name = detail.get("name") or s.get("name") or s["id"]
                if outgoing is None or not limit_tb:
                    lines.append(f"- `{name}`")
                    continue
                total_tb = _bytes_to_tb(float(outgoing))
                percent = float((Decimal(outgoing) / (Decimal(1024) ** 4) / limit_tb) * 100)
                lines.append(
                    f"🖥 *{name}* (`{s['id']}`)\n"
                    f"💾 已用(出站): *{total_tb} TB* / {limit_tb} TB\n"
                    f"📈 使用率: *{percent:.2f}%*"
                )
            return "\n".join(lines)

        try:
            sid = int(args[0])
        except Exception:
            return "⚠️ 用法: /traffic <ID>"
        detail = client.get_server(sid)
        if not detail:
            return "❌ 服务器不存在"
        outbound = detail.get("outgoing_traffic")
        inbound = detail.get("ingoing_traffic")
        outbound_tb = _bytes_to_tb(float(outbound)) if outbound is not None else Decimal("0.000")
        inbound_tb = _bytes_to_tb(float(inbound)) if inbound is not None else Decimal("0.000")
        usage = None
        if limit_tb and outbound is not None:
            usage = float((Decimal(outbound) / (Decimal(1024) ** 4) / limit_tb) * 100)
        usage_text = f"{usage:.2f}%" if usage is not None else "N/A"
        return (
            "📊 *流量详情*\n\n"
            f"🖥 *{detail.get('name')}* (`{sid}`)\n"
            f"💾 已用(出站): *{outbound_tb} TB* / {limit_tb if limit_tb is not None else 'N/A'} TB\n"
            f"📈 使用率: *{usage_text}*\n"
            f"📥 入站: {inbound_tb} TB"
        )

    if command == "/today":
        if not args:
            servers = client.get_servers()
            lines = ["📅 *今日流量*\n"]
            for s in servers:
                detail = client.get_server(s["id"]) or {}
                name = detail.get("name") or s.get("name") or s["id"]
                usage = _get_today_traffic_bytes(client, s["id"])
                out_tb = _bytes_to_tb_precise(float(usage["out_bytes"]), places="0.000")
                in_tb = _bytes_to_tb_precise(float(usage["in_bytes"]), places="0.000")
                lines.append(f"🖥 *{name}* (`{s['id']}`)\n⬆️ {out_tb} TB | ⬇️ {in_tb} TB")
            return "\n".join(lines)
        try:
            sid = int(args[0])
        except Exception:
            return "⚠️ 用法: /today <ID>"
        detail = client.get_server(sid)
        if not detail:
            return "❌ 服务器不存在"
        usage = _get_today_traffic_bytes(client, sid)
        out_tb = _bytes_to_tb_precise(float(usage["out_bytes"]), places="0.000")
        in_tb = _bytes_to_tb_precise(float(usage["in_bytes"]), places="0.000")
        return (
            "📅 *今日流量*\n\n"
            f"🖥 *{detail.get('name')}* (`{sid}`)\n"
            f"⬆️ {out_tb} TB | ⬇️ {in_tb} TB"
        )

    if command == "/report":
        return _build_manual_report(config, client)

    if command == "/reportstatus":
        state = _load_report_state()
        last_time = state.get("last_time")
        return f"📋 上次汇报时间: {last_time}" if last_time else "📋 暂无汇报记录"

    if command == "/reportreset":
        _update_report_state(lambda state: state.clear())
        return "♻️ 已重置汇报区间"

    if command == "/dnstest":
        if not args:
            return "⚠️ 用法: /dnstest <ID>"
        try:
            sid = int(args[0])
        except Exception:
            return "⚠️ 用法: /dnstest <ID>"
        detail = client.get_server(sid)
        if not detail:
            return "❌ 服务器不存在"
        cf_cfg = config.get("cloudflare", {}) or {}
        record_map = cf_cfg.get("record_map", {}) or {}
        record_cfg = record_map.get(str(sid)) or record_map.get(detail.get("name"))
        resolved = _resolve_cf_record(record_cfg, cf_cfg.get("zone_id", ""), cf_cfg.get("api_token", ""))
        ip = detail.get("public_net", {}).get("ipv4", {}).get("ip")
        if not resolved or not ip:
            return "❌ DNS 配置缺失"
        attempts = _parse_int_or_default(cf_cfg.get("update_retries"), CF_RETRY_ATTEMPTS)
        delay_seconds = _parse_float_or_default(cf_cfg.get("update_retry_delay"), CF_RETRY_DELAY_SECONDS)
        result = client.update_cloudflare_a_record(
            resolved["api_token"],
            resolved["zone_id"],
            resolved["record"],
            ip,
            attempts=attempts,
            delay_seconds=delay_seconds,
        )
        if result.get("success"):
            return f"✅ DNS已更新: {resolved['record']} -> {ip}"
        return f"⚠️ DNS更新失败: {resolved['record']} ({result.get('error', '未知错误')})"

    if command == "/dnscheck":
        cf_cfg = config.get("cloudflare", {}) or {}
        record_map = cf_cfg.get("record_map", {}) or {}
        servers = client.get_servers()
        if args:
            try:
                target_id = int(args[0])
                servers = [s for s in servers if s["id"] == target_id]
            except Exception:
                return "⚠️ 用法: /dnscheck <ID>"
        results = ["✅ **DNS 解析检查**"]
        for s in servers:
            record_cfg = record_map.get(str(s["id"])) or record_map.get(s.get("name", ""))
            record = None
            if isinstance(record_cfg, dict):
                record = record_cfg.get("record") or record_cfg.get("name")
            elif isinstance(record_cfg, str):
                record = record_cfg
            ip = s.get("public_net", {}).get("ipv4", {}).get("ip")
            if not record or not ip:
                results.append(f"- `{s.get('name') or s['id']}`: 缺少记录或IP")
                continue
            try:
                socket.setdefaulttimeout(5)
                resolved = socket.gethostbyname(record)
                ok = "✅" if resolved == ip else "❌"
                results.append(f"- `{s.get('name')}`: {ok} {record} -> {resolved} (期望 {ip})")
            except Exception as e:
                results.append(f"- `{s.get('name')}`: ❌ {e}")
        return "\n".join(results)

    if command == "/startserver":
        if not args:
            return "⚠️ 用法: /startserver <ID>"
        try:
            sid = int(args[0])
        except Exception:
            return "⚠️ 用法: /startserver <ID>"
        return "✅ 已启动服务器" if client.power_on_server(sid) else "❌ 启动失败"

    if command == "/stopserver":
        if not args:
            return "⚠️ 用法: /stopserver <ID>"
        try:
            sid = int(args[0])
        except Exception:
            return "⚠️ 用法: /stopserver <ID>"
        return "✅ 已停止服务器" if client.power_off_server(sid) else "❌ 停止失败"

    if command == "/reboot":
        if not args:
            return "⚠️ 用法: /reboot <ID>"
        try:
            sid = int(args[0])
        except Exception:
            return "⚠️ 用法: /reboot <ID>"
        return "✅ 已重启服务器" if client.reboot_server(sid) else "❌ 重启失败"

    if command == "/delete":
        if len(args) < 2 or args[1].lower() != "confirm":
            return "⚠️ 用法: /delete <ID> confirm"
        try:
            sid = int(args[0])
        except Exception:
            return "⚠️ 用法: /delete <ID> confirm"
        return "✅ 已删除服务器" if client.delete_server(sid) else "❌ 删除失败"

    if command == "/rebuild":
        if not args:
            return "⚠️ 用法: /rebuild <ID>"
        target = None
        try:
            sid = int(args[0])
            target = client.get_server(sid)
            if target:
                name = target.get("name") or str(sid)
                result = _perform_rebuild(sid, name, config, "Telegram 指令", client)
            else:
                return "❌ 服务器不存在"
        except Exception:
            name = " ".join(args).strip()
            servers = client.get_servers()
            match = next((s for s in servers if s.get("name") == name), None)
            if not match:
                return "❌ 服务器不存在"
            result = _perform_rebuild(match["id"], name, config, "Telegram 指令", client)
        if result.get("success"):
            return "✅ 已触发重建"
        return f"❌ 重建失败: {result.get('error', '未知错误')}"

    if command == "/snapshots":
        snapshots = client.get_snapshots()
        if not snapshots:
            return "📦 暂无快照"
        lines = ["📦 快照列表\n"]
        for idx, s in enumerate(snapshots[:10], start=1):
            name = s.get("name") or s.get("description") or "snapshot"
            lines.append(f"{idx}. 📸 {name}\n   🆔 ID: {s.get('id')}\n")
        return "\n".join(lines).strip()

    if command == "/createsnapshot":
        if not args:
            return "⚠️ 用法: /createsnapshot <ID>"
        try:
            sid = int(args[0])
        except Exception:
            return "⚠️ 用法: /createsnapshot <ID>"
        description = " ".join(args[1:]).strip()
        image = client.create_snapshot(sid, description=description)
        if image:
            return f"✅ 快照已触发: `{image.get('id')}`"
        return "❌ 创建快照失败"

    if command == "/createfromsnapshots":
        telegram_cfg = config.get("telegram", {}) or {}
        bot_token = telegram_cfg.get("bot_token", "")
        chat_id = telegram_cfg.get("chat_id", "")
        def _task() -> None:
            cfg = _load_yaml(CONFIG_PATH)
            cli = HetznerClient(cfg["hetzner"]["api_token"])
            _create_from_snapshot_map(cfg, cli)
            _save_yaml(CONFIG_PATH, cfg)
            if telegram_cfg.get("enabled") and bot_token and chat_id:
                _send_telegram_markdown(bot_token, chat_id, "✅ 已根据快照配置创建服务器")
        threading.Thread(target=_task, daemon=True).start()
        return "🚀 已开始根据快照创建服务器，请稍候查看结果"

    if command == "/createfromsnapshot":
        if not args:
            return "⚠️ 用法: /createfromsnapshot <ID>"
        target_id = args[0]
        rebuild_cfg = config.get("rebuild", {}) or {}
        snapshot_map = rebuild_cfg.get("snapshot_id_map", {}) or {}
        snapshot_id = snapshot_map.get(str(target_id))
        if not snapshot_id:
            return "❌ 未找到该ID对应的快照"

        telegram_cfg = config.get("telegram", {}) or {}
        bot_token = telegram_cfg.get("bot_token", "")
        chat_id = telegram_cfg.get("chat_id", "")

        def _task() -> None:
            cfg = _load_yaml(CONFIG_PATH)
            cli = HetznerClient(cfg["hetzner"]["api_token"])
            rb = cfg.get("rebuild", {}) or {}
            snap_map = rb.get("snapshot_id_map", {}) or {}
            snap_id = snap_map.get(str(target_id))
            if not snap_id:
                if telegram_cfg.get("enabled") and bot_token and chat_id:
                    _send_telegram_markdown(bot_token, chat_id, "❌ 未找到该ID对应的快照")
                return
            template = rb.get("fallback_template", {}) or {}
            server_type = template.get("server_type")
            location = template.get("location")
            ssh_keys = template.get("ssh_keys") or []
            cf_cfg = cfg.get("cloudflare", {}) or {}
            record_cfg = (cf_cfg.get("record_map", {}) or {}).get(str(target_id))
            record = None
            if isinstance(record_cfg, dict):
                record = record_cfg.get("record") or record_cfg.get("name")
            elif isinstance(record_cfg, str):
                record = record_cfg
            name = record.split(".", 1)[0] if record else f"auto-{target_id}"

            created = cli.create_server_from_snapshot(
                name=name,
                server_type=server_type,
                location=location,
                snapshot_id=int(snap_id),
                ssh_keys=ssh_keys,
            )
            if not created:
                if telegram_cfg.get("enabled") and bot_token and chat_id:
                    _send_telegram_markdown(bot_token, chat_id, "❌ 创建服务器失败")
                return
            new_id = str(created.get("id"))
            new_ip = (created.get("public_net") or {}).get("ipv4", {}).get("ip")
            if new_id:
                _update_config_mapping(cfg, str(target_id), new_id)
                _save_yaml(CONFIG_PATH, cfg)
                resolved = _resolve_cf_record(record_cfg, cf_cfg.get("zone_id", ""), cf_cfg.get("api_token", ""))
                if resolved and new_ip:
                    cli.update_cloudflare_a_record(
                        resolved["api_token"], resolved["zone_id"], resolved["record"], new_ip
                    )
            if telegram_cfg.get("enabled") and bot_token and chat_id:
                _send_telegram_markdown(bot_token, chat_id, f"✅ 已创建服务器: {new_id}")

        threading.Thread(target=_task, daemon=True).start()
        return "🚀 已开始创建服务器，请稍候查看结果"

    if command == "/scheduleon":
        scheduler_cfg = config.get("scheduler", {}) or {}
        scheduler_cfg["enabled"] = True
        config["scheduler"] = scheduler_cfg
        _save_yaml(CONFIG_PATH, config)
        return "✅ 定时任务已开启"

    if command == "/scheduleoff":
        scheduler_cfg = config.get("scheduler", {}) or {}
        scheduler_cfg["enabled"] = False
        config["scheduler"] = scheduler_cfg
        _save_yaml(CONFIG_PATH, config)
        return "⏸️ 定时任务已关闭"

    if command == "/schedulestatus":
        scheduler_cfg = config.get("scheduler", {}) or {}
        enabled = scheduler_cfg.get("enabled")
        tasks = _normalize_scheduler_tasks(config)
        if not tasks:
            return f"📋 定时状态: {'开启' if enabled else '关闭'}\n无任务"
        lines = [f"📋 定时状态: {'开启' if enabled else '关闭'}"]
        now = _now_local()
        for task in tasks:
            action = task.get("action")
            times = task.get("times") or []
            if isinstance(times, str):
                times = [times]
            next_times = []
            for t in times:
                try:
                    hh, mm = t.split(":", 1)
                    target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                    if target <= now:
                        target = target + timedelta(days=1)
                    next_times.append(target.strftime("%m-%d %H:%M"))
                except Exception:
                    next_times.append(t)
            lines.append(f"- {action}: {', '.join(next_times)}")
        return "\n".join(lines)

    if command == "/scheduleset":
        delete_times: List[str] = []
        create_times: List[str] = []
        for arg in args:
            if "=" not in arg:
                continue
            key, value = arg.split("=", 1)
            times = [t.strip() for t in value.split(",") if t.strip()]
            if key == "delete":
                delete_times = times
            elif key == "create":
                create_times = times
        tasks: List[Dict[str, Any]] = []
        if delete_times:
            tasks.append({"action": "delete_all", "times": delete_times})
        if create_times:
            tasks.append({"action": "create_from_snapshots", "times": create_times})
        scheduler_cfg = config.get("scheduler", {}) or {}
        scheduler_cfg["enabled"] = True
        scheduler_cfg["tasks"] = tasks
        config["scheduler"] = scheduler_cfg
        _save_yaml(CONFIG_PATH, config)
        return "✅ 定时任务已更新"

    if command == "/dnsync":
        result = _sync_cloudflare_records(config, client)
        return f"✅ DNS 同步完成，更新 {result['updated']} 项，跳过 {result['skipped']} 项"

    return "⚠️ 未知指令"


def _handle_bot_callback(
    data_value: str,
    config: Dict[str, Any],
    client: "HetznerClient",
) -> tuple[str, str]:
    if not data_value:
        return "⚠️ 未知指令", BOT_STATE.get("menu_state") or "root"
    if data_value.startswith("menu:"):
        menu = data_value.split(":", 1)[1]
        if menu == "root":
            BOT_STATE["menu_state"] = "root"
            return "🏠 主菜单", "root"
        if menu in {"query", "control", "snapshot", "schedule"}:
            BOT_STATE["menu_state"] = menu
            label = {
                "query": "📊 已切换到查询菜单",
                "control": "🔧 已切换到控制菜单",
                "snapshot": "💾 已切换到快照菜单",
                "schedule": "⏰ 已切换到定时菜单",
            }[menu]
            return label, menu
    if data_value == "toggle:code":
        current = bool(BOT_STATE.get("code_mode"))
        BOT_STATE["code_mode"] = not current
        state = "开启" if BOT_STATE["code_mode"] else "关闭"
        return f"🧾 代码块模式已{state}", BOT_STATE.get("menu_state") or "root"
    if data_value.startswith("prompt:"):
        pending = data_value.split(":", 1)[1]
        BOT_STATE["pending_cmd"] = pending
        if pending == "/scheduleset":
            return (
                "请输入定时参数，例如:\n"
                "/scheduleset delete=23:50,01:00 create=08:00,09:00",
                BOT_STATE.get("menu_state") or "root",
            )
        if pending == "/delete":
            return "请输入ID和 confirm，例如: 123456 confirm", BOT_STATE.get("menu_state") or "root"
        return "请输入ID，例如: 123456", BOT_STATE.get("menu_state") or "root"
    if data_value.startswith("cmd:"):
        cmd = data_value.split(":", 1)[1]
        reply = _handle_bot_command(cmd, config, client)
        return reply, BOT_STATE.get("menu_state") or "root"
    return "⚠️ 未知指令", BOT_STATE.get("menu_state") or "root"


def _telegram_bot_loop() -> None:
    while True:
        try:
            config = _load_yaml(CONFIG_PATH)
            telegram_cfg = config.get("telegram", {})
            if not telegram_cfg.get("enabled"):
                time.sleep(10)
                continue
            bot_token = telegram_cfg.get("bot_token", "")
            chat_id = str(telegram_cfg.get("chat_id", "")).strip()
            if not bot_token or not chat_id:
                time.sleep(10)
                continue

            offset = BOT_STATE.get("update_offset", 0)
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
            resp = requests.get(url, params={"timeout": 25, "offset": offset}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                time.sleep(10)
                continue
            for update in data.get("result", []):
                update_id = update.get("update_id")
                if update_id is not None:
                    BOT_STATE["update_offset"] = update_id + 1
                callback = update.get("callback_query") or {}
                if callback:
                    callback_id = callback.get("id")
                    data_value = callback.get("data") or ""
                    message = callback.get("message") or {}
                    chat_id_cb = str(message.get("chat", {}).get("id", "")).strip()
                    if chat_id_cb and chat_id_cb == chat_id:
                        reply, menu_state = _handle_bot_callback(data_value, config, client)
                        _answer_telegram_callback(bot_token, callback_id)
                        _send_telegram_markdown(
                            bot_token,
                            chat_id,
                            _maybe_wrap_codeblock(reply),
                            reply_markup=_telegram_inline_keyboard(menu_state),
                        )
                        if not BOT_STATE.get("reply_keyboard_enabled"):
                            _send_telegram_message(
                                bot_token,
                                chat_id,
                                " ",
                                reply_markup=_telegram_reply_keyboard_root(),
                            )
                            BOT_STATE["reply_keyboard_enabled"] = True
                    continue
                message = update.get("message") or {}
                if not message:
                    continue
                if str(message.get("chat", {}).get("id")) != chat_id:
                    continue
                text = message.get("text", "")
                if not text:
                    continue
                message_id = message.get("message_id")
                if message_id is not None:
                    if message_id == BOT_STATE.get("last_message_id") and text == BOT_STATE.get("last_message_text"):
                        continue
                    BOT_STATE["last_message_id"] = message_id
                    BOT_STATE["last_message_text"] = text
                client = HetznerClient(config["hetzner"]["api_token"])
                reply = _handle_bot_command(text, config, client)
                menu_state = BOT_STATE.get("menu_state") or "root"
                _send_telegram_markdown(
                    bot_token,
                    chat_id,
                    _maybe_wrap_codeblock(reply),
                    reply_markup=_telegram_inline_keyboard(menu_state),
                )
                if not BOT_STATE.get("reply_keyboard_enabled"):
                    _send_telegram_message(
                        bot_token,
                        chat_id,
                        " ",
                        reply_markup=_telegram_reply_keyboard_root(),
                    )
                    BOT_STATE["reply_keyboard_enabled"] = True
        except Exception as e:
            print(f"[alert] telegram bot error: {e}")
        time.sleep(3)


app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _start_traffic_monitor() -> None:
    if os.environ.get("HETZNER_WEB_DISABLE_WORKERS", "").lower() in ("1", "true", "yes"):
        print("[info] background workers disabled by HETZNER_WEB_DISABLE_WORKERS")
        return
    try:
        persisted = _load_threshold_state()
        for sid, level in persisted.items():
            ALERT_STATE[str(sid)] = {
                "last_level": int(level),
                "last_outgoing": None,
                "auto_rebuild": False,
            }
    except Exception as e:
        print(f"[alert] threshold state load failed: {e}")
    def _backfill_wrapper() -> None:
        try:
            def backfill(state: Dict[str, Any]) -> None:
                if not state.get("rebuild_backfilled"):
                    _backfill_rebuild_stats(state)

            _update_report_state(backfill)
        except Exception as e:
            print(f"[alert] rebuild backfill error: {e}")

    threading.Thread(target=_monitor_traffic_loop, daemon=True).start()
    threading.Thread(target=_daily_report_loop, daemon=True).start()
    threading.Thread(target=_telegram_bot_loop, daemon=True).start()
    threading.Thread(target=_schedule_loop, daemon=True).start()
    threading.Thread(target=_snapshot_loop, daemon=True).start()
    threading.Thread(target=_backfill_wrapper, daemon=True).start()
    threading.Thread(target=_dns_sync_loop, daemon=True).start()
    try:
        config = _load_yaml(CONFIG_PATH)
        telegram_cfg = config.get("telegram", {})
        bot_token = telegram_cfg.get("bot_token", "")
        chat_id = telegram_cfg.get("chat_id", "")
        if telegram_cfg.get("enabled") and bot_token and chat_id:
            now = _now_local().strftime("%Y-%m-%d %H:%M:%S")
            levels = _parse_alert_levels(telegram_cfg.get("notify_levels"))
            notify_text = f"{', '.join(str(x) for x in levels)}%" if levels else "-"
            client = HetznerClient(config["hetzner"]["api_token"])
            servers = client.get_servers()
            server_count = len(servers)
            limit_gb = (config.get("traffic") or {}).get("limit_gb")
            limit_text = f"{limit_gb} GB" if limit_gb else "未设置"
            total_outbound_bytes = 0.0
            top_name = "-"
            top_percent = 0.0
            if limit_gb:
                limit_bytes = float(Decimal(limit_gb) * (Decimal(1024) ** 3))
            else:
                limit_bytes = 0.0
            for s in servers:
                detail = client.get_server(s["id"]) or {}
                outgoing = detail.get("outgoing_traffic")
                if outgoing is None:
                    continue
                total_outbound_bytes += float(outgoing)
                if limit_bytes > 0:
                    percent = (float(outgoing) / limit_bytes) * 100
                    if percent >= top_percent:
                        top_percent = percent
                        top_name = detail.get("name") or s.get("name") or str(s["id"])
            total_outbound_tb = _bytes_to_tb(total_outbound_bytes)
            _send_telegram_markdown(
                bot_token,
                chat_id,
                (
                    "✅ 监控已启动\n"
                    f"时间: {now}\n"
                    f"服务器: {server_count} 台\n"
                    f"阈值: {notify_text}\n"
                    f"流量上限: {limit_text}\n"
                    f"当前累计出站(总): {total_outbound_tb} TB\n"
                    f"最高使用率: {top_name} · {top_percent:.1f}%"
                ),
            )
    except Exception as e:
        print(f"[alert] startup notify error: {e}")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/demo")
def demo() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/servers")
def api_servers(request: Request) -> JSONResponse:
    _require_auth(request)
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    servers = client.get_servers()
    traffic_cfg = config.get("traffic", {})
    limit_gb = traffic_cfg.get("limit_gb")
    limit_tb = None
    if limit_gb:
        try:
            limit_tb = _quantize_tb(Decimal(limit_gb) / Decimal(1024))
        except Exception:
            limit_tb = None
    rows = []
    for s in servers:
        detail = client.get_server(s["id"]) or {}
        outgoing = detail.get("outgoing_traffic")
        ingoing = detail.get("ingoing_traffic")
        outbound_tb = _bytes_to_tb(float(outgoing)) if outgoing is not None else Decimal("0.000")
        inbound_tb = _bytes_to_tb(float(ingoing)) if ingoing is not None else Decimal("0.000")
        rows.append(
            {
                "id": s["id"],
                "name": s["name"],
                "status": s["status"],
                "ip": s["public_net"]["ipv4"]["ip"] if s["public_net"].get("ipv4") else None,
                "server_type": s["server_type"]["name"],
                "location": (
                    s.get("location")
                    or s.get("datacenter", {}).get("location")
                    or {}
                ).get("name", "Unknown"),
                "outbound_tb": str(outbound_tb),
                "inbound_tb": str(inbound_tb),
                "outbound_bytes": outgoing,
                "inbound_bytes": ingoing,
            }
        )
    state = _load_report_state()
    web_cfg = _load_json(WEB_CONFIG_PATH)
    hourly = _merge_hourly_series(state.get("hourly", {}))
    tracking = _compute_tracking_totals(hourly, web_cfg.get("tracking_start"))
    name_map = {str(s["id"]): s.get("name") or str(s["id"]) for s in servers}
    rebuilds = _detect_last_rebuilds(state.get("hourly", {}), name_map)
    rebuild_summary = _summarize_rebuild_stats(state)
    return JSONResponse(
        {
            "servers": rows,
            "updated_at": _now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "tracking": tracking,
            "traffic": {
                "limit_gb": limit_gb,
                "limit_tb": str(limit_tb) if limit_tb is not None else None,
                "cost_per_tb_eur": 1,
            },
            "rebuilds": rebuilds,
            "rebuild_summary": rebuild_summary,
            "missing_servers": _configured_missing_servers(config, servers),
            "manual_create_options": _manual_create_options(config),
        }
    )


@app.get("/api/qb")
def api_qb(request: Request) -> JSONResponse:
    _require_auth(request)
    config = _load_yaml(CONFIG_PATH)
    return JSONResponse(_collect_qbittorrent_stats(config))


@app.post("/api/rebuild")
async def api_rebuild(request: Request) -> JSONResponse:
    _require_auth(request)
    payload = await request.json()
    server_id = int(payload.get("server_id"))
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    detail = client.get_server(server_id) or {}
    name = detail.get("name") or str(server_id)
    result = _perform_rebuild(server_id, name, config, "Web API", client)
    if not result.get("success"):
        return JSONResponse(result, status_code=500)
    return JSONResponse({"rebuild": result, "dns": result.get("dns")})


@app.post("/api/create_missing")
async def api_create_missing(request: Request) -> JSONResponse:
    _require_auth(request)
    payload = await request.json()
    server_name = str(payload.get("name") or "").strip()
    server_type = str(payload.get("server_type") or "").strip() or None
    preferred_location = (
        str(payload.get("preferred_location") or "").strip() or None
    )
    raw_allow_fallback = payload.get("allow_fallback", True)
    if not isinstance(raw_allow_fallback, bool):
        return JSONResponse(
            {
                "success": False,
                "error": "allow_fallback 必须是布尔值",
                "error_code": "invalid_allow_fallback",
            },
            status_code=400,
        )
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    result = _perform_manual_create(
        server_name,
        config,
        client,
        server_type=server_type,
        preferred_location=preferred_location,
        allow_fallback=raw_allow_fallback,
    )
    if not result.get("success"):
        error_code = result.get("error_code")
        if error_code in {"already_exists", "manual_create_in_progress"}:
            status_code = 409
        elif error_code in {
            "manual_create_disabled",
            "name_not_allowed",
            "invalid_manual_create_config",
            "server_type_not_allowed",
            "location_not_allowed",
        }:
            status_code = 400
        else:
            status_code = 500
        return JSONResponse(result, status_code=status_code)
    return JSONResponse({"create": result, "dns": result.get("dns")})


@app.post("/api/dns_check")
async def api_dns_check(request: Request) -> JSONResponse:
    _require_auth(request)
    payload = await request.json()
    server_id = payload.get("server_id")
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    servers = client.get_servers()
    if server_id:
        servers = [s for s in servers if s["id"] == int(server_id)]
    cf_cfg = config.get("cloudflare", {})
    record_map = cf_cfg.get("record_map", {})
    results = []
    for s in servers:
        record = record_map.get(str(s["id"])) or record_map.get(s.get("name", ""))
        ip = s["public_net"]["ipv4"]["ip"] if s["public_net"].get("ipv4") else None
        if not record or not ip:
            results.append({"id": s["id"], "status": "missing"})
            continue
        try:
            socket.setdefaulttimeout(5)
            resolved = socket.gethostbyname(record)
            ok = resolved == ip
            results.append({"id": s["id"], "record": record, "resolved": resolved, "expected": ip, "ok": ok})
        except Exception as e:
            results.append({"id": s["id"], "record": record, "error": str(e)})
    return JSONResponse({"results": results})


@app.get("/api/hourly")
def api_hourly(request: Request, date: Optional[str] = None) -> JSONResponse:
    _require_auth(request)
    state = _load_report_state()
    hourly = state.get("hourly", {})
    config = _load_yaml(CONFIG_PATH)
    name_map = _active_server_name_map(config)
    include_ids = set(name_map.keys()) if name_map else None
    include_names = set(name_map.values()) if name_map else None
    keys = sorted(hourly.keys())
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format")
        selected_keys = [key for key in keys if key.startswith(date)]
        if not selected_keys:
            return JSONResponse({"servers": {}, "hours": []})
        prev_map = {keys[i]: keys[i - 1] for i in range(1, len(keys))}
        rows: Dict[str, Any] = {}
        for curr_key in selected_keys:
            prev_key = prev_map.get(curr_key)
            prev_raw = hourly.get(prev_key, {}) if prev_key else {}
            curr_raw = hourly.get(curr_key, {})
            prev = _filter_snapshot(prev_raw, include_ids, name_map, include_names)
            curr = _filter_snapshot(curr_raw, include_ids, name_map, include_names)
            deltas = _delta_by_name(prev, curr)
            for name in deltas:
                if name not in rows:
                    rows[name] = {"name": name, "deltas": []}
            for name, data in rows.items():
                delta = deltas.get(name, {})
                delta_tb = str(_quantize_tb(delta["out"])) if delta.get("has_out") else None
                delta_in_tb = str(_quantize_tb(delta["in"])) if delta.get("has_in") else None
                data["deltas"].append({"hour": curr_key, "tb": delta_tb, "in_tb": delta_in_tb})
        return JSONResponse({"servers": rows, "hours": selected_keys})

    keys = keys[-25:]
    rows: Dict[str, Any] = {}
    for i in range(1, len(keys)):
        prev_key = keys[i - 1]
        curr_key = keys[i]
        prev_raw = hourly.get(prev_key, {})
        curr_raw = hourly.get(curr_key, {})
        prev = _filter_snapshot(prev_raw, include_ids, name_map, include_names)
        curr = _filter_snapshot(curr_raw, include_ids, name_map, include_names)
        deltas = _delta_by_name(prev, curr)
        for name in deltas:
            if name not in rows:
                rows[name] = {"name": name, "deltas": []}
        for name, data in rows.items():
            delta = deltas.get(name, {})
            delta_tb = str(_quantize_tb(delta["out"])) if delta.get("has_out") else None
            delta_in_tb = str(_quantize_tb(delta["in"])) if delta.get("has_in") else None
            data["deltas"].append({"hour": curr_key, "tb": delta_tb, "in_tb": delta_in_tb})
    return JSONResponse({"servers": rows, "hours": keys[1:]})


@app.get("/api/daily")
def api_daily(request: Request) -> JSONResponse:
    _require_auth(request)
    state = _load_report_state()
    hourly = state.get("hourly", {})
    config = _load_yaml(CONFIG_PATH)
    name_map = _active_server_name_map(config)
    include_ids = set(name_map.keys()) if name_map else None
    include_names = set(name_map.values()) if name_map else None
    keys = sorted(hourly.keys())
    if len(keys) < 2:
        return JSONResponse({"days": [], "peak": "0.000", "total": "0.000", "servers": []})

    daily_totals: Dict[str, Decimal] = {}
    daily_in_totals: Dict[str, Decimal] = {}
    per_server: Dict[str, Dict[str, Decimal]] = {}
    per_server_in: Dict[str, Dict[str, Decimal]] = {}
    for i in range(1, len(keys)):
        prev_key = keys[i - 1]
        curr_key = keys[i]
        date_key = _date_from_hour_key(curr_key)
        if not date_key:
            continue
        prev_raw = hourly.get(prev_key, {})
        curr_raw = hourly.get(curr_key, {})
        prev = _filter_snapshot(prev_raw, include_ids, name_map, include_names)
        curr = _filter_snapshot(curr_raw, include_ids, name_map, include_names)
        deltas = _delta_by_name(prev, curr)
        for name, data in deltas.items():
            if data.get("has_out"):
                delta_tb = data["out"]
                daily_totals[date_key] = daily_totals.get(date_key, Decimal("0.000")) + delta_tb
                if name not in per_server:
                    per_server[name] = {}
                per_server[name][date_key] = per_server[name].get(date_key, Decimal("0.000")) + delta_tb
            if data.get("has_in"):
                delta_in_tb = data["in"]
                daily_in_totals[date_key] = daily_in_totals.get(date_key, Decimal("0.000")) + delta_in_tb
                if name not in per_server_in:
                    per_server_in[name] = {}
                per_server_in[name][date_key] = per_server_in[name].get(date_key, Decimal("0.000")) + delta_in_tb

    day_keys = sorted(daily_totals.keys())
    day_keys = day_keys[-35:]
    days = []
    for date_key in day_keys:
        total = _quantize_tb(daily_totals[date_key])
        inbound_total = _quantize_tb(daily_in_totals.get(date_key, Decimal("0.000")))
        days.append({"date": date_key, "outbound_tb": str(total), "inbound_tb": str(inbound_total)})

    peak = _quantize_tb(max((Decimal(d["outbound_tb"]) for d in days), default=Decimal("0.000")))
    total = _quantize_tb(sum((Decimal(d["outbound_tb"]) for d in days), Decimal("0.000")))
    in_peak = _quantize_tb(max((Decimal(d["inbound_tb"]) for d in days), default=Decimal("0.000")))
    in_total = _quantize_tb(sum((Decimal(d["inbound_tb"]) for d in days), Decimal("0.000")))
    servers = []
    for name in sorted(per_server.keys()):
        rows = []
        for date_key in day_keys:
            value = _quantize_tb(per_server[name].get(date_key, Decimal("0.000")))
            in_value = _quantize_tb(per_server_in.get(name, {}).get(date_key, Decimal("0.000")))
            rows.append({"date": date_key, "outbound_tb": str(value), "inbound_tb": str(in_value)})
        servers.append({"id": name, "name": name, "days": rows})
    return JSONResponse(
        {
            "days": days,
            "peak": str(peak),
            "total": str(total),
            "in_peak": str(in_peak),
            "in_total": str(in_total),
            "servers": servers,
        }
    )


@app.get("/api/cycle")
def api_cycle(request: Request) -> JSONResponse:
    _require_auth(request)
    state = _load_report_state()
    hourly = state.get("hourly", {})
    config = _load_yaml(CONFIG_PATH)
    client = HetznerClient(config["hetzner"]["api_token"])
    servers = client.get_servers()
    include_ids = {str(s["id"]) for s in servers}
    name_map = {str(s["id"]): s.get("name") or str(s["id"]) for s in servers}
    return JSONResponse(_compute_cycle_data(hourly, include_ids=include_ids, name_map=name_map))
