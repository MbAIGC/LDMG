**LDMG** - Lite Docker Manger Gram

- 1、起因最近很多镜像升级速度比较频繁，每次都要进ssh ，进目录， down pull up
- 2、所以用AI写了个脚本只要运行 ./docker-upgrade.sh
- 3、通过2大大方便了升级流程，但是又感觉和TG互动会更方便升级镜像。

**一定要制定docker compose所在的目录，或者说只适合docker compose容器都在一个目录下**
例如：
/docker
 - emby
   - docker-compose.yml
 - openlist
   - docker-compose.yml

备注：本AI项目只测试了docker compose镜像管理。

---

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `BOT_TOKEN` | ✅ | - | Telegram Bot Token |
| `ALLOWED_USER_IDS` | ✅ | - | 允许使用的 Telegram User ID（多个用逗号分隔） |
| `PAGE_SIZE` | ❌ | `6` | 主面板每页展示的项目数量 |
| `LOG_DIR` | ❌ | `logs` | 滚动审计日志目录 |

## 命令

- `/start`、`/list` — 打开项目管理面板（支持分页、按状态分组）
- `/status` — 所有容器实时状态速览
- `/prune` — 镜像清理菜单（悬空镜像 / 所有未使用镜像）
- `/upgrade` — 命令行升级（`/upgrade 01`、`/upgrade 01 emby`、`/upgrade all`）
- `/help` — 使用帮助

## 优化记录

每次代码审查与优化记录按规范归档在 [docs/optimization-logs](docs/optimization-logs/README.md)，下一次优化前建议先阅读索引与最近一份记录。
