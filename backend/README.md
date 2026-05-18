# backend

后端源码当前仍保留在 `src/eiketsu_env/`，这样可以保持 Python 包名、CLI 入口、Alembic 和 Docker 启动方式稳定。

当前职责映射：

- `src/eiketsu_env/server_app.py`：FastAPI 路由和页面入口。
- `src/eiketsu_env/services/leaderboard.py`：排行榜查询、统计、排序、分页数据。
- `src/eiketsu_env/services/server_share.py`：邀请码、上传导入、服务端配置。
- `src/eiketsu_env/db/`：数据库模型、会话和迁移入口。

后续如果要把代码实体迁入 `backend/`，建议单独开一批重构，同时更新 `pyproject.toml`、Docker、Alembic、CLI 和测试导入路径。
