# 版本管理范围

这个仓库建议只管理“能重建项目”的内容，把运行时产生的数据、浏览器登录态、打包产物和本地配置全部排除。

## 应该进入 Git

- `src/`：客户端、服务端、网页渲染、采集、上传、分析逻辑。
- `backend/`：后端职责索引；当前 Python 后端源码仍保留在 `src/eiketsu_env/` 以保持包入口稳定。
- `frontend/leaderboard/`：VPS 排行榜页面模板、CSS 和 JS。
- `assets/`：随源码部署的卡牌主数据和 UI/图片素材。
- `tests/`：回归测试和接口测试。
- `alembic/`、`alembic.ini`：数据库结构迁移。
- `scripts/`：构建 exe、部署 VPS、发布更新等可复现脚本。
- `deploy/`、`.dockerignore`：服务端部署结构和打包配置。
- `pyproject.toml`、`README.md`、`.env.example`：项目依赖和配置模板。
- `shared/share_config.json`：旧本地共享流程的公开配置样例。

## 不应该进入 Git

- `data/`：SQLite、采集结果、服务端本地数据。
- `.tmp/`、`dist/.tmp/`：浏览器 profile、cookie 临时复制、登录检测临时文件。
- `.venv/`：本机 Python 虚拟环境。
- `build/`、`dist/`：PyInstaller 中间产物和 exe 发布包。
- `.env`、`client_config.json`：本地 token、服务端地址、个人配置。
- `shared/contributions/`、`shared/reports/`：旧 Git 共享流程产生的贡献包和报告。
- 调试截图，例如 `leaderboard-*.png`、`local-*.png`。

## 发布建议

Git 管源码；exe 包体和 VPS 更新包建议走单独发布流程。以后每次发新版时：

1. 修改源码和测试。
2. 更新 `pyproject.toml` 里的版本号。
3. 运行测试。
4. 用 `scripts/build_client_exe.ps1` 生成 `dist/EiketsuCollector_x.y.z.exe`。
5. 只把源码提交到 Git，exe 用管理页或发布渠道上传。
