# GitHub 自动测试与 VPS 部署

这个仓库已经配置 `.github/workflows/ci-deploy.yml`：

- Pull Request：只运行测试，不部署。
- Push 到 `main`：运行测试；测试通过后打包源码；通过 SSH 上传到 VPS；在 VPS 上重建并重启 `api` 服务。
- 手动触发：可以在 GitHub Actions 页面点 `Run workflow`。

## 需要配置的 GitHub Secrets

进入 GitHub 仓库：

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

添加：

- `VPS_HOST`：VPS IP 或域名。
- `VPS_USER`：SSH 用户名，例如 `ubuntu`。
- `VPS_SSH_KEY`：用于登录 VPS 的私钥内容。
- `VPS_PORT`：SSH 端口；默认 22 时可以不填。
- `VPS_REMOTE_DIR`：VPS 上项目目录；不填时默认 `~/eiketsu-env-db`。

不要把 VPS 地址、私钥、管理口令写进代码或 README 正文。

## 建议使用专用部署密钥

建议给 GitHub Actions 单独准备一个无密码部署密钥，而不是复用你日常登录用的私钥。

在本机生成：

```powershell
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\eiketsu_github_deploy -C "github-actions-eiketsu"
```

把公钥内容追加到 VPS：

```powershell
type $env:USERPROFILE\.ssh\eiketsu_github_deploy.pub
```

复制输出，在 VPS 上追加到：

```bash
~/.ssh/authorized_keys
```

把私钥内容填入 GitHub Secret：

```powershell
type $env:USERPROFILE\.ssh\eiketsu_github_deploy
```

复制完整输出，包括：

```text
-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----
```

## 部署时会做什么

GitHub Actions 会打包这些文件：

- `pyproject.toml`
- `README.md`
- `Dockerfile`
- `docker-compose.yml`
- `alembic.ini`
- `alembic/`
- `src/`

然后在 VPS 上执行：

```bash
docker compose build api
docker compose run --rm api alembic upgrade head
docker compose up -d api
docker compose ps
```

这样前端页面、服务端接口、数据库迁移都会跟随 `main` 分支自动更新。

## 注意

GitHub Actions 不会上传：

- `data/`
- `.tmp/`
- `.venv/`
- `build/`
- `dist/`
- `.env`
- 本地 token、cookies、浏览器 profile

exe 客户端仍建议走单独发布流程，不放进 Git。
