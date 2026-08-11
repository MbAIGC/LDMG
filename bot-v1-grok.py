#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LDMG - Lite Docker Mnager Gram
功能完全对应之前的 Bash 脚本
"""

import os
import re
import subprocess
import logging
from typing import List, Dict, Optional

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ==================== 配置 ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}

# 日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ==================== 权限检查 ====================
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return False
    return user_id in ALLOWED_USER_IDS


async def check_permission(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_allowed(user.id):
        if update.message:
            await update.message.reply_text("❌ 无权限操作")
        elif update.callback_query:
            await update.callback_query.answer("❌ 无权限", show_alert=True)
        return False
    return True


# ==================== 核心：获取 Compose 项目 ====================
def get_compose_projects() -> List[Dict]:
    """扫描所有 Compose 项目，返回列表"""
    projects = []
    seen_dirs = set()

    # 方法1：docker compose ls -a
    try:
        result = subprocess.run(
            ["docker", "compose", "ls", "-a", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            import json
            data = json.loads(result.stdout)
            # 兼容单个对象或数组
            if isinstance(data, dict):
                data = [data]
            for item in data:
                name = item.get("Name", "")
                status = item.get("Status", "")
                config_files = item.get("ConfigFiles", "")
                if not name:
                    continue
                first_file = config_files.split(",")[0].strip() if config_files else ""
                work_dir = os.path.dirname(first_file) if first_file else ""
                if work_dir and os.path.isdir(work_dir) and work_dir not in seen_dirs:
                    seen_dirs.add(work_dir)
                    containers = get_project_containers(name)
                    projects.append({
                        "name": name,
                        "dir": work_dir,
                        "status": status,
                        "containers": containers,
                    })
    except Exception as e:
        logger.warning(f"docker compose ls 失败: {e}")

    # 方法2：补充标签扫描
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", "label=com.docker.compose.project",
                "--format",
                '{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.project.working_dir"}}',
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                name, work_dir = parts[0].strip(), parts[1].strip()
                if work_dir and os.path.isdir(work_dir) and work_dir not in seen_dirs:
                    seen_dirs.add(work_dir)
                    containers = get_project_containers(name)
                    projects.append({
                        "name": name,
                        "dir": work_dir,
                        "status": "from-label",
                        "containers": containers,
                    })
    except Exception as e:
        logger.warning(f"标签扫描失败: {e}")

    # 按名称排序
    projects.sort(key=lambda x: x["name"])
    return projects


def get_project_containers(project_name: str) -> str:
    try:
        result = subprocess.run(
            [
                "docker", "ps", "-a",
                "--filter", f"label=com.docker.compose.project={project_name}",
                "--format", "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            names = [n.strip() for n in result.stdout.strip().splitlines() if n.strip()]
            return ",".join(names)
    except Exception:
        pass
    return ""


# ==================== 执行命令并实时反馈 ====================
async def run_command_with_feedback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    cmd: List[str],
    cwd: Optional[str] = None,
    title: str = "执行中",
):
    """执行命令，把关键输出推送到 Telegram"""
    message = update.effective_message
    status_msg = await message.reply_text(f"⏳ {title}\n<code>{' '.join(cmd)}</code>", parse_mode="HTML")

    try:
        process = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        output_lines = []
        while True:
            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break
            if line:
                line = line.rstrip()
                output_lines.append(line)
                # 每积累几行或遇到关键信息就更新一次（避免刷屏）
                if len(output_lines) % 8 == 0 or any(
                    k in line.lower() for k in ["pulling", "downloaded", "created", "started", "error", "done"]
                ):
                    preview = "\n".join(output_lines[-15:])  # 只显示最近 15 行
                    try:
                        await status_msg.edit_text(
                            f"⏳ {title}\n<code>{preview[-3500:]}</code>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

        returncode = process.wait()
        full_output = "\n".join(output_lines[-30:])  # 最终显示最后 30 行

        if returncode == 0:
            await status_msg.edit_text(
                f"✅ {title} 完成\n<code>{full_output[-3500:]}</code>",
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                f"❌ {title} 失败 (exit {returncode})\n<code>{full_output[-3500:]}</code>",
                parse_mode="HTML",
            )
        return returncode == 0
    except Exception as e:
        await status_msg.edit_text(f"❌ 执行出错: {e}")
        return False


# ==================== 命令处理 ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permission(update):
        return
    await cmd_list(update, context)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permission(update):
        return

    projects = get_compose_projects()
    text = "📋 <b>Docker Compose 升级助手</b>\n\n"
    text += "<b>00.</b> 清理未使用镜像\n"
    text += "────────────────\n"

    keyboard = [
        [InlineKeyboardButton("00. 清理未使用镜像", callback_data="prune")]
    ]

    if not projects:
        text += "\n暂无 Compose 项目"
    else:
        for i, p in enumerate(projects, 1):
            num = f"{i:02d}"
            status = p["status"]
            status_icon = "🟢" if "running" in status else "🟡"
            text += f"<b>{num}.</b> {p['name']}  {status_icon} [{status}]\n"
            text += f"     路径: <code>{p['dir']}</code>\n"
            text += f"     容器: {p['containers'] or '-'}\n\n"
            keyboard.append([
                InlineKeyboardButton(
                    f"{num}. 升级 {p['name']}",
                    callback_data=f"upgrade:{i-1}"
                )
            ])

    keyboard.append([InlineKeyboardButton("🔄 刷新列表", callback_data="refresh")])
    keyboard.append([InlineKeyboardButton("⬆️ 升级全部", callback_data="upgrade_all")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )
        await update.callback_query.answer()
    else:
        await update.message.reply_text(
            text, reply_markup=reply_markup, parse_mode="HTML"
        )


async def cmd_prune(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_permission(update):
        return

    message = update.effective_message
    await message.reply_text("🔍 正在扫描未使用的镜像...")

    # 先 dry-run 展示
    try:
        result = subprocess.run(
            ["docker", "image", "prune", "-a", "--dry-run"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        dry_output = result.stdout + result.stderr
    except Exception as e:
        dry_output = str(e)

    keyboard = [
        [
            InlineKeyboardButton("✅ 确认删除", callback_data="prune_confirm"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel"),
        ]
    ]
    await message.reply_text(
        f"将删除以下未使用镜像：\n<code>{dry_output[-3000:]}</code>\n\n确认删除吗？",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def do_prune(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🗑 正在删除未使用镜像...")

    success = await run_command_with_feedback(
        update, context,
        ["docker", "image", "prune", "-a", "-f"],
        title="清理未使用镜像",
    )
    if success:
        # 显示磁盘使用
        try:
            df = subprocess.run(
                ["docker", "system", "df"],
                capture_output=True, text=True, timeout=15
            )
            await update.effective_message.reply_text(
                f"<b>当前磁盘使用：</b>\n<code>{df.stdout}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def do_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE, index: int):
    projects = get_compose_projects()
    if index < 0 or index >= len(projects):
        await update.effective_message.reply_text("❌ 无效的项目序号")
        return

    p = projects[index]
    message = update.effective_message
    await message.reply_text(
        f"🚀 开始升级项目 <b>{p['name']}</b>\n路径: <code>{p['dir']}</code>",
        parse_mode="HTML",
    )

    # pull
    await run_command_with_feedback(
        update, context,
        ["docker", "compose", "pull"],
        cwd=p["dir"],
        title=f"拉取镜像 - {p['name']}",
    )

    # up -d
    await run_command_with_feedback(
        update, context,
        ["docker", "compose", "up", "-d"],
        cwd=p["dir"],
        title=f"重建启动 - {p['name']}",
    )

    await message.reply_text(f"✅ 项目 <b>{p['name']}</b> 升级完成", parse_mode="HTML")


async def do_upgrade_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    projects = get_compose_projects()
    if not projects:
        await update.effective_message.reply_text("没有可升级的项目")
        return

    message = update.effective_message
    await message.reply_text(f"🚀 开始升级全部 {len(projects)} 个项目...")

    for i, p in enumerate(projects, 1):
        await message.reply_text(f"[{i}/{len(projects)}] 升级 <b>{p['name']}</b>...", parse_mode="HTML")
        await run_command_with_feedback(
            update, context,
            ["docker", "compose", "pull"],
            cwd=p["dir"],
            title=f"拉取 - {p['name']}",
        )
        await run_command_with_feedback(
            update, context,
            ["docker", "compose", "up", "-d"],
            cwd=p["dir"],
            title=f"启动 - {p['name']}",
        )

    await message.reply_text("✅ 全部项目升级完成")


# ==================== 回调查询处理 ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await check_permission(update):
        return

    data = query.data

    if data == "refresh":
        await cmd_list(update, context)
    elif data == "prune":
        await query.answer()
        await cmd_prune(update, context)
    elif data == "prune_confirm":
        await do_prune(update, context)
    elif data == "cancel":
        await query.answer("已取消")
        await query.edit_message_text("❌ 已取消操作")
    elif data == "upgrade_all":
        await query.answer()
        await do_upgrade_all(update, context)
    elif data.startswith("upgrade:"):
        await query.answer()
        try:
            idx = int(data.split(":")[1])
            await do_upgrade(update, context, idx)
        except Exception as e:
            await query.message.reply_text(f"❌ 错误: {e}")


async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """支持 /upgrade 01 或 /upgrade all"""
    if not await check_permission(update):
        return

    if not context.args:
        await update.message.reply_text("用法: /upgrade 01  或  /upgrade all")
        return

    arg = context.args[0].lower()
    if arg in ("all", "a"):
        await do_upgrade_all(update, context)
        return

    # 支持 01 / 1 / 00
    if arg in ("00", "0"):
        await cmd_prune(update, context)
        return

    try:
        num = int(arg)
        if num < 1:
            raise ValueError
        await do_upgrade(update, context, num - 1)
    except ValueError:
        await update.message.reply_text("请输入正确的序号，例如 /upgrade 01")


# ==================== 主函数 ====================
def main():
    if not BOT_TOKEN:
        logger.error("请设置 BOT_TOKEN 环境变量")
        return
    if not ALLOWED_USER_IDS:
        logger.warning("警告：ALLOWED_USER_IDS 为空，将拒绝所有请求")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("prune", cmd_prune))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot 启动中...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
