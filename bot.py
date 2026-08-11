#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LDMG - Lite Docker Manager Gram
基于 TG Bot 的 Docker Compose 升级助手 (全量修复与美化增强版)
"""

import os
import json
import time
import html
import re
import uuid
import asyncio
import subprocess
import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from typing import List, Dict, Optional, Any

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# 加载 .env 环境变量
load_dotenv()

# ==================== 配置 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}

COMMAND_TIMEOUT = 300  # Docker 单个命令超时时间（秒）

# 主面板每页项目数，可通过环境变量 PAGE_SIZE 覆盖，默认 6
try:
    PAGE_SIZE = int(os.getenv("PAGE_SIZE", "6"))
except ValueError:
    PAGE_SIZE = 6
if PAGE_SIZE < 1:
    PAGE_SIZE = 6

PROJECTS_CACHE_TTL = 15  # 项目扫描结果缓存秒数

# 全局任务状态管理
_EXEC_LOCK: Optional[asyncio.Lock] = None
_CURRENT_PROCESS: Optional[asyncio.subprocess.Process] = None
_CANCEL_REQUESTED: bool = False
_CURRENT_TASK: Optional[str] = None
_COMPOSE_BIN: Optional[List[str]] = None
_PROJECTS_CACHE: List[Dict] = []
_PROJECTS_CACHE_TIME: float = 0.0

def get_exec_lock() -> asyncio.Lock:
    """获取并发互斥锁"""
    global _EXEC_LOCK
    if _EXEC_LOCK is None:
        _EXEC_LOCK = asyncio.Lock()
    return _EXEC_LOCK


async def begin_task(task_id: str) -> bool:
    """原子地获取执行锁并登记当前任务；拿不到锁返回 False。

    用近零超时把「检查 + 获取」合并为一步，避免快速连点时
    lock.locked() 检查与 async with 之间出现竞态。
    """
    global _CURRENT_TASK, _CANCEL_REQUESTED
    lock = get_exec_lock()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=0.05)
    except asyncio.TimeoutError:
        return False
    _CURRENT_TASK = task_id
    _CANCEL_REQUESTED = False
    return True


def end_task() -> None:
    """释放执行锁并清理任务状态（必须在任务的 finally 中调用）。"""
    global _CURRENT_TASK, _CANCEL_REQUESTED
    _CURRENT_TASK = None
    _CANCEL_REQUESTED = False
    try:
        get_exec_lock().release()
    except RuntimeError:
        pass


# ==================== Callback Data 内存映射系统 ====================
_CALLBACK_STORE: Dict[str, Dict[str, Any]] = {}

def create_cb_data(action: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """生成短 ID 并存储真实载荷，规避 Telegram 限制"""
    if payload is None:
        payload = {}
    short_id = uuid.uuid4().hex[:8]
    key = f"{action}:{short_id}"
    _CALLBACK_STORE[key] = payload
    
    # 自动清理过期的映射，防止内存膨胀
    if len(_CALLBACK_STORE) > 800:
        for k in list(_CALLBACK_STORE.keys())[:200]:
            _CALLBACK_STORE.pop(k, None)
            
    return key

def get_cb_payload(key: str) -> Optional[Dict[str, Any]]:
    """根据 short key 获取回调数据"""
    return _CALLBACK_STORE.get(key)


# ==================== 日志与安全脱敏配置 ====================
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

class TokenMaskFilter(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.token:
            return True
        if isinstance(record.msg, str) and self.token in record.msg:
            record.msg = record.msg.replace(self.token, "[REDACTED_BOT_TOKEN]")
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    arg.replace(self.token, "[REDACTED_BOT_TOKEN]") if isinstance(arg, str) else arg 
                    for arg in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (v.replace(self.token, "[REDACTED_BOT_TOKEN]") if isinstance(v, str) else v) 
                    for k, v in record.args.items()
                }
        if record.exc_text and self.token in record.exc_text:
            record.exc_text = record.exc_text.replace(self.token, "[REDACTED_BOT_TOKEN]")
        return True

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

logging.basicConfig(
    format=LOG_FORMAT,
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _setup_file_logging() -> None:
    """滚动文件日志（操作审计回溯用），初始化失败不影响主流程。"""
    try:
        log_dir = os.getenv("LOG_DIR", "logs")
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "ldmg.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logging.getLogger().addHandler(file_handler)
    except Exception as e:
        logger.warning(f"日志文件初始化失败（不影响运行）: {e}")


_setup_file_logging()

if BOT_TOKEN:
    mask_filter = TokenMaskFilter(BOT_TOKEN)
    for handler in logging.getLogger().handlers:
        handler.addFilter(mask_filter)


# ==================== 权限与辅助函数 ====================
def is_allowed(user_id: int) -> bool:
    return bool(ALLOWED_USER_IDS) and user_id in ALLOWED_USER_IDS

async def check_permission(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        if update.message:
            await update.message.reply_text("❌ 无权限操作")
        elif update.callback_query:
            await update.callback_query.answer("❌ 无权限", show_alert=True)
        return False
    return True

def get_user_identifier(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Unknown User"
    username = f"@{user.username}" if user.username else user.first_name
    return f"{user.id} ({username})"

def render_progress_bar(percentage: int, width: int = 8) -> str:
    """生成自定义进度条图标"""
    filled = int(round(width * percentage / 100))
    return "▰" * filled + "▱" * (width - filled)

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import NetworkError, TimedOut, BadRequest
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning(f"🌐 遇到临时网络波动: {context.error}")
        return
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return
    logger.error("❌ 未捕获的系统异常:", exc_info=True)


# ==================== Docker Compose 扫描与状态 ====================
def get_compose_bin() -> List[str]:
    """探测可用的 compose 命令：优先 docker compose（v2 插件），回退 docker-compose（v1/v2 独立版）。"""
    global _COMPOSE_BIN
    if _COMPOSE_BIN is not None:
        return _COMPOSE_BIN

    for cand in (["docker", "compose"], ["docker-compose"]):
        try:
            probe = subprocess.run(
                [*cand, "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if probe.returncode == 0:
                _COMPOSE_BIN = cand
                logger.info(f"使用 compose 命令: {' '.join(cand)}")
                return cand
        except Exception:
            continue

    _COMPOSE_BIN = []
    logger.warning("未检测到 docker compose / docker-compose 命令")
    return []


def build_compose_cmd(project: Dict, *args: str) -> List[str]:
    """按项目生成 compose 命令（携带完整 -f 文件列表，兼容多 compose 文件项目）。"""
    compose_bin = get_compose_bin()
    if not compose_bin:
        raise RuntimeError("未检测到 docker compose / docker-compose 命令")
    cmd = list(compose_bin)
    for cf in project.get("config_files") or []:
        cmd += ["-f", cf]
    cmd += list(args)
    return cmd


def get_project_services(work_dir: str, config_files: List[str]) -> List[str]:
    """获取项目的服务定义"""
    compose_bin = get_compose_bin()
    if not compose_bin:
        return []
    try:
        cmd = list(compose_bin)
        for cf in config_files:
            cmd += ["-f", cf]
        cmd += ["config", "--services"]
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]
    except Exception as e:
        logger.warning(f"获取 {work_dir} 服务列表失败: {e}")
    return []


def _get_compose_projects_sync() -> List[Dict]:
    projects = []
    seen_keys = set()
    compose_bin = get_compose_bin()
    if not compose_bin:
        return projects

    try:
        result = subprocess.run(
            [*compose_bin, "ls", "-a", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            try:
                data = json.loads(result.stdout)
                if isinstance(data, dict):
                    data = [data]
            except json.JSONDecodeError:
                data = []
                for line in result.stdout.strip().splitlines():
                    if line.strip():
                        try:
                            data.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass

            for item in data:
                name = item.get("Name", "")
                status = item.get("Status", "")
                config_files = [
                    c.strip()
                    for c in (item.get("ConfigFiles", "") or "").split(",")
                    if c.strip()
                ]
                if not name:
                    continue
                first_file = config_files[0] if config_files else ""
                work_dir = os.path.dirname(first_file) if first_file else ""

                unique_key = f"{name}:{work_dir}"
                if work_dir and os.path.isdir(work_dir) and unique_key not in seen_keys:
                    seen_keys.add(unique_key)
                    services = get_project_services(work_dir, config_files)
                    projects.append({
                        "name": name,
                        "dir": work_dir,
                        "status": status,
                        "services": services,
                        "config_files": config_files,
                    })
    except Exception as e:
        logger.warning(f"docker compose ls 扫描失败: {e}")

    projects.sort(key=lambda x: x["name"])
    return projects


def invalidate_projects_cache() -> None:
    """使项目扫描缓存立即过期（升级/清理操作后调用）。"""
    global _PROJECTS_CACHE_TIME
    _PROJECTS_CACHE_TIME = 0.0


async def get_compose_projects(force_refresh: bool = False) -> List[Dict]:
    """带 TTL 的项目列表，避免每次点击按钮都全量扫描。"""
    global _PROJECTS_CACHE, _PROJECTS_CACHE_TIME
    now = time.monotonic()
    if (
        not force_refresh
        and _PROJECTS_CACHE
        and (now - _PROJECTS_CACHE_TIME) < PROJECTS_CACHE_TTL
    ):
        return _PROJECTS_CACHE
    projects = await asyncio.to_thread(_get_compose_projects_sync)
    _PROJECTS_CACHE = projects
    _PROJECTS_CACHE_TIME = now
    return projects


# ==================== 可视化进度条 & 日志执行引擎 ====================
async def edit_html_safe(message, html_text: str, fallback: Optional[str] = None) -> None:
    """先按 HTML 编辑，失败则降级为纯文本，避免长输出截断导致消息卡死。"""
    if fallback is None:
        fallback = re.sub(r"<[^>]+>", "", html_text)
    try:
        await message.edit_text(html_text, parse_mode="HTML")
    except Exception:
        try:
            await message.edit_text(fallback)
        except Exception:
            pass


async def run_command_with_feedback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    cmd: List[str],
    cwd: Optional[str] = None,
    title: str = "执行中",
    progress_pct: int = 50,
    task_id: Optional[str] = None,
    delete_on_success: bool = False,
) -> bool:
    global _CURRENT_PROCESS
    message = update.effective_message

    start_time = time.time()
    safe_title = html.escape(title)
    safe_cmd = html.escape(" ".join(cmd))

    cancel_cb = create_cb_data("task_cancel", {"task_id": task_id} if task_id else {})
    cancel_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 中断执行", callback_data=cancel_cb)]])

    p_bar = render_progress_bar(progress_pct)
    status_msg = await message.reply_text(
        f"⚙️ <b>{safe_title}</b> [{p_bar}]\n"
        f"⏱ <b>已用时：</b>0.0s\n"
        f"<code>{safe_cmd}</code>",
        reply_markup=cancel_markup,
        parse_mode="HTML",
    )

    output_lines = deque(maxlen=100)
    process: Optional[asyncio.subprocess.Process] = None

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _CURRENT_PROCESS = process

        last_update_time = time.time()

        async def read_stream() -> None:
            nonlocal last_update_time
            partial = ""
            while True:
                if _CANCEL_REQUESTED:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    break

                chunk = await process.stdout.read(4096)
                if not chunk:
                    break

                partial += chunk.decode("utf-8", errors="replace")

                while "\n" in partial:
                    line, partial = partial.split("\n", 1)
                    line = line.rstrip("\r")
                    if line.strip():
                        output_lines.append(line)

                # 进度模式用 \r 原地刷新同一行，只保留最后一次进度
                if "\r" in partial:
                    partial = partial.rsplit("\r", 1)[-1]

                now = time.time()
                if now - last_update_time >= 1.2:
                    elapsed = round(now - start_time, 1)
                    dyn_pct = min(progress_pct, max(5, 5 + int(elapsed * (progress_pct - 5) / 45)))
                    p_bar_cur = render_progress_bar(dyn_pct)
                    preview_lines = list(output_lines)[-11:]
                    if partial.strip():
                        preview_lines.append(partial.strip())
                    preview = "\n".join(preview_lines)
                    safe_preview = html.escape(preview[-3500:])
                    try:
                        await status_msg.edit_text(
                            f"⚙️ <b>{safe_title}</b> [{p_bar_cur}]\n"
                            f"⏱ <b>已用时：</b>{elapsed}s\n"
                            f"<code>{safe_preview}</code>",
                            reply_markup=cancel_markup,
                            parse_mode="HTML",
                        )
                        last_update_time = now
                    except Exception:
                        pass

        await asyncio.wait_for(read_stream(), timeout=COMMAND_TIMEOUT)
        returncode = await process.wait()

        elapsed = round(time.time() - start_time, 1)
        full_output = "\n".join(list(output_lines)[-25:])
        safe_full_output = html.escape(full_output[-3500:])

        if _CANCEL_REQUESTED:
            await edit_html_safe(
                status_msg,
                f"🛑 <b>{safe_title} 已取消</b>\n"
                f"⏱ <b>已用时：</b>{elapsed}s\n"
                f"<code>{safe_full_output}</code>",
            )
            return False

        if returncode == 0:
            if delete_on_success:
                try:
                    await status_msg.delete()
                except Exception:
                    await edit_html_safe(
                        status_msg,
                        f"✅ <b>{safe_title} 完成</b>\n"
                        f"⏱ <b>总耗时：</b>{elapsed}s",
                    )
                return True
            p_done = render_progress_bar(100)
            await edit_html_safe(
                status_msg,
                f"✅ <b>{safe_title} 完成</b> [{p_done}]\n"
                f"⏱ <b>总耗时：</b>{elapsed}s\n"
                f"<code>{safe_full_output}</code>",
            )
            return True
        else:
            await edit_html_safe(
                status_msg,
                f"❌ <b>{safe_title} 失败 (Code {returncode})</b>\n"
                f"⏱ <b>耗时：</b>{elapsed}s\n"
                f"<code>{safe_full_output}</code>",
            )
            return False

    except asyncio.TimeoutError:
        if process:
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
        await edit_html_safe(
            status_msg,
            f"⏰ <b>{safe_title} 超时中断</b>\n"
            f"单条指令耗时超过 {COMMAND_TIMEOUT} 秒，已强行终止。",
        )
        return False

    except Exception as e:
        if process:
            try:
                process.kill()
            except Exception:
                pass
        await edit_html_safe(status_msg, f"❌ 执行发生异常: {html.escape(str(e))}")
        return False
    finally:
        _CURRENT_PROCESS = None


# ==================== 核心面板与分页菜单 ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permission(update):
        return
    await cmd_list(update, context)

async def cmd_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 1,
    force_refresh: bool = False,
):
    """主面板列表（支持分页、统计与分组）"""
    if not await check_permission(update):
        return

    user_str = get_user_identifier(update)
    logger.info(f"▶️ [操作审计] 用户 [{user_str}] 查看项目列表 (页码: {page})")

    projects = await get_compose_projects(force_refresh=force_refresh)
    total_projects = len(projects)
    running_cnt = sum(1 for p in projects if "running" in p["status"].lower())

    # 运行中项目排前，组内按名称排序，主面板按状态分组展示
    projects = sorted(
        projects,
        key=lambda p: (0 if "running" in p["status"].lower() else 1, p["name"].lower()),
    )

    total_pages = max(1, (total_projects + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * PAGE_SIZE
    page_projects = projects[start_idx:start_idx + PAGE_SIZE]

    text = "🐳 <b>Docker Compose 管理面板 - TGBOT版</b>\n"
    text += f"📊 <b>统计：</b>共 {total_projects} 个项目 | 🟢 {running_cnt} 运行中 | 🟡 {total_projects - running_cnt} 停止\n"
    text += f"📖 <b>页码：</b>{page} / {total_pages}\n"
    text += "\n"

    keyboard = []

    if not projects:
        text += "⚠️ 暂未检测到任何 Docker Compose 项目"
    else:
        last_group = None
        for i, p in enumerate(page_projects, start=start_idx + 1):
            is_running = "running" in p["status"].lower()
            group = "running" if is_running else "stopped"
            if group != last_group:
                text += "🟢 <b>运行中</b>\n" if group == "running" else "🟡 <b>已停止</b>\n"
                last_group = group

            num = f"{i:02d}"
            name = p["name"]
            status = p["status"]
            status_icon = "🟢" if is_running else "🟡"

            disp_name = name[:26] + ".." if len(name) > 28 else name
            safe_name = html.escape(name)
            safe_dir = html.escape(p["dir"])
            services_str = ", ".join(p["services"]) if p["services"] else "-"
            safe_services = html.escape(services_str)

            text += f"<b>{num}.</b> {safe_name} {status_icon} <code>[{html.escape(status)}]</code>\n"
            text += f"     路径：<code>{safe_dir}</code>\n"
            text += f"     容器({len(p['services'])}): {safe_services}\n\n"

            if len(p["services"]) > 1:
                cb_data = create_cb_data("p_sel", {"name": name, "page": page})
                keyboard.append([InlineKeyboardButton(f"⚙️ {num}. {disp_name} (多服务)", callback_data=cb_data)])
            else:
                cb_data = create_cb_data("up_s_ask", {"name": name, "page": page})
                keyboard.append([InlineKeyboardButton(f"🚀 {num}. {disp_name}", callback_data=cb_data)])

    page_nav = []
    if page > 1:
        cb_prev = create_cb_data("page_turn", {"page": page - 1})
        page_nav.append(InlineKeyboardButton("◀ 上一页", callback_data=cb_prev))
    page_nav.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        cb_next = create_cb_data("page_turn", {"page": page + 1})
        page_nav.append(InlineKeyboardButton("下一页 ▶", callback_data=cb_next))
    
    keyboard.append(page_nav)

    cb_prune_menu = create_cb_data("prune_menu")
    keyboard.append([
        InlineKeyboardButton("🧹 镜像清理菜单", callback_data=cb_prune_menu),
        InlineKeyboardButton("⬆️ 升级全部项目", callback_data="upgrade_all")
    ])
    keyboard.append([
        InlineKeyboardButton("🔄 刷新状态", callback_data=create_cb_data("page_turn", {"page": page, "refresh": True}))
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
        await update.callback_query.answer()
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def show_project_detail(update: Update, project_name: str, back_page: int = 1):
    query = update.callback_query
    projects = await get_compose_projects()
    target_p = next((p for p in projects if p["name"] == project_name), None)

    if not target_p:
        await query.answer("❌ 未找到该项目", show_alert=True)
        return

    safe_name = html.escape(target_p['name'])
    safe_dir = html.escape(target_p['dir'])

    text = f"📦 <b>项目卡片：{safe_name}</b>\n\n"
    text += f"📂 <b>路径：</b><code>{safe_dir}</code>\n"
    status_icon = "🟢" if "running" in target_p["status"].lower() else "🟡"
    text += f"{status_icon} <b>状态：</b>{html.escape(target_p['status'])}\n\n"
    text += "⚙️ <b>请选择操作控制范围：</b>\n"

    cb_all = create_cb_data("up_s_ask", {"name": project_name, "page": back_page})
    keyboard = [[InlineKeyboardButton("⚡ 升级全部服务容器", callback_data=cb_all)]]

    if target_p["services"]:
        for svc in target_p["services"]:
            cb_svc = create_cb_data("up_svc_ask", {"name": project_name, "svc": svc, "page": back_page})
            disp_svc = svc[:24] + ".." if len(svc) > 26 else svc
            keyboard.append([InlineKeyboardButton(f"🔹 仅升级服务: {disp_svc}", callback_data=cb_svc)])

    cb_back = create_cb_data("page_turn", {"page": back_page})
    keyboard.append([InlineKeyboardButton("🔙 返回列表", callback_data=cb_back)])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    await query.answer()


# ==================== 确认弹窗函数 ====================
async def ask_single_upgrade(update: Update, project_name: str, back_page: int = 1):
    query = update.callback_query
    projects = await get_compose_projects()
    target_p = next((p for p in projects if p["name"] == project_name), None)

    safe_name = html.escape(project_name)
    safe_dir = html.escape(target_p["dir"]) if target_p else "未知路径"

    cb_confirm = create_cb_data("up_p_do", {"name": project_name})
    cb_back = create_cb_data("page_turn", {"page": back_page})
    
    keyboard = [
        [
            InlineKeyboardButton("✅ 确认升级", callback_data=cb_confirm),
            InlineKeyboardButton("🔙 取消返回", callback_data=cb_back),
        ]
    ]

    text = (
        f"🚀 <b>升级确认 - [{safe_name}]</b>\n\n"
        f"📂 <b>工作目录：</b><code>{safe_dir}</code>\n"
        f"🛠 <b>执行步骤：</b>\n"
        f"  1. <code>{' '.join(get_compose_bin()) + ' pull'}</code>\n"
        f"  2. <code>{' '.join(get_compose_bin()) + ' up -d'}</code>\n"
    )

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    await query.answer()


async def ask_svc_upgrade(update: Update, project_name: str, service_name: str, back_page: int = 1):
    query = update.callback_query
    projects = await get_compose_projects()
    target_p = next((p for p in projects if p["name"] == project_name), None)

    safe_p = html.escape(project_name)
    safe_s = html.escape(service_name)
    safe_dir = html.escape(target_p["dir"]) if target_p else "未知路径"

    cb_confirm = create_cb_data("up_svc_do", {"name": project_name, "svc": service_name})
    cb_back = create_cb_data("p_sel", {"name": project_name, "page": back_page})

    keyboard = [
        [
            InlineKeyboardButton("✅ 确认升级单一服务", callback_data=cb_confirm),
            InlineKeyboardButton("🔙 返回", callback_data=cb_back),
        ]
    ]

    text = (
        f"🚀 <b>服务升级确认 - [{safe_s}]</b>\n\n"
        f"📦 <b>所属项目：</b>{safe_p}\n"
        f"📂 <b>工作路径：</b><code>{safe_dir}</code>\n\n"
        f"💡 仅重建并升级 <code>{safe_s}</code>，项目内其他容器不受影响。"
    )

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    await query.answer()


# ==================== 执行逻辑底座 ====================
async def do_upgrade_project(update: Update, context: ContextTypes.DEFAULT_TYPE, project_name: str):
    query = update.callback_query
    user_str = get_user_identifier(update)
    task_id = uuid.uuid4().hex

    if not await begin_task(task_id):
        if query: await query.answer("⚠️ 当前已有正在运行的部署/清理任务", show_alert=True)
        return

    if query: await query.answer()

    try:
        projects = await get_compose_projects()
        target_p = next((p for p in projects if p["name"] == project_name), None)

        if not target_p:
            await update.effective_message.reply_text(f"❌ 找不到对应项目: <code>{html.escape(project_name)}</code>", parse_mode="HTML")
            return

        if not get_compose_bin():
            await update.effective_message.reply_text("❌ 未检测到 docker compose / docker-compose 命令", parse_mode="HTML")
            return

        message = update.effective_message
        safe_name = html.escape(target_p["name"])
        logger.info(f"🚀 [操作审计] 用户 [{user_str}] 升级完整项目 [{project_name}]")

        await message.reply_text(f"🚀 <b>开始升级项目 [{safe_name}]</b>", parse_mode="HTML")

        # 阶段 1：Pull
        pull_ok = await run_command_with_feedback(
            update, context,
            build_compose_cmd(target_p, "pull"),
            cwd=target_p["dir"],
            title=f"拉取新镜像 - {target_p['name']}",
            progress_pct=30,
            task_id=task_id,
            delete_on_success=True,
        )
        if not pull_ok:
            return

        # 阶段 2：Up
        up_ok = await run_command_with_feedback(
            update, context,
            build_compose_cmd(target_p, "up", "-d"),
            cwd=target_p["dir"],
            title=f"重建与启动 - {target_p['name']}",
            progress_pct=80,
            task_id=task_id,
        )

        if up_ok:
            logger.info(f"✅ 项目 [{project_name}] 升级完成")
            await message.reply_text(f"🎉 项目 <b>{safe_name}</b> 整体升级成功！", parse_mode="HTML")
    finally:
        invalidate_projects_cache()
        end_task()


async def do_upgrade_service(update: Update, context: ContextTypes.DEFAULT_TYPE, project_name: str, service_name: str):
    query = update.callback_query
    user_str = get_user_identifier(update)
    task_id = uuid.uuid4().hex

    if not await begin_task(task_id):
        if query: await query.answer("⚠️ 当前已有正在运行的任务", show_alert=True)
        return

    if query: await query.answer()

    try:
        projects = await get_compose_projects()
        target_p = next((p for p in projects if p["name"] == project_name), None)

        if not target_p:
            await update.effective_message.reply_text(f"❌ 未找到项目: <code>{html.escape(project_name)}</code>", parse_mode="HTML")
            return

        if not get_compose_bin():
            await update.effective_message.reply_text("❌ 未检测到 docker compose / docker-compose 命令", parse_mode="HTML")
            return

        message = update.effective_message
        safe_name = html.escape(target_p["name"])
        safe_svc = html.escape(service_name)

        logger.info(f"🚀 [操作审计] 用户 [{user_str}] 升级单服务 [{project_name} -> {service_name}]")

        await message.reply_text(f"🚀 <b>升级服务 [{safe_svc}] ({safe_name})</b>", parse_mode="HTML")

        pull_ok = await run_command_with_feedback(
            update, context,
            build_compose_cmd(target_p, "pull", service_name),
            cwd=target_p["dir"],
            title=f"拉取服务镜像 - {service_name}",
            progress_pct=30,
            task_id=task_id,
            delete_on_success=True,
        )
        if not pull_ok:
            return

        up_ok = await run_command_with_feedback(
            update, context,
            build_compose_cmd(target_p, "up", "-d", service_name),
            cwd=target_p["dir"],
            title=f"重建启动服务 - {service_name}",
            progress_pct=80,
            task_id=task_id,
        )

        if up_ok:
            logger.info(f"✅ 服务 [{service_name}] 升级完成")
            await message.reply_text(f"🎉 服务 <code>{safe_svc}</code> 升级成功！", parse_mode="HTML")
    finally:
        invalidate_projects_cache()
        end_task()


async def do_upgrade_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_str = get_user_identifier(update)
    task_id = uuid.uuid4().hex

    if not await begin_task(task_id):
        if query: await query.answer("⚠️ 当前已有其他任务在运行", show_alert=True)
        return

    if query: await query.answer()

    try:
        projects = await get_compose_projects()
        if not projects:
            await update.effective_message.reply_text("未检测到可升级的项目")
            return

        if not get_compose_bin():
            await update.effective_message.reply_text("❌ 未检测到 docker compose / docker-compose 命令", parse_mode="HTML")
            return

        logger.info(f"🚀 [操作审计] 用户 [{user_str}] 触发批量升级共 {len(projects)} 个项目")
        message = update.effective_message
        await message.reply_text(f"🚀 <b>开始批量升级全部 {len(projects)} 个项目...</b>", parse_mode="HTML")

        success_list = []
        fail_list = []
        aborted = False

        for i, p in enumerate(projects, 1):
            if _CANCEL_REQUESTED:
                aborted = True
                break

            p_name = p['name']
            pct = int((i / len(projects)) * 100)

            pull_ok = await run_command_with_feedback(
                update, context,
                build_compose_cmd(p, "pull"),
                cwd=p["dir"],
                title=f"批量拉取 - {p_name}",
                progress_pct=pct,
                task_id=task_id,
                delete_on_success=True,
            )

            up_ok = False
            if pull_ok:
                up_ok = await run_command_with_feedback(
                    update, context,
                    build_compose_cmd(p, "up", "-d"),
                    cwd=p["dir"],
                    title=f"批量启动 - {p_name}",
                    progress_pct=pct,
                    task_id=task_id,
                )

            if pull_ok and up_ok:
                success_list.append(p_name)
            else:
                fail_list.append(p_name)

        if aborted:
            remaining = len(projects) - i + 1
            summary = f"🛑 <b>批量升级已中止</b>（剩余 {remaining} 个项目未处理）\n"
        else:
            summary = "🏁 <b>批量升级任务完成</b>\n"
        summary += "───────────────────────────\n"
        summary += f"✅ <b>成功 ({len(success_list)})：</b> {html.escape(', '.join(success_list)) if success_list else '无'}\n"
        summary += f"❌ <b>失败 ({len(fail_list)})：</b> {html.escape(', '.join(fail_list)) if fail_list else '无'}"
        await message.reply_text(summary, parse_mode="HTML")
    finally:
        invalidate_projects_cache()
        end_task()


# ==================== 镜像清理与分类处理 ====================
async def show_prune_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query: await query.answer()

    text = "🧹 <b>Docker 镜像清理中心</b>\n\n"
    text += "请选择清理类型：\n"
    text += "• <b>悬空镜像 (Dangling)</b>：无标签且未被使用的临时镜像层（安全推荐）\n"
    text += "• <b>所有未使用镜像 (All Unused)</b>：没有任何容器正在使用的全部旧镜像（深度清理）"

    cb_dangling = create_cb_data("prune_req", {"all": False})
    cb_all = create_cb_data("prune_req", {"all": True})

    keyboard = [
        [InlineKeyboardButton("🍂 仅清理悬空镜像 (Dangling)", callback_data=cb_dangling)],
        [InlineKeyboardButton("🗑 清理所有未使用镜像 (All Unused)", callback_data=cb_all)],
        [InlineKeyboardButton("🔙 返回主菜单", callback_data=create_cb_data("page_turn", {"page": 1}))]
    ]

    message = update.effective_message
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def cmd_prune(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permission(update): return
    await show_prune_menu(update, context)


async def ask_prune_confirm(update: Update, prune_all: bool):
    query = update.callback_query
    message = update.effective_message

    await query.answer()
    label = "所有未使用" if prune_all else "悬空 (dangling)"
    await query.edit_message_text(f"🔍 正在扫描系统中的 <b>{label}</b> 镜像...", parse_mode="HTML")

    cmd = ["docker", "images"]
    if not prune_all:
        cmd.extend(["-f", "dangling=true"])
    cmd.extend(["--format", "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        
        if proc.returncode != 0:
            await message.reply_text(f"❌ 扫描镜像异常: <code>{html.escape(stdout.decode())}</code>", parse_mode="HTML")
            return

        dry_output = stdout.decode('utf-8', errors='replace').strip()

        if not dry_output or len(dry_output.splitlines()) <= 1:
            await message.reply_text(f"✨ 系统内未检测到可清理的 <b>{label}</b> 镜像！", parse_mode="HTML")
            return
    except Exception as e:
        await message.reply_text(f"❌ 执行扫描出错: {html.escape(str(e))}", parse_mode="HTML")
        return

    cb_confirm = create_cb_data("prune_do", {"all": prune_all})
    cb_cancel = create_cb_data("page_turn", {"page": 1})

    keyboard = [
        [
            InlineKeyboardButton("✅ 确认清理", callback_data=cb_confirm),
            InlineKeyboardButton("❌ 取消", callback_data=cb_cancel),
        ]
    ]
    
    safe_dry = html.escape(dry_output[-3000:])
    await message.reply_text(
        f"🧹 <b>待清理镜像预判 (范围: {label})：</b>\n<code>{safe_dry}</code>\n\n确认执行清理吗？",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def do_prune(update: Update, context: ContextTypes.DEFAULT_TYPE, prune_all: bool):
    query = update.callback_query
    task_id = uuid.uuid4().hex

    if not await begin_task(task_id):
        await query.answer("⚠️ 当前已有任务在运行", show_alert=True)
        return
    await query.answer()

    try:
        await query.edit_message_text("🗑 正在执行镜像清理，请稍候...")

        cmd = ["docker", "image", "prune", "-f"]
        if prune_all:
            cmd.append("-a")

        await run_command_with_feedback(
            update, context, cmd,
            title="清理系统镜像",
            progress_pct=90,
            task_id=task_id,
        )
    finally:
        end_task()


# ==================== 状态速览 (/status) ====================
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permission(update): return

    message = update.effective_message
    status_msg = await message.reply_text("🔍 正在拉取 Docker 容器状态速览...")

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a",
            "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode('utf-8', errors='replace').strip()

        if proc.returncode != 0:
            await status_msg.edit_text(
                f"❌ 获取 Docker 状态失败: <code>{html.escape(output[-1500:])}</code>",
                parse_mode="HTML",
            )
            return

        if not output:
            await status_msg.edit_text("⚠️ 未找到正在运行或已停止的 Docker 容器。")
            return

        safe_output = html.escape(output[-3800:])
        await status_msg.edit_text(
            f"📊 <b>Docker 容器实时状态速览</b>\n\n<code>{safe_output}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ 获取状态失败: {html.escape(str(e))}")


# ==================== 命令行指令解析 ====================
async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permission(update): return

    user_str = get_user_identifier(update)

    if not context.args:
        await update.message.reply_text(
            "💡 <b>/upgrade 命令行升级指南：</b>\n\n"
            "• <code>/upgrade 01</code> : 升级列表中第 01 个项目\n"
            "• <code>/upgrade 01 emby</code> : 仅升级第 01 个项目中的 emby 容器\n"
            "• <code>/upgrade all</code> : 升级所有检测到的项目",
            parse_mode="HTML"
        )
        return

    arg = context.args[0].lower()
    service_name = context.args[1] if len(context.args) > 1 else None
    logger.info(f"▶️ [操作审计] 用户 [{user_str}] 执行命令: /upgrade {' '.join(context.args)}")

    if arg in ("all", "a"):
        keyboard = [[
            InlineKeyboardButton("🚀 确认升级全部", callback_data="upgrade_all_confirm"),
            InlineKeyboardButton("❌ 取消", callback_data=create_cb_data("page_turn", {"page": 1})),
        ]]
        await update.message.reply_text("⚠️ <b>确认批量升级全部项目的全部容器？</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    try:
        num = int(arg)
        if num < 1: raise ValueError
        idx = num - 1
        
        projects = await get_compose_projects()
        if 0 <= idx < len(projects):
            target_p = projects[idx]
            p_name = target_p['name']
            safe_p_name = html.escape(p_name)
            safe_dir = html.escape(target_p['dir'])
            
            if service_name:
                safe_svc = html.escape(service_name)
                cb_confirm = create_cb_data("up_svc_do", {"name": p_name, "svc": service_name})
                keyboard = [[
                    InlineKeyboardButton("✅ 确认升级指定服务", callback_data=cb_confirm),
                    InlineKeyboardButton("❌ 取消", callback_data=create_cb_data("page_turn", {"page": 1})),
                ]]
                await update.message.reply_text(
                    f"🚀 确认升级 <b>{safe_p_name}</b> 中的服务 <code>{safe_svc}</code>？\n📂 路径：<code>{safe_dir}</code>",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
            else:
                cb_confirm = create_cb_data("up_p_do", {"name": p_name})
                keyboard = [[
                    InlineKeyboardButton("✅ 确认升级整个项目", callback_data=cb_confirm),
                    InlineKeyboardButton("❌ 取消", callback_data=create_cb_data("page_turn", {"page": 1})),
                ]]
                await update.message.reply_text(
                    f"🚀 确认升级项目 <b>{safe_p_name}</b>？\n📂 路径：<code>{safe_dir}</code>",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="HTML"
                )
        else:
            await update.message.reply_text("❌ 无效的项目序号")
    except ValueError:
        await update.message.reply_text("❌ 格式不正确。示例: <code>/upgrade 01</code> 或 <code>/upgrade 01 emby</code>", parse_mode="HTML")


# ==================== Callback 路由 Handler ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _CANCEL_REQUESTED
    query = update.callback_query
    if not await check_permission(update):
        return

    data = query.data

    if data == "noop":
        await query.answer()
        return

    if data == "upgrade_all":
        await query.answer()
        projects = await get_compose_projects()
        keyboard = [[
            InlineKeyboardButton("🚀 确认升级全部", callback_data="upgrade_all_confirm"),
            InlineKeyboardButton("❌ 取消", callback_data=create_cb_data("page_turn", {"page": 1})),
        ]]
        await query.edit_message_text(
            f"⚠️ <b>确认批量升级全部项目？</b>\n共有 {len(projects)} 个 Compose 项目。",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    if data == "upgrade_all_confirm":
        await do_upgrade_all(update, context)
        return

    if ":" in data:
        action, _ = data.split(":", 1)
        payload = get_cb_payload(data)

        if action == "task_cancel":
            task_id = (payload or {}).get("task_id")
            if task_id and task_id == _CURRENT_TASK:
                _CANCEL_REQUESTED = True
                if _CURRENT_PROCESS:
                    try:
                        _CURRENT_PROCESS.kill()
                    except Exception:
                        pass
                await query.answer("🛑 已发送中断信号，正在停止任务...", show_alert=True)
            else:
                await query.answer("⏳ 该任务已结束或不存在", show_alert=True)
            return

        if payload is None and action not in ("task_cancel",):
            await query.answer("⚠️ 菜单响应超时，请重新输入 /list 打开", show_alert=True)
            return

        if action == "page_turn":
            page = payload.get("page", 1)
            await cmd_list(update, context, page=page, force_refresh=bool(payload.get("refresh")))

        elif action == "p_sel":
            await show_project_detail(update, payload["name"], back_page=payload.get("page", 1))

        elif action == "up_s_ask":
            await ask_single_upgrade(update, payload["name"], back_page=payload.get("page", 1))

        elif action == "up_svc_ask":
            await ask_svc_upgrade(update, payload["name"], payload["svc"], back_page=payload.get("page", 1))

        elif action == "up_p_do":
            await do_upgrade_project(update, context, payload["name"])

        elif action == "up_svc_do":
            await do_upgrade_service(update, context, payload["name"], payload["svc"])

        elif action == "prune_menu":
            await show_prune_menu(update, context)

        elif action == "prune_req":
            await ask_prune_confirm(update, prune_all=payload["all"])

        elif action == "prune_do":
            await do_prune(update, context, prune_all=payload["all"])

        else:
            await query.answer()


# ==================== 帮助命令 ====================
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permission(update):
        return
    text = (
        "ℹ️ <b>LDMG 使用帮助</b>\n\n"
        "• <code>/start</code> 或 <code>/list</code> — 打开项目管理面板\n"
        "• <code>/status</code> — 查看所有容器实时状态\n"
        "• <code>/prune</code> — 打开镜像清理菜单\n"
        "• <code>/upgrade</code> — 查看升级命令用法\n"
        "• <code>/upgrade 01</code> — 升级列表中第 01 个项目\n"
        "• <code>/upgrade 01 emby</code> — 升级第 01 个项目中的 emby 服务\n"
        "• <code>/upgrade all</code> — 升级全部项目\n\n"
        "⚙️ 可通过环境变量 <code>PAGE_SIZE</code> 调整主面板每页项目数（默认 6）。"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def _set_bot_commands(application: Application) -> None:
    """启动时注册 Bot 命令菜单（设置后无需再手动在 BotFather 配置）。"""
    commands = [
        ("start", "打开管理面板"),
        ("list", "项目列表"),
        ("status", "容器状态速览"),
        ("prune", "镜像清理"),
        ("upgrade", "升级项目/服务"),
        ("help", "使用帮助"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Bot 命令菜单已注册")
    except Exception as e:
        logger.warning(f"注册 Bot 命令菜单失败: {e}")


# ==================== 主程序入口 ====================
def main():
    if not BOT_TOKEN:
        logger.error("请设置 BOT_TOKEN 环境变量")
        return
    if not ALLOWED_USER_IDS:
        logger.error("⚠️ ALLOWED_USER_IDS 未配置或为空，任何用户都无法使用 Bot，请检查 .env / 环境变量")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(_set_bot_commands)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("prune", cmd_prune))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("help", cmd_help))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(global_error_handler)

    logger.info(f"Bot 启动成功 | PAGE_SIZE={PAGE_SIZE} | 允许用户数={len(ALLOWED_USER_IDS)}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
