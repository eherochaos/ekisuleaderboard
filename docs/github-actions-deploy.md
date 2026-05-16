# GitHub 自动测试与 VPS 部署

这个仓库已经配置 `.github/workflows/ci-deploy.yml`：

- Pull Request：只运行测试，不部署。
- Push 到 `main`：运行测试；测试通过后打包源码；通过 SSH 上传到 VPS；在 VPS 上重建并重启 `api` 服务。
- 手动触发：可以在 GitHub Actions 页面点 `Run workflow`。

测试 job 使用 `windows-latest`，因为普通用户客户端、浏览器登录态读取和 Tk GUI 都以 Windows 为第一目标环境。部署 job 仍使用 `ubuntu-latest`，只负责打包源码并通过 SSH 控制 VPS。

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

## 如果 Deploy to VPS 显示 Permission denied

如果日志里出现：

```text
Permission denied (publickey,password).
scp: Connection closed
```

说明 GitHub Actions 已经连到 VPS，但 VPS 不接受这把 SSH key。优先检查：

- `VPS_SSH_KEY` 填的是私钥全文，不是 `.pub` 公钥，也不是本机文件路径。
- 私钥开头应为 `-----BEGIN OPENSSH PRIVATE KEY-----`。
- 与该私钥配对的 `.pub` 公钥已经追加到 VPS 用户的 `~/.ssh/authorized_keys`。
- `VPS_USER` 与公钥放置的用户一致，例如 `ubuntu` 就必须放到 `/home/ubuntu/.ssh/authorized_keys`。
- `VPS_HOST` 只填 IP 或域名，不要填 `ubuntu@IP`。
- `VPS_USER` 只填用户名，例如 `ubuntu`。

Actions 日志里的 `Configure SSH` 会打印一行公钥指纹，例如：

```text
256 SHA256:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx github-actions-eiketsu (ED25519)
```

它必须和本机部署 key 的指纹一致：

```powershell
ssh-keygen -lf C:\Users\WINDOWS\.ssh\eiketsu_github_deploy.pub
```

如果不一致，说明 GitHub 的 `VPS_SSH_KEY` 粘错了。

本机可以用这条命令验证部署 key 是否能登录：

```powershell
ssh -i $env:USERPROFILE\.ssh\eiketsu_github_deploy -o BatchMode=yes ubuntu@43.128.141.76 "echo ok"
```

如果这里也失败，先把公钥追加到 VPS：

```powershell
type $env:USERPROFILE\.ssh\eiketsu_github_deploy.pub | ssh ubuntu@43.128.141.76 "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
```

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
