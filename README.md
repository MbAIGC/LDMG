<div align="center">

# 🐳 LDMG

**Lite Docker Manager Gram**

用 Telegram Bot 管理 Docker Compose 项目：一键拉取新镜像、重建容器、批量升级、清理镜像，
再也不用每次升级都 SSH 进服务器敲 `docker compose down && pull && up -d`。

</div>

---

## ✨ 功能特性

- 🚀 **一键升级**：单项目 / 单服务 / 全部项目，自动执行 `pull` + `up -d`
- 📊 **项目管理面板**：按运行状态分组、分页浏览、项目与服务一目了然
- 🧹 **镜像清理**：区分「悬空镜像」与「所有未使用镜像」，先预览再确认
- 🛑 **可中断任务**：单命令超时自动终止，升级过程随时可取消，取消按钮与任务绑定
- 📶 **实时进度**：动态进度条 + 日志预览，兼容 compose 的 `\r` 进度输出
- 🔐 **权限控制**：仅白名单内的 Telegram 用户可操作
- 📝 **审计日志**：所有操作滚动写入日志文件，方便回溯
- 🖥 **多架构镜像**：`linux/amd64` 与 `linux/arm64` 双平台发布

## 🚀 快速开始

### 1. 创建 Telegram Bot

找 [@BotFather](https://t.me/BotFather) 创建 Bot 并获取 `BOT_TOKEN`；
在 [@userinfobot](https://t.me/userinfobot) 查询自己的 User ID，作为 `ALLOWED_USER_IDS`。

### 2. 配置环境变量

复制 `.env.example` 为 `.env` 并填写：

```dotenv
BOT_TOKEN=123456:ABCDEF-你的BotToken
ALLOWED_USER_IDS=123456789,987654321
PAGE_SIZE=6
```

### 3. 启动

```bash
docker compose up -d
```

> **路径挂载（重要）**
>
> `docker compose ls` 返回的是宿主机侧的绝对路径。请按实际环境把 compose 文件所在目录
> 挂载到容器内相同路径，例如宿主 `/docker` 下按项目分子目录：
>
> ```yaml
> volumes:
>   - /docker:/docker
> ```
>
> ```text
> /docker
>  ├── emby
>  │    └── docker-compose.yml
>  └── openlist
>       └── docker-compose.yml
> ```
>
> 备注：本项目仅测试过 docker compose（v2）管理的容器。

## ⚙️ 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | - | Telegram Bot Token |
| `ALLOWED_USER_IDS` | ✅ | - | 允许使用的 Telegram User ID（多个用逗号分隔） |
| `PAGE_SIZE` | ❌ | `6` | 主面板每页展示的项目数量 |
| `LOG_DIR` | ❌ | `logs` | 滚动审计日志目录 |

## 📖 命令

| 命令 | 说明 |
|---|---|
| `/start`、`/list` | 打开项目管理面板（分页、按状态分组） |
| `/status` | 所有容器实时状态速览 |
| `/prune` | 镜像清理菜单（悬空 / 所有未使用） |
| `/upgrade` | 命令行升级：`/upgrade 01`、`/upgrade 01 emby`、`/upgrade all` |
| `/help` | 使用帮助 |

## 🗂 镜像

`ghcr.io/mbaigc/ldmg`

- tags：`main` / `latest` / `sha-<短哈希>`
- 平台：`linux/amd64`、`linux/arm64`

## 📁 目录结构

```text
LDMG/
├── bot.py                  # Telegram Bot 主程序
├── bot-v1-grok.py          # 早期版本（保留参考）
├── Dockerfile              # 多阶段精简镜像
├── docker-compose.yml      # 部署编排
├── requirements.txt        # Python 依赖
├── .env.example            # 环境变量示例
└── docs/optimization-logs/ # 优化/更新记录（按轮次归档）
```

## 🔒 安全提醒

- 容器挂载了 `docker.sock`，等同于宿主机 root 权限，请务必配置 `ALLOWED_USER_IDS` 白名单；
- 不要把 Bot Token 提交到仓库或暴露在公开环境；
- 如需进一步收敛权限，可考虑只读挂载、限制网络等最小化部署。

## 📝 更新记录

每次代码审查与优化记录按 `YYYY-MM-DD-NN-主题` 规范归档在
[docs/optimization-logs](docs/optimization-logs/README.md)，下一次优化前建议先阅读索引与最近一份记录。

| 日期 | 记录 | 摘要 |
|---|---|---|
| 2026-08-11 | [全量优化与发布记录](docs/optimization-logs/2026-08-11-01-全量优化与发布记录.md) | 修复取消语义、锁竞态等遗留问题，精简镜像，双平台发布 |
| 2026-08-10 | [两轮审查记录](docs/optimization-logs/2026-08-10-01-两轮审查记录.md) | 首轮完整审查与二轮全量修复版复核 |
